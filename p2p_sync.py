#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点名软件 P2P 增量同步引擎（仅依赖标准库）

同步范围：
    call_signs / roll_call_records / call_sign_library / walkie_talkies
同步方式：
    局域网 UDP 广播发现 -> HTTP 直连同步
    跨网 -> 轻量中继协助 UDP 打洞 -> 直连同步
    打洞失败 -> 轻量中继信箱转发（兜底）
冲突策略：
    最后修改胜出（_updated_at 毫秒时间戳，相同则 device_id 较大者胜）
删除策略：
    p2p_tombstones 墓碑表，避免已删除记录被旧数据"复活"

数据库改动全部是附加列/附加表，不影响原有业务逻辑。
"""

import argparse
import configparser
import json
import os
import random
import socket
import sqlite3
import struct
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

PROTOCOL = "p2p-sync/1"
PACKET_MAGIC = b"RCP2"
MSG_HELLO = 1
MSG_HELLO_ACK = 2
MSG_DATA = 3
MSG_ACK = 4
MSG_PUNCH = 5
UDP_CHUNK = 1100
KEY_SEP = "\x1f"

# 需要同步的表及其业务主键（walkie_talkies 无主键，用 brand+model 复合键）
SYNC_TABLES = (
    {"name": "call_signs", "key": ("call_sign",)},
    {"name": "roll_call_records", "key": ("id",), "numeric": ("id",)},
    {"name": "call_sign_library", "key": ("call_sign",)},
    {"name": "walkie_talkies", "key": ("brand", "model")},
)


def now_ms():
    return int(time.time() * 1000)


def make_key(spec, row):
    """把一行记录转换成字符串主键；缺字段返回 None"""
    parts = []
    for col in spec["key"]:
        val = row.get(col)
        if val is None or str(val).strip() == "":
            return None
        parts.append(str(val))
    return KEY_SEP.join(parts)


def is_newer(candidate, current):
    """LWW 比较：(时间戳, device_id) 元组，大者胜"""
    if candidate[0] != current[0]:
        return candidate[0] > current[0]
    return candidate[1] > current[1]


def detect_local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class P2PSyncEngine:
    """P2P 同步引擎：start() 启动，stop() 停止，sync_now() 立即同步"""

    def __init__(self, db_path, config_path=None, logger=None, on_status=None):
        self.db_path = os.path.abspath(db_path)
        self.config_path = os.path.abspath(
            config_path or os.path.join(os.path.dirname(self.db_path), "app_config.ini"))
        self.log = logger or (lambda msg: print(msg))
        self.on_status = on_status

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sync_request = threading.Event()
        self._columns = {}
        self._specs = {spec["name"]: spec for spec in SYNC_TABLES}
        self._lan_peers = {}
        self._hello_waiters = {}
        self._ack_waiters = {}
        self._recv_parts = {}
        self._punch_cooldown = {}
        self._relay_registered = 0.0
        self._relay_ok_at = 0.0  # 最近一次中继请求成功的时间，用于指示灯显示中继连通状态
        self._threads = []
        self._httpd = None
        self._udp_sock = None
        self._running = False
        self._status = {
            "running": False,
            "device_id": "",
            "device_name": "",
            "relay_url": "",
            "last_sync": 0,
            "last_mode": "",
            "last_peer": "",
            "last_message": "",
            "applied": 0,
            "skipped": 0,
            "deleted": 0,
            "peers": {},
        }

        self._load_config()

    # ---------------- 配置 ----------------
    def _load_config(self):
        config = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            config.read(self.config_path, encoding="utf-8")
        if "P2P" not in config:
            config["P2P"] = {}

        section = config["P2P"]
        device_id = (section.get("device_id") or "").strip()
        if not device_id:
            device_id = uuid.uuid4().hex
            section["device_id"] = device_id
            self._save_config(config)

        self.device_id = device_id
        self.device_name = (section.get("device_name") or "主控电脑").strip() or "主控电脑"
        self.relay_url = (section.get("relay_url") or "").strip().rstrip("/")
        self.relay_token = (section.get("relay_token") or "").strip()
        self.udp_port = section.getint("udp_port", fallback=4571)
        self.http_port = section.getint("http_port", fallback=4572)
        self.interval = max(15, section.getint("sync_interval", fallback=60))
        self.enabled = section.getboolean("enabled", fallback=False)

        self._status["device_id"] = self.device_id
        self._status["device_name"] = self.device_name
        self._status["relay_url"] = self.relay_url
        self._status["udp_port"] = self.udp_port
        self._status["http_port"] = self.http_port

    def _save_config(self, config):
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                config.write(fh)
        except OSError as exc:
            self.log("保存 P2P 配置失败: %s" % exc)

    # ---------------- 数据库 ----------------
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA recursive_triggers=OFF")
        except sqlite3.Error:
            pass
        return conn

    @staticmethod
    def _table_columns(conn, table):
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
        except sqlite3.Error:
            return []

    def initialize(self):
        """附加同步列、墓碑表、状态表与触发器（幂等）"""
        conn = self._connect()
        try:
            for spec in SYNC_TABLES:
                table = spec["name"]
                cols = self._table_columns(conn, table)
                if not cols:
                    self.log("P2P: 跳过不存在的表 %s" % table)
                    continue
                if "_updated_at" not in cols:
                    conn.execute("ALTER TABLE %s ADD COLUMN _updated_at INTEGER DEFAULT 0" % table)
                if "_device_id" not in cols:
                    conn.execute("ALTER TABLE %s ADD COLUMN _device_id TEXT DEFAULT ''" % table)
                self._columns[table] = self._table_columns(conn, table)

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS p2p_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS p2p_tombstones (
                    entity     TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    device_id  TEXT DEFAULT '',
                    PRIMARY KEY (entity, key)
                );
                CREATE TABLE IF NOT EXISTS p2p_peer_state (
                    peer_id     TEXT PRIMARY KEY,
                    name        TEXT DEFAULT '',
                    last_sync   INTEGER DEFAULT 0,
                    sent_upto   INTEGER DEFAULT 0,
                    recv_upto   INTEGER DEFAULT 0
                );
                """
            )
            conn.execute("INSERT OR REPLACE INTO p2p_meta(key, value) VALUES('device_id', ?)",
                         (self.device_id,))
            conn.execute("INSERT OR REPLACE INTO p2p_meta(key, value) VALUES('suppress', '1')")
            conn.commit()

            stamp = now_ms()
            for spec in SYNC_TABLES:
                table = spec["name"]
                if table not in self._columns:
                    continue
                conn.execute(
                    "UPDATE %s SET _updated_at = ?, _device_id = '' "
                    "WHERE _updated_at IS NULL OR _updated_at = 0" % table, (stamp,))
                conn.execute("CREATE INDEX IF NOT EXISTS idx_%s_p2p_ts ON %s(_updated_at)"
                             % (table, table))
            conn.commit()

            for spec in SYNC_TABLES:
                if spec["name"] in self._columns:
                    self._create_triggers(conn, spec)
            self._set_meta(conn, "suppress", "0")
            conn.commit()
        finally:
            conn.close()

    def _create_triggers(self, conn, spec):
        table = spec["name"]
        device = self.device_id.replace("'", "''")
        stamp = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
        guard = "COALESCE((SELECT value FROM p2p_meta WHERE key='suppress'),'0') <> '1'"

        # DELETE 触发器只有 OLD，INSERT/UPDATE 才有 NEW
        if table == "walkie_talkies":
            old_key_expr = "COALESCE(OLD.brand,'') || char(31) || COALESCE(OLD.model,'')"
        else:
            old_key_expr = "COALESCE(CAST(OLD.%s AS TEXT),'')" % spec["key"][0]

        for name in ("%s_p2p_ins" % table, "%s_p2p_upd" % table, "%s_p2p_del" % table):
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)

        conn.executescript(
            """
            CREATE TRIGGER {t}_p2p_ins AFTER INSERT ON {t}
            FOR EACH ROW WHEN {guard}
            BEGIN
                UPDATE {t} SET _updated_at = {stamp}, _device_id = '{dev}'
                WHERE rowid = NEW.rowid;
            END;

            CREATE TRIGGER {t}_p2p_upd AFTER UPDATE ON {t}
            FOR EACH ROW WHEN {guard}
            BEGIN
                UPDATE {t} SET _updated_at = {stamp}, _device_id = '{dev}'
                WHERE rowid = NEW.rowid;
            END;

            CREATE TRIGGER {t}_p2p_del AFTER DELETE ON {t}
            FOR EACH ROW WHEN {guard}
            BEGIN
                INSERT OR REPLACE INTO p2p_tombstones(entity, key, updated_at, device_id)
                VALUES('{t}', {old_key_expr}, {stamp}, '{dev}');
            END;
            """.format(t=table, guard=guard, stamp=stamp, dev=device,
                       old_key_expr=old_key_expr)
        )

    @staticmethod
    def _set_meta(conn, key, value):
        conn.execute("INSERT OR REPLACE INTO p2p_meta(key, value) VALUES(?, ?)", (key, str(value)))

    @staticmethod
    def _key_where(spec, key):
        parts = key.split(KEY_SEP)
        clauses = []
        params = []
        for index, col in enumerate(spec["key"]):
            val = parts[index] if index < len(parts) else ""
            if col in spec.get("numeric", ()):
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    return None, None
            clauses.append("%s = ?" % col)
            params.append(val)
        return " AND ".join(clauses), params

    def _get_local(self, conn, spec, key):
        where, params = self._key_where(spec, key)
        if where is None:
            return None
        row = conn.execute("SELECT * FROM %s WHERE %s LIMIT 1" % (spec["name"], where),
                           params).fetchone()
        return dict(row) if row else None

    def _extract_since(self, since):
        since = int(since or 0)
        result = {"tables": {}, "tombstones": [], "max_ts": since}
        conn = self._connect()
        try:
            for spec in SYNC_TABLES:
                table = spec["name"]
                if table not in self._columns:
                    result["tables"][table] = []
                    continue
                rows = conn.execute("SELECT * FROM %s WHERE _updated_at > ?" % table,
                                    (since,)).fetchall()
                result["tables"][table] = [dict(row) for row in rows]
                for row in result["tables"][table]:
                    result["max_ts"] = max(result["max_ts"], int(row.get("_updated_at") or 0))
            tombs = conn.execute(
                "SELECT entity, key, updated_at, device_id FROM p2p_tombstones "
                "WHERE updated_at > ?", (since,)).fetchall()
            result["tombstones"] = [dict(row) for row in tombs]
            for row in result["tombstones"]:
                result["max_ts"] = max(result["max_ts"], int(row.get("updated_at") or 0))
        finally:
            conn.close()
        return result

    def _apply_changes(self, tables, tombstones):
        """把远端增量合并进本地库，返回统计"""
        stats = {"applied": 0, "skipped": 0, "deleted": 0}
        conn = self._connect()
        begun = False
        try:
            for _ in range(5):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    begun = True
                    break
                except sqlite3.OperationalError:
                    time.sleep(0.3)
            if not begun:
                raise sqlite3.OperationalError("数据库忙碌，稍后重试")
            self._set_meta(conn, "suppress", "1")

            # 1) 墓碑：删除本地较旧的记录
            for tomb in tombstones or []:
                if not isinstance(tomb, dict):
                    continue
                entity = str(tomb.get("entity") or "")
                spec = self._specs.get(entity)
                if not spec or entity not in self._columns:
                    continue
                key = str(tomb.get("key") or "")
                if not key:
                    continue
                ts = int(tomb.get("updated_at") or 0)
                dev = str(tomb.get("device_id") or "")
                local = self._get_local(conn, spec, key)
                if local and is_newer((ts, dev),
                                      (int(local.get("_updated_at") or 0),
                                       str(local.get("_device_id") or ""))):
                    where, params = self._key_where(spec, key)
                    if where is None:
                        continue
                    conn.execute("DELETE FROM %s WHERE %s" % (entity, where), params)
                    stats["deleted"] += 1
                existing = conn.execute(
                    "SELECT updated_at, device_id FROM p2p_tombstones "
                    "WHERE entity = ? AND key = ?", (entity, key)).fetchone()
                if existing is None or is_newer((ts, dev),
                                                (int(existing[0] or 0), str(existing[1] or ""))):
                    conn.execute(
                        "INSERT OR REPLACE INTO p2p_tombstones(entity, key, updated_at, device_id) "
                        "VALUES(?,?,?,?)", (entity, key, ts, dev))

            # 2) 数据行：LWW 合并
            for entity, rows in (tables or {}).items():
                spec = self._specs.get(entity)
                if not spec or entity not in self._columns or not isinstance(rows, list):
                    continue
                known = self._columns[entity]
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = make_key(spec, row)
                    if not key:
                        continue
                    ts = int(row.get("_updated_at") or 0)
                    dev = str(row.get("_device_id") or "")
                    local = self._get_local(conn, spec, key)
                    if local and not is_newer((ts, dev),
                                              (int(local.get("_updated_at") or 0),
                                               str(local.get("_device_id") or ""))):
                        stats["skipped"] += 1
                        continue
                    tomb = conn.execute(
                        "SELECT updated_at, device_id FROM p2p_tombstones "
                        "WHERE entity = ? AND key = ?", (entity, key)).fetchone()
                    if local is None and tomb and is_newer(
                            (int(tomb[0] or 0), str(tomb[1] or "")), (ts, dev)):
                        stats["skipped"] += 1
                        continue

                    cols = [col for col in known if col in row]
                    if local:
                        updates = [col for col in cols if col not in spec["key"]]
                        if updates:
                            where, params = self._key_where(spec, key)
                            if where is None:
                                continue
                            conn.execute(
                                "UPDATE %s SET %s WHERE %s" % (
                                    entity, ", ".join("%s = ?" % col for col in updates), where),
                                [row[col] for col in updates] + params)
                            stats["applied"] += 1
                    elif cols:
                        try:
                            conn.execute(
                                "INSERT INTO %s (%s) VALUES (%s)" % (
                                    entity, ", ".join(cols), ", ".join("?" * len(cols))),
                                [row[col] for col in cols])
                        except sqlite3.IntegrityError:
                            # 业务主键不同但自增 id 冲突时，去掉 id 重新插入
                            retry_cols = [col for col in cols if col != "id"]
                            conn.execute(
                                "INSERT INTO %s (%s) VALUES (%s)" % (
                                    entity, ", ".join(retry_cols),
                                    ", ".join("?" * len(retry_cols))),
                                [row[col] for col in retry_cols])
                        stats["applied"] += 1

                    conn.execute(
                        "DELETE FROM p2p_tombstones WHERE entity = ? AND key = ? AND updated_at <= ?",
                        (entity, key, ts))

            self._set_meta(conn, "suppress", "0")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return stats

    # ---------------- 对等端记录 ----------------
    @staticmethod
    def _peer_row(conn, peer_id):
        return conn.execute("SELECT * FROM p2p_peer_state WHERE peer_id = ?",
                            (peer_id,)).fetchone()

    def _peer_sent_upto(self, peer_id):
        conn = self._connect()
        try:
            row = self._peer_row(conn, peer_id)
            return int(row["sent_upto"] or 0) if row else 0
        finally:
            conn.close()

    def _update_peer(self, conn, peer_id, name=None, sent_upto=None, recv_upto=None, synced=False):
        row = self._peer_row(conn, peer_id)
        old_sent = int(row["sent_upto"] or 0) if row else 0
        old_recv = int(row["recv_upto"] or 0) if row else 0
        old_name = str(row["name"] or "") if row else ""
        conn.execute(
            "INSERT OR REPLACE INTO p2p_peer_state(peer_id, name, last_sync, sent_upto, recv_upto) "
            "VALUES(?,?,?,?,?)",
            (
                peer_id,
                name or old_name,
                int(time.time()) if synced else (int(row["last_sync"] or 0) if row else 0),
                max(old_sent, int(sent_upto or 0)),
                max(old_recv, int(recv_upto or 0)),
            ))

    # ---------------- 同步协议 ----------------
    def _build_payload(self, peer_id, want_reply=False):
        since = self._peer_sent_upto(peer_id)
        data = self._extract_since(since)
        return {
            "protocol": PROTOCOL,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "since": since,
            "max_ts": data["max_ts"],
            "want_reply": bool(want_reply),
            "tables": data["tables"],
            "tombstones": data["tombstones"],
        }

    def handle_payload(self, payload, source="直连"):
        if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
            raise ValueError("协议不匹配")
        peer_id = str(payload.get("device_id") or "")
        if not peer_id or peer_id == self.device_id:
            raise ValueError("无效的对等设备")
        stats = self._apply_changes(payload.get("tables") or {},
                                    payload.get("tombstones") or [])
        conn = self._connect()
        try:
            self._update_peer(conn, peer_id, name=str(payload.get("device_name") or ""),
                              recv_upto=int(payload.get("max_ts") or 0), synced=True)
            conn.commit()
        finally:
            conn.close()
        self._note_sync(peer_id, source, stats)
        return self._build_payload(peer_id)

    def _note_sync(self, peer_id, mode, stats):
        with self._lock:
            self._status["last_sync"] = int(time.time())
            self._status["last_mode"] = mode
            self._status["last_peer"] = peer_id
            self._status["applied"] += int(stats.get("applied") or 0)
            self._status["skipped"] += int(stats.get("skipped") or 0)
            self._status["deleted"] += int(stats.get("deleted") or 0)
            self._status["peers"][peer_id] = {
                "mode": mode,
                "time": int(time.time()),
                "applied": int(stats.get("applied") or 0),
                "skipped": int(stats.get("skipped") or 0),
                "deleted": int(stats.get("deleted") or 0),
            }
        self.log("P2P 同步(%s) 来自 %s: 新增/更新 %d，跳过 %d，删除 %d" % (
            mode, peer_id[:8], stats.get("applied", 0), stats.get("skipped", 0),
            stats.get("deleted", 0)))
        self._emit_status()

    def _emit_status(self):
        if not self.on_status:
            return
        try:
            self.on_status(self.get_status())
        except Exception as exc:
            self.log("P2P 状态回调异常: %s" % exc)

    def get_status(self):
        with self._lock:
            status = dict(self._status)
            # 仅保留最近同步过的对端，避免已离线的设备一直显示为“已连接”
            now = int(time.time())
            active_after = now - max(300, int(self.interval) * 3)
            active_peers = {
                pid: info for pid, info in self._status["peers"].items()
                if int(info.get("time") or 0) >= active_after
            }
            status["peers"] = active_peers
            status["lan_peers"] = list(self._lan_peers.keys())
            status["running"] = self._running
            # 中继连通状态：最近一次中继请求成功（注册/取设备列表），有效期为同步间隔的2倍+60秒
            relay_fresh = now - max(120, int(self.interval) * 2 + 60)
            status["relay_online"] = bool(self.relay_url) and self._relay_ok_at >= relay_fresh
        return status

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._running:
            return True
        self.initialize()
        self._running = True
        self._stop_event.clear()

        try:
            self._start_http_server()
        except Exception as exc:
            self.log("P2P: HTTP 服务启动失败(%s)，局域网直连不可用" % exc)
        try:
            self._start_udp()
        except Exception as exc:
            self.log("P2P: UDP 端口启动失败(%s)，将仅使用中继信箱" % exc)

        thread = threading.Thread(target=self._sync_loop, name="p2p-sync", daemon=True)
        thread.start()
        self._threads.append(thread)
        self.log("P2P 同步已启动: 设备 %s(%s)，UDP %d，HTTP %d，中继 %s" % (
            self.device_name, self.device_id[:8], self.udp_port, self.http_port,
            self.relay_url or "未配置"))
        self._emit_status()
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._sync_request.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._udp_sock is not None:
            try:
                self._udp_sock.close()
            except OSError:
                pass
            self._udp_sock = None
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []
        self.log("P2P 同步已停止")
        self._emit_status()

    def sync_now(self):
        if not self._running:
            return False
        self._sync_request.set()
        return True

    # ---------------- HTTP 服务端 ----------------
    def _start_http_server(self):
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if urlparse(self.path).path == "/p2p/status":
                    self._send(200, engine.get_status())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if urlparse(self.path).path != "/p2p/sync":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    response = engine.handle_payload(payload, source="局域网直连")
                    self._send(200, response)
                except Exception as exc:
                    self._send(500, {"error": str(exc)})

        httpd = ThreadingHTTPServer(("0.0.0.0", self.http_port), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, name="p2p-http", daemon=True)
        thread.start()
        self._threads.append(thread)

    # ---------------- UDP 发现 / 打洞 / 传输 ----------------
    def _start_udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", self.udp_port))
        self._udp_sock = sock
        thread = threading.Thread(target=self._udp_loop, name="p2p-udp", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _udp_loop(self):
        sock = self._udp_sock
        while not self._stop_event.is_set() and sock is not None:
            try:
                sock.settimeout(1.0)
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._on_udp_packet(data, addr)
            except Exception as exc:
                self.log("P2P: UDP 包处理异常 %s" % exc)

    def _on_udp_packet(self, data, addr):
        if len(data) < 5 or not data.startswith(PACKET_MAGIC):
            return
        packet_type = data[4]
        body = data[5:]

        if packet_type in (MSG_HELLO, MSG_HELLO_ACK):
            try:
                info = json.loads(body.decode("utf-8"))
            except ValueError:
                return
            device_id = str(info.get("device_id") or "")
            if not device_id or device_id == self.device_id:
                return
            self._lan_peers[device_id] = {
                "addr": addr,
                "http_port": int(info.get("http_port") or 0),
                "name": info.get("device_name") or "",
                "seen": time.time(),
            }
            if packet_type == MSG_HELLO:
                # 回包里必须是我方自己的身份信息，否则对方会当成自己的包丢弃
                self._send_packet(MSG_HELLO_ACK, self._hello_info(), addr)
            waiter = self._hello_waiters.get(device_id)
            if waiter is not None:
                waiter.set()
            return

        if packet_type == MSG_DATA:
            if len(body) < 8:
                return
            msg_id, total, seq = struct.unpack("!IHH", body[:8])
            chunk = body[8:]
            self._send_raw(PACKET_MAGIC + bytes([MSG_ACK]) + struct.pack("!IH", msg_id, seq), addr)
            state = self._recv_parts.get(msg_id)
            if state is None or state["total"] != total:
                state = {"total": total, "parts": {}, "addr": addr, "ts": time.time()}
                self._recv_parts[msg_id] = state
            state["parts"][seq] = chunk
            if len(state["parts"]) == total:
                self._recv_parts.pop(msg_id, None)
                raw = b"".join(state["parts"].get(i, b"") for i in range(total))
                threading.Thread(target=self._handle_udp_message,
                                 args=(addr, raw), daemon=True).start()
            return

        if packet_type == MSG_ACK:
            if len(body) < 6:
                return
            msg_id, seq = struct.unpack("!IH", body[:6])
            waiter = self._ack_waiters.get((msg_id, seq))
            if waiter is not None:
                waiter.set()
            return

        if packet_type == MSG_PUNCH:
            try:
                info = json.loads(body.decode("utf-8"))
            except ValueError:
                return
            threading.Thread(target=self._incoming_punch, args=(info, addr),
                             daemon=True).start()

    def _incoming_punch(self, info, addr):
        device_id = str(info.get("device_id") or "")
        if not device_id or device_id == self.device_id:
            return
        # 优先用打洞包里携带的公网/本地候选地址互发 HELLO，双方向打开 NAT
        for candidate in (info.get("public_addr"), info.get("local_addr")):
            if not candidate:
                continue
            host, _, port = str(candidate).rpartition(":")
            if host and port.isdigit() and host not in ("127.0.0.1", "0.0.0.0"):
                if self._handshake((host, int(port)), device_id, attempts=3):
                    self._lan_peers[device_id] = {
                        "addr": (host, int(port)), "http_port": 0,
                        "name": info.get("device_name") or "", "seen": time.time(),
                    }
                    return

    def _send_raw(self, data, addr):
        sock = self._udp_sock
        if sock is None:
            return
        try:
            sock.sendto(data, addr)
        except OSError:
            pass

    def _send_packet(self, packet_type, obj, addr):
        data = PACKET_MAGIC + bytes([packet_type]) + \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_raw(data, addr)

    def _hello_info(self):
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "udp_port": self.udp_port,
            "http_port": self.http_port,
            "local_addr": "%s:%d" % (detect_local_ip(), self.udp_port),
        }

    def _handshake(self, addr, device_id, attempts=4):
        if self._udp_sock is None:
            return False
        waiter = threading.Event()
        self._hello_waiters[device_id] = waiter
        try:
            info = self._hello_info()
            for _ in range(attempts):
                self._send_packet(MSG_HELLO, info, addr)
                if waiter.wait(1.0):
                    return True
            return False
        finally:
            self._hello_waiters.pop(device_id, None)

    def _udp_send_payload(self, addr, payload):
        """可靠 UDP 分片发送（逐片 ACK + 重传），成功返回 True"""
        if self._udp_sock is None:
            return False
        raw = zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        chunks = [raw[i:i + UDP_CHUNK] for i in range(0, len(raw), UDP_CHUNK)] or [b""]
        msg_id = random.getrandbits(31)
        total = len(chunks)
        for seq, chunk in enumerate(chunks):
            packet = PACKET_MAGIC + bytes([MSG_DATA]) + \
                struct.pack("!IHH", msg_id, total, seq) + chunk
            acked = False
            for _ in range(4):
                waiter = threading.Event()
                self._ack_waiters[(msg_id, seq)] = waiter
                try:
                    self._send_raw(packet, addr)
                    if waiter.wait(0.8):
                        acked = True
                        break
                finally:
                    self._ack_waiters.pop((msg_id, seq), None)
            if not acked:
                return False
            time.sleep(0.002)
        return True

    def _handle_udp_message(self, addr, raw):
        try:
            payload = json.loads(zlib.decompress(raw).decode("utf-8"))
        except Exception:
            return
        try:
            response = self.handle_payload(payload, source="跨网打洞直连")
        except Exception as exc:
            self.log("P2P: UDP 同步失败 %s" % exc)
            return
        # 只有对方明确请求回包时才回应，避免双方无限互发空包
        if not payload.get("want_reply"):
            return
        try:
            self._udp_send_payload(addr, response)
        except Exception as exc:
            self.log("P2P: UDP 回包失败 %s" % exc)

    # ---------------- HTTP 客户端 ----------------
    def _relay_headers(self, url):
        headers = {"Accept": "application/json"}
        token = getattr(self, "relay_token", "")
        # 令牌只发给中继，避免在设备直连时泄露给对端
        if token and self.relay_url and url.startswith(self.relay_url):
            headers["X-Relay-Token"] = token
        return headers

    def _get_json(self, url, timeout=8):
        request = Request(url, headers=self._relay_headers(url), method="GET")
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, url, obj, timeout=10):
        headers = self._relay_headers(url)
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            url,
            data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST")
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _register_relay(self):
        now = time.time()
        if now - self._relay_registered < 30:
            return
        self._relay_registered = now
        try:
            self._post_json(self.relay_url + "/api/register", {
                "device_id": self.device_id,
                "device_name": self.device_name,
                "local_addr": "%s:%d" % (detect_local_ip(), self.udp_port),
                "udp_port": self.udp_port,
                "http_port": self.http_port,
            }, timeout=6)
            self._relay_ok_at = time.time()
        except Exception as exc:
            self.log("P2P: 中继注册失败 %s" % exc)

    def _relay_peers(self):
        try:
            data = self._get_json(self.relay_url + "/api/peers?device_id=" +
                                  quote(self.device_id), timeout=8)
        except Exception as exc:
            self.log("P2P: 获取中继设备列表失败 %s" % exc)
            return []
        self._relay_ok_at = time.time()
        peers = data.get("peers")
        return [p for p in peers if isinstance(p, dict)] if isinstance(peers, list) else []

    def _try_punch(self, peer_id, peer_name):
        if self._udp_sock is None:
            return False
        now = time.time()
        if now - self._punch_cooldown.get(peer_id, 0) < 300:
            return False
        self._punch_cooldown[peer_id] = now
        try:
            info = self._hello_info()
            info["target"] = peer_id
            data = self._post_json(self.relay_url + "/api/punch", info, timeout=8)
        except Exception as exc:
            self.log("P2P: 请求打洞失败 %s" % exc)
            return False

        candidates = []
        for item in data.get("candidates") or []:
            host, _, port = str(item).rpartition(":")
            if host and port.isdigit():
                candidates.append((host, int(port)))
        for addr in candidates:
            if addr[0] in ("127.0.0.1", "0.0.0.0"):
                continue
            if self._handshake(addr, peer_id):
                self._lan_peers[peer_id] = {
                    "addr": addr, "http_port": 0, "name": peer_name or "",
                    "seen": time.time(),
                }
                self.log("P2P: 与 %s 打洞成功 %s" % (peer_id[:8], addr))
                payload = self._build_payload(peer_id, want_reply=True)
                if not self._udp_send_payload(addr, payload):
                    return False
                conn = self._connect()
                try:
                    self._update_peer(conn, peer_id, name=peer_name,
                                      sent_upto=int(payload.get("max_ts") or 0), synced=True)
                    conn.commit()
                finally:
                    conn.close()
                self._note_sync(peer_id, "跨网打洞直连", {"applied": 0, "skipped": 0,
                                                          "deleted": 0})
                return True
        return False

    def _sync_peer_http(self, peer_id, base_url):
        payload = self._build_payload(peer_id)
        response = self._post_json(base_url.rstrip("/") + "/p2p/sync", payload, timeout=10)
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
            return False
        stats = self._apply_changes(response.get("tables") or {},
                                    response.get("tombstones") or [])
        conn = self._connect()
        try:
            self._update_peer(conn, peer_id, name=str(response.get("device_name") or ""),
                              sent_upto=int(payload.get("max_ts") or 0),
                              recv_upto=int(response.get("max_ts") or 0), synced=True)
            conn.commit()
        finally:
            conn.close()
        self._note_sync(peer_id, "局域网直连", stats)
        return True

    def _poll_mailbox(self):
        try:
            data = self._get_json(self.relay_url + "/api/mailbox?device_id=" +
                                  quote(self.device_id), timeout=8)
        except Exception as exc:
            self.log("P2P: 收取中继信箱失败 %s" % exc)
            return
        ids = []
        for message in data.get("messages") or []:
            if not isinstance(message, dict):
                continue
            payload = message.get("payload")
            if isinstance(payload, dict):
                try:
                    self.handle_payload(payload, source="中继信箱")
                except Exception as exc:
                    self.log("P2P: 处理信箱消息失败 %s" % exc)
            if message.get("id") is not None:
                ids.append(message.get("id"))
        if ids:
            try:
                self._post_json(self.relay_url + "/api/mailbox/ack",
                                {"device_id": self.device_id, "ids": ids}, timeout=6)
            except Exception:
                pass

    def _send_mailbox(self, peer_id, peer_name):
        payload = self._build_payload(peer_id)
        has_data = any(payload["tables"].values()) or bool(payload["tombstones"])
        if not has_data:
            return False
        try:
            self._post_json(self.relay_url + "/api/mailbox", {
                "to": peer_id,
                "from": self.device_id,
                "payload": payload,
            }, timeout=15)
        except Exception as exc:
            self.log("P2P: 发送信箱消息失败 %s" % exc)
            return False
        conn = self._connect()
        try:
            self._update_peer(conn, peer_id, name=peer_name,
                              sent_upto=int(payload.get("max_ts") or 0), synced=True)
            conn.commit()
        finally:
            conn.close()
        self.log("P2P: 已通过中继信箱向 %s 发送增量" % peer_id[:8])
        return True

    # ---------------- 同步循环 ----------------
    def _announce(self):
        sock = self._udp_sock
        if sock is None:
            return
        data = PACKET_MAGIC + bytes([MSG_HELLO]) + \
            json.dumps(self._hello_info(), ensure_ascii=False).encode("utf-8")
        for target in (("255.255.255.255", self.udp_port),):
            try:
                sock.sendto(data, target)
            except OSError:
                pass

    def _sync_loop(self):
        self._stop_event.wait(2)
        while not self._running and not self._stop_event.is_set():
            self._stop_event.wait(0.5)
        while not self._stop_event.is_set():
            try:
                self._sync_cycle()
            except Exception as exc:
                self.log("P2P: 同步循环异常 %s" % exc)
                with self._lock:
                    self._status["last_message"] = "同步异常: %s" % exc
                self._emit_status()

            deadline = time.time() + self.interval
            while time.time() < deadline and not self._stop_event.is_set():
                if self._sync_request.wait(1.0):
                    break
            self._sync_request.clear()

    def _sync_cycle(self):
        self._announce()
        # 清理超时的局域网对等端
        now = time.time()
        for device_id in list(self._lan_peers.keys()):
            if now - self._lan_peers[device_id]["seen"] > 180:
                self._lan_peers.pop(device_id, None)

        handled = set()
        # 1) 局域网 HTTP 直连
        for device_id, info in list(self._lan_peers.items()):
            if info.get("http_port"):
                base_url = "http://%s:%d" % (info["addr"][0], info["http_port"])
                try:
                    if self._sync_peer_http(device_id, base_url):
                        handled.add(device_id)
                except Exception as exc:
                    self.log("P2P: 局域网直连 %s 失败 %s" % (device_id[:8], exc))

        # 2) 跨网：打洞优先，中继信箱兜底
        if self.relay_url:
            self._register_relay()
            self._poll_mailbox()
            for peer in self._relay_peers():
                peer_id = str(peer.get("device_id") or "")
                if not peer_id or peer_id == self.device_id or peer_id in handled:
                    continue
                peer_name = str(peer.get("device_name") or "")
                if peer_id in self._lan_peers and self._lan_peers[peer_id].get("addr"):
                    addr = self._lan_peers[peer_id]["addr"]
                    # 打洞通道可能已失效，先握手确认再传数据
                    if not self._handshake(addr, peer_id):
                        self._lan_peers.pop(peer_id, None)
                        addr = None
                    payload = self._build_payload(peer_id, want_reply=True) if addr else None
                    if addr and self._udp_send_payload(addr, payload):
                        conn = self._connect()
                        try:
                            self._update_peer(conn, peer_id, name=peer_name,
                                              sent_upto=int(payload.get("max_ts") or 0),
                                              synced=True)
                            conn.commit()
                        finally:
                            conn.close()
                        continue
                if self._try_punch(peer_id, peer_name):
                    continue
                self._send_mailbox(peer_id, peer_name)

        with self._lock:
            self._status["last_message"] = "同步完成 %s" % time.strftime("%H:%M:%S")
        self._emit_status()


def main():
    parser = argparse.ArgumentParser(description="点名软件 P2P 同步引擎（独立运行便于测试）")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "call_sign_history.db"))
    parser.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "app_config.ini"))
    parser.add_argument("--once", action="store_true", help="只执行一次同步后退出")
    args = parser.parse_args()

    def log(msg):
        try:
            sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
            sys.stdout.flush()
        except Exception:
            pass

    engine = P2PSyncEngine(args.db, args.config, logger=log)
    engine.start()
    if args.once:
        engine.sync_now()
        time.sleep(10)
        engine.stop()
        return
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()

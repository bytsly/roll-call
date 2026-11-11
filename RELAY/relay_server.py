#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点名软件 P2P 轻量中继服务（仅依赖标准库）

只做四件事：
1. 设备注册与互相发现（记录 device_id、名称、公网 IP、本地地址与端口）
2. 协助 UDP 打洞（双向朝公网地址发送打洞包）
3. 离线消息信箱（跨网直连失败时，代为转发增量数据包）
4. MMDVM 更新程序（更新软件）的常开 TCP 中继：46001 端口，
   保存联系人修改历史，供节点上线时补齐（跨时段同步）

不保存点名业务数据、不做数据库合并、不提供整库上传下载。
运行方式：
    python relay_server.py --host 0.0.0.0 --port 4570 --db relay_data.db
    python relay_server.py --mmdvm-port 46001      # 46001 中继可单独使用
"""

import argparse
import csv
import io
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAX_MAIL_BYTES = 4 * 1024 * 1024     # 单条信箱消息上限
MAIL_TTL = 7 * 86400                 # 信箱消息保留 7 天
DEVICE_TTL = 600                     # 设备在线判定 10 分钟
PUNCH_MAGIC = b"RCP2\x05"            # 与 p2p_sync 的打洞包前缀一致

MMDVM_PORT = 46001                   # 更新软件 P2P 默认端口
MMDVM_PROTO_VERSION = 1              # 与 p2p_sync.PROTO_VERSION 一致
MMDVM_MAX_HISTORY = 5000             # 中继保存的修改条数上限
MMDVM_MAX_SEEN = 20000               # 去重窗口
MMDVM_CLIENT_TIMEOUT = 120           # 客户端静默超时（秒）
MMDVM_CONNECT_LOG_INTERVAL = 1800    # 同一节点“接入”日志最小间隔（秒），避免重连刷屏
MMDVM_LONG_CONNECT = 300             # 连接超过该时长才记录“断开”（秒）
CLEANUP_INTERVAL = 600               # 定期清理间隔（秒）

APP_CONNECT_LOG_INTERVAL = 1800      # 点名软件“主控接入”日志最小间隔（秒）
APP_CONNECT_LOG_TS = {}              # (主控呼号, 来源IP) -> 最近记录时间
APP_CONNECT_LOG_LOCK = threading.Lock()

RELAY_VERSION = "1.1"                # 中继服务版本号

# 手机点名：页面文件名与允许查询的字段（防止 SQL 注入）
MOBILE_PAGE = "mobile_roll_call.html"
SIGNUP_PAGE = "signup.html"
MOBILE_OPTION_FIELDS = ("qth", "equipment", "power", "antenna", "access_method")

DB_LOCK = threading.Lock()
DB_PATH = "relay_data.db"
RELAY_TOKEN = ""
QUIET = False
VERBOSE = False


def _write_log(msg):
    try:
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stdout.flush()
    except Exception:
        pass


def log(msg):
    """重要日志（默认显示）：启动、报错、联系人修改、点名等"""
    if not QUIET:
        _write_log(msg)


def detail(msg):
    """例行日志（默认隐藏，--verbose 时显示）：连接/断开、HTTP 访问等"""
    if not QUIET and VERBOSE:
        _write_log(msg)


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id   TEXT PRIMARY KEY,
                device_name TEXT,
                public_ip   TEXT,
                local_addr  TEXT,
                udp_port    INTEGER,
                http_port   INTEGER,
                last_seen   INTEGER
            );
            CREATE TABLE IF NOT EXISTS mailbox (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                to_device  TEXT NOT NULL,
                from_device TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mailbox_to ON mailbox(to_device);
            CREATE TABLE IF NOT EXISTS mmdvm_updates (
                msg_id     TEXT PRIMARY KEY,
                radio_id   TEXT,
                callsign   TEXT,
                name       TEXT,
                city       TEXT,
                state      TEXT,
                country    TEXT,
                remarks    TEXT,
                ts         REAL,
                author     TEXT,
                created_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_mmdvm_ts ON mmdvm_updates(ts);
            CREATE TABLE IF NOT EXISTS master_call_signs (
                call_sign      TEXT PRIMARY KEY,
                qth            TEXT,
                equipment      TEXT,
                power          TEXT,
                antenna        TEXT,
                access_method  TEXT,
                updated_at     INTEGER NOT NULL DEFAULT 0,
                device_id      TEXT DEFAULT '',
                deleted        INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_master_call_signs_ts ON master_call_signs(updated_at);
            CREATE TABLE IF NOT EXISTS library_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS mobile_batches (
                batch_id   TEXT PRIMARY KEY,
                name       TEXT,
                operator   TEXT,
                created_at INTEGER NOT NULL,
                status     TEXT NOT NULL DEFAULT 'open',
                closed_at  INTEGER
            );
            CREATE TABLE IF NOT EXISTS mobile_records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id       TEXT NOT NULL,
                ranking        TEXT,
                roll_time      TEXT,
                call_sign      TEXT NOT NULL,
                qth            TEXT,
                equipment      TEXT,
                power          TEXT,
                antenna        TEXT,
                access_method  TEXT,
                remarks        TEXT,
                ip_address     TEXT,
                created_at     INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mobile_records_batch
                ON mobile_records(batch_id, id);
            CREATE TABLE IF NOT EXISTS signup_sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                start_at   INTEGER NOT NULL DEFAULT 0,
                note       TEXT DEFAULT '',
                capacity   INTEGER NOT NULL DEFAULT 0,
                status     TEXT NOT NULL DEFAULT 'open',
                created_by TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signup_entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                call_sign  TEXT NOT NULL,
                qth        TEXT DEFAULT '',
                equipment  TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                UNIQUE(session_id, call_sign)
            );
            CREATE INDEX IF NOT EXISTS idx_signup_entries_session
                ON signup_entries(session_id, id);
            CREATE TABLE IF NOT EXISTS device_catalog (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                brand      TEXT NOT NULL,
                model      TEXT NOT NULL,
                name       TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            """
        )
        conn.execute("INSERT OR IGNORE INTO library_meta(key, value) VALUES('version', '0')")
        # baseline：灌入全国库等整库基线变更时更新，客户端据此做一次整库拉取
        conn.execute("INSERT OR IGNORE INTO library_meta(key, value) VALUES('baseline', '0')")
        # 旧表列名 current_time 会被 SQLite 当作 CURRENT_TIME 关键字，迁移为 roll_time
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mobile_records)")]
        if "current_time" in cols and "roll_time" not in cols:
            conn.execute("ALTER TABLE mobile_records RENAME COLUMN current_time TO roll_time")
        # 首次升级：已有主控库数据时把版本置 1，触发客户端下次登录整库拉取
        if conn.execute("SELECT COUNT(*) FROM master_call_signs").fetchone()[0] \
                and _library_version(conn) == 0:
            conn.execute("UPDATE library_meta SET value = '1' WHERE key = 'version'")
        # 整库基线导入时若未设置 updated_at（默认 0），增量同步与 LWW 都会忽略这些行；
        # 统一归一到基线时间戳，使客户端整库拉取时能正确应用
        conn.execute(
            "UPDATE master_call_signs SET updated_at = "
            "COALESCE((SELECT CAST(value AS INTEGER) FROM library_meta WHERE key='baseline'), 0) "
            "WHERE updated_at = 0")
        conn.commit()
    finally:
        conn.close()


def _library_version(conn):
    """中继主控库版本号（每次接受变更后 +1）"""
    row = conn.execute("SELECT value FROM library_meta WHERE key = 'version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _library_baseline(conn):
    """中继主控库基线标记（整库基线变更时更新，0 表示未灌入过基线）"""
    row = conn.execute("SELECT value FROM library_meta WHERE key = 'baseline'").fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _bump_library_version(conn):
    """版本号递增，返回新版本"""
    conn.execute("INSERT OR IGNORE INTO library_meta(key, value) VALUES('version', '0')")
    conn.execute("UPDATE library_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                 "WHERE key = 'version'")
    return _library_version(conn)


def cleanup():
    """清理过期消息与离线的设备记录"""
    conn = _connect()
    try:
        now = int(time.time())
        conn.execute("DELETE FROM mailbox WHERE created_at < ?", (now - MAIL_TTL,))
        conn.execute("DELETE FROM devices WHERE last_seen < ?", (now - DEVICE_TTL,))
        conn.commit()
    finally:
        conn.close()


def cleanup_loop():
    """后台线程：按 CLEANUP_INTERVAL 周期清理过期数据"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup()
        except Exception as exc:
            log("定期清理失败: %s" % exc)


def _peer_dict(row, exclude=None):
    return {
        "device_id": row["device_id"],
        "device_name": row["device_name"] or "",
        "public_ip": row["public_ip"] or "",
        "local_addr": row["local_addr"] or "",
        "udp_port": row["udp_port"] or 0,
        "http_port": row["http_port"] or 0,
        "last_seen": row["last_seen"] or 0,
    }


def _accept_lww(conn, call_sign, ts, device_id):
    """LWW 判定：远端 (时间戳, 设备ID) 更大时才接受"""
    row = conn.execute(
        "SELECT updated_at, device_id FROM master_call_signs WHERE call_sign = ?",
        (call_sign,)).fetchone()
    if row is None:
        return True
    return (int(ts), str(device_id)) > (int(row["updated_at"] or 0), str(row["device_id"] or ""))


def punch_targets(targets):
    """向目标地址反复发送打洞包，帮助双方 NAT 打开映射"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for ip, port, info in targets:
            data = PUNCH_MAGIC + json.dumps(info, ensure_ascii=False).encode("utf-8")
            for _ in range(6):
                try:
                    sock.sendto(data, (ip, port))
                except OSError:
                    pass
                time.sleep(0.08)
    finally:
        sock.close()


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "CallSignRelay/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # HTTP 访问日志属于例行信息，默认不显示（--verbose 可打开）
        detail("%s %s" % (self.address_string(), fmt % args))

    # ---------- 基础工具 ----------
    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not RELAY_TOKEN:
            return True
        return self.headers.get("X-Relay-Token", "") == RELAY_TOKEN

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            conn = _connect()
            try:
                n_dev = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                n_mail = conn.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
                n_upd = conn.execute("SELECT COUNT(*) FROM mmdvm_updates").fetchone()[0]
            finally:
                conn.close()
            health = {
                "ok": True,
                "service": "call-sign-p2p-relay",
                "version": RELAY_VERSION,
                "devices": n_dev,
                "mailbox": n_mail,
                "time": int(time.time()),
            }
            if MMDVM_RELAY is not None:
                health["mmdvm"] = {
                    "port": MMDVM_RELAY.port,
                    "clients": MMDVM_RELAY.client_count(),
                    "updates": n_upd,
                }
            self._json(200, health)
            return

        # ---------- 手机点名：手机端公开访问 ----------
        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/mobile_roll_call")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path in ("/mobile_roll_call", "/mobile_roll_call/"):
            self._serve_mobile_page()
            return

        if path == "/api/master_database/list":
            prefix = (query.get("prefix") or [""])[0].strip().upper()
            try:
                limit = min(int((query.get("limit") or ["10"])[0]), 50)
            except (TypeError, ValueError):
                limit = 10
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT call_sign FROM master_call_signs "
                    "WHERE deleted = 0 AND call_sign LIKE ? "
                    "ORDER BY call_sign LIMIT ?", (prefix + "%", limit)).fetchall()
            finally:
                conn.close()
            self._json(200, {"call_signs": [r["call_sign"] for r in rows]})
            return

        if path == "/api/master_database/search":
            call_sign = (query.get("call_sign") or [""])[0].strip().upper()
            conn = _connect()
            try:
                if call_sign:
                    rows = conn.execute(
                        "SELECT call_sign, qth, equipment, power, antenna, access_method "
                        "FROM master_call_signs WHERE call_sign = ? AND deleted = 0 LIMIT 1",
                        (call_sign,)).fetchall()
                else:
                    rows = []
            finally:
                conn.close()
            self._json(200, {"records": [dict(r) for r in rows]})
            return

        if path.startswith("/api/options/"):
            field = path.rsplit("/", 1)[-1]
            if field not in MOBILE_OPTION_FIELDS:
                self._json(404, {"error": "not found"})
                return
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT %s FROM master_call_signs "
                    "WHERE deleted = 0 AND %s IS NOT NULL AND %s != '' ORDER BY %s"
                    % (field, field, field, field)).fetchall()
                options = [r[0] for r in rows]
                if field == "equipment":
                    try:
                        options += [r[0] for r in conn.execute(
                            "SELECT name FROM device_catalog WHERE name != ''")]
                    except sqlite3.Error:
                        pass
                    options = sorted(set(options))
            finally:
                conn.close()
            self._json(200, {"status": "success", "options": options})
            return

        if path == "/api/mobile_roll_call/export_csv":
            self._serve_mobile_csv(query)
            return

        if path == "/api/mobile_roll_call/check_data_before_delete":
            self._api_mobile_check_data()
            return

        # ---------- 场次报名：公开访问 ----------
        if path in ("/signup", "/signup/"):
            self._serve_signup_page()
            return

        if path == "/api/signup/sessions":
            self._api_signup_list()
            return

        if path == "/api/signup/options":
            self._api_signup_options()
            return

        if path == "/api/signup/lookup":
            self._api_signup_lookup(query)
            return

        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return

        if path == "/api/mobile/records":
            self._api_mobile_records(query)
            return

        if path == "/api/peers":
            me = (query.get("device_id") or [""])[0]
            conn = _connect()
            try:
                rows = conn.execute("SELECT * FROM devices WHERE last_seen >= ?",
                                    (int(time.time()) - DEVICE_TTL,)).fetchall()
            finally:
                conn.close()
            peers = [_peer_dict(r) for r in rows if r["device_id"] != me]
            self._json(200, {"peers": peers})
            return

        if path == "/api/mailbox":
            device_id = (query.get("device_id") or [""])[0]
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT id, from_device, payload, created_at FROM mailbox "
                    "WHERE to_device = ? ORDER BY id LIMIT 20", (device_id,)).fetchall()
            finally:
                conn.close()
            messages = []
            for r in rows:
                try:
                    payload = json.loads(r["payload"])
                except ValueError:
                    payload = None
                messages.append({
                    "id": r["id"],
                    "from": r["from_device"],
                    "created_at": r["created_at"],
                    "payload": payload,
                })
            self._json(200, {"messages": messages})
            return

        if path == "/api/library/stats":
            conn = _connect()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM master_call_signs WHERE deleted = 0").fetchone()[0]
                last = conn.execute(
                    "SELECT MAX(updated_at) FROM master_call_signs").fetchone()[0] or 0
                version = _library_version(conn)
            finally:
                conn.close()
            self._json(200, {"ok": True, "version": version, "count": count,
                             "last_update": last})
            return

        if path == "/api/library/version":
            conn = _connect()
            try:
                version = _library_version(conn)
                baseline = _library_baseline(conn)
                count = conn.execute(
                    "SELECT COUNT(*) FROM master_call_signs WHERE deleted = 0").fetchone()[0]
                last = conn.execute(
                    "SELECT MAX(updated_at) FROM master_call_signs").fetchone()[0] or 0
            finally:
                conn.close()
            self._json(200, {"ok": True, "version": version, "baseline": baseline,
                             "count": count, "last_update": last})
            return

        if path == "/api/library/snapshot":
            """整库快照：返回中继汇聚后的全部呼号（含墓碑），供登录时整库拉取"""
            conn = _connect()
            try:
                version = _library_version(conn)
                baseline = _library_baseline(conn)
                count = conn.execute(
                    "SELECT COUNT(*) FROM master_call_signs WHERE deleted = 0").fetchone()[0]
                last = conn.execute(
                    "SELECT MAX(updated_at) FROM master_call_signs").fetchone()[0] or 0
                rows = conn.execute(
                    "SELECT call_sign, qth, equipment, power, antenna, access_method, "
                    "updated_at, device_id, deleted FROM master_call_signs "
                    "ORDER BY updated_at").fetchall()
            finally:
                conn.close()
            snapshot = []
            for r in rows:
                snapshot.append({
                    "call_sign": r["call_sign"],
                    "qth": r["qth"], "equipment": r["equipment"], "power": r["power"],
                    "antenna": r["antenna"], "access_method": r["access_method"],
                    "updated_at": r["updated_at"] or 0,
                    "device_id": r["device_id"] or "",
                    "deleted": 1 if r["deleted"] else 0,
                })
            self._json(200, {"ok": True, "version": version, "baseline": baseline,
                             "count": count, "last_update": last,
                             "server_time": int(time.time() * 1000),
                             "rows": snapshot})
            return

        self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path

        # 手机端提交点名为公开接口（无令牌），但必须存在进行中的点名批次
        if path == "/api/mobile_roll_call":
            try:
                body = self._body()
            except Exception as exc:
                self._json(400, {"error": "bad json: %s" % exc})
                return
            self._api_mobile_submit(body)
            return

        # 手机端点“结束点名并返回首页”：关闭批次并清理已结束批次的历史记录
        if path == "/api/mobile_roll_call/delete_tables":
            self._api_mobile_delete_tables()
            return

        # 场次报名：HAM 报名（公开）、主控申请/关闭场次（页面内校验口令）
        if path == "/api/signup/entry":
            try:
                body = self._body()
            except Exception as exc:
                self._json(400, {"error": "bad json: %s" % exc})
                return
            self._api_signup_entry(body)
            return

        if path in ("/api/signup/sessions", "/api/signup/sessions/close"):
            try:
                body = self._body()
            except Exception as exc:
                self._json(400, {"error": "bad json: %s" % exc})
                return
            if not self._signup_master_ok(body):
                self._json(403, {"status": "error", "message": "主控口令错误"})
                return
            if path.endswith("/close"):
                self._api_signup_close(body)
            else:
                self._api_signup_create(body)
            return

        # 设备库：手机点名 / 报名页面自行添加品牌+型号
        if path == "/api/device/add":
            try:
                body = self._body()
            except Exception as exc:
                self._json(400, {"error": "bad json: %s" % exc})
                return
            self._api_device_add(body)
            return

        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return

        try:
            body = self._body()
        except Exception as exc:
            self._json(400, {"error": "bad json: %s" % exc})
            return

        if path == "/api/mobile/batch":
            self._api_mobile_batch_create(body)
            return

        if path == "/api/mobile/batch/close":
            self._api_mobile_batch_close(body)
            return

        if path == "/api/register":
            self._handle_register(body)
            return

        if path == "/api/punch":
            self._handle_punch(body)
            return

        if path == "/api/mailbox":
            self._handle_mail_post(body)
            return

        if path == "/api/mailbox/ack":
            ids = body.get("ids") or []
            device_id = body.get("device_id") or ""
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            with DB_LOCK:
                conn = _connect()
                try:
                    for mid in ids:
                        conn.execute("DELETE FROM mailbox WHERE id = ? AND to_device = ?",
                                     (int(mid), device_id))
                    conn.commit()
                finally:
                    conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/library/sync":
            self._handle_library_sync(body)
            return

        if path == "/api/announce":
            self._handle_announce(body)
            return

        self._json(404, {"error": "not found"})

    # ---------- 手机点名 ----------
    def _serve_mobile_page(self):
        page = _resource_path(MOBILE_PAGE)
        try:
            with open(page, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            self._json(404, {"error": "手机点名页面未部署: %s" % MOBILE_PAGE})
            return
        html = html.replace("{{ version }}", "relay v%s" % RELAY_VERSION)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_mobile_csv(self, query):
        batch_id = (query.get("batch_id") or [""])[0].strip()
        conn = _connect()
        try:
            if not batch_id:
                row = conn.execute(
                    "SELECT batch_id FROM mobile_batches ORDER BY created_at DESC LIMIT 1").fetchone()
                batch_id = row["batch_id"] if row else ""
            if batch_id:
                rows = conn.execute(
                    "SELECT ranking, roll_time, call_sign, qth, equipment, power, "
                    "antenna, access_method, remarks, ip_address, created_at "
                    "FROM mobile_records WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
            else:
                rows = []
        finally:
            conn.close()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["名次", "点名时间", "呼号", "QTH", "设备", "功率",
                         "天线", "接入方式", "备注", "IP", "记录时间"])
        for r in rows:
            writer.writerow([
                r["ranking"], r["roll_time"], r["call_sign"], r["qth"],
                r["equipment"], r["power"], r["antenna"], r["access_method"],
                r["remarks"], r["ip_address"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"])),
            ])

        body = buf.getvalue().encode("utf-8-sig")
        filename = "mobile_roll_call_%s.csv" % (batch_id or "records")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 场次报名 ----------
    def _serve_signup_page(self):
        page = _resource_path(SIGNUP_PAGE)
        try:
            with open(page, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            self._json(404, {"error": "报名页面未部署: %s" % SIGNUP_PAGE})
            return
        html = html.replace("{{ version }}", "relay v%s" % RELAY_VERSION)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _signup_master_ok(self, body):
        """主控操作校验：中继启用口令时必须匹配，未启用时放行"""
        if not RELAY_TOKEN:
            return True
        return str(body.get("token") or "") == RELAY_TOKEN

    def _api_signup_list(self):
        conn = _connect()
        try:
            sessions = conn.execute(
                "SELECT id, title, start_at, note, capacity, status, created_by, created_at "
                "FROM signup_sessions ORDER BY status = 'closed', start_at, id").fetchall()
            entries = conn.execute(
                "SELECT session_id, call_sign, qth, equipment, created_at "
                "FROM signup_entries ORDER BY id").fetchall()
        finally:
            conn.close()
        grouped = {}
        for r in entries:
            grouped.setdefault(r["session_id"], []).append({
                "call_sign": r["call_sign"], "qth": r["qth"] or "",
                "equipment": r["equipment"] or "", "created_at": r["created_at"],
            })
        data = []
        for s in sessions:
            item = dict(s)
            item["entries"] = grouped.get(s["id"], [])
            data.append(item)
        self._json(200, {"status": "success", "server_time": int(time.time()),
                         "sessions": data})

    def _api_signup_create(self, body):
        title = str(body.get("title") or "").strip()
        note = str(body.get("note") or "").strip()
        created_by = str(body.get("created_by") or "").strip().upper()
        start_raw = body.get("start_at")
        try:
            capacity = max(0, int(body.get("capacity") or 0))
        except (TypeError, ValueError):
            capacity = 0

        if not title:
            self._json(400, {"status": "error", "message": "场次名称不能为空"})
            return

        start_at = 0
        if isinstance(start_raw, (int, float)) and start_raw > 0:
            start_at = int(start_raw)
        elif isinstance(start_raw, str) and start_raw.strip():
            text = start_raw.strip().replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    start_at = int(time.mktime(time.strptime(text, fmt)))
                    break
                except ValueError:
                    continue
            if not start_at:
                self._json(400, {"status": "error",
                                 "message": "开始时间格式应为 YYYY-MM-DD HH:MM"})
                return

        now = int(time.time())
        with DB_LOCK:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "INSERT INTO signup_sessions(title, start_at, note, capacity, status, "
                    "created_by, created_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                    (title, start_at, note, capacity, created_by, now))
                conn.commit()
                session_id = cursor.lastrowid
            finally:
                conn.close()
        log("场次报名: 主控 %s 申请场次《%s》开始=%s 名额=%s"
            % (created_by or "-", title,
               time.strftime("%Y-%m-%d %H:%M", time.localtime(start_at)) if start_at else "待定",
               capacity or "不限"))
        self._json(200, {"status": "success", "message": "场次已创建", "session_id": session_id})

    def _api_signup_close(self, body):
        try:
            session_id = int(body.get("session_id") or 0)
        except (TypeError, ValueError):
            session_id = 0
        with DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute("SELECT title FROM signup_sessions WHERE id = ?",
                                   (session_id,)).fetchone()
                if not row:
                    self._json(404, {"status": "error", "message": "场次不存在"})
                    return
                conn.execute("UPDATE signup_sessions SET status = 'closed' WHERE id = ?",
                             (session_id,))
                conn.commit()
            finally:
                conn.close()
        log("场次报名: 关闭场次《%s》" % row["title"])
        self._json(200, {"status": "success", "message": "场次已关闭报名"})

    def _client_db_path(self):
        """查找点名软件呼号库（中继与主控同机时可直接读取）"""
        base = _base_dir()
        parent = os.path.dirname(base)
        for candidate in (
            os.path.join(base, "call_sign_history.db"),
            os.path.join(parent, "call_sign_history.db"),
            os.path.join(parent, "dist", "call_sign_history.db"),
        ):
            if os.path.exists(candidate):
                return candidate
        return None

    def _api_signup_options(self):
        """QTH / 设备候选值：合并中继点名记录与主控呼号库"""
        qth, equipment = set(), set()

        conn = _connect()
        try:
            for r in conn.execute(
                    "SELECT DISTINCT qth FROM mobile_records WHERE qth IS NOT NULL AND qth <> ''"):
                qth.add(str(r[0]).strip())
            for r in conn.execute(
                    "SELECT DISTINCT equipment FROM mobile_records "
                    "WHERE equipment IS NOT NULL AND equipment <> ''"):
                equipment.add(str(r[0]).strip())
            for r in conn.execute(
                    "SELECT DISTINCT qth FROM signup_entries WHERE qth IS NOT NULL AND qth <> ''"):
                qth.add(str(r[0]).strip())
            for r in conn.execute(
                    "SELECT DISTINCT equipment FROM signup_entries "
                    "WHERE equipment IS NOT NULL AND equipment <> ''"):
                equipment.add(str(r[0]).strip())
            for r in conn.execute("SELECT name FROM device_catalog WHERE name <> ''"):
                equipment.add(str(r[0]).strip())
        finally:
            conn.close()

        db = self._client_db_path()
        if db:
            try:
                client = sqlite3.connect(db, timeout=5)
                try:
                    for table in ("call_sign_library", "call_signs", "roll_call_records"):
                        try:
                            qth.update(str(r[0]).strip() for r in client.execute(
                                "SELECT DISTINCT qth FROM %s WHERE qth IS NOT NULL AND qth <> ''" % table))
                            equipment.update(str(r[0]).strip() for r in client.execute(
                                "SELECT DISTINCT equipment FROM %s "
                                "WHERE equipment IS NOT NULL AND equipment <> ''" % table))
                        except sqlite3.Error:
                            continue
                    try:
                        equipment.update(str(r[0]).strip() for r in client.execute(
                            "SELECT DISTINCT model FROM walkie_talkies "
                            "WHERE model IS NOT NULL AND model <> ''"))
                    except sqlite3.Error:
                        pass
                finally:
                    client.close()
            except sqlite3.Error:
                pass

        self._json(200, {"status": "success", "qth": sorted(qth),
                         "equipment": sorted(equipment)})

    def _api_signup_lookup(self, query):
        """按呼号带出最近使用的 QTH / 设备"""
        call_sign = (query.get("call_sign") or [""])[0].strip().upper()
        if not call_sign:
            self._json(400, {"status": "error", "message": "呼号不能为空"})
            return
        qth, equipment, source = "", "", ""

        db = self._client_db_path()
        if db:
            try:
                client = sqlite3.connect(db, timeout=5)
                try:
                    for table in ("call_sign_library", "call_signs", "roll_call_records"):
                        try:
                            row = client.execute(
                                "SELECT qth, equipment FROM %s WHERE UPPER(call_sign) = ? "
                                "ORDER BY id DESC LIMIT 1" % table, (call_sign,)).fetchone()
                        except sqlite3.Error:
                            continue
                        if row and (row[0] or row[1]):
                            qth = (row[0] or "").strip()
                            equipment = (row[1] or "").strip()
                            source = "呼号库" if table != "roll_call_records" else "点名记录"
                            break
                finally:
                    client.close()
            except sqlite3.Error:
                pass

        if not qth and not equipment:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT qth, equipment FROM mobile_records WHERE UPPER(call_sign) = ? "
                    "ORDER BY id DESC LIMIT 1", (call_sign,)).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT qth, equipment FROM signup_entries WHERE UPPER(call_sign) = ? "
                        "ORDER BY id DESC LIMIT 1", (call_sign,)).fetchone()
            finally:
                conn.close()
            if row:
                qth = (row["qth"] or "").strip()
                equipment = (row["equipment"] or "").strip()
                source = "历史报名"

        self._json(200, {"status": "success", "call_sign": call_sign,
                         "qth": qth, "equipment": equipment, "source": source})

    def _api_device_add(self, body):
        """新增设备型号（品牌 + 型号），写入中继设备库供两个页面选择"""
        brand = str(body.get("brand") or "").strip()
        model = str(body.get("model") or "").strip()
        if not brand or not model:
            self._json(400, {"status": "error", "message": "品牌和型号不能为空"})
            return
        name = "%s-%s" % (brand, model)
        now_ms = int(time.time() * 1000)
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO device_catalog(brand, model, name, created_at) "
                "VALUES (?, ?, ?, ?)", (brand, model, name, now_ms))
            conn.commit()
        finally:
            conn.close()
        log("设备库新增: %s" % name)
        self._json(200, {"status": "success", "name": name})

    def _api_signup_entry(self, body):
        try:
            session_id = int(body.get("session_id") or 0)
        except (TypeError, ValueError):
            session_id = 0
        call_sign = str(body.get("call_sign") or "").strip().upper()
        qth = str(body.get("qth") or "").strip()
        equipment = str(body.get("equipment") or "").strip()

        if not session_id:
            self._json(400, {"status": "error", "message": "请选择要报名的场次"})
            return
        if not call_sign:
            self._json(400, {"status": "error", "message": "呼号不能为空"})
            return
        if not qth:
            self._json(400, {"status": "error", "message": "QTH不能为空"})
            return
        if not equipment:
            self._json(400, {"status": "error", "message": "设备不能为空"})
            return

        now = int(time.time())
        with DB_LOCK:
            conn = _connect()
            try:
                session = conn.execute(
                    "SELECT id, title, status, capacity FROM signup_sessions WHERE id = ?",
                    (session_id,)).fetchone()
                if not session:
                    self._json(404, {"status": "error", "message": "场次不存在"})
                    return
                if session["status"] != "open":
                    self._json(403, {"status": "error", "message": "该场次已关闭报名"})
                    return
                exists = conn.execute(
                    "SELECT 1 FROM signup_entries WHERE session_id = ? AND call_sign = ?",
                    (session_id, call_sign)).fetchone()
                if exists:
                    self._json(409, {"status": "error", "message": "该呼号已报名本场次"})
                    return
                if session["capacity"] and conn.execute(
                        "SELECT COUNT(*) FROM signup_entries WHERE session_id = ?",
                        (session_id,)).fetchone()[0] >= session["capacity"]:
                    self._json(403, {"status": "error", "message": "本场次报名人数已满"})
                    return
                conn.execute(
                    "INSERT INTO signup_entries(session_id, call_sign, qth, equipment, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", (session_id, call_sign, qth, equipment, now))
                # 报名数据直接写入中继主控呼号库，主控点名软件同步后即可使用最新QTH/设备
                now_ms = int(time.time() * 1000)
                conn.execute(
                    "INSERT INTO master_call_signs(call_sign, qth, equipment, updated_at, "
                    "device_id, deleted) VALUES(?,?,?,?,?,0) "
                    "ON CONFLICT(call_sign) DO UPDATE SET qth=excluded.qth, "
                    "equipment=excluded.equipment, updated_at=excluded.updated_at, "
                    "device_id=excluded.device_id, deleted=0",
                    (call_sign, qth, equipment, now_ms, "relay-signup"))
                _bump_library_version(conn)
                conn.commit()
            finally:
                conn.close()
        log("场次报名: %s 报名《%s》QTH=%s 设备=%s（已写入中继呼号库）"
            % (call_sign, session["title"], qth, equipment))
        self._json(200, {"status": "success", "message": "报名成功",
                         "session_id": session_id, "call_sign": call_sign})

    def _api_mobile_submit(self, body):
        ranking = str(body.get("ranking") or "").strip()
        current_time = str(body.get("currentTime") or "").strip()
        call_sign = str(body.get("callSign") or "").strip().upper()
        qth = str(body.get("qth") or "").strip()
        equipment = str(body.get("equipment") or "").strip()
        power = str(body.get("power") or "").strip()
        antenna = str(body.get("antenna") or "").strip()
        access_method = str(body.get("access_method") or "").strip()
        remarks = str(body.get("remarks") or "").strip()

        if not call_sign:
            self._json(400, {"status": "error", "message": "呼号不能为空"})
            return
        if not qth:
            self._json(400, {"status": "error", "message": "QTH位置信息不能为空"})
            return
        if not equipment:
            self._json(400, {"status": "error", "message": "设备型号不能为空"})
            return

        now = int(time.time())
        is_host = ranking == "0"
        started = False
        record_id = None
        batch_id = ""
        with DB_LOCK:
            conn = _connect()
            try:
                requested = str(body.get("batch_id") or "").strip()
                batch = None
                if requested:
                    row = conn.execute(
                        "SELECT batch_id, status FROM mobile_batches WHERE batch_id = ?",
                        (requested,)).fetchone()
                    if row and row["status"] == "open":
                        batch = row["batch_id"]
                if not batch:
                    row = conn.execute(
                        "SELECT batch_id FROM mobile_batches WHERE status = 'open' "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
                    batch = row["batch_id"] if row else ""
                if not batch and is_host:
                    # 名次 0 表示主控签到：主控签到后自动开启点名批次
                    batch = "%s_%d" % (call_sign, now)
                    conn.execute(
                        "INSERT INTO mobile_batches(batch_id, name, operator, created_at, status) "
                        "VALUES (?, ?, ?, ?, 'open')",
                        (batch, call_sign, call_sign, now))
                    started = True
                if not batch:
                    self._json(403, {"status": "error",
                                     "message": "当前没有进行中的点名，请主控签到（名次填 0）后开始点名"})
                    return
                cursor = conn.execute(
                    "INSERT INTO mobile_records(batch_id, ranking, roll_time, call_sign, qth, "
                    "equipment, power, antenna, access_method, remarks, ip_address, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (batch, ranking, current_time, call_sign, qth, equipment, power, antenna,
                     access_method, remarks, self.client_address[0], now))
                # 点名数据同步写入中继主控呼号库，主控点名软件同步后即为最新信息
                conn.execute(
                    "INSERT INTO master_call_signs(call_sign, qth, equipment, power, antenna, "
                    "access_method, updated_at, device_id, deleted) "
                    "VALUES(?,?,?,?,?,?,?,?,0) "
                    "ON CONFLICT(call_sign) DO UPDATE SET qth=excluded.qth, "
                    "equipment=excluded.equipment, "
                    "power=CASE WHEN excluded.power <> '' THEN excluded.power "
                    "ELSE master_call_signs.power END, "
                    "antenna=CASE WHEN excluded.antenna <> '' THEN excluded.antenna "
                    "ELSE master_call_signs.antenna END, "
                    "access_method=CASE WHEN excluded.access_method <> '' THEN excluded.access_method "
                    "ELSE master_call_signs.access_method END, "
                    "updated_at=excluded.updated_at, device_id=excluded.device_id, deleted=0",
                    (call_sign, qth, equipment, power, antenna, access_method,
                     int(time.time() * 1000), "mobile-roll-call"))
                _bump_library_version(conn)
                conn.commit()
                record_id = cursor.lastrowid
                batch_id = batch
            finally:
                conn.close()

        if started:
            message = "主控签到成功，点名已开始！"
        elif is_host:
            message = "主控签到成功！"
        else:
            message = "点名成功！"
        log("手机点名: 呼号=%s 名次=%s 设备=%s 批次=%s（已写入中继呼号库）"
            % (call_sign, ranking, equipment, batch_id))
        self._json(200, {
            "status": "success",
            "message": message,
            "data": {
                "record_id": record_id,
                "batch_id": batch_id,
                "is_host": is_host,
                "started": started,
                "ranking": ranking,
                "current_time": current_time,
                "call_sign": call_sign,
                "qth": qth,
                "equipment": equipment,
                "power": power,
                "antenna": antenna,
                "access_method": access_method,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            },
        })

    def _api_mobile_records(self, query):
        batch_id = (query.get("batch_id") or [""])[0].strip()
        try:
            since = int((query.get("since") or ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        try:
            limit = min(int((query.get("limit") or ["500"])[0]), 1000)
        except (TypeError, ValueError):
            limit = 500

        conn = _connect()
        try:
            if not batch_id:
                row = conn.execute(
                    "SELECT batch_id FROM mobile_batches ORDER BY created_at DESC LIMIT 1").fetchone()
                batch_id = row["batch_id"] if row else ""
            if batch_id:
                rows = conn.execute(
                    "SELECT id, batch_id, ranking, roll_time, call_sign, qth, equipment, power, "
                    "antenna, access_method, remarks, ip_address, created_at "
                    "FROM mobile_records WHERE batch_id = ? AND id > ? "
                    "ORDER BY id LIMIT ?", (batch_id, since, limit)).fetchall()
            else:
                rows = []
        finally:
            conn.close()

        records = [dict(r) for r in rows]
        self._json(200, {
            "status": "success",
            "batch_id": batch_id,
            "records": records,
            "last_id": records[-1]["id"] if records else since,
        })

    def _api_mobile_batch_create(self, body):
        operator = str(body.get("operator") or "").strip().upper()
        name = str(body.get("name") or "").strip()
        batch_id = ""
        reused = False
        with DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT batch_id FROM mobile_batches WHERE status = 'open' AND operator = ? "
                    "ORDER BY created_at DESC LIMIT 1", (operator,)).fetchone()
                if row:
                    batch_id = row["batch_id"]
                    reused = True
                else:
                    batch_id = "%s_%d" % (operator or "batch", int(time.time()))
                    conn.execute(
                        "INSERT INTO mobile_batches(batch_id, name, operator, created_at, status) "
                        "VALUES (?, ?, ?, ?, 'open')",
                        (batch_id, name or operator, operator, int(time.time())))
                    conn.commit()
            finally:
                conn.close()
        self._json(200, {"status": "success", "batch_id": batch_id, "reused": reused})

    def _api_mobile_batch_close(self, body):
        batch_id = str(body.get("batch_id") or "").strip()
        operator = str(body.get("operator") or "").strip().upper()
        now = int(time.time())
        with DB_LOCK:
            conn = _connect()
            try:
                if batch_id:
                    conn.execute(
                        "UPDATE mobile_batches SET status = 'closed', closed_at = ? "
                        "WHERE status = 'open' AND batch_id = ?", (now, batch_id))
                elif operator:
                    conn.execute(
                        "UPDATE mobile_batches SET status = 'closed', closed_at = ? "
                        "WHERE status = 'open' AND operator = ?", (now, operator))
                else:
                    conn.execute(
                        "UPDATE mobile_batches SET status = 'closed', closed_at = ? "
                        "WHERE status = 'open'", (now,))
                conn.commit()
            finally:
                conn.close()
        self._json(200, {"status": "success"})

    def _api_mobile_check_data(self):
        """手机端结束点名前的数据量检查"""
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT batch_id, COUNT(*) AS n FROM mobile_records GROUP BY batch_id").fetchall()
            open_row = conn.execute(
                "SELECT batch_id FROM mobile_batches WHERE status = 'open' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        total = sum(r["n"] for r in rows)
        self._json(200, {
            "status": "success",
            "success": True,
            "has_data": total > 0,
            "total_records": total,
            "open_batch": open_row["batch_id"] if open_row else "",
            "batch_stats": [{"batch_id": r["batch_id"], "record_count": r["n"]}
                            for r in rows],
        })

    def _api_mobile_delete_tables(self):
        """手机端结束点名：关闭批次，并清理已结束批次 2 分钟前的记录"""
        closed = []
        purged = 0
        cutoff = int(time.time()) - 120
        with DB_LOCK:
            conn = _connect()
            try:
                now = int(time.time())
                for row in conn.execute(
                        "SELECT batch_id FROM mobile_batches WHERE status = 'open'").fetchall():
                    conn.execute(
                        "UPDATE mobile_batches SET status = 'closed', closed_at = ? "
                        "WHERE batch_id = ?", (now, row["batch_id"]))
                    closed.append(row["batch_id"])
                cur = conn.execute(
                    "DELETE FROM mobile_records WHERE created_at < ? AND batch_id IN "
                    "(SELECT batch_id FROM mobile_batches WHERE status = 'closed')",
                    (cutoff,))
                purged = max(0, cur.rowcount)
                conn.commit()
            finally:
                conn.close()
        self._json(200, {
            "status": "success",
            "success": True,
            "message": "已结束点名",
            "deleted_tables": closed,
            "purged_records": purged,
        })

    # ---------- 业务处理 ----------
    def _handle_library_sync(self, body):
        """呼号主库汇聚：LWW 合并客户端变更，并返回 since 之后的远端变更"""
        device_id = (body.get("device_id") or "").strip()
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        rows = body.get("rows") or []
        deleted = body.get("deleted") or []
        now_ms = int(time.time() * 1000)
        applied = 0
        with DB_LOCK:
            conn = _connect()
            try:
                for row in rows:
                    call_sign = str(row.get("call_sign") or "").strip()
                    if not call_sign:
                        continue
                    ts = int(row.get("updated_at") or 0) or now_ms
                    dev = str(row.get("device_id") or device_id)
                    if not _accept_lww(conn, call_sign, ts, dev):
                        continue
                    conn.execute(
                        "INSERT INTO master_call_signs(call_sign, qth, equipment, power, antenna, "
                        "access_method, updated_at, device_id, deleted) VALUES(?,?,?,?,?,?,?,?,0) "
                        "ON CONFLICT(call_sign) DO UPDATE SET qth=excluded.qth, "
                        "equipment=excluded.equipment, power=excluded.power, antenna=excluded.antenna, "
                        "access_method=excluded.access_method, updated_at=excluded.updated_at, "
                        "device_id=excluded.device_id, deleted=0",
                        (call_sign, row.get("qth"), row.get("equipment"), row.get("power"),
                         row.get("antenna"), row.get("access_method"), ts, dev),
                    )
                    applied += 1
                for item in deleted:
                    if isinstance(item, dict):
                        call_sign = str(item.get("call_sign") or "").strip()
                        ts = int(item.get("updated_at") or 0)
                    else:
                        call_sign = str(item or "").strip()
                        ts = 0
                    if not call_sign:
                        continue
                    ts = ts or now_ms
                    if not _accept_lww(conn, call_sign, ts, device_id):
                        continue
                    conn.execute(
                        "INSERT INTO master_call_signs(call_sign, updated_at, device_id, deleted) "
                        "VALUES(?,?,?,1) ON CONFLICT(call_sign) DO UPDATE SET "
                        "updated_at=excluded.updated_at, device_id=excluded.device_id, deleted=1",
                        (call_sign, ts, device_id),
                    )
                    applied += 1
                if applied:
                    _bump_library_version(conn)
                conn.commit()
                version = _library_version(conn)
                baseline = _library_baseline(conn)
                changes = conn.execute(
                    "SELECT * FROM master_call_signs WHERE updated_at > ? ORDER BY updated_at",
                    (since,)).fetchall()
            finally:
                conn.close()

        out_rows = []
        out_deleted = []
        for r in changes:
            if r["deleted"]:
                out_deleted.append({"call_sign": r["call_sign"],
                                    "updated_at": r["updated_at"],
                                    "device_id": r["device_id"] or ""})
            else:
                out_rows.append({
                    "call_sign": r["call_sign"],
                    "qth": r["qth"], "equipment": r["equipment"], "power": r["power"],
                    "antenna": r["antenna"], "access_method": r["access_method"],
                    "updated_at": r["updated_at"], "device_id": r["device_id"] or "",
                })
        self._json(200, {"ok": True, "applied": applied, "server_time": now_ms,
                         "version": version, "baseline": baseline,
                         "rows": out_rows, "deleted": out_deleted})

    def _handle_register(self, body):
        device_id = (body.get("device_id") or "").strip()
        if not device_id:
            self._json(400, {"error": "device_id required"})
            return
        public_ip = self.client_address[0]
        now = int(time.time())
        with DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO devices(device_id, device_name, public_ip, local_addr, "
                    "udp_port, http_port, last_seen) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET device_name=excluded.device_name, "
                    "public_ip=excluded.public_ip, local_addr=excluded.local_addr, "
                    "udp_port=excluded.udp_port, http_port=excluded.http_port, "
                    "last_seen=excluded.last_seen",
                    (
                        device_id,
                        (body.get("device_name") or "")[:64],
                        public_ip,
                        (body.get("local_addr") or "")[:128],
                        int(body.get("udp_port") or 0),
                        int(body.get("http_port") or 0),
                        now,
                    ),
                )
                conn.commit()
                rows = conn.execute("SELECT * FROM devices WHERE last_seen >= ?",
                                    (now - DEVICE_TTL,)).fetchall()
            finally:
                conn.close()
        peers = [_peer_dict(r) for r in rows if r["device_id"] != device_id]
        self._json(200, {"ok": True, "public_ip": public_ip, "peers": peers})
        # 设备每 30 秒自动注册一次，属于例行动作，默认不显示
        detail("设备注册: %s (%s) 公网 %s" % (device_id[:8], body.get("device_name") or "", public_ip))

    def _handle_announce(self, body):
        """点名软件在主控呼号确认后登记一次，中继日志显示接入提示"""
        callsign = (body.get("callsign") or "").strip().upper()
        if not callsign:
            self._json(400, {"error": "callsign required"})
            return
        device_name = (body.get("device_name") or "").strip() or "未知设备"
        public_ip = self.client_address[0]
        key = (callsign, public_ip)
        now = time.time()
        text = "点名软件接入: 主控 %s（%s）%s" % (callsign, device_name, public_ip)
        with APP_CONNECT_LOG_LOCK:
            if now - APP_CONNECT_LOG_TS.get(key, 0) >= APP_CONNECT_LOG_INTERVAL:
                APP_CONNECT_LOG_TS[key] = now
                show = True
            else:
                show = False
        if show:
            log(text)
        else:
            detail(text)
        self._json(200, {"ok": True})

    def _handle_punch(self, body):
        device_id = (body.get("device_id") or "").strip()
        target_id = (body.get("target") or "").strip()
        if not device_id or not target_id:
            self._json(400, {"error": "device_id/target required"})
            return
        conn = _connect()
        try:
            me = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            peer = conn.execute("SELECT * FROM devices WHERE device_id = ?", (target_id,)).fetchone()
        finally:
            conn.close()
        if peer is None:
            self._json(404, {"error": "target not registered"})
            return

        candidates = []
        if peer["local_addr"]:
            candidates.append(peer["local_addr"])
        if peer["public_ip"] and peer["udp_port"]:
            candidates.append("%s:%d" % (peer["public_ip"], peer["udp_port"]))

        packet = {
            "device_id": device_id,
            "device_name": body.get("device_name") or "",
            "udp_port": int(body.get("udp_port") or 0),
            "http_port": int(body.get("http_port") or 0),
            "local_addr": body.get("local_addr") or "",
            "public_addr": "",
        }
        if me is not None and me["public_ip"] and me["udp_port"]:
            packet["public_addr"] = "%s:%d" % (me["public_ip"], me["udp_port"])

        targets = []
        if peer["public_ip"] and peer["udp_port"]:
            targets.append((peer["public_ip"], peer["udp_port"], packet))
        if me is not None:
            reply = {
                "device_id": peer["device_id"],
                "device_name": peer["device_name"] or "",
                "udp_port": peer["udp_port"] or 0,
                "http_port": peer["http_port"] or 0,
                "local_addr": peer["local_addr"] or "",
                "public_addr": "",
            }
            if peer["public_ip"] and peer["udp_port"]:
                reply["public_addr"] = "%s:%d" % (peer["public_ip"], peer["udp_port"])
            if me["public_ip"] and me["udp_port"]:
                targets.append((me["public_ip"], me["udp_port"], reply))

        if targets:
            threading.Thread(target=punch_targets, args=(targets,), daemon=True).start()

        self._json(200, {
            "ok": True,
            "target": _peer_dict(peer),
            "candidates": candidates,
        })

    def _handle_mail_post(self, body):
        to_device = (body.get("to") or "").strip()
        from_device = (body.get("from") or "").strip()
        payload = body.get("payload")
        if not to_device or not from_device or payload is None:
            self._json(400, {"error": "to/from/payload required"})
            return
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_MAIL_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        now = int(time.time())
        with DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO mailbox(to_device, from_device, payload, created_at) "
                    "VALUES(?,?,?,?)", (to_device, from_device, raw, now))
                conn.commit()
            finally:
                conn.close()
        self._json(200, {"ok": True})


# ======================================================================
# MMDVM 更新程序（更新软件）P2P 中继：TCP 46001
# 协议与 更新软件\p2p_sync.py 完全一致（JSON Lines）：
#   hello / peer_list / sync_request / user_update
# 中继不产生业务数据，只做三件事：去重、持久化、向新上线节点补齐历史。
# ======================================================================

class MmdvmClient:
    """一条连到中继的 MMDVM 节点连接"""

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.send_lock = threading.Lock()
        self.alive = True
        self.callsign = ""
        self.listen_port = 0
        self.connected_at = time.time()
        self.rfile = sock.makefile("r", encoding="utf-8", newline="\n")

    def send(self, msg):
        """发送一条 JSON 消息，失败返回 False"""
        if not self.alive:
            return False
        try:
            data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            with self.send_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            self.alive = False
            return False

    def close(self):
        """标记断开并 shutdown。只从其他线程调用此方法，
        文件对象由接收线程（_client_loop 的 finally）关闭，避免跨线程 close 卡死"""
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close_files(self):
        """关闭文件对象与 socket，仅在接收线程退出后调用"""
        try:
            self.rfile.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class MmdvmRelay:
    """常开中继：接收并保存联系人修改，节点重连后按时间戳补齐"""

    def __init__(self, host="0.0.0.0", port=MMDVM_PORT,
                 max_history=MMDVM_MAX_HISTORY):
        self.host = host
        self.port = port
        self.max_history = max_history
        self.callsign = "MMDVM-RELAY"
        self.node_id = uuid.uuid4().hex
        self.clients = []
        self.clients_lock = threading.Lock()
        self.history = []
        self.history_lock = threading.Lock()
        self.seen_ids = set()
        self.seen_order = deque(maxlen=MMDVM_MAX_SEEN)
        self.seen_lock = threading.Lock()
        self.connect_log_ts = {}
        self.stop_event = threading.Event()
        self.server_sock = None
        self._load_history()

    # ------------------------------------------------------------ 历史
    def _load_history(self):
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT msg_id, radio_id, callsign, name, city, state, country, "
                "remarks, ts, author FROM mmdvm_updates ORDER BY ts DESC LIMIT ?",
                (self.max_history,)).fetchall()
        except sqlite3.Error as exc:
            log("MMDVM 中继历史加载失败: %s" % exc)
            rows = []
        finally:
            conn.close()
        for row in reversed(rows):
            msg = {
                "type": "user_update",
                "msg_id": row["msg_id"],
                "radio_id": row["radio_id"] or "",
                "callsign": row["callsign"] or "",
                "name": row["name"] or "",
                "city": row["city"] or "",
                "state": row["state"] or "",
                "country": row["country"] or "",
                "remarks": row["remarks"] or "",
                "ts": float(row["ts"] or 0),
                "author": row["author"] or "",
            }
            self.history.append(msg)
            self._mark_seen(msg["msg_id"])

    @staticmethod
    def _msg_ts(msg):
        try:
            return float(msg.get("ts", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _latest_ts(self):
        with self.history_lock:
            if not self.history:
                return 0.0
            return max(self._msg_ts(m) for m in self.history)

    def _mark_seen(self, msg_id):
        if len(self.seen_order) == self.seen_order.maxlen:
            self.seen_ids.discard(self.seen_order[0])
        self.seen_order.append(msg_id)
        self.seen_ids.add(msg_id)

    def _claim(self, msg_id):
        """原子去重：首次见到该 msg_id 返回 True，重复消息返回 False"""
        if not msg_id:
            return False
        with self.seen_lock:
            if msg_id in self.seen_ids:
                return False
            self._mark_seen(msg_id)
            return True

    def _store(self, msg):
        """写入 SQLite，并裁剪超量旧记录"""
        now = int(time.time())
        stale = []
        with self.history_lock:
            self.history.append(msg)
            overflow = len(self.history) - self.max_history
            if overflow > 0:
                stale = [m["msg_id"] for m in self.history[:overflow]]
                del self.history[:overflow]
        with DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO mmdvm_updates(msg_id, radio_id, callsign, "
                    "name, city, state, country, remarks, ts, author, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (msg.get("msg_id"), str(msg.get("radio_id", "")),
                     str(msg.get("callsign", "")), str(msg.get("name", "")),
                     str(msg.get("city", "")), str(msg.get("state", "")),
                     str(msg.get("country", "")), str(msg.get("remarks", "")),
                     self._msg_ts(msg), str(msg.get("author", "")), now))
                for mid in stale:
                    conn.execute("DELETE FROM mmdvm_updates WHERE msg_id = ?", (mid,))
                conn.commit()
            except sqlite3.Error as exc:
                log("MMDVM 中继写入失败: %s" % exc)
            finally:
                conn.close()

    # ------------------------------------------------------------ 连接
    def _add_client(self, client):
        with self.clients_lock:
            self.clients.append(client)

    def _remove_client(self, client):
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)
        client.close()

    def client_count(self):
        with self.clients_lock:
            return sum(1 for c in self.clients if c.alive)

    def _forward(self, msg, exclude=None):
        with self.clients_lock:
            clients = [c for c in self.clients if c is not exclude]
        for c in clients:
            if not c.send(msg):
                self._remove_client(c)

    # ------------------------------------------------------------ 消息
    def _handle(self, client, msg):
        mtype = msg.get("type")

        if mtype == "hello":
            client.callsign = str(msg.get("callsign", ""))
            try:
                client.listen_port = int(msg.get("listen_port", 0) or 0)
            except (TypeError, ValueError):
                client.listen_port = 0
            # 同一呼号+来源 IP 30 分钟内只记录一次，避免断线重连刷屏
            key = (client.callsign or "?", client.address[0])
            now = time.time()
            text = "MMDVM 节点接入: %s %s:%d" % (
                client.callsign or "?", client.address[0], client.address[1])
            if now - self.connect_log_ts.get(key, 0) >= MMDVM_CONNECT_LOG_INTERVAL:
                self.connect_log_ts[key] = now
                log(text)
            else:
                detail(text)
            return

        if mtype == "sync_request":
            try:
                since = float(msg.get("since", 0) or 0)
            except (TypeError, ValueError):
                since = 0.0
            with self.history_lock:
                newer = [m for m in self.history if self._msg_ts(m) > since]
            for m in newer:
                if not client.send(m):
                    break
            if newer:
                log("MMDVM 已向 %s 补齐 %d 条历史" % (client.address[0], len(newer)))
            return

        if mtype == "user_update":
            if not self._claim(msg.get("msg_id")):
                return
            self._store(msg)
            log("MMDVM 收到修改 %s(%s) 来自 %s" % (
                msg.get("callsign", "?"), msg.get("radio_id", "?"),
                msg.get("author", "?")))
            self._forward(msg, exclude=client)
            return

        # peer_list 等类型不处理：中继是星形中心，由中继统一补齐历史

    def _client_loop(self, client):
        try:
            while not self.stop_event.is_set() and client.alive:
                try:
                    line = client.rfile.readline()
                except socket.timeout:
                    # 静默超时：发一条心跳探测，写失败说明连接已断
                    if not client.send({"type": "ping", "ts": time.time()}):
                        break
                    continue
                except (OSError, ValueError):
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    self._handle(client, msg)
        finally:
            self._remove_client(client)
            client.close_files()
            duration = time.time() - client.connected_at
            text = "MMDVM 节点断开 %s:%d（连接 %d 秒）" % (
                client.address[0], client.address[1], int(duration))
            # 短连接/反复重连属于例行抖动，默认不显示；稳定连接断开才记录
            if duration >= MMDVM_LONG_CONNECT:
                log(text)
            else:
                detail(text)

    def _listen_loop(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(32)
            srv.settimeout(2)
            self.server_sock = srv
        except OSError as exc:
            log("MMDVM 中继端口 %d 监听失败: %s" % (self.port, exc))
            return
        log("MMDVM 中继已启动: tcp://%s:%d  已加载历史 %d 条" % (
            self.host, self.port, len(self.history)))
        while not self.stop_event.is_set():
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # 读超时用于探测死连接（见 _client_loop 的 ping）
            sock.settimeout(MMDVM_CLIENT_TIMEOUT)
            client = MmdvmClient(sock, addr)
            self._add_client(client)
            # 与 p2p_sync.P2PNode._add_peer 相同：先 hello，再反熵请求
            client.send({
                "type": "hello",
                "v": MMDVM_PROTO_VERSION,
                "node_id": self.node_id,
                "callsign": self.callsign,
                "listen_port": 0,
                "peers": [],
            })
            client.send({"type": "sync_request", "since": self._latest_ts()})
            threading.Thread(target=self._client_loop, args=(client,),
                             daemon=True, name="mmdvm-client").start()

    def start(self):
        threading.Thread(target=self._listen_loop, daemon=True,
                         name="mmdvm-listen").start()

    def stop(self):
        self.stop_event.set()
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
        with self.clients_lock:
            clients = list(self.clients)
        for c in clients:
            c.close()


MMDVM_RELAY = None


def _base_dir():
    """程序所在目录（兼容源码运行和 PyInstaller 打包后的 exe）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resource_path(name):
    """优先取程序同目录的文件，其次取 PyInstaller 内置资源"""
    candidate = os.path.join(_base_dir(), name)
    if os.path.exists(candidate):
        return candidate
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), name)
    if os.path.exists(bundled):
        return bundled
    return candidate


def _file_token():
    """未指定 --token / 环境变量时，从程序同目录的 relay.token 读取"""
    try:
        with open(os.path.join(_base_dir(), "relay.token"), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class RelayHTTPServer(ThreadingHTTPServer):
    """忽略客户端提前断开产生的噪声异常，避免刷无用 traceback"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        log("HTTP 请求处理异常 %s: %s" % (client_address, exc))


def main():
    global DB_PATH, RELAY_TOKEN, QUIET, VERBOSE, MMDVM_RELAY
    parser = argparse.ArgumentParser(description="点名软件 P2P 轻量中继")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=4570, help="监听端口")
    parser.add_argument("--db", default=os.path.join(_base_dir(), "relay_data.db"),
                        help="中继数据文件")
    parser.add_argument("--token", default=os.environ.get("P2P_RELAY_TOKEN", ""), help="可选访问令牌")
    parser.add_argument("--quiet", action="store_true", help="不打印任何日志")
    parser.add_argument("--verbose", action="store_true",
                        help="打印连接/断开、HTTP 访问等例行日志")
    parser.add_argument("--mmdvm-port", type=int, default=MMDVM_PORT,
                        help="更新软件联系人同步中继端口（0=关闭）")
    parser.add_argument("--mmdvm-history", type=int, default=MMDVM_MAX_HISTORY,
                        help="中继保存的联系人修改条数上限")
    args = parser.parse_args()

    DB_PATH = args.db
    RELAY_TOKEN = args.token or _file_token()
    QUIET = args.quiet
    VERBOSE = args.verbose

    init_db()
    cleanup()
    threading.Thread(target=cleanup_loop, daemon=True,
                     name="cleanup").start()

    if args.mmdvm_port > 0:
        MMDVM_RELAY = MmdvmRelay(host=args.host, port=args.mmdvm_port,
                                 max_history=args.mmdvm_history)
        MMDVM_RELAY.start()

    httpd = RelayHTTPServer((args.host, args.port), RelayHandler)
    log("中继服务已启动: v%s  http://%s:%d  数据文件: %s" % (
        RELAY_VERSION, args.host, args.port, DB_PATH))
    log("健康检查: http://127.0.0.1:%d/health" % args.port)
    log("访问令牌: %s" % ("已启用（桌面端需配置 relay_token）" if RELAY_TOKEN else "未启用"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("正在停止中继服务...")
    finally:
        if MMDVM_RELAY is not None:
            MMDVM_RELAY.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()

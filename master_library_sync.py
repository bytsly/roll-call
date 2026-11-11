#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""呼号库（主控呼号.db）与 P2P 中继服务器的汇聚同步

- 为呼号库 call_sign_library 表附加 _updated_at / _device_id 同步列与触发器
- 启动后立即同步一次，随后每 interval 秒与中继双向同步
- 上传本地修改，同时拉取其他设备上传的修改
- 冲突规则：LWW（时间戳大者胜，相同则 device_id 大者胜）
"""

import configparser
import json
import os
import sqlite3
import threading
import time
from urllib.request import Request, urlopen

TABLE = "call_sign_library"


def now_ms():
    return int(time.time() * 1000)


class MasterLibrarySync:
    """呼号主库 <-> 中继汇聚同步（后台线程）"""

    def __init__(self, db_path, config_path=None, logger=None, interval=300, mirror_db=None,
                 callsign=""):
        self.db_path = os.path.abspath(db_path)
        self.config_path = os.path.abspath(
            config_path or os.path.join(os.path.dirname(self.db_path), "app_config.ini"))
        self.mirror_db = os.path.abspath(mirror_db) if mirror_db else ""
        self.log = logger or (lambda msg: print(msg))
        self.interval = max(30, int(interval))
        self.callsign = (callsign or "").strip().upper()

        self.relay_url = ""
        self.device_id = ""
        self.device_name = ""
        self.relay_token = ""
        self.enabled = False
        self._stop_event = threading.Event()
        self._thread = None
        self._load_config()

    # ---------------- 配置 ----------------
    def _load_config(self):
        config = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            config.read(self.config_path, encoding="utf-8")
        section = config["P2P"] if "P2P" in config else {}
        self.relay_url = (section.get("relay_url") or "").strip().rstrip("/")
        self.device_id = (section.get("device_id") or "").strip()
        self.device_name = (section.get("device_name") or "").strip()
        self.relay_token = (section.get("relay_token") or "").strip()
        self.enabled = bool(self.relay_url and self.device_id)

    # ---------------- 数据库 ----------------
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self):
        """附加同步列、状态表、墓碑表与触发器；主库缺少目标表时返回 False"""
        if not os.path.exists(self.db_path):
            self.log("主库文件不存在，跳过汇聚同步: %s" % self.db_path)
            return False
        conn = self._connect()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % TABLE)]
            if not cols:
                self.log("主库缺少 %s 表，跳过汇聚同步" % TABLE)
                return False
            if "_updated_at" not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN _updated_at INTEGER DEFAULT 0" % TABLE)
            if "_device_id" not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN _device_id TEXT DEFAULT ''" % TABLE)
            if "access_method" not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN access_method TEXT" % TABLE)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lib_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS lib_tombstones (
                    call_sign  TEXT PRIMARY KEY,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    device_id  TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_lib_ts ON call_sign_library(_updated_at);
                """
            )
            conn.execute("INSERT OR IGNORE INTO lib_meta(key, value) VALUES('suppress', '0')")
            conn.execute("INSERT OR IGNORE INTO lib_meta(key, value) VALUES('last_push', '0')")
            conn.execute("INSERT OR IGNORE INTO lib_meta(key, value) VALUES('last_pull', '0')")
            conn.execute("INSERT OR IGNORE INTO lib_meta(key, value) VALUES('relay_version', '0')")
            conn.execute("INSERT OR IGNORE INTO lib_meta(key, value) VALUES('relay_baseline', '0')")
            # 历史行使用极小时间戳，首次同步会上传全库，但不会覆盖其他设备的新修改；
            # 期间屏蔽触发器，避免把全库误标记为“刚修改”
            conn.execute("INSERT OR REPLACE INTO lib_meta(key, value) VALUES('suppress', '1')")
            conn.execute("UPDATE %s SET _updated_at = 1 "
                         "WHERE _updated_at IS NULL OR _updated_at = 0" % TABLE)
            conn.execute("INSERT OR REPLACE INTO lib_meta(key, value) VALUES('suppress', '0')")
            conn.commit()
            self._create_triggers(conn)
            conn.commit()
        finally:
            conn.close()
        return True

    def _create_triggers(self, conn):
        device = self.device_id.replace("'", "''")
        stamp = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
        guard = "COALESCE((SELECT value FROM lib_meta WHERE key='suppress'),'0') <> '1'"
        for name in ("call_sign_library_lib_ins", "call_sign_library_lib_upd",
                     "call_sign_library_lib_del"):
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        conn.executescript(
            """
            CREATE TRIGGER call_sign_library_lib_ins AFTER INSERT ON call_sign_library
            FOR EACH ROW WHEN {guard}
            BEGIN
                UPDATE call_sign_library SET _updated_at = {stamp}, _device_id = '{dev}'
                WHERE rowid = NEW.rowid;
            END;

            CREATE TRIGGER call_sign_library_lib_upd AFTER UPDATE ON call_sign_library
            FOR EACH ROW WHEN {guard}
            BEGIN
                UPDATE call_sign_library SET _updated_at = {stamp}, _device_id = '{dev}'
                WHERE rowid = NEW.rowid;
            END;

            CREATE TRIGGER call_sign_library_lib_del AFTER DELETE ON call_sign_library
            FOR EACH ROW WHEN {guard}
            BEGIN
                INSERT OR REPLACE INTO lib_tombstones(call_sign, updated_at, device_id)
                VALUES(COALESCE(OLD.call_sign,''), {stamp}, '{dev}');
            END;
            """.format(guard=guard, stamp=stamp, dev=device)
        )

    @staticmethod
    def _get_meta(conn, key, default=0):
        row = conn.execute("SELECT value FROM lib_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _set_meta(conn, key, value):
        conn.execute("INSERT OR REPLACE INTO lib_meta(key, value) VALUES(?, ?)",
                     (key, str(value)))

    def _local_relay_version(self):
        """本地已拉取到的中继主控库版本"""
        conn = self._connect()
        try:
            return self._get_meta(conn, "relay_version", 0)
        finally:
            conn.close()

    def _set_relay_version(self, version):
        conn = self._connect()
        try:
            self._set_meta(conn, "relay_version", int(version or 0))
            conn.commit()
        finally:
            conn.close()

    def _local_baseline(self):
        """本地已拉取到的整库基线标记"""
        conn = self._connect()
        try:
            return self._get_meta(conn, "relay_baseline", 0)
        finally:
            conn.close()

    # ---------------- 同步 ----------------
    def _collect_changes(self, since):
        # 1 是历史行/中继基线行的标记时间戳，不属于待上传修改
        since = max(int(since or 0), 1)
        conn = self._connect()
        try:
            max_ts = since
            rows = []
            for r in conn.execute(
                    "SELECT call_sign, qth, equipment, power, antenna, access_method, "
                    "_updated_at, _device_id FROM %s WHERE _updated_at > ?" % TABLE, (since,)):
                ts = int(r["_updated_at"] or 0)
                max_ts = max(max_ts, ts)
                rows.append({
                    "call_sign": r["call_sign"],
                    "qth": r["qth"],
                    "equipment": r["equipment"],
                    "power": r["power"],
                    "antenna": r["antenna"],
                    "access_method": r["access_method"],
                    "updated_at": ts,
                    "device_id": r["_device_id"] or self.device_id,
                })
            deleted = []
            for r in conn.execute(
                    "SELECT call_sign, updated_at FROM lib_tombstones WHERE updated_at > ?",
                    (since,)):
                ts = int(r["updated_at"] or 0)
                max_ts = max(max_ts, ts)
                deleted.append({"call_sign": r["call_sign"], "updated_at": ts,
                                "device_id": self.device_id})
            return rows, deleted, max_ts
        finally:
            conn.close()

    def _apply_remote(self, data):
        remote_rows = data.get("rows") or []
        remote_del = data.get("deleted") or []
        remote_max = 0
        applied = 0
        conn = self._connect()
        try:
            self._set_meta(conn, "suppress", "1")
            for row in remote_rows:
                call_sign = str(row.get("call_sign") or "").strip()
                if not call_sign:
                    continue
                ts = int(row.get("updated_at") or 0)
                dev = str(row.get("device_id") or "")
                remote_max = max(remote_max, ts)
                cur = conn.execute(
                    "SELECT _updated_at, _device_id FROM %s WHERE call_sign = ?" % TABLE,
                    (call_sign,)).fetchone()
                if cur is not None and (ts, dev) <= (int(cur["_updated_at"] or 0),
                                                     str(cur["_device_id"] or "")):
                    continue
                if cur is None:
                    conn.execute(
                        "INSERT INTO %s(call_sign, qth, equipment, power, antenna, "
                        "access_method, _updated_at, _device_id) VALUES(?,?,?,?,?,?,?,?)" % TABLE,
                        (call_sign, row.get("qth"), row.get("equipment"), row.get("power"),
                         row.get("antenna"), row.get("access_method"), ts, dev))
                else:
                    conn.execute(
                        "UPDATE %s SET qth=?, equipment=?, power=?, antenna=?, access_method=?, "
                        "_updated_at=?, _device_id=? WHERE call_sign=?" % TABLE,
                        (row.get("qth"), row.get("equipment"), row.get("power"),
                         row.get("antenna"), row.get("access_method"), ts, dev, call_sign))
                applied += 1
            for item in remote_del:
                call_sign = str(item.get("call_sign") or "").strip()
                if not call_sign:
                    continue
                ts = int(item.get("updated_at") or 0)
                dev = str(item.get("device_id") or "")
                remote_max = max(remote_max, ts)
                cur = conn.execute(
                    "SELECT _updated_at, _device_id FROM %s WHERE call_sign = ?" % TABLE,
                    (call_sign,)).fetchone()
                if cur is not None and (ts, dev) <= (int(cur["_updated_at"] or 0),
                                                     str(cur["_device_id"] or "")):
                    continue
                if cur is not None:
                    conn.execute("DELETE FROM %s WHERE call_sign = ?" % TABLE, (call_sign,))
                    applied += 1
                conn.execute(
                    "INSERT OR REPLACE INTO lib_tombstones(call_sign, updated_at, device_id) "
                    "VALUES(?,?,?)", (call_sign, ts, dev))
            self._set_meta(conn, "suppress", "0")
            conn.commit()
        finally:
            conn.close()
        self._mirror_remote(remote_rows, remote_del)
        return applied, remote_max

    def _mirror_remote(self, rows, deleted):
        """把拉取到的呼号信息同步写入界面使用的运行库（call_sign_history.db）"""
        if not self.mirror_db or self.mirror_db == self.db_path:
            return
        if not os.path.exists(self.mirror_db):
            return
        try:
            conn = sqlite3.connect(self.mirror_db, timeout=15)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % TABLE)]
                if not cols:
                    return
                for row in rows:
                    call_sign = str(row.get("call_sign") or "").strip()
                    if not call_sign:
                        continue
                    values = (row.get("qth"), row.get("equipment"), row.get("power"),
                              row.get("antenna"), row.get("access_method"))
                    cur = conn.execute(
                        "SELECT id FROM %s WHERE call_sign = ?" % TABLE, (call_sign,)).fetchone()
                    if cur is not None:
                        conn.execute(
                            "UPDATE %s SET qth=?, equipment=?, power=?, antenna=?, "
                            "access_method=? WHERE call_sign=?" % TABLE,
                            values + (call_sign,))
                    else:
                        conn.execute(
                            "INSERT INTO %s(call_sign, qth, equipment, power, antenna, "
                            "access_method) VALUES(?,?,?,?,?,?)" % TABLE,
                            (call_sign,) + values)
                for item in deleted:
                    call_sign = str(item.get("call_sign") or "").strip()
                    if call_sign:
                        conn.execute("DELETE FROM %s WHERE call_sign = ?" % TABLE, (call_sign,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self.log("运行库呼号信息回写失败: %s" % exc)

    def _mirror_full_library(self):
        """把本地主控库全量回写运行库和主数据库（只增改，不删除）"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT call_sign, qth, equipment, power, antenna, access_method "
                "FROM %s" % TABLE).fetchall()
        finally:
            conn.close()
        if not rows:
            return
        if self.mirror_db and self.mirror_db != self.db_path:
            self._upsert_rows(self.mirror_db, rows, "运行库")
        # 同步合并进主数据库，避免登录时被旧主库清理
        master_db = os.path.join(os.path.dirname(self.db_path), "master_call_sign_library.db")
        if os.path.exists(master_db):
            self._upsert_rows(master_db, rows, "主数据库")

    def _upsert_rows(self, db_path, rows, label):
        """按呼号把行合并写入目标库，返回新增条数"""
        if not os.path.exists(db_path):
            return 0
        conn = sqlite3.connect(db_path, timeout=15)
        inserted = 0
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % TABLE)]
            if not cols:
                return 0
            fields = [c for c in ("qth", "equipment", "power", "antenna", "access_method")
                      if c in cols]
            for row in rows:
                call_sign = str(row["call_sign"] or "").strip()
                if not call_sign:
                    continue
                cur = conn.execute(
                    "SELECT id FROM %s WHERE call_sign = ?" % TABLE, (call_sign,)).fetchone()
                if cur is not None:
                    if fields:
                        conn.execute(
                            "UPDATE %s SET %s WHERE call_sign = ?"
                            % (TABLE, ", ".join("%s = ?" % f for f in fields)),
                            [row[f] for f in fields] + [call_sign])
                else:
                    all_cols = ["call_sign"] + fields
                    conn.execute(
                        "INSERT INTO %s(%s) VALUES(%s)"
                        % (TABLE, ", ".join(all_cols), ", ".join(["?"] * len(all_cols))),
                        [call_sign] + [row[f] for f in fields])
                    inserted += 1
            conn.commit()
        finally:
            conn.close()
        if inserted:
            self.log("已同步%s: 补回 %d 条" % (label, inserted))
        return inserted

    def _apply_snapshot(self, data):
        """整库快照写入本地：先上传本地修改后，按较新者胜合并中继主控库"""
        rows = data.get("rows") or []
        applied = 0
        conn = self._connect()
        try:
            self._set_meta(conn, "suppress", "1")
            for row in rows:
                call_sign = str(row.get("call_sign") or "").strip()
                if not call_sign:
                    continue
                ts = int(row.get("updated_at") or 0)
                dev = str(row.get("device_id") or "")
                deleted = int(row.get("deleted") or 0)
                cur = conn.execute(
                    "SELECT _updated_at, _device_id FROM %s WHERE call_sign = ?" % TABLE,
                    (call_sign,)).fetchone()
                cur_key = ((int(cur["_updated_at"] or 0), str(cur["_device_id"] or ""))
                           if cur is not None else None)
                if deleted:
                    if cur is not None and (ts, dev) > cur_key:
                        conn.execute("DELETE FROM %s WHERE call_sign = ?" % TABLE, (call_sign,))
                        applied += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO lib_tombstones(call_sign, updated_at, device_id) "
                        "VALUES(?,?,?)", (call_sign, ts, dev))
                    continue
                if cur_key is not None and (ts, dev) <= cur_key:
                    continue
                if cur is None:
                    conn.execute(
                        "INSERT INTO %s(call_sign, qth, equipment, power, antenna, "
                        "access_method, _updated_at, _device_id) VALUES(?,?,?,?,?,?,?,?)" % TABLE,
                        (call_sign, row.get("qth"), row.get("equipment"), row.get("power"),
                         row.get("antenna"), row.get("access_method"), ts, dev))
                else:
                    conn.execute(
                        "UPDATE %s SET qth=?, equipment=?, power=?, antenna=?, access_method=?, "
                        "_updated_at=?, _device_id=? WHERE call_sign=?" % TABLE,
                        (row.get("qth"), row.get("equipment"), row.get("power"),
                         row.get("antenna"), row.get("access_method"), ts, dev, call_sign))
                applied += 1
            self._set_meta(conn, "suppress", "0")
            self._set_meta(conn, "last_pull", int(data.get("server_time") or 0) or
                           self._get_meta(conn, "last_pull"))
            self._set_meta(conn, "relay_version", int(data.get("version") or 0))
            self._set_meta(conn, "relay_baseline", int(data.get("baseline") or 0))
            conn.commit()
        finally:
            conn.close()

        active = [r for r in rows if not int(r.get("deleted") or 0)]
        deleted = [r for r in rows if int(r.get("deleted") or 0)]
        self._mirror_remote(active, deleted)
        self.log("已从中继拉取主控库: 共 %d 条（删除 %d 条），本地应用 %d 条"
                 % (len(active), len(deleted), applied))
        return applied

    def startup_sync(self, timeout=30):
        """登录/启动时比对中继版本；中继有更新则先上传本地修改，再整库拉取"""
        if not self.enabled:
            return False
        try:
            return self._startup_sync_with_relay(timeout)
        finally:
            # 无论同步是否成功，把本地主控库全量回写运行库，
            # 修复登录时被旧主库同步清理掉、但实际仍有效的呼号
            try:
                self._mirror_full_library()
            except Exception as exc:
                self.log("运行库呼号修复回写失败: %s" % exc)

    def _startup_sync_with_relay(self, timeout=30):
        try:
            info = self._get_json(self.relay_url + "/api/library/version", timeout=timeout)
            remote_baseline = int(info.get("baseline") or 0)
            remote_last = int(info.get("last_update") or 0)
        except Exception as exc:
            self.log("中继版本查询失败，改用增量同步: %s" % exc)
            return self.sync_now(timeout=timeout)

        # 仅两种情况整库拉取：本地从未同步过，或中继整库基线变更（如灌入全国呼号库）；
        # 其余情况只做增量，避免大库每次变更都全量传输
        need_full = self._local_relay_version() <= 0 or \
            remote_baseline > self._local_baseline()
        if not need_full:
            return self.sync_now(timeout=timeout)

        # 整库拉取前先把本地离线修改上传（since 用中继最新时间，避免重复拉旧数据）
        if not self.sync_now(timeout=timeout, since=remote_last):
            return False
        try:
            snapshot = self._get_json(self.relay_url + "/api/library/snapshot",
                                      timeout=max(timeout, 120))
        except Exception as exc:
            self.log("中继主控库整库拉取失败: %s" % exc)
            return False
        self._apply_snapshot(snapshot)
        return True

    def sync_now(self, timeout=20, since=None):
        """执行一次双向同步；since 指定拉取起点（None 时使用本地 last_pull）"""
        if not self.enabled:
            return False
        try:
            conn = self._connect()
            try:
                last_push = self._get_meta(conn, "last_push")
                last_pull = self._get_meta(conn, "last_pull")
            finally:
                conn.close()
            pull_from = last_pull if since is None else int(since)

            rows, deleted, push_upto = self._collect_changes(last_push)
            payload = {"device_id": self.device_id, "since": pull_from,
                       "rows": rows, "deleted": deleted}
            data = self._post_json(self.relay_url + "/api/library/sync", payload, timeout=timeout)
            applied, remote_max = self._apply_remote(data)

            conn = self._connect()
            try:
                self._set_meta(conn, "last_push", max(last_push, push_upto, remote_max))
                self._set_meta(conn, "last_pull", int(data.get("server_time") or pull_from))
                if data.get("version") is not None:
                    self._set_meta(conn, "relay_version", int(data.get("version") or 0))
                conn.commit()
            finally:
                conn.close()

            if rows or deleted or applied:
                self.log("中继呼号库同步: 上传 %d 条 / 删除 %d 条, 接收 %d 条"
                         % (len(rows), len(deleted), applied))
            return True
        except Exception as exc:
            self.log("中继呼号库同步失败: %s" % exc)
            return False

    def _relay_headers(self):
        headers = {"Accept": "application/json"}
        if getattr(self, "relay_token", ""):
            headers["X-Relay-Token"] = self.relay_token
        return headers

    def _get_json(self, url, timeout=20):
        request = Request(url, headers=self._relay_headers(), method="GET")
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, url, obj, timeout=20):
        headers = self._relay_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            url,
            data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST")
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # ---------------- 生命周期 ----------------
    def start(self):
        if not self.enabled:
            self.log("未配置中继地址或设备号，跳过呼号主库汇聚")
            return
        if not self.ensure_schema():
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="master-library-sync",
                                        daemon=True)
        self._thread.start()
        self.log("呼号主库汇聚已启动，中继: %s" % self.relay_url)

    def _loop(self):
        first = True
        # 主控呼号已输入：先向中继登记一次，让中继日志显示“点名软件接入”
        if self.callsign:
            self._announce_relay()
        while not self._stop_event.is_set():
            if first:
                # 登录/启动首轮：比对中继版本，有更新则整库拉取
                self.startup_sync()
                first = False
            else:
                self.sync_now()
            self._stop_event.wait(self.interval)

    def _announce_relay(self):
        """向中继登记当前主控呼号（失败只记录日志，不影响同步）"""
        try:
            self._post_json(self.relay_url + "/api/announce", {
                "device_id": self.device_id,
                "device_name": self.device_name,
                "callsign": self.callsign,
            }, timeout=8)
            self.log("已向中继登记主控呼号: %s" % self.callsign)
        except Exception as exc:
            self.log("中继登记主控呼号失败: %s" % exc)

    def stop(self):
        self._stop_event.set()

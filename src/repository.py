from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ENTITY, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('seepage','displacement')),
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, kind, observed_at)
                );
                CREATE TABLE IF NOT EXISTS sync_batches (
                    batch_id TEXT PRIMARY KEY,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'current'
                        CHECK(status IN ('current','superseded')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, revision)
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def _append_audit_locked(self, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        event["id"] = int(cur.lastrowid)
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._append_audit_locked(action, entity_type, entity_id, actor, detail)

    def _latest_readings_locked(self, item_id: int) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE item_id=? ORDER BY observed_at, id", (item_id,)
        ).fetchall()
        latest: Dict[str, Any] = {}
        for row in rows:
            latest[row["kind"]] = dict(row)
        return latest

    def apply_reading_batch(self, item_id: int, batch_id: str, expected_version: int,
                            readings: List[Dict[str, Any]], actor: str,
                            conclusion_builder) -> tuple:
        """单个事务内应用一个补传批次：幂等检查、版本校验、读数入库、
        版本递增、复核结论失效重算、审计和批次回执一起提交或一起回滚。"""
        now = utc_now()
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT item_id, result FROM sync_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is not None:
                if int(batch["item_id"]) != item_id:
                    raise ConflictError("批次号已用于其他缺陷")
                return json.loads(batch["result"]), True
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            item = dict(row)
            if int(item["version"]) != expected_version:
                raise ConflictError(
                    f"版本冲突，当前版本为{item['version']}",
                    details={"current_version": item["version"]})
            inserted = []
            try:
                for reading in readings:
                    cur = self.conn.execute(
                        """INSERT INTO readings(item_id, kind, value, unit, observed_at,
                           batch_id, external_ref, created_by, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (item_id, reading["kind"], reading["value"], reading["unit"],
                         reading["observed_at"], batch_id, reading["external_ref"],
                         actor, now),
                    )
                    inserted.append(dict(reading, id=int(cur.lastrowid), item_id=item_id,
                                         batch_id=batch_id, created_by=actor, created_at=now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("读数与已有记录冲突（同一缺陷同类型同观测时间）") from exc
            new_version = int(item["version"]) + 1
            self.conn.execute(
                "UPDATE items SET version=?, updated_at=? WHERE id=?",
                (new_version, now, item_id),
            )
            latest = self._latest_readings_locked(item_id)
            previous = self.conn.execute(
                "SELECT id, revision FROM conclusions WHERE item_id=? AND status='current'",
                (item_id,),
            ).fetchone()
            superseded_revision = None
            if previous is not None:
                superseded_revision = int(previous["revision"])
                self.conn.execute(
                    "UPDATE conclusions SET status='superseded' WHERE id=?",
                    (previous["id"],),
                )
            revision = int(self.conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 AS rev FROM conclusions WHERE item_id=?",
                (item_id,),
            ).fetchone()["rev"])
            conclusion = conclusion_builder(item, latest)
            cur = self.conn.execute(
                """INSERT INTO conclusions(item_id, revision, detail, status, created_by,
                   created_at) VALUES(?,?,?,?,?,?)""",
                (item_id, revision,
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                 "current", actor, now),
            )
            observed_times = [r["observed_at"] for r in inserted]
            self._append_audit_locked("reading_sync", ENTITY, item_id, actor, {
                "batch_id": batch_id, "inserted": len(inserted),
                "kinds": sorted({r["kind"] for r in inserted}),
                "observed_from": min(observed_times), "observed_to": max(observed_times),
                "version": new_version,
            })
            self._append_audit_locked("conclusion", ENTITY, item_id, actor, {
                "revision": revision, "superseded_revision": superseded_revision,
                "escalation_required": conclusion["escalation_required"],
                "priority": conclusion["priority"],
            })
            result = {
                "item_id": item_id, "batch_id": batch_id, "version": new_version,
                "inserted": inserted, "effective": latest,
                "conclusion": {
                    "id": int(cur.lastrowid), "revision": revision, "status": "current",
                    "detail": conclusion, "superseded_revision": superseded_revision,
                },
            }
            self.conn.execute(
                """INSERT INTO sync_batches(batch_id, item_id, actor, expected_version,
                   result, created_at) VALUES(?,?,?,?,?,?)""",
                (batch_id, item_id, actor, expected_version,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), now),
            )
            return result, False

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY observed_at, id",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_conclusions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? ORDER BY revision DESC",
                (item_id,),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = json.loads(entry["detail"])
            result.append(entry)
        return result

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

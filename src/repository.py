from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ENTITY, ID_PREFIX, STATES, recalculate_review


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
                CREATE TABLE IF NOT EXISTS backfill_batches (
                    batch_id TEXT PRIMARY KEY,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_id TEXT NOT NULL REFERENCES backfill_batches(batch_id),
                    kind TEXT NOT NULL CHECK(kind IN ('seepage','displacement')),
                    value REAL NOT NULL,
                    unit TEXT,
                    observed_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_readings_item ON readings(item_id, id);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    conclusion TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{{}}',
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(item_id, id);
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

    def _append_audit_tx(self, conn, action: str, entity_type: str, entity_id: int,
                         actor: str, detail: dict) -> Dict[str, Any]:
        """在已有的数据库事务内追加一条审计事件，调用方负责提交/回滚。"""
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = conn.execute(
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
            event = self._append_audit_tx(
                self.conn, action, entity_type, entity_id, actor, detail)
        return event

    @staticmethod
    def _review(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reviews(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reviews WHERE item_id=? ORDER BY id DESC", (item_id,)
            ).fetchall()
        return [self._review(row) for row in rows]

    def get_review(self, review_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("复核结论不存在")
        return self._review(row)

    def _replay_backfill(self, batch_id: str) -> Dict[str, Any]:
        """幂等重放：批次已存在时返回既有结果，不重复写入。"""
        batch = self.conn.execute(
            "SELECT * FROM backfill_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        item_id = int(batch["item_id"])
        readings = [dict(row) for row in self.conn.execute(
            "SELECT * FROM readings WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()]
        review_row = self.conn.execute(
            "SELECT * FROM reviews WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchone()
        return {
            "replayed": True,
            "batch_id": batch_id,
            "readings": readings,
            "review": self._review(review_row) if review_row else None,
            "item": self.get_item(item_id),
        }

    def backfill_readings(self, item_id: int, expected_version: int, batch_id: str,
                          readings: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """按批次补传现场读数。

        整批写入在一个事务内完成：批次登记、读数入库、旧复核失效、新复核重算、
        审计追加，要么全部成功要么全部回滚。batch_id 已存在时幂等重放；
        版本乐观锁失败则抛出携带 current_version 的 ConflictError。
        """
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            existing = self.conn.execute(
                "SELECT 1 FROM backfill_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if existing:
                return self._replay_backfill(batch_id)
            cur = self.conn.execute(
                "UPDATE items SET version=version+1, updated_at=? WHERE id=? AND version=?",
                (now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                current = self.conn.execute(
                    "SELECT version FROM items WHERE id=?", (item_id,)
                ).fetchone()
                raise ConflictError(
                    "版本冲突，请刷新后重试",
                    current_version=int(current["version"]) if current else None,
                )
            self.conn.execute(
                "INSERT INTO backfill_batches(batch_id, item_id, actor, created_at) "
                "VALUES(?,?,?,?)",
                (batch_id, item_id, actor, now),
            )
            for reading in readings:
                self.conn.execute(
                    """INSERT INTO readings(item_id, batch_id, kind, value, unit,
                       observed_at, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, batch_id, reading["kind"], reading["value"],
                     reading.get("unit"), reading["observed_at"], actor, now),
                )
            stale_rows = self.conn.execute(
                "SELECT id FROM reviews WHERE item_id=? AND stale=0", (item_id,)
            ).fetchall()
            stale_ids = [int(r["id"]) for r in stale_rows]
            if stale_ids:
                self.conn.executemany(
                    "UPDATE reviews SET stale=1 WHERE id=?",
                    [(review_id,) for review_id in stale_ids],
                )
            all_readings = [dict(r) for r in self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()]
            conclusion, detail = recalculate_review(dict(row), all_readings)
            cur = self.conn.execute(
                """INSERT INTO reviews(item_id, conclusion, detail, stale, created_by, created_at)
                   VALUES(?,?,?,0,?,?)""",
                (item_id, conclusion, json.dumps(detail, ensure_ascii=False, sort_keys=True),
                 actor, now),
            )
            review_id = int(cur.lastrowid)
            self._append_audit_tx(self.conn, "backfill", ENTITY, item_id, actor, {
                "batch_id": batch_id,
                "readings": len(readings),
                "kinds": sorted({r["kind"] for r in readings}),
                "stale_review_ids": stale_ids,
            })
            self._append_audit_tx(self.conn, "review", ENTITY, item_id, actor, {
                "review_id": review_id,
                "conclusion": conclusion,
                "stale_review_ids": stale_ids,
                "batch_id": batch_id,
            })
        return {
            "replayed": False,
            "batch_id": batch_id,
            "readings": [dict(r) for r in self.conn.execute(
                "SELECT * FROM readings WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()],
            "review": self.get_review(review_id),
            "item": self.get_item(item_id),
        }

    def recalculate_review(self, item_id: int, actor: str) -> Dict[str, Any]:
        """根据当前全部读数重算复核结论，旧结论标记失效，历史仍可查。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            stale_rows = self.conn.execute(
                "SELECT id FROM reviews WHERE item_id=? AND stale=0", (item_id,)
            ).fetchall()
            stale_ids = [int(r["id"]) for r in stale_rows]
            if stale_ids:
                self.conn.executemany(
                    "UPDATE reviews SET stale=1 WHERE id=?",
                    [(review_id,) for review_id in stale_ids],
                )
            all_readings = [dict(r) for r in self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()]
            conclusion, detail = recalculate_review(dict(row), all_readings)
            cur = self.conn.execute(
                """INSERT INTO reviews(item_id, conclusion, detail, stale, created_by, created_at)
                   VALUES(?,?,?,0,?,?)""",
                (item_id, conclusion, json.dumps(detail, ensure_ascii=False, sort_keys=True),
                 actor, now),
            )
            review_id = int(cur.lastrowid)
            self._append_audit_tx(self.conn, "review", ENTITY, item_id, actor, {
                "review_id": review_id,
                "conclusion": conclusion,
                "stale_review_ids": stale_ids,
                "recalculated": True,
            })
        return self.get_review(review_id)

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

import tempfile, threading, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "3号坝段渗流缺陷", "description": "汛期重点观测",
             "severity": "major", "quantity": 5, "threshold": 10,
             "external_ref": "SYNC-1"}, "creator", "inspector")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def sync(self, batch_id, version, readings, actor="inspector-li", role="inspector"):
        return self.service.sync_readings(
            self.item["id"],
            {"batch_id": batch_id, "expected_version": version, "readings": readings},
            actor, role)

    def reading(self, kind, value, observed_at):
        return {"kind": kind, "value": value, "observed_at": observed_at}

    def test_batch_sync_and_idempotent_replay(self):
        result, replayed = self.sync("B-1", 1, [
            self.reading("seepage", 12.0, "2026-09-30T08:00:00Z"),
            self.reading("displacement", 3.0, "2026-09-30T08:05:00+00:00"),
        ])
        self.assertFalse(replayed)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["conclusion"]["revision"], 1)
        self.assertTrue(result["conclusion"]["detail"]["escalation_required"])
        again, replayed = self.sync("B-1", 1, [
            self.reading("seepage", 12.0, "2026-09-30T08:00:00Z"),
            self.reading("displacement", 3.0, "2026-09-30T08:05:00+00:00"),
        ])
        self.assertTrue(replayed)
        self.assertEqual(again["version"], 2)
        self.assertEqual([r["id"] for r in again["inserted"]],
                         [r["id"] for r in result["inserted"]])
        readings = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual(len(readings), 2)
        self.assertEqual(self.service.get_item(self.item["id"], "viewer")["version"], 2)

    def test_earlier_observed_reading_merged_not_dropped(self):
        # 办公室先处置了10:00的读数
        office, _ = self.sync("B-OFFICE", 1, [
            self.reading("seepage", 12.0, "2026-09-30T10:00:00Z"),
        ], actor="engineer-wang", role="dam_engineer")
        self.assertEqual(office["effective"]["seepage"]["value"], 12.0)
        # 巡检员补传现场08:00的读数，观测时间更早，必须保留且按观测时间排序
        field, _ = self.sync("B-FIELD", 2, [
            self.reading("seepage", 15.0, "2026-09-30T08:00:00Z"),
        ])
        timeline = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual([r["observed_at"] for r in timeline],
                         ["2026-09-30T08:00:00+00:00", "2026-09-30T10:00:00+00:00"])
        # 生效读数由观测时间决定而非到达顺序：仍是10:00那条
        self.assertEqual(field["effective"]["seepage"]["value"], 12.0)
        self.assertEqual(field["effective"]["seepage"]["batch_id"], "B-OFFICE")

    def test_conclusion_superseded_and_history_queryable(self):
        self.sync("B-1", 1, [self.reading("seepage", 12.0, "2026-09-30T08:00:00Z")])
        result, _ = self.sync("B-2", 2, [
            self.reading("seepage", 3.0, "2026-09-30T12:00:00Z")])
        self.assertFalse(result["conclusion"]["detail"]["escalation_required"])
        self.assertEqual(result["conclusion"]["superseded_revision"], 1)
        conclusions = self.service.list_conclusions(self.item["id"], "viewer")
        self.assertEqual([c["revision"] for c in conclusions], [2, 1])
        self.assertEqual([c["status"] for c in conclusions], ["current", "superseded"])
        # 旧结论内容仍可查
        self.assertTrue(conclusions[1]["detail"]["escalation_required"])
        # 审计链完整且包含补传与结论事件
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events]
        self.assertEqual(actions.count("reading_sync"), 2)
        self.assertEqual(actions.count("conclusion"), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_sync_only_one_wins_and_loser_gets_current_version(self):
        barrier = threading.Barrier(2)
        outcomes = {}

        def submit(name, batch_id):
            barrier.wait()
            try:
                result, _ = self.sync(batch_id, 1, [
                    self.reading("seepage", 11.0, "2026-09-30T08:00:00Z")], actor=name)
                outcomes[name] = ("ok", result["version"])
            except ConflictError as exc:
                outcomes[name] = ("conflict", exc.details.get("current_version"))

        threads = [threading.Thread(target=submit, args=("inspector-a", "B-A")),
                   threading.Thread(target=submit, args=("inspector-b", "B-B"))]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(v[0] for v in outcomes.values()), ["conflict", "ok"])
        loser_name = [name for name, v in outcomes.items() if v[0] == "conflict"][0]
        self.assertEqual(outcomes[loser_name][1], 2)  # 后到的拿到当前版本
        # 失败者按当前版本重试后成立
        loser_batch = "B-A" if loser_name == "inspector-a" else "B-B"
        retry, replayed = self.sync(loser_batch, 2, [
            self.reading("displacement", 4.0, "2026-09-30T09:00:00Z")], actor=loser_name)
        self.assertFalse(replayed)
        self.assertEqual(retry["version"], 3)
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 2)

    def test_failed_batch_leaves_nothing_and_retries_with_same_batch(self):
        self.sync("B-1", 1, [self.reading("seepage", 8.0, "2026-09-30T08:00:00Z")])
        audits_before = len(self.service.audit("viewer", self.item["id"]))
        # 批内第二条与已有读数冲突 -> 整批回滚
        with self.assertRaises(ConflictError):
            self.sync("B-2", 2, [
                self.reading("displacement", 2.0, "2026-09-30T09:00:00Z"),
                self.reading("seepage", 9.0, "2026-09-30T08:00:00Z"),
            ])
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 1)
        self.assertEqual(self.service.get_item(self.item["id"], "viewer")["version"], 2)
        self.assertEqual(len(self.service.list_conclusions(self.item["id"], "viewer")), 1)
        self.assertEqual(len(self.service.audit("viewer", self.item["id"])), audits_before)
        self.assertTrue(self.repo.verify_audit_chain())
        # 按原批次号修正后重试，正常成立
        result, replayed = self.sync("B-2", 2, [
            self.reading("displacement", 2.0, "2026-09-30T09:00:00Z"),
            self.reading("seepage", 9.0, "2026-09-30T08:30:00Z"),
        ])
        self.assertFalse(replayed)
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 3)

    def test_validation_and_permission(self):
        with self.assertRaises(ValidationError):
            self.service.sync_readings(self.item["id"],
                {"expected_version": 1, "readings": [self.reading("seepage", 1, "2026-09-30T08:00:00Z")]},
                "a", "inspector")
        with self.assertRaises(ValidationError):
            self.sync("B-V", 1, [])
        with self.assertRaises(ValidationError):
            self.sync("B-V", 1, [self.reading("crack", 1, "2026-09-30T08:00:00Z")])
        with self.assertRaises(ValidationError):
            self.sync("B-V", 1, [self.reading("seepage", 1, "not-a-time")])
        with self.assertRaises(ValidationError):
            self.sync("B-V", "1", [self.reading("seepage", 1, "2026-09-30T08:00:00Z")])
        with self.assertRaises(PermissionDenied):
            self.sync("B-V", 1, [self.reading("seepage", 1, "2026-09-30T08:00:00Z")], role="viewer")
        with self.assertRaises(NotFoundError):
            self.service.sync_readings(9999,
                {"batch_id": "B-V", "expected_version": 1,
                 "readings": [self.reading("seepage", 1, "2026-09-30T08:00:00Z")]}, "a", "inspector")
        # 批次号不能跨缺陷复用
        other = self.service.create_item(
            {"title": "另一缺陷", "description": "d", "severity": "minor",
             "quantity": 1, "threshold": 5, "external_ref": "SYNC-2"}, "creator", "inspector")
        self.sync("B-X", 1, [self.reading("seepage", 1, "2026-09-30T08:00:00Z")])
        with self.assertRaises(ConflictError):
            self.service.sync_readings(other["id"],
                {"batch_id": "B-X", "expected_version": 1,
                 "readings": [self.reading("seepage", 1, "2026-09-30T08:00:00Z")]}, "a", "inspector")


if __name__ == "__main__":
    unittest.main()

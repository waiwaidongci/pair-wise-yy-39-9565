import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "backfill item", "description": "seepage and displacement",
             "severity": "major", "quantity": 5, "threshold": 10,
             "external_ref": "BF-1"},
            "creator", "inspector")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, batch_id, readings, expected_version=None):
        return self.service.backfill_readings(
            self.item["id"],
            {"batch_id": batch_id,
             "expected_version": expected_version or self.item["version"],
             "readings": readings},
            "inspector", "inspector")

    def test_backfill_merges_readings_recalculates_review_and_keeps_records(self):
        # 现场观测时间早于缺陷创建（办公室处置）时间，属于补时读数
        readings = [
            {"kind": "seepage", "value": 12.5, "unit": "L/s",
             "observed_at": "2026-09-29T08:00:00+00:00"},
            {"kind": "displacement", "value": 3.1, "unit": "mm",
             "observed_at": "2026-09-29T09:00:00+00:00"},
        ]
        result = self._batch("B-20260929-01", readings)
        self.assertFalse(result["replayed"])
        self.assertEqual(len(result["readings"]), 2)
        # 旧复核结论重算：渗流 12.5 达到阈值 10 -> abnormal
        self.assertEqual(result["review"]["conclusion"], "abnormal")
        self.assertEqual(result["item"]["version"], self.item["version"] + 1)
        # 补时读数没有被丢弃
        stored = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual(len(stored), 2)
        self.assertEqual({r["kind"] for r in stored}, {"seepage", "displacement"})
        # 原处置（记录）与审计记录仍可查，未被改写
        self.assertEqual(len(self.service.list_records(self.item["id"], "viewer")), 0)
        self.assertTrue(self.repo.verify_audit_chain())
        audit = self.service.audit("viewer", self.item["id"])
        actions = {e["action"] for e in audit}
        self.assertIn("backfill", actions)
        self.assertIn("review", actions)

    def test_old_review_invalidated_and_history_still_queryable(self):
        first = self._batch("B-1", [
            {"kind": "seepage", "value": 1, "unit": "L/s",
             "observed_at": "2026-09-29T08:00:00+00:00"}])
        self.assertEqual(first["review"]["conclusion"], "normal")
        # 第二批次读数升高，重算后异常
        second = self.service.backfill_readings(
            self.item["id"],
            {"batch_id": "B-2", "expected_version": first["item"]["version"],
             "readings": [{"kind": "seepage", "value": 20, "unit": "L/s",
                           "observed_at": "2026-09-30T08:00:00+00:00"}]},
            "inspector", "inspector")
        self.assertEqual(second["review"]["conclusion"], "abnormal")
        self.assertEqual(second["review"]["stale"], 0)
        reviews = self.service.list_reviews(self.item["id"], "viewer")
        self.assertEqual(len(reviews), 2)
        # 旧结论失效但仍可查
        stale = [r for r in reviews if r["stale"] == 1]
        fresh = [r for r in reviews if r["stale"] == 0]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["conclusion"], "normal")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["conclusion"], "abnormal")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_idempotent_batch_retry_no_duplicate(self):
        readings = [{"kind": "seepage", "value": 5, "unit": "L/s",
                     "observed_at": "2026-09-29T08:00:00+00:00"}]
        first = self._batch("B-IDEM", readings)
        self.assertFalse(first["replayed"])
        # 回网后按原批次重试
        second = self.service.backfill_readings(
            self.item["id"],
            {"batch_id": "B-IDEM", "expected_version": first["item"]["version"],
             "readings": readings},
            "inspector", "inspector")
        self.assertTrue(second["replayed"])
        self.assertEqual(len(second["readings"]), 1)
        stored = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual(len(stored), 1)
        # 版本没有因为重试再次跳号
        self.assertEqual(second["item"]["version"], first["item"]["version"])

    def test_failed_batch_leaves_no_half_records(self):
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self._batch("B-BAD", [
                {"kind": "seepage", "value": 5,
                 "observed_at": "2026-09-29T08:00:00+00:00"},
                {"kind": "unknown_kind", "value": 1,
                 "observed_at": "2026-09-29T09:00:00+00:00"},
            ])
        # 半条记录都不能留下（只有建项时的 create 审计）
        self.assertEqual(self.service.list_readings(self.item["id"], "viewer"), [])
        self.assertEqual(self.service.list_reviews(self.item["id"], "viewer"), [])
        audit = self.repo.list_audit(self.item["id"])
        self.assertEqual({e["action"] for e in audit}, {"create"})
        # 原批次号可重新使用
        result = self._batch("B-BAD", [
            {"kind": "seepage", "value": 5, "unit": "L/s",
             "observed_at": "2026-09-29T08:00:00+00:00"}])
        self.assertFalse(result["replayed"])

    def test_concurrent_backfill_only_one_wins(self):
        from src.domain import ConflictError
        barrier = threading.Barrier(2)
        results = {}

        def do_backfill(name, batch_id):
            barrier.wait()
            try:
                results[name] = ("ok", self._batch(batch_id, [
                    {"kind": "seepage", "value": 6, "unit": "L/s",
                     "observed_at": "2026-09-29T08:00:00+00:00"}]))
            except ConflictError as exc:
                results[name] = ("conflict", exc)

        t1 = threading.Thread(target=do_backfill, args=("a", "B-A"))
        t2 = threading.Thread(target=do_backfill, args=("b", "B-B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = {name: outcome for name, (outcome, _) in results.items()}
        self.assertEqual(sorted(statuses.values()), ["conflict", "ok"])
        winner = results[[n for n, s in statuses.items() if s == "ok"][0]][1]
        loser = results[[n for n, s in statuses.items() if s == "conflict"][0]][1]
        # 后到的拿到当前版本
        self.assertEqual(loser.extra["current_version"], winner["item"]["version"])
        self.assertEqual(winner["item"]["version"], self.item["version"] + 1)

    def test_manual_recalculate_invalidates_old_review(self):
        self._batch("B-1", [
            {"kind": "seepage", "value": 1, "unit": "L/s",
             "observed_at": "2026-09-29T08:00:00+00:00"}])
        review = self.service.recalculate_review(self.item["id"], "inspector", "inspector")
        self.assertEqual(review["conclusion"], "normal")
        self.assertEqual(review["stale"], 0)
        reviews = self.service.list_reviews(self.item["id"], "viewer")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(sum(1 for r in reviews if r["stale"] == 1), 1)

    def test_viewer_cannot_backfill(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.backfill_readings(
                self.item["id"],
                {"batch_id": "B-X", "expected_version": 1,
                 "readings": [{"kind": "seepage", "value": 1,
                               "observed_at": "2026-09-29T08:00:00+00:00"}]},
                "viewer", "viewer")


class BackfillHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, body, actor="inspector", role="inspector"):
        req = Request(self._url(path), data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json",
                               "X-Actor": actor, "X-Role": role}, method="POST")
        try:
            with urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path, actor="viewer", role="viewer"):
        req = Request(self._url(path), headers={"X-Actor": actor, "X-Role": role})
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_http_backfill_conflict_returns_current_version(self):
        _, item = self._post("/api/items", {
            "title": "http item", "description": "d", "severity": "major",
            "quantity": 5, "threshold": 10}, actor="inspector", role="inspector")
        body = {"batch_id": "B-HTTP", "expected_version": 99,
                "readings": [{"kind": "seepage", "value": 1,
                              "observed_at": "2026-09-29T08:00:00+00:00"}]}
        status, payload = self._post(f"/api/items/{item['id']}/readings/backfill", body)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "ConflictError")
        self.assertEqual(payload["current_version"], item["version"])

    def test_http_readings_and_reviews_endpoints(self):
        _, item = self._post("/api/items", {
            "title": "http item 2", "description": "d", "severity": "major",
            "quantity": 5, "threshold": 10}, actor="inspector", role="inspector")
        status, result = self._post(
            f"/api/items/{item['id']}/readings/backfill",
            {"batch_id": "B-HTTP-2", "expected_version": item["version"],
             "readings": [{"kind": "displacement", "value": 1,
                           "observed_at": "2026-09-29T08:00:00+00:00"}]})
        self.assertEqual(status, 201)
        self.assertEqual(result["review"]["conclusion"], "normal")
        status, payload = self._get(f"/api/items/{item['id']}/readings")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["readings"]), 1)
        status, payload = self._get(f"/api/items/{item['id']}/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["reviews"]), 1)
        # 旧接口与演示页照旧可用
        status, payload = self._get(f"/api/items/{item['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], item["id"])
        with urlopen(self._url("/")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("大坝巡检", resp.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()

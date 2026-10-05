import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from src.domain import (
    Actor,
    PermissionDenied,
    ValidationError,
)
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("insp-1", "inspector")
        self.dispatcher = Actor("disp-1", "dispatcher")
        self.equipment = self.service.create(
            self.admin,
            "equipment",
            {
                "asset_no": "E-1",
                "equipment_type": "elevator",
                "location": "Tower A",
                "inspection_interval_days": 365,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _inspection(self, entity_id="INSP-1", scheduled="2026-10-01T09:00:00Z"):
        return self.service.create(
            self.admin,
            "inspection",
            {
                "id": entity_id,
                "equipment_id": self.equipment["id"],
                "scheduled_at": scheduled,
                "cycle_days": 365,
            },
        )

    def merge(self, actor, records):
        return self.service.merge_offline(actor, records)

    def test_new_offline_inspection_lands_once_and_replays_first_result(self):
        record = {
            "record_id": "INSP-9",
            "kind": "inspection",
            "entity_id": "INSP-9",
            "status": "passed",
            "data": {
                "equipment_id": self.equipment["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
                "findings": "captured in machine room",
            },
        }
        first = self.merge(self.inspector, [record])
        second = self.merge(self.inspector, [record])
        self.assertEqual(first, second)
        self.assertEqual(first[0]["outcome"], "created")

        entity = self.service.get("INSP-9")
        self.assertEqual(entity["status"], "passed")
        rows = [e for e in self.service.list("inspection") if e["id"] == "INSP-9"]
        self.assertEqual(len(rows), 1)

        inbox = self.service.offline_inbox()
        self.assertEqual([i["record_id"] for i in inbox], ["INSP-9"])
        self.assertEqual(inbox[0]["status"], "merged")
        self.assertEqual(inbox[0]["attempts"], 1)

    def test_only_field_changed_fast_forwards(self):
        online = self._inspection("INSP-2")
        record = {
            "record_id": "INSP-2",
            "kind": "inspection",
            "entity_id": "INSP-2",
            "status": "passed",
            "data": {
                "equipment_id": self.equipment["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
                "findings": "offline pass",
            },
            "base": {"version": 1, "status": "scheduled", "data": online["data"]},
        }
        result = self.merge(self.inspector, [record])[0]
        self.assertEqual(result["outcome"], "updated")

        entity = self.service.get("INSP-2")
        self.assertEqual(entity["status"], "passed")
        self.assertEqual(entity["version"], 2)
        self.assertEqual(entity["data"]["findings"], "offline pass")

    def test_both_sides_changed_keeps_both_copies_and_freezes_state(self):
        online = self._inspection("INSP-3")
        self.service.transition(
            self.admin, "INSP-3", "fail", {"findings": "server side fail"},
            expected_version=1,
        )
        record = {
            "record_id": "INSP-3",
            "kind": "inspection",
            "entity_id": "INSP-3",
            "status": "passed",
            "data": {
                "equipment_id": self.equipment["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
                "findings": "field side pass",
            },
            "base": {"version": 1, "status": "scheduled", "data": online["data"]},
        }
        first = self.merge(self.inspector, [record])[0]
        replay = self.merge(self.inspector, [record])[0]
        self.assertEqual(first["outcome"], "conflict")
        self.assertEqual(first["conflict_id"], replay["conflict_id"])

        # Canonical inspection and equipment state are untouched pending a decision.
        self.assertEqual(self.service.get("INSP-3")["status"], "failed")
        self.assertEqual(self.service.get(self.equipment["id"])["status"], "in_service")

        conflicts = self.service.list("sync_conflict")
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["status"], "open")
        self.assertEqual(conflict["data"]["server_status"], "failed")
        self.assertEqual(conflict["data"]["field_status"], "passed")
        self.assertEqual(conflict["data"]["server_data"]["findings"], "server side fail")
        self.assertEqual(conflict["data"]["field_data"]["findings"], "field side pass")

        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.inspector, conflict["id"], "resolve", {"choice": "field"}
            )

    def test_admin_resolves_with_field_then_entity_lands(self):
        online = self._inspection("INSP-4")
        self.service.transition(
            self.admin, "INSP-4", "fail", {"findings": "server"}, expected_version=1
        )
        record = {
            "record_id": "INSP-4",
            "kind": "inspection",
            "entity_id": "INSP-4",
            "status": "passed",
            "data": {
                "equipment_id": self.equipment["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
                "findings": "field",
            },
            "base": {"version": 1, "status": "scheduled", "data": online["data"]},
        }
        conflict_id = self.merge(self.inspector, [record])[0]["conflict_id"]
        self.service.transition(self.admin, conflict_id, "resolve", {"choice": "field"})

        entity = self.service.get("INSP-4")
        self.assertEqual(entity["status"], "passed")
        self.assertEqual(entity["data"]["findings"], "field")
        self.assertEqual(self.service.get(conflict_id)["status"], "resolved")

        inbox = {i["record_id"]: i for i in self.service.offline_inbox()}["INSP-4"]
        self.assertEqual(inbox["status"], "resolved")
        self.assertEqual(inbox["result"]["outcome"], "resolved")
        self.assertEqual(inbox["result"]["resolution"], "field")

    def test_admin_resolves_with_server_and_entity_stays(self):
        online = self._inspection("INSP-5")
        self.service.transition(
            self.admin, "INSP-5", "fail", {"findings": "server"}, expected_version=1
        )
        record = {
            "record_id": "INSP-5",
            "kind": "inspection",
            "entity_id": "INSP-5",
            "status": "passed",
            "data": {
                "equipment_id": self.equipment["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
                "findings": "field",
            },
            "base": {"version": 1, "status": "scheduled", "data": online["data"]},
        }
        conflict_id = self.merge(self.inspector, [record])[0]["conflict_id"]
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, conflict_id, "resolve", {"choice": "both"})
        self.service.transition(self.admin, conflict_id, "resolve", {"choice": "server"})
        entity = self.service.get("INSP-5")
        self.assertEqual(entity["status"], "failed")
        self.assertEqual(entity["data"]["findings"], "server")

    def test_failed_record_is_retained_and_retried_on_reconnect(self):
        record = {
            "record_id": "AL-404",
            "kind": "alarm",
            "entity_id": "AL-404",
            "status": "received",
            "data": {
                "equipment_id": "MISSING-EQ",
                "code": "DOOR",
                "occurred_at": "2026-10-01T10:00:00Z",
            },
        }
        result = self.merge(self.dispatcher, [record])[0]
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("equipment", result["error"])

        failed = self.service.offline_inbox(status="failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["attempts"], 1)
        self.assertIsNone(self.service.repository.get_entity("AL-404"))

        # Dependency arrives, the retained record is retried and merges.
        self.service.create(
            self.admin,
            "equipment",
            {
                "id": "MISSING-EQ",
                "asset_no": "E-2",
                "equipment_type": "elevator",
                "location": "Tower B",
                "inspection_interval_days": 365,
            },
        )
        retried = self.service.retry_offline(self.dispatcher)
        self.assertEqual(
            [(r["record_id"], r["outcome"]) for r in retried],
            [("AL-404", "created")],
        )
        self.assertEqual(self.service.get("AL-404")["status"], "received")
        self.assertEqual(self.service.offline_inbox(status="failed"), [])

    def test_duplicate_alarm_upload_answers_once(self):
        record = {
            "record_id": "AL-1",
            "kind": "alarm",
            "entity_id": "AL-1",
            "status": "received",
            "data": {
                "equipment_id": self.equipment["id"],
                "code": "DOOR",
                "occurred_at": "2026-10-02T10:00:00Z",
            },
        }
        first = self.merge(self.dispatcher, [record, record])
        self.assertEqual(len(first), 2)
        self.assertEqual({r["outcome"] for r in first}, {"created"})
        self.assertEqual(
            len([e for e in self.service.list("alarm") if e["id"] == "AL-1"]), 1
        )

    def test_invalid_envelope_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.merge(self.inspector, [{"record_id": "x"}])
        with self.assertRaises(ValidationError):
            self.merge(self.inspector, "not-a-list")
        self.assertEqual(self.service.offline_inbox(), [])

    def test_unreachable_status_fails_and_stays_for_retry(self):
        record = {
            "record_id": "AL-2",
            "kind": "alarm",
            "entity_id": "AL-2",
            "status": "closed",  # received -> closed is not a legal one-step transition
            "data": {
                "equipment_id": self.equipment["id"],
                "code": "DOOR",
                "occurred_at": "2026-10-02T10:00:00Z",
            },
        }
        result = self.merge(self.dispatcher, [record])[0]
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("not reachable", result["error"])
        self.assertIsNone(self.service.repository.get_entity("AL-2"))
        failed = self.service.offline_inbox(status="failed")
        self.assertEqual([i["record_id"] for i in failed], ["AL-2"])

    def test_concurrent_duplicate_upload_ingests_once(self):
        import threading

        record = {
            "record_id": "AL-7",
            "kind": "alarm",
            "entity_id": "AL-7",
            "status": "received",
            "data": {
                "equipment_id": self.equipment["id"],
                "code": "DOOR",
                "occurred_at": "2026-10-02T11:00:00Z",
            },
        }
        answers = []
        errors = []
        barrier = threading.Barrier(4)

        def upload():
            try:
                barrier.wait()
                answers.append(self.service.merge_offline(self.dispatcher, [record])[0])
            except Exception as exc:  # pragma: no cover - failure signal
                errors.append(exc)

        threads = [threading.Thread(target=upload) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(answers), 4)
        self.assertTrue(all(answer["outcome"] == "created" for answer in answers), answers)
        self.assertEqual(
            len([e for e in self.service.list("alarm") if e["id"] == "AL-7"]), 1
        )
        inbox = self.service.offline_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["attempts"], 1)


class OfflineHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        import threading

        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server(
            "127.0.0.1", 0, self.service, RuleEngine(), static_dir
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin = Actor("admin", "admin")
        self._request(
            "POST",
            "/api/equipment",
            {
                "asset_no": "E-HTTP",
                "equipment_type": "elevator",
                "location": "X",
                "inspection_interval_days": 365,
            },
            self.admin,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _request(self, method, path, body, actor):
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": actor.user_id,
                "X-Role": actor.role,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_merge_retry_and_inbox_over_http(self):
        inspector = Actor("insp-http", "inspector")
        record = {
            "record_id": "INSP-HTTP",
            "kind": "inspection",
            "entity_id": "INSP-HTTP",
            "status": "passed",
            "data": {
                "equipment_id": self.service.list("equipment")[0]["id"],
                "scheduled_at": "2026-10-03T09:00:00Z",
                "cycle_days": 365,
                "findings": "ok",
            },
        }
        status, payload = self._request(
            "POST", "/api/offline-records", {"records": [record]}, inspector
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["outcome"], "created")

        status, payload = self._request(
            "POST", "/api/offline-records", {"records": [record]}, inspector
        )
        self.assertEqual(payload["items"][0]["outcome"], "created")

        status, payload = self._request(
            "POST", "/api/offline-records/retry", {"record_ids": []}, inspector
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["outcome"], "created")

        with urllib.request.urlopen(
            "http://127.0.0.1:%s/api/offline-records" % self.port, timeout=5
        ) as response:
            inbox = json.loads(response.read().decode("utf-8"))
        self.assertEqual(inbox["items"][0]["record_id"], "INSP-HTTP")
        self.assertEqual(inbox["items"][0]["status"], "merged")


if __name__ == "__main__":
    unittest.main()

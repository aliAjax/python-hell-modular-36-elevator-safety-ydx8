import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("inspector", "inspector")
        self.admin = Actor("admin", "admin")
        self.equipment = self.service.create(
            self.admin,
            "equipment",
            {"asset_no": "E-1", "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _inspection(self, source_id, record_id, findings=None, base_version=None, base_data=None, status=None):
        data = {
            "equipment_id": self.equipment["id"],
            "scheduled_at": "2026-09-27T09:00:00Z",
            "cycle_days": 365,
        }
        if findings is not None:
            data["findings"] = findings
        record = {"source_id": source_id, "record_id": record_id, "kind": "inspection", "data": data}
        if status is not None:
            record["status"] = status
        if base_version is not None:
            record["base_version"] = base_version
        if base_data is not None:
            record["base_data"] = base_data
        return record

    def _alarm(self, source_id, record_id, code="DOOR-JAM"):
        return {
            "source_id": source_id,
            "record_id": record_id,
            "kind": "alarm",
            "data": {"equipment_id": self.equipment["id"], "code": code, "occurred_at": "2026-09-27T10:00:00Z"},
        }

    def test_merge_creates_real_inspection_and_alarm(self):
        results = self.service.merge_offline(self.actor, [
            self._inspection("tab-1", "R-1"),
            self._alarm("tab-1", "R-2"),
        ])
        self.assertEqual([r["status"] for r in results], ["merged", "merged"])
        inspections = self.service.list("inspection")
        alarms = self.service.list("alarm")
        self.assertEqual(len(inspections), 1)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(inspections[0]["id"], results[0]["entity"]["id"])
        records = self.service.list_offline_records()
        self.assertEqual({r["status"] for r in records}, {"merged"})

    def test_duplicate_upload_answers_with_first_result(self):
        record = self._inspection("tab-1", "R-1")
        first = self.service.merge_offline(self.actor, [record])[0]
        second = self.service.merge_offline(self.actor, [dict(record)])[0]
        self.assertEqual(first["entity"]["id"], second["entity"]["id"])
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["entity"]["version"], second["entity"]["version"])
        self.assertEqual(len(self.service.list("inspection")), 1)

    def test_conflict_keeps_both_copies_and_marks_conflict(self):
        record = self._inspection("tab-1", "R-1")
        merged = self.service.merge_offline(self.actor, [record])[0]
        entity_id = merged["entity"]["id"]
        base_data = dict(merged["entity"]["data"])
        # Server side modifies the inspection after the field copy was made.
        self.service.transition(self.admin, entity_id, "pass", {"findings": "server finding"})
        # Field side also modified the same record and re-uploads with its base marker.
        field_record = self._inspection("tab-1", "R-1", findings="field finding",
                                        base_version=1, base_data=base_data, status="passed")
        result = self.service.merge_offline(self.actor, [field_record])[0]
        self.assertEqual(result["status"], "conflict")
        entity = self.service.get(entity_id)
        self.assertEqual(entity["status"], "conflict")
        self.assertTrue(entity["data"]["_conflict"])
        self.assertEqual(entity["data"]["_server_snapshot"]["data"]["findings"], "server finding")
        self.assertEqual(entity["data"]["_field_copy"]["data"]["findings"], "field finding")
        # Equipment status is untouched while the conflict is unresolved.
        self.assertEqual(self.service.get(self.equipment["id"])["status"], "in_service")

    def test_conflict_resolve_server_choice(self):
        record = self._inspection("tab-1", "R-1")
        entity_id = self.service.merge_offline(self.actor, [record])[0]["entity"]["id"]
        base_data = dict(self.service.get(entity_id)["data"])
        self.service.transition(self.admin, entity_id, "pass", {"findings": "server finding"})
        field_record = self._inspection("tab-1", "R-1", findings="field finding",
                                        base_version=1, base_data=base_data, status="passed")
        self.service.merge_offline(self.actor, [field_record])
        resolved = self.service.resolve_conflict(self.admin, entity_id, "server")
        self.assertEqual(resolved["status"], "passed")
        self.assertEqual(resolved["data"]["findings"], "server finding")
        self.assertNotIn("_conflict", resolved["data"])

    def test_conflict_resolve_field_choice(self):
        record = self._inspection("tab-1", "R-1")
        entity_id = self.service.merge_offline(self.actor, [record])[0]["entity"]["id"]
        base_data = dict(self.service.get(entity_id)["data"])
        self.service.transition(self.admin, entity_id, "pass", {"findings": "server finding"})
        field_record = self._inspection("tab-1", "R-1", findings="field finding",
                                        base_version=1, base_data=base_data, status="passed")
        self.service.merge_offline(self.actor, [field_record])
        resolved = self.service.resolve_conflict(self.admin, entity_id, "field")
        self.assertEqual(resolved["status"], "passed")
        self.assertEqual(resolved["data"]["findings"], "field finding")
        self.assertNotIn("_conflict", resolved["data"])

    def test_non_admin_cannot_resolve_conflict(self):
        record = self._inspection("tab-1", "R-1")
        entity_id = self.service.merge_offline(self.actor, [record])[0]["entity"]["id"]
        base_data = dict(self.service.get(entity_id)["data"])
        self.service.transition(self.admin, entity_id, "pass", {"findings": "server finding"})
        field_record = self._inspection("tab-1", "R-1", findings="field finding",
                                        base_version=1, base_data=base_data, status="passed")
        self.service.merge_offline(self.actor, [field_record])
        with self.assertRaises(PermissionDenied):
            self.service.resolve_conflict(self.actor, entity_id, "server")

    def test_failed_record_is_kept_and_retried(self):
        good = self._inspection("tab-1", "R-1")
        bad = {
            "source_id": "tab-1",
            "record_id": "R-2",
            "kind": "inspection",
            "data": {"scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365},
        }
        results = self.service.merge_offline(self.actor, [good, bad])
        self.assertEqual(results[0]["status"], "merged")
        self.assertEqual(results[1]["status"], "failed")
        self.assertIn("equipment", results[1]["error"])
        records = self.service.list_offline_records()
        by_id = {r["record_id"]: r for r in records}
        self.assertEqual(by_id["R-1"]["status"], "merged")
        self.assertEqual(by_id["R-2"]["status"], "failed")
        # Retry on reconnect: the field record is fixed and now merges.
        bad["data"]["equipment_id"] = self.equipment["id"]
        retry = self.service.merge_offline(self.actor, [bad])[0]
        self.assertEqual(retry["status"], "merged")
        self.assertEqual(self.service.list_offline_records(status="failed"), [])

    def test_retry_does_not_duplicate_merged_record(self):
        record = self._inspection("tab-1", "R-1")
        self.service.merge_offline(self.actor, [record])
        self.service.merge_offline(self.actor, [record])
        self.assertEqual(len(self.service.list("inspection")), 1)

    def test_second_device_same_record_id_is_separate(self):
        first = self.service.merge_offline(self.actor, [self._inspection("tab-1", "R-1")])[0]
        second = self.service.merge_offline(self.actor, [self._inspection("tab-2", "R-1")])[0]
        self.assertNotEqual(first["entity"]["id"], second["entity"]["id"])
        self.assertEqual(len(self.service.list("inspection")), 2)


if __name__ == "__main__":
    unittest.main()

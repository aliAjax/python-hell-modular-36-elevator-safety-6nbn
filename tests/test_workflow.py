import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def test_full_safety_flow(self):
        equipment = self.create("equipment", {"asset_no": "E-100", "equipment_type": "elevator", "location": "Tower A", "inspection_interval_days": 365})
        inspection = self.create("inspection", {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})
        inspection = self.act(inspection, "pass", {"findings": "normal"})
        self.assertEqual(inspection["status"], "passed")

        remediation = self.create("remediation", {"equipment_id": equipment["id"], "issue": "door alignment", "owner": "Maint", "due_at": "2026-10-01"})
        remediation = self.act(remediation, "submit_evidence", {"evidence": "IMG-1"})
        remediation = self.act(remediation, "verify", {})
        remediation = self.act(remediation, "close", {})
        self.assertEqual(remediation["status"], "closed")

        permit = self.create("permit", {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"})
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})
        self.assertEqual(permit["status"], "granted")

        alarm = self.create("alarm", {"equipment_id": equipment["id"], "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"})
        alarm = self.act(alarm, "dispatch", {"team": "Alpha"})
        job = self.create("rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha"})
        job = self.act(job, "arrive", {})
        job = self.act(job, "complete", {"outcome": "passenger freed"})
        alarm = self.act(alarm, "resolve", {"resolution": "passenger safe"})
        alarm = self.act(alarm, "close", {})
        self.assertEqual(alarm["status"], "closed")

    def test_component_replacement_requires_part_serial(self):
        equipment = self.create("equipment", {"asset_no": "E-200", "equipment_type": "escalator", "location": "Mall", "inspection_interval_days": 180})
        with self.assertRaises(Exception):
            self.create("maintenance", {"equipment_id": equipment["id"], "work_type": "component_replacement", "planned_at": "2026-10-01"})


if __name__ == "__main__":
    unittest.main()

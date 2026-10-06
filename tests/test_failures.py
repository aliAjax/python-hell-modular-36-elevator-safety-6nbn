import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(self.admin, "equipment", {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365})

    def test_permission_denied(self):
        equipment = self.equipment()
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("viewer", "viewer"), equipment["id"], "suspend", {})

    def test_version_conflict(self):
        equipment = self.equipment()
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, equipment["id"], "suspend", {}, 999)

    def test_duplicate_active_alarm_is_rejected(self):
        equipment = self.equipment()
        payload = {"equipment_id": equipment["id"], "code": "A1", "occurred_at": "2026-09-27T10:00:00Z"}
        self.service.create(self.admin, "alarm", payload)
        with self.assertRaises(ConflictError):
            self.service.create(self.admin, "alarm", payload)

    def test_invalid_transition(self):
        equipment = self.equipment()
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, equipment["id"], "grant", {})


if __name__ == "__main__":
    unittest.main()

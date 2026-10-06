import unittest

from src.domain import Actor, ConflictError, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.actor = Actor("tester", "admin")

    def test_asset_number_validation(self):
        equipment = {"id": "e-1", "kind": "equipment", "status": "in_service", "data": {"asset_no": "E-1"}}
        lookup = lambda kind, field, value: [equipment] if kind == "equipment" and field == "asset_no" else []
        with self.assertRaises(ConflictError):
            self.rules.validate_create(self.actor, "equipment", {"asset_no": "E-1", "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365}, lookup)

    def test_inspection_requires_equipment(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "inspection", {"equipment_id": "missing", "scheduled_at": "2026-09-27", "cycle_days": 365}, lambda k, f, v: [])

    def test_remediation_requires_owner_and_due_date(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "remediation", {"equipment_id": "e-1", "issue": "x"}, lambda k, f, v: [])


if __name__ == "__main__":
    unittest.main()

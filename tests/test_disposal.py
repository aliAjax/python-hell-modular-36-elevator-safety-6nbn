import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DisposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(
            self.admin, "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def passed_inspection(self, equipment_id, scheduled_at="2026-09-20T09:00:00Z"):
        inspection = self.service.create(
            self.admin, "inspection",
            {"equipment_id": equipment_id, "scheduled_at": scheduled_at, "cycle_days": 365},
        )
        return self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})

    def granted_permit(self, equipment_id):
        permit = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment_id, "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, permit["id"], "request_review", {})
        return self.service.transition(self.admin, permit["id"], "grant", {})

    def scheduled_inspection(self, equipment_id, scheduled_at="2026-10-10T09:00:00Z"):
        return self.service.create(
            self.admin, "inspection",
            {"equipment_id": equipment_id, "scheduled_at": scheduled_at, "cycle_days": 365},
        )

    def receive(self, source_notice_id, equipment_id, revision, **extra):
        payload = {"source_notice_id": source_notice_id, "equipment_id": equipment_id, "revision": revision}
        payload.update(extra)
        return self.service.receive_notice(self.admin, payload)

    def test_duplicate_notice_keeps_latest_revision(self):
        equipment = self.equipment()
        notice1, _ = self.receive("N-1", equipment["id"], 1)
        self.assertEqual(notice1["status"], "active")
        notice2, _ = self.receive("N-1", equipment["id"], 2)
        self.assertEqual(notice2["status"], "active")
        self.assertEqual(self.service.get(notice1["id"])["status"], "superseded")
        # lower revision rejected, latest kept
        with self.assertRaises(ConflictError):
            self.receive("N-1", equipment["id"], 1)
        # same revision is idempotent
        again, _ = self.receive("N-1", equipment["id"], 2)
        self.assertEqual(again["id"], notice2["id"])

    def test_notice_effects_suspend_review_reschedule(self):
        equipment = self.equipment()
        self.passed_inspection(equipment["id"])
        permit = self.granted_permit(equipment["id"])
        inspection = self.scheduled_inspection(equipment["id"])
        _, disposal = self.receive("N-1", equipment["id"], 1)
        self.assertEqual(self.service.get(equipment["id"])["status"], "suspended")
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        self.assertEqual(self.service.get(inspection["id"])["status"], "rescheduled")
        self.assertTrue(disposal["status"] == "open")
        for step in ("suspend_equipment", "review_permits", "reschedule_inspections"):
            self.assertTrue(disposal["data"]["progress"][step])

    def test_notice_update_invalidates_and_recalculates(self):
        equipment = self.equipment()
        notice1, disposal1 = self.receive("N-1", equipment["id"], 1)
        notice2, disposal2 = self.receive("N-1", equipment["id"], 2)
        self.assertEqual(self.service.get(notice1["id"])["status"], "superseded")
        self.assertEqual(self.service.get(disposal1["id"])["status"], "superseded")
        self.assertEqual(notice2["status"], "active")
        self.assertEqual(disposal2["status"], "open")
        self.assertNotEqual(disposal1["id"], disposal2["id"])
        # recalculation re-applies effects on the new disposal
        for step in ("suspend_equipment", "review_permits", "reschedule_inspections"):
            self.assertTrue(disposal2["data"]["progress"][step])

    def test_claim_first_wins_later_sees_handler(self):
        equipment = self.equipment()
        _, disposal = self.receive("N-1", equipment["id"], 1)
        alice = Actor("alice", "admin")
        bob = Actor("bob", "admin")
        claimed = self.service.claim_disposal(alice, disposal["id"])
        self.assertEqual(claimed["data"]["handler_id"], "alice")
        with self.assertRaises(ConflictError) as ctx:
            self.service.claim_disposal(bob, disposal["id"])
        self.assertIn("alice", str(ctx.exception))

    def test_concurrent_claim_race(self):
        equipment = self.equipment()
        _, disposal = self.receive("N-1", equipment["id"], 1)
        results = {}

        def claim(name):
            try:
                record = self.service.claim_disposal(Actor(name, "admin"), disposal["id"])
                results[name] = ("ok", record["data"]["handler_id"])
            except ConflictError as exc:
                results[name] = ("conflict", str(exc))

        threads = [threading.Thread(target=claim, args=(n,)) for n in ("alice", "bob", "carol")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [name for name, outcome in results.items() if outcome[0] == "ok"]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        for name, outcome in results.items():
            if name != winner:
                self.assertEqual(outcome[0], "conflict")
                self.assertIn(winner, outcome[1])

    def test_resumable_processing_keeps_progress_on_failure(self):
        equipment = self.equipment()
        self.passed_inspection(equipment["id"])
        permit = self.granted_permit(equipment["id"])
        inspection = self.scheduled_inspection(equipment["id"])

        calls = {"n": 0}
        original = self.service.transition

        def flaky(actor, entity_id, action, data=None, expected_version=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated write failure")
            return original(actor, entity_id, action, data, expected_version)

        self.service.transition = flaky
        with self.assertRaises(RuntimeError):
            self.receive("N-1", equipment["id"], 1)

        disposal = [d for d in self.service.list("disposal") if d["status"] == "open"][0]
        self.assertEqual(disposal["data"]["progress"], {"suspend_equipment": True})
        self.assertEqual(self.service.get(equipment["id"])["status"], "suspended")
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")
        self.assertEqual(self.service.get(inspection["id"])["status"], "scheduled")

        self.service.transition = original
        disposal = self.service.process_disposal(self.admin, disposal["id"])
        self.assertEqual(
            disposal["data"]["progress"],
            {"suspend_equipment": True, "review_permits": True, "reschedule_inspections": True},
        )
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        self.assertEqual(self.service.get(inspection["id"])["status"], "rescheduled")

    def test_cannot_return_to_service_until_closed_and_reinspected(self):
        equipment = self.equipment()
        inspection = self.scheduled_inspection(equipment["id"], scheduled_at="2026-10-10T09:00:00Z")
        self.receive("N-1", equipment["id"], 1, effective_at="2026-10-01T00:00:00Z")

        # blocked: no re-inspection yet
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})

        # close remediation
        remediation = self.service.create(
            self.admin, "remediation",
            {"equipment_id": equipment["id"], "issue": "fix", "owner": "Maint", "due_at": "2026-10-05"},
        )
        self.service.transition(self.admin, remediation["id"], "submit_evidence", {"evidence": "IMG"})
        self.service.transition(self.admin, remediation["id"], "verify", {})
        self.service.transition(self.admin, remediation["id"], "close", {})

        # still blocked: re-inspection not passed
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})

        # re-schedule and pass the re-inspection
        self.service.transition(self.admin, inspection["id"], "rearrange", {"scheduled_at": "2026-10-08T09:00:00Z"})
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})

        equipment = self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        self.assertEqual(equipment["status"], "in_service")

    def test_open_remediation_blocks_return_even_with_passed_inspection(self):
        equipment = self.equipment()
        inspection = self.scheduled_inspection(equipment["id"], scheduled_at="2026-10-10T09:00:00Z")
        self.receive("N-1", equipment["id"], 1, effective_at="2026-10-01T00:00:00Z")
        self.service.transition(self.admin, inspection["id"], "rearrange", {"scheduled_at": "2026-10-08T09:00:00Z"})
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        # remediation left open
        self.service.create(
            self.admin, "remediation",
            {"equipment_id": equipment["id"], "issue": "fix", "owner": "Maint", "due_at": "2026-10-05"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})


if __name__ == "__main__":
    unittest.main()

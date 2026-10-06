import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.equipment = self.service.create(self.admin, "equipment", {
            "asset_no": "E-N1", "equipment_type": "elevator",
            "location": "Tower B", "inspection_interval_days": 365,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def notice(self, revision=1, **extra):
        payload = {
            "notice_no": "NT-1", "revision": revision,
            "equipment_id": self.equipment["id"],
            "issued_at": "2026-10-06T08:00:00Z", "reason": "regulator stop-use",
        }
        payload.update(extra)
        return self.service.create(self.admin, "notice", payload)

    def granted_permit(self):
        inspection = self.service.create(self.admin, "inspection", {
            "equipment_id": self.equipment["id"],
            "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365,
        })
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "normal"})
        permit = self.service.create(self.admin, "permit", {
            "equipment_id": self.equipment["id"],
            "purpose": "return_to_service", "requested_by": "ops",
        })
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        return self.service.transition(self.admin, permit["id"], "grant", {})

    def scheduled_inspection(self):
        return self.service.create(self.admin, "inspection", {
            "equipment_id": self.equipment["id"],
            "scheduled_at": "2026-11-01T09:00:00Z", "cycle_days": 365,
        })

    def test_effective_notice_applies_effects(self):
        permit = self.granted_permit()
        inspection = self.scheduled_inspection()
        notice = self.notice()

        self.assertTrue(notice["data"]["effects_complete"])
        self.assertEqual(len(notice["data"]["applied_steps"]), 3)
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["status"], "suspended")
        self.assertEqual(equipment["data"]["suspended_by_notice"], notice["id"])
        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "pending_review")
        self.assertEqual(permit["data"]["notice_hold"], notice["id"])
        inspection = self.service.get(inspection["id"])
        self.assertEqual(inspection["status"], "returned")
        self.assertEqual(inspection["data"]["returned_by_notice"], notice["id"])

    def test_duplicate_revisions_keep_only_latest(self):
        first = self.notice(revision=1)
        second = self.notice(revision=2)
        first = self.service.get(first["id"])
        self.assertEqual(first["status"], "superseded")
        self.assertEqual(first["data"]["superseded_by"], second["id"])
        self.assertEqual(second["status"], "received")
        with self.assertRaises(ConflictError):
            self.notice(revision=2)
        with self.assertRaises(ConflictError):
            self.notice(revision=1)

    def test_new_revision_recomputes_linked_effects(self):
        permit = self.granted_permit()
        inspection = self.scheduled_inspection()
        first = self.notice(revision=1)
        second = self.notice(revision=2)

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "pending_review")
        self.assertEqual(permit["data"]["notice_hold"], second["id"])
        self.assertEqual(permit["data"]["hold_revision"], 2)
        inspection = self.service.get(inspection["id"])
        self.assertEqual(inspection["status"], "returned")
        self.assertEqual(inspection["data"]["returned_by_notice"], second["id"])
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["data"]["suspended_by_notice"], second["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_rescinded_revision_releases_holds(self):
        permit = self.granted_permit()
        inspection = self.scheduled_inspection()
        self.notice(revision=1)
        self.notice(revision=2, effective=False)

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "granted")
        self.assertNotIn("notice_hold", permit["data"])
        inspection = self.service.get(inspection["id"])
        self.assertEqual(inspection["status"], "scheduled")
        self.assertNotIn("returned_by_notice", inspection["data"])
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["status"], "suspended")
        self.assertNotIn("suspended_by_notice", equipment["data"])
        # no active effective notice remains, so manual return is allowed
        equipment = self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        self.assertEqual(equipment["status"], "in_service")

    def test_withdraw_releases_holds(self):
        permit = self.granted_permit()
        inspection = self.scheduled_inspection()
        notice = self.notice()
        notice = self.service.transition(self.admin, notice["id"], "withdraw", {})

        self.assertEqual(notice["status"], "withdrawn")
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")
        self.assertEqual(self.service.get(inspection["id"])["status"], "scheduled")

    def test_claim_first_wins_and_loser_sees_handler(self):
        notice = self.notice()
        claimed = self.service.transition(Actor("officer-a", "dispatcher"), notice["id"], "claim", {})
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["data"]["claimed_by"], "officer-a")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(Actor("officer-b", "dispatcher"), notice["id"], "claim", {})
        self.assertIn("officer-a", str(ctx.exception))

    def test_concurrent_claims_first_persisted_wins(self):
        notice = self.notice()
        barrier = threading.Barrier(2)
        results = {}

        def claim(name, officer):
            barrier.wait()
            try:
                self.service.transition(
                    Actor(officer, "dispatcher"), notice["id"], "claim", {}, notice["version"]
                )
                results[name] = ("ok", officer)
            except ConflictError as exc:
                results[name] = ("conflict", str(exc))

        threads = [
            threading.Thread(target=claim, args=("a", "officer-a")),
            threading.Thread(target=claim, args=("b", "officer-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        outcomes = sorted(value[0] for value in results.values())
        self.assertEqual(outcomes, ["conflict", "ok"])
        winner = [value[1] for value in results.values() if value[0] == "ok"][0]
        conflict = [value[1] for value in results.values() if value[0] == "conflict"][0]
        self.assertIn(winner, conflict)
        self.assertEqual(self.service.get(notice["id"])["data"]["claimed_by"], winner)

    def test_failed_write_keeps_progress_and_retry_completes_rest(self):
        permit = self.granted_permit()
        inspection = self.scheduled_inspection()
        repository = self.service.repository
        original = repository.update_entity

        def flaky(entity_id, expected_version, status, data):
            if entity_id == permit["id"]:
                raise ConflictError("simulated write failure")
            return original(entity_id, expected_version, status, data)

        repository.update_entity = flaky
        notice = self.notice()
        repository.update_entity = original

        self.assertEqual(notice["status"], "received")
        self.assertFalse(notice["data"]["effects_complete"])
        self.assertEqual(notice["data"]["applied_steps"], ["equipment:hold@r1"])
        self.assertIn("permits", notice["data"]["apply_error"])
        self.assertEqual(self.service.get(self.equipment["id"])["status"], "suspended")
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")
        self.assertEqual(self.service.get(inspection["id"])["status"], "scheduled")

        notice = self.service.transition(self.admin, notice["id"], "claim", {})
        notice = self.service.transition(self.admin, notice["id"], "apply", {})
        self.assertEqual(notice["status"], "applied")
        self.assertTrue(notice["data"]["effects_complete"])
        self.assertIsNone(notice["data"]["apply_error"])
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        self.assertEqual(self.service.get(inspection["id"])["status"], "returned")
        # equipment step was not re-applied: only one version bump from the first run
        self.assertEqual(self.service.get(self.equipment["id"])["version"], 2)

    def test_return_to_service_needs_closed_remediation_and_passed_reinspection(self):
        self.notice()
        equipment = self.service.get(self.equipment["id"])
        self.assertEqual(equipment["status"], "suspended")

        remediation = self.service.create(self.admin, "remediation", {
            "equipment_id": self.equipment["id"], "issue": "brake wear",
            "owner": "Maint", "due_at": "2026-10-10",
        })
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        self.assertIn("remediation", str(ctx.exception))

        remediation = self.service.transition(self.admin, remediation["id"], "submit_evidence", {"evidence": "IMG-9"})
        remediation = self.service.transition(self.admin, remediation["id"], "verify", {})
        self.service.transition(self.admin, remediation["id"], "close", {})
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        self.assertIn("re-inspection", str(ctx.exception))

        inspection = self.scheduled_inspection()
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "re-inspection ok"})
        equipment = self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        self.assertEqual(equipment["status"], "in_service")


if __name__ == "__main__":
    unittest.main()

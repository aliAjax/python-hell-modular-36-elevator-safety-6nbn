import hashlib
import sqlite3
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationError
from .rules import ACTIVE_NOTICE_STATUSES, RuleEngine

NOTICE_EFFECT_STEPS = ("equipment", "permits", "inspections")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "notice":
            entity = self._register_notice(actor, entity)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "notice" and action == "apply":
            entity = self._apply_notice_effects(actor, entity)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        try:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        except ConflictError:
            if entity["kind"] == "notice" and action == "claim":
                current = self.repository.get_entity(entity_id)
                claimed_by = current["data"].get("claimed_by") if current else None
                if claimed_by:
                    raise ConflictError("notice already claimed by " + str(claimed_by))
            raise
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "notice" and action == "withdraw":
            updated = self._apply_notice_effects(actor, updated, force_effective=False)
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def _register_notice(self, actor, notice):
        """Supersede older revisions of the same notice, then (re)compute effects."""
        self._supersede_older_revisions(actor, notice)
        try:
            return self._apply_notice_effects(actor, notice)
        except (DomainError, sqlite3.Error, OSError):
            # Progress is persisted on the notice; a later apply retries the rest.
            return self.repository.get_entity(notice["id"])

    def _supersede_older_revisions(self, actor, notice):
        notice_no = notice["data"].get("notice_no")
        revision = int(notice["data"].get("revision", 0))
        for other in self._lookup("notice", "*", None):
            if other["id"] == notice["id"]:
                continue
            if other["data"].get("notice_no") != notice_no:
                continue
            if other["status"] not in ACTIVE_NOTICE_STATUSES:
                continue
            if int(other["data"].get("revision", 0)) >= revision:
                continue
            merged = dict(other["data"])
            merged["superseded_by"] = notice["id"]
            self.repository.update_entity(other["id"], other["version"], "superseded", merged)
            self.audit.record(
                other["id"],
                actor,
                "supersede",
                other["status"],
                "superseded",
                {"superseded_by": notice["id"], "revision": revision},
            )

    def _notice_family_ids(self, notice):
        notice_no = notice["data"].get("notice_no")
        return {
            item["id"]
            for item in self._lookup("notice", "*", None)
            if item["data"].get("notice_no") == notice_no
        }

    def _save_notice_progress(self, notice, data):
        return self.repository.update_entity(notice["id"], notice["version"], notice["status"], data)

    def _apply_notice_effects(self, actor, notice, force_effective=None):
        """Run the staged disposal steps; each finished step is recorded so a
        retry only fills in the items that are still missing."""
        data = dict(notice["data"])
        revision = int(data.get("revision", 0))
        effective = bool(data.get("effective", True)) if force_effective is None else force_effective
        mode = "hold" if effective else "release"
        keys = ["%s:%s@r%s" % (step, mode, revision) for step in NOTICE_EFFECT_STEPS]
        done = set(data.get("applied_steps", []))
        ran = False
        for step, key in zip(NOTICE_EFFECT_STEPS, keys):
            if key in done:
                continue
            try:
                if step == "equipment":
                    self._effect_equipment(actor, notice, effective)
                elif step == "permits":
                    self._effect_permits(actor, notice, effective)
                else:
                    self._effect_inspections(actor, notice, effective)
            except Exception as exc:
                data["apply_error"] = "%s: %s" % (step, exc)
                data["applied_steps"] = sorted(done)
                data["effects_complete"] = False
                self._save_notice_progress(notice, data)
                self.audit.record(
                    notice["id"], actor, "effects_failed", notice["status"], notice["status"],
                    {"step": step, "error": str(exc)},
                )
                raise
            ran = True
            done.add(key)
            data["apply_error"] = None
            data["applied_steps"] = sorted(done)
            data["effects_complete"] = all(key in done for key in keys)
            notice = self._save_notice_progress(notice, data)
        if ran and data.get("effects_complete"):
            self.audit.record(
                notice["id"], actor, "effects_applied", notice["status"], notice["status"],
                {"revision": revision, "mode": mode},
            )
        return notice

    def _effect_equipment(self, actor, notice, effective):
        equipment_id = notice["data"].get("equipment_id")
        equipment = self.repository.get_entity(equipment_id)
        if not equipment:
            raise ValidationError("notice equipment not found: " + str(equipment_id))
        family = self._notice_family_ids(notice)
        revision = int(notice["data"].get("revision", 0))
        data = dict(equipment["data"])
        if effective:
            if equipment["status"] == "in_service":
                data["suspended_by_notice"] = notice["id"]
                data["notice_revision"] = revision
                self.repository.update_entity(equipment["id"], equipment["version"], "suspended", data)
                self.audit.record(
                    equipment["id"], actor, "notice_suspend", "in_service", "suspended",
                    {"notice_id": notice["id"], "revision": revision},
                )
            elif (
                equipment["status"] == "suspended"
                and data.get("suspended_by_notice") in family
                and data.get("suspended_by_notice") != notice["id"]
            ):
                data["suspended_by_notice"] = notice["id"]
                data["notice_revision"] = revision
                self.repository.update_entity(equipment["id"], equipment["version"], "suspended", data)
                self.audit.record(
                    equipment["id"], actor, "notice_retag", "suspended", "suspended",
                    {"notice_id": notice["id"], "revision": revision},
                )
        elif data.get("suspended_by_notice") in family:
            # The suspension itself is lifted only by an explicit return_to_service.
            data.pop("suspended_by_notice", None)
            data.pop("notice_revision", None)
            self.repository.update_entity(equipment["id"], equipment["version"], equipment["status"], data)
            self.audit.record(
                equipment["id"], actor, "notice_release", equipment["status"], equipment["status"],
                {"notice_id": notice["id"], "revision": revision},
            )

    def _effect_permits(self, actor, notice, effective):
        family = self._notice_family_ids(notice)
        revision = int(notice["data"].get("revision", 0))
        for permit in self._lookup("permit", "*", None):
            if permit["data"].get("equipment_id") != notice["data"].get("equipment_id"):
                continue
            data = dict(permit["data"])
            hold = data.get("notice_hold")
            if effective:
                if permit["status"] == "granted":
                    data["notice_hold"] = notice["id"]
                    data["hold_revision"] = revision
                    self.repository.update_entity(permit["id"], permit["version"], "pending_review", data)
                    self.audit.record(
                        permit["id"], actor, "notice_hold", "granted", "pending_review",
                        {"notice_id": notice["id"], "revision": revision},
                    )
                elif (
                    permit["status"] == "pending_review"
                    and hold in family
                    and (hold != notice["id"] or data.get("hold_revision") != revision)
                ):
                    data["notice_hold"] = notice["id"]
                    data["hold_revision"] = revision
                    self.repository.update_entity(permit["id"], permit["version"], "pending_review", data)
                    self.audit.record(
                        permit["id"], actor, "notice_retag", "pending_review", "pending_review",
                        {"notice_id": notice["id"], "revision": revision},
                    )
            elif permit["status"] == "pending_review" and hold in family:
                data.pop("notice_hold", None)
                data.pop("hold_revision", None)
                self.repository.update_entity(permit["id"], permit["version"], "granted", data)
                self.audit.record(
                    permit["id"], actor, "notice_release", "pending_review", "granted",
                    {"notice_id": notice["id"], "revision": revision},
                )

    def _effect_inspections(self, actor, notice, effective):
        family = self._notice_family_ids(notice)
        revision = int(notice["data"].get("revision", 0))
        for inspection in self._lookup("inspection", "*", None):
            if inspection["data"].get("equipment_id") != notice["data"].get("equipment_id"):
                continue
            data = dict(inspection["data"])
            tag = data.get("returned_by_notice")
            if effective:
                if inspection["status"] == "scheduled":
                    data["returned_by_notice"] = notice["id"]
                    data["return_revision"] = revision
                    self.repository.update_entity(inspection["id"], inspection["version"], "returned", data)
                    self.audit.record(
                        inspection["id"], actor, "notice_return", "scheduled", "returned",
                        {"notice_id": notice["id"], "revision": revision},
                    )
                elif (
                    inspection["status"] == "returned"
                    and tag in family
                    and (tag != notice["id"] or data.get("return_revision") != revision)
                ):
                    data["returned_by_notice"] = notice["id"]
                    data["return_revision"] = revision
                    self.repository.update_entity(inspection["id"], inspection["version"], "returned", data)
                    self.audit.record(
                        inspection["id"], actor, "notice_retag", "returned", "returned",
                        {"notice_id": notice["id"], "revision": revision},
                    )
            elif inspection["status"] == "returned" and tag in family:
                data.pop("returned_by_notice", None)
                data.pop("return_revision", None)
                self.repository.update_entity(inspection["id"], inspection["version"], "scheduled", data)
                self.audit.record(
                    inspection["id"], actor, "notice_release", "returned", "scheduled",
                    {"notice_id": notice["id"], "revision": revision},
                )

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

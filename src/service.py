import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


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
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
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

    def _open_disposal_for_notice(self, notice_id):
        for disposal in self.list("disposal"):
            if disposal["data"].get("notice_id") == notice_id and disposal["status"] == "open":
                return disposal
        return None

    def receive_notice(self, actor, data):
        """接收监管平台停用通知，按修订号去重并驱动处置账。

        重复通知按修订号留最新版：同来源通知，高修订号生效、旧版置为失效；
        通知生效时设备转停用、已有许可转待复核、未开始检验退回重排。
        通知一更新，关联处置账失效并重新计算。
        """
        payload = dict(data or {})
        self.rules.validate_create(actor, "notice", payload, self._lookup)
        source_notice_id = payload["source_notice_id"]
        revision = int(payload["revision"])
        existing = self._lookup("notice", "source_notice_id", source_notice_id)
        active = [n for n in existing if n["status"] == "active"]
        if active:
            current = active[0]
        elif existing:
            current = max(existing, key=lambda n: int(n["data"]["revision"]))
        else:
            current = None
        if current:
            current_revision = int(current["data"]["revision"])
            if current_revision == revision:
                return current, self._open_disposal_for_notice(current["id"])
            if current_revision > revision:
                raise ConflictError(
                    "a newer revision (%d) is already active for notice %s"
                    % (current_revision, source_notice_id)
                )
            self.repository.update_entity(current["id"], current["version"], "superseded", current["data"])
            old_disposal = self._open_disposal_for_notice(current["id"])
            if old_disposal:
                self.repository.update_entity(
                    old_disposal["id"], old_disposal["version"], "superseded", old_disposal["data"]
                )
            self.audit.record(current["id"], actor, "notice_superseded", "active", "superseded", {"revision": current_revision})

        notice_id = str(payload.pop("id", "") or uuid4())
        notice = self.repository.create_entity(notice_id, "notice", "active", payload, actor.user_id)
        self.audit.record(
            notice_id, actor, "receive_notice", None, "active",
            {"source_notice_id": source_notice_id, "revision": revision},
        )

        disposal_data = {
            "notice_id": notice_id,
            "equipment_id": payload["equipment_id"],
            "handler_id": None,
            "progress": {},
            "affected_permit_ids": [],
            "affected_inspection_ids": [],
            "remediation_ids": [],
            "reinspection_ids": [],
        }
        disposal_id = str(uuid4())
        disposal = self.repository.create_entity(disposal_id, "disposal", "open", disposal_data, actor.user_id)
        self.audit.record(disposal_id, actor, "open_disposal", None, "open", {"notice_id": notice_id})
        self.process_disposal(actor, disposal_id)
        return self.get(notice_id), self.get(disposal_id)

    def process_disposal(self, actor, disposal_id):
        """重算处置账，逐步推进并持久化进度。

        每一步完成后立即落库进度；若某一步写入失败，已完成的进度保留，
        重试时只补未完成项，已完成步骤幂等跳过。
        """
        disposal = self.get(disposal_id)
        if disposal["kind"] != "disposal":
            raise ValidationError("not a disposal record: " + disposal_id)
        if disposal["status"] == "superseded":
            raise ConflictError("disposal superseded by a newer notice")
        notice = self.get(disposal["data"].get("notice_id"))
        equipment_id = disposal["data"].get("equipment_id")

        def step(name, fn):
            nonlocal disposal
            data = disposal["data"]
            progress = data.get("progress") or {}
            if progress.get(name):
                return
            fn(data)
            progress[name] = True
            data["progress"] = progress
            disposal = self.repository.update_entity(
                disposal["id"], disposal["version"], disposal["status"], data
            )

        def suspend_equipment(data):
            equipment = self.get(equipment_id)
            if equipment["status"] == "in_service":
                self.transition(actor, equipment_id, "suspend", {})

        def review_permits(data):
            affected = list(data.get("affected_permit_ids") or [])
            for permit in self.list("permit"):
                if permit["data"].get("equipment_id") == equipment_id and permit["status"] == "granted":
                    self.transition(actor, permit["id"], "reconsider", {})
                    if permit["id"] not in affected:
                        affected.append(permit["id"])
            data["affected_permit_ids"] = affected

        def reschedule_inspections(data):
            affected = list(data.get("affected_inspection_ids") or [])
            for inspection in self.list("inspection"):
                if inspection["data"].get("equipment_id") == equipment_id and inspection["status"] == "scheduled":
                    self.transition(actor, inspection["id"], "withdraw", {})
                    if inspection["id"] not in affected:
                        affected.append(inspection["id"])
            data["affected_inspection_ids"] = affected

        def link_remediations(data):
            data["remediation_ids"] = [
                r["id"] for r in self.list("remediation")
                if r["data"].get("equipment_id") == equipment_id
            ]

        def link_reinspections(data):
            cutoff = notice["data"].get("effective_at") or notice["data"].get("issued_at") or notice["created_at"]
            reinspection_ids = []
            for inspection in self.list("inspection"):
                if inspection["data"].get("equipment_id") == equipment_id and inspection["status"] == "passed":
                    if str(inspection["data"].get("scheduled_at", "")) >= str(cutoff):
                        reinspection_ids.append(inspection["id"])
            data["reinspection_ids"] = reinspection_ids

        for name, fn in (
            ("suspend_equipment", suspend_equipment),
            ("review_permits", review_permits),
            ("reschedule_inspections", reschedule_inspections),
        ):
            step(name, fn)

        # 关联类步骤为只读记账，每次重算以反映最新状态（复检、整改进度）
        data = disposal["data"]
        link_remediations(data)
        link_reinspections(data)
        disposal = self.repository.update_entity(
            disposal["id"], disposal["version"], disposal["status"], data
        )

        return self.get(disposal_id)

    def claim_disposal(self, actor, disposal_id):
        """认领处置账：先入账的推进，后到的看到承办人。"""
        disposal = self.get(disposal_id)
        if disposal["kind"] != "disposal":
            raise ValidationError("not a disposal record: " + disposal_id)
        if disposal["status"] != "open":
            raise ConflictError("disposal is not open")
        if disposal["data"].get("handler_id"):
            raise ConflictError("disposal already claimed by " + str(disposal["data"]["handler_id"]))
        updated = self.repository.claim_disposal(disposal_id, actor.user_id)
        self.audit.record(disposal_id, actor, "claim", "open", "open", {"handler_id": actor.user_id})
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, InvalidTransition, NotFoundError
from .rules import (
    APPLICATION_ACTION_LABEL,
    CASCADE_GRANT_INVALIDATION,
    INVALIDATION_REASONS,
    RuleEngine,
)


SYSTEM_ACTOR = Actor("system", "admin")


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
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _grants_for_application(self, application_id):
        return self.repository.find_entities(
            "grant", "application_id", application_id
        )

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )

        cascade_grants = []
        if entity["kind"] == "application" and action in CASCADE_GRANT_INVALIDATION:
            cascade_grants = self._grants_for_application(entity_id)
            in_use = [grant for grant in cascade_grants if grant["status"] == "active"]
            if in_use:
                # 使用中的授权不能被静默处理：提示管理员先撤销
                ids = ", ".join(grant["id"] for grant in in_use)
                label = APPLICATION_ACTION_LABEL[action]
                raise InvalidTransition(
                    "申请%s前必须先撤销使用中的授权（%s），未生效授权将在%s后自动失效"
                    % (label, ids, label)
                )

        updated = self.repository.update_entity(entity_id, expected, next_status, {
            **entity["data"],
            **patch,
        })
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )

        if cascade_grants:
            self._cascade_void_grants(entity_id, updated, action)
        return updated

    def _cascade_void_grants(self, application_id, application, action):
        reason = INVALIDATION_REASONS[action]
        for grant in self._grants_for_application(application_id):
            if grant["status"] != "issued":
                continue
            data = dict(grant["data"])
            data["invalid_reason"] = reason
            voided = self.repository.update_entity(
                grant["id"], grant["version"], "voided", data
            )
            self.audit.record(
                grant["id"],
                SYSTEM_ACTOR,
                "void",
                grant["status"],
                voided["status"],
                {
                    "cascade_from": "application",
                    "application_id": application_id,
                    "application_action": action,
                    "reason": reason,
                },
            )

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

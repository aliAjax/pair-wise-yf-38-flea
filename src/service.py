from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, GrantsInUseError, NotFoundError
from .rules import CLOSING_ACTIONS, RuleEngine


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

        if kind == "grant":
            # Bind the grant to the exact approved application version and
            # frozen purpose snapshot it was issued against.
            application = self._lookup("application", "id", payload.get("application_id"))
            application = application[0] if application else None
            if not application:
                raise NotFoundError("application not found")
            payload.update(self.rules.binding_for_grant(application))

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

        if entity["kind"] == "application" and action in CLOSING_ACTIONS:
            return self._close_application(actor, entity, action, next_status, patch, expected)

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

    def _close_application(self, actor, entity, action, next_status, patch, expected):
        """Withdraw / reject / expire an application and cascade onto grants."""
        linked = self.repository.find_entities("grant", "application_id", entity["id"])
        active = [grant for grant in linked if grant["status"] == "active"]
        if active:
            raise GrantsInUseError(
                "application has %d active grant(s); revoke them before %s"
                % (len(active), action)
            )

        reason_code = CLOSING_ACTIONS[action]
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)

        # Unused grants (issued but never activated) lose their effect
        # immediately with a visible reason.
        invalidated = []
        for grant in linked:
            if grant["status"] != "issued":
                continue
            grant_data = dict(grant["data"])
            grant_data["invalid_reason"] = reason_code
            grant_data["invalid_detail"] = patch.get("reason", action)
            grant_data["invalidated_by"] = actor.user_id
            grant_data["invalidated_at"] = updated["updated_at"]
            self.repository.update_entity(grant["id"], grant["version"], "invalid", grant_data)
            self.audit.record(
                grant["id"],
                actor,
                "invalidate",
                grant["status"],
                "invalid",
                {"reason": reason_code, "source_application": entity["id"]},
            )
            invalidated.append(grant["id"])

        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch, "invalidated_grants": invalidated},
        )
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

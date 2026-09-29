from datetime import datetime, timezone
from hashlib import sha256

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def purpose_version(purpose):
    digest = sha256(str(purpose).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:12]


def _today():
    return datetime.now(timezone.utc).date()


def _as_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        raise ValidationError("%s must be an ISO date" % field)


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")
    if not data.get("applicant_org", "").strip():
        raise ValidationError("applicant_org is required")


def _validate_approve(actor, entity, data, lookup):
    applicant_org = entity["data"].get("applicant_org", "")
    if not applicant_org:
        raise ValidationError("applicant_org is required on the application")

    reviewers = data.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 3:
        raise ValidationError("exactly three committee reviewers are required")
    normalized = []
    seen = set()
    for item in reviewers:
        if not isinstance(item, dict):
            raise ValidationError("each reviewer needs reviewer_id and reviewer_org")
        reviewer_id = str(item.get("reviewer_id", "")).strip()
        reviewer_org = str(item.get("reviewer_org", "")).strip()
        if not reviewer_id or not reviewer_org:
            raise ValidationError("each reviewer needs reviewer_id and reviewer_org")
        if reviewer_id in seen:
            raise ValidationError("the three reviewers must be distinct people")
        seen.add(reviewer_id)
        if item.get("declared_conflict"):
            raise PermissionDenied(
                "reviewer %s declared a conflict of interest" % reviewer_id
            )
        if reviewer_org == applicant_org:
            raise PermissionDenied(
                "reviewer %s shares the applicant organization and is not independent"
                % reviewer_id
            )
        normalized.append({"reviewer_id": reviewer_id, "reviewer_org": reviewer_org})

    approval_expires = _as_date(data.get("expires_at"), "expires_at")
    if approval_expires < _today():
        raise ValidationError("approval expires_at must not be in the past")

    # The approval freezes the application data and purpose into a versioned
    # snapshot. Later grants can only bind to this immutable snapshot, which
    # lives on the new entity version produced by the approve transition.
    snapshot = dict(entity["data"])
    snapshot["purpose"] = entity["data"].get("purpose")
    return {
        "reviewers": normalized,
        "approved_purpose_version": purpose_version(entity["data"].get("purpose")),
        "approved_application_version": entity["version"] + 1,
        "approval_snapshot": snapshot,
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _validate_amend(actor, entity, data, lookup):
    if "purpose" not in data and "expires_at" not in data:
        raise ValidationError("amend requires a new purpose or expires_at")
    patch = {}
    if "purpose" in data:
        if not str(data.get("purpose", "")).strip():
            raise ValidationError("purpose is required")
        patch["purpose"] = data["purpose"]
    if "expires_at" in data:
        expires = _as_date(data.get("expires_at"), "expires_at")
        if expires < _today():
            raise ValidationError("expires_at must not be in the past")
        patch["expires_at"] = data["expires_at"]
    return patch


def _validate_grant_create(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("application does not exist")
    if application["status"] != "approved":
        raise ValidationError(
            "grant can only bind to an approved application, current status: "
            + application["status"]
        )
    if data.get("dataset_id") != application["data"].get("dataset_id"):
        raise ValidationError("grant dataset must match the approved application")
    if not str(data.get("recipient_org", "")).strip():
        raise ValidationError("recipient_org is required")
    return {"application": application}


def _validate_grant_activate(actor, entity, data, lookup):
    starts_at = _as_date(data.get("starts_at"), "starts_at")
    expires_at = _as_date(data.get("expires_at"), "expires_at")
    if expires_at < starts_at:
        raise ValidationError("grant expiry must be after start")

    application = _find_one(lookup, "application", "id", entity["data"].get("application_id"))
    if not application:
        raise ValidationError("source application no longer exists")

    # Re-check the source application right before the grant takes effect.
    if application["status"] != "approved":
        raise InvalidTransition(
            "source application is %s, grant cannot activate" % application["status"]
        )

    bound_version = entity["data"].get("bound_application_version")
    if bound_version is not None and application["version"] != int(bound_version):
        raise ConflictError(
            "application changed after approval (bound v%s, current v%s); "
            "require a new approval before activation" % (bound_version, application["version"])
        )

    bound_purpose = entity["data"].get("bound_purpose_version")
    current_purpose = purpose_version(application["data"].get("purpose"))
    if bound_purpose and current_purpose != bound_purpose:
        raise ConflictError(
            "purpose no longer matches the approved version; grant cannot activate"
        )

    # Re-check the applicant institution against the approved snapshot.
    snapshot = application["data"].get("approval_snapshot") or {}
    approved_org = snapshot.get("applicant_org") or application["data"].get("applicant_org")
    if approved_org and entity["data"].get("recipient_org") != approved_org:
        raise PermissionDenied(
            "recipient_org does not match the applicant organization on the approval"
        )

    # Re-check the validity window: grant must be within the approval window.
    approval_expires = application["data"].get("expires_at")
    if approval_expires and expires_at > _as_date(approval_expires, "expires_at"):
        raise ValidationError(
            "grant expiry %s exceeds approval expiry %s"
            % (data.get("expires_at"), approval_expires)
        )
    if expires_at < _today():
        raise ValidationError("grant expires_at must not be in the past")

    return {"activated_by": actor.user_id, "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _validate_grant_invalidate(actor, entity, data, lookup):
    if "reason" not in data or not str(data.get("reason", "")).strip():
        raise ValidationError("reason is required")
    return {}


def _validate_grant_revoke(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("reason is required")
    return {"invalid_reason": "admin_revoked", "revoked_by": actor.user_id}


def _validate_grant_expire(actor, entity, data, lookup):
    return {"invalid_reason": "grant_expired"}


CUSTOM_CREATE = {
    "dataset": _validate_dataset,
    "application": _validate_application,
    "grant": _validate_grant_create,
}
CUSTOM_TRANSITIONS = {
    ("application", "approve"): _validate_approve,
    ("application", "amend"): _validate_amend,
    ("grant", "activate"): _validate_grant_activate,
    ("grant", "invalidate"): _validate_grant_invalidate,
    ("grant", "revoke"): _validate_grant_revoke,
    ("grant", "expire"): _validate_grant_expire,
}

# Actions that close an application and cascade onto its unused grants.
CLOSING_ACTIONS = {
    "withdraw": "application_withdrawn",
    "reject": "application_rejected",
    "expire": "application_expired",
}


class RuleEngine:
    ALIASES = {"datasets": "dataset", "applications": "application", "grants": "grant"}
    INITIAL_STATUS = {
        "dataset": "registered",
        "application": "draft",
        "grant": "issued",
    }
    TRANSITIONS = {
        "dataset": {
            "restrict": (("registered",), "restricted"),
            "publish": (("restricted",), "published"),
        },
        "application": {
            "submit": (("draft",), "submitted"),
            "review": (("submitted", "approved"), "under_review"),
            "approve": (("under_review",), "approved"),
            "amend": (("approved",), "approved"),
            "reject": (
                ("submitted", "under_review", "approved"),
                "rejected",
            ),
            "withdraw": (
                ("submitted", "under_review", "approved"),
                "withdrawn",
            ),
            "expire": (("approved",), "expired"),
        },
        "grant": {
            "activate": (("issued",), "active"),
            "revoke": (("active",), "revoked"),
            "invalidate": (("issued",), "invalid"),
            "expire": (("active",), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "dataset": ("name", "access_policy"),
        "application": ("dataset_id", "applicant_id", "applicant_org", "purpose"),
        "grant": ("application_id", "dataset_id", "recipient", "recipient_org"),
    }
    ACTION_REQUIRED = {
        ("dataset", "restrict"): ("reason",),
        ("application", "review"): ("committee_id",),
        ("application", "approve"): ("reviewers", "terms", "expires_at"),
        ("application", "amend"): ("reason",),
        ("application", "reject"): ("reason",),
        ("application", "withdraw"): ("reason",),
        ("application", "expire"): ("reason",),
        ("grant", "activate"): ("starts_at", "expires_at"),
        ("grant", "revoke"): ("reason",),
        ("grant", "invalidate"): ("reason",),
        ("grant", "expire"): ("expired_at",),
    }
    CREATE_ROLES = {
        "dataset": ("admin", "committee"),
        "application": ("admin", "applicant"),
        "grant": ("admin", "committee"),
    }
    ROLE_ACTIONS = {
        "restrict": ("admin", "committee"),
        "publish": ("admin", "committee"),
        "submit": ("admin", "applicant"),
        "review": ("admin", "committee"),
        "approve": ("admin", "committee"),
        "amend": ("admin", "committee"),
        "reject": ("admin", "committee"),
        "withdraw": ("admin", "applicant"),
        "expire": ("admin", "committee"),
        "activate": ("admin", "committee"),
        "revoke": ("admin", "committee"),
        "invalidate": ("admin", "committee"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        extras = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if isinstance(extras, dict):
            # Only carry across custom extras that are not part of the user
            # payload (e.g. bound application references).
            for key, value in extras.items():
                if key != "application":
                    payload.setdefault(key, value)
        return payload

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def binding_for_grant(self, application):
        """Versioned binding stamped on a freshly issued grant."""
        return {
            "bound_application_version": application["version"],
            "bound_purpose_version": application["data"].get("approved_purpose_version"),
            "bound_approval_expires_at": application["data"].get("expires_at"),
            "bound_applicant_org": application["data"].get("applicant_org"),
            "bound_applicant_id": application["data"].get("applicant_id"),
            "bound_dataset_id": application["data"].get("dataset_id"),
        }

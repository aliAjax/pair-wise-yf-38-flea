from datetime import date, datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 申请进入终态/失效时，对已发授权的联动动作
CASCADE_GRANT_INVALIDATION = {"withdraw", "reject", "expire"}
APPLICATION_ACTION_LABEL = {"withdraw": "撤回", "reject": "驳回", "expire": "过期"}
INVALIDATION_REASONS = {
    "withdraw": "来源申请已撤回，未生效授权自动失效",
    "reject": "来源申请已驳回，未生效授权自动失效",
    "expire": "来源申请已过期（超出批准有效期），未生效授权自动失效",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value, label):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise ValidationError("%s 日期格式无效，应为 YYYY-MM-DD" % label)


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not str(data.get("purpose", "")).strip():
        raise ValidationError("purpose is required")
    if not str(data.get("institution", "")).strip():
        raise ValidationError("institution is required")
    # 用途从创建起带版本号，每次修订用途时递增，批准时冻结该版本
    return {"purpose_version": 1}


def _normalize_reviewers(data, applicant_id):
    """批准必须记录三名互不相同、且明确无利益关联的委员。"""
    raw = data.get("reviewers")
    if raw is None:
        raw = data.get("approvals") or []  # 兼容旧请求字段
    records = []
    seen = set()
    for item in raw:
        if isinstance(item, dict):
            reviewer_id = str(item.get("committee_id") or item.get("id") or "").strip()
            conflicted = bool(item.get("conflict_of_interest", False))
        else:
            reviewer_id = str(item).strip()
            conflicted = False
        if not reviewer_id:
            raise ValidationError("每名委员都必须有身份标识")
        if conflicted:
            raise PermissionDenied("委员 %s 申报了利益关联，不能参与批准" % reviewer_id)
        if reviewer_id == str(applicant_id or ""):
            raise PermissionDenied("申请人本人不能作为无利益关联委员参与批准")
        if reviewer_id in seen:
            raise ValidationError("委员 %s 重复，批准需要三名不同的委员" % reviewer_id)
        seen.add(reviewer_id)
        records.append({"committee_id": reviewer_id, "conflict_of_interest": False})
    if len(records) < 3:
        raise ValidationError("批准至少需要三名无利益关联的委员")
    return records


def _validate_approve(actor, entity, data, lookup):
    current = entity["data"]
    if data.get("conflict_of_interest"):
        raise PermissionDenied("存在利益关联的操作者不能批准访问申请")
    reviewers = _normalize_reviewers(data, current.get("applicant_id"))
    expires_at = _parse_date(data.get("expires_at"), "expires_at")
    if expires_at < date.today():
        raise ValidationError("批准有效期不能早于当天")

    # 批准即冻结：记录三名委员、申请数据快照与用途版本。
    # 授权只能绑定该快照，事后用途/有效期被改不影响已冻结内容，但生效核对会发现漂移。
    purpose_version = int(current.get("purpose_version", 1))
    approved_version = int(entity["version"]) + 1
    snapshot = {
        "version": approved_version,
        "reviewers": reviewers,
        "applicant_id": current.get("applicant_id"),
        "institution": current.get("institution"),
        "dataset_id": current.get("dataset_id"),
        "purpose": current.get("purpose"),
        "purpose_version": purpose_version,
        "terms": data.get("terms"),
        "expires_at": str(data.get("expires_at"))[:10],
        "approved_by": actor.user_id,
        "approved_at": _now(),
    }
    patch = {
        "terms": data.get("terms"),
        "expires_at": str(data.get("expires_at"))[:10],
        "reviewers": reviewers,
        "approved": snapshot,
        "purpose_version": purpose_version,
    }
    return patch


def _validate_amend(actor, entity, data, lookup):
    """批准后修订用途/机构/有效期：当前数据被更新，冻结的批准快照保持不变。"""
    current = entity["data"]
    patch = {}
    changes = {}
    for field in ("purpose", "institution"):
        value = data.get(field)
        if value is not None and str(value).strip():
            value = str(value).strip()
            if value != str(current.get(field, "")):
                changes[field] = {"from": current.get(field), "to": value}
            patch[field] = value
    if data.get("expires_at"):
        new_expiry = _parse_date(data.get("expires_at"), "expires_at")
        expiry_text = str(data.get("expires_at"))[:10]
        if new_expiry < date.today():
            raise ValidationError("新的批准有效期不能早于当天")
        if expiry_text != str(current.get("expires_at", ""))[:10]:
            changes["expires_at"] = {"from": current.get("expires_at"), "to": expiry_text}
        patch["expires_at"] = expiry_text
    if not changes:
        raise ValidationError("修订至少需要变更用途、申请人机构或有效期之一")
    purpose_version = int(current.get("purpose_version", 1))
    if "purpose" in changes:
        purpose_version += 1
    patch["purpose_version"] = purpose_version
    history = list(current.get("amendments") or [])
    history.append({"at": _now(), "by": actor.user_id, "changes": changes})
    patch["amendments"] = history
    return patch


def _validate_application_expire(actor, entity, data, lookup):
    # 通常由到期扫描任务在 expires_at 过后触发；管理员也可手工登记过期。
    return {}


def _validate_grant_create(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("来源申请不存在")
    app_data = application["data"]
    if application["status"] != "approved" or not app_data.get("approved"):
        raise ValidationError(
            "授权只能绑定已批准的申请版本，当前申请状态：%s" % application["status"]
        )
    approved = app_data["approved"]
    if data.get("dataset_id") != app_data.get("dataset_id"):
        raise ValidationError("授权数据集必须与来源申请一致")
    requested_version = data.get("application_version")
    if requested_version is not None and int(requested_version) != int(approved["version"]):
        raise ConflictError(
            "授权只能绑定当前已批准版本 v%s，不能绑定 v%s"
            % (approved["version"], requested_version)
        )
    institution = str(data.get("recipient_institution", "")).strip()
    if not institution:
        raise ValidationError("recipient_institution is required")
    if institution != str(app_data.get("institution", "")).strip():
        raise ValidationError("授权接收机构必须与申请人机构一致")
    approval_expiry = _parse_date(approved["expires_at"], "approved.expires_at")
    if approval_expiry < date.today():
        raise ValidationError("来源批准已过有效期，不能签发授权")

    # 绑定批准时的冻结快照，授权此后不再随申请当前数据漂移
    binding = {
        "application_version": int(approved["version"]),
        "applicant_id": approved.get("applicant_id"),
        "institution": approved.get("institution"),
        "purpose": approved.get("purpose"),
        "purpose_version": approved.get("purpose_version"),
        "terms": approved.get("terms"),
        "expires_at": approved.get("expires_at"),
        "approved_at": approved.get("approved_at"),
    }
    return {"application_version": binding["application_version"], "bound_approval": binding}


def valid_grant_window(expires_at, as_of):
    return str(expires_at)[:10] >= str(as_of)[:10]


def _validate_grant_activate(actor, entity, data, lookup):
    starts_at = _parse_date(data.get("starts_at"), "starts_at")
    expires_at = _parse_date(data.get("expires_at"), "expires_at")
    if expires_at < starts_at:
        raise ValidationError("授权到期日必须晚于或等于生效日")

    grant_data = entity["data"]
    application = _find_one(lookup, "application", "id", grant_data.get("application_id"))
    if not application:
        raise ValidationError("来源申请不存在，授权不能生效")
    bound = grant_data.get("bound_approval")
    if not bound:
        raise ValidationError("授权未绑定已批准版本，不能生效")
    if application["status"] != "approved":
        raise ValidationError(
            "来源申请当前状态为 %s，授权不能生效；如需访问请重新提出申请"
            % application["status"]
        )

    # 生效前再次核对：申请人机构、用途（含用途版本）、批准有效期必须与绑定快照一致
    current = application["data"]
    drifts = []
    if str(current.get("institution", "")) != str(bound.get("institution", "")):
        drifts.append("申请人机构")
    if str(current.get("purpose", "")) != str(bound.get("purpose", "")):
        drifts.append("数据用途")
    if int(current.get("purpose_version", 1)) != int(bound.get("purpose_version", 1)):
        drifts.append("用途版本")
    if str(current.get("expires_at", ""))[:10] != str(bound.get("expires_at", ""))[:10]:
        drifts.append("批准有效期")
    current_approved = current.get("approved") or {}
    if int(current_approved.get("version", bound.get("application_version"))) != int(
        bound.get("application_version")
    ):
        drifts.append("批准版本")
    if drifts:
        raise ValidationError(
            "绑定的批准内容已变更（%s），授权不能照旧生效，请按当前批准版本重新申请授权"
            % "、".join(drifts)
        )

    if str(grant_data.get("recipient_institution", "")) != str(
        current.get("institution", "")
    ):
        raise ValidationError("生效核对失败：授权接收机构与申请人机构不一致")
    if expires_at > _parse_date(bound["expires_at"], "approved.expires_at"):
        raise ValidationError(
            "生效核对失败：授权有效期超出批准有效期（至 %s）" % bound["expires_at"]
        )
    if expires_at < date.today():
        raise ValidationError("生效核对失败：授权已过有效期")
    return {"activated_by": actor.user_id, "validated_at": _now()}


def _validate_grant_revoke(actor, entity, data, lookup):
    return {"invalid_reason": "管理员撤销：%s" % data.get("reason", "")}


def _validate_grant_expire(actor, entity, data, lookup):
    return {"invalid_reason": "授权已到期（%s）" % data.get("expired_at", "")}


def _validate_grant_void(actor, entity, data, lookup):
    return {"invalid_reason": str(data.get("reason", "manual void"))}


CUSTOM_CREATE = {
    "dataset": _validate_dataset,
    "application": _validate_application,
    "grant": _validate_grant_create,
}
CUSTOM_TRANSITIONS = {
    ("application", "approve"): _validate_approve,
    ("application", "amend"): _validate_amend,
    ("application", "expire"): _validate_application_expire,
    ("grant", "activate"): _validate_grant_activate,
    ("grant", "revoke"): _validate_grant_revoke,
    ("grant", "expire"): _validate_grant_expire,
    ("grant", "void"): _validate_grant_void,
}


class RuleEngine:
    ALIASES = {'datasets': 'dataset', 'applications': 'application', 'grants': 'grant'}
    INITIAL_STATUS = {'dataset': 'registered', 'application': 'draft', 'grant': 'issued'}
    TRANSITIONS = {
        'dataset': {
            'restrict': (('registered',), 'restricted'),
            'publish': (('restricted',), 'published'),
        },
        'application': {
            'submit': (('draft',), 'submitted'),
            'review': (('submitted',), 'under_review'),
            'approve': (('under_review',), 'approved'),
            'reject': (('under_review', 'approved'), 'rejected'),
            'withdraw': (('submitted', 'under_review', 'approved'), 'withdrawn'),
            'reconsider': (('approved',), 'under_review'),
            'amend': (('approved',), 'approved'),
            'expire': (('approved',), 'expired'),
        },
        'grant': {
            'activate': (('issued',), 'active'),
            'revoke': (('active',), 'revoked'),
            'expire': (('active',), 'expired'),
            'void': (('issued',), 'voided'),
        },
    }
    CREATE_REQUIRED = {
        'dataset': ('name', 'access_policy'),
        'application': ('dataset_id', 'applicant_id', 'institution', 'purpose'),
        'grant': ('application_id', 'dataset_id', 'recipient', 'recipient_institution'),
    }
    ACTION_REQUIRED = {
        ('dataset', 'restrict'): ('reason',),
        ('application', 'review'): ('committee_id',),
        ('application', 'approve'): ('terms', 'expires_at'),
        ('application', 'reject'): ('reason',),
        ('application', 'withdraw'): ('reason',),
        ('grant', 'activate'): ('starts_at', 'expires_at'),
        ('grant', 'revoke'): ('reason',),
        ('grant', 'expire'): ('expired_at',),
        ('grant', 'void'): ('reason',),
    }
    CREATE_ROLES = {
        'dataset': ('admin', 'committee'),
        'application': ('admin', 'applicant'),
        'grant': ('admin', 'committee'),
    }
    ROLE_ACTIONS = {
        'restrict': ('admin', 'committee'),
        'publish': ('admin', 'committee'),
        'submit': ('admin', 'applicant'),
        'review': ('admin', 'committee'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'withdraw': ('admin', 'applicant'),
        'amend': ('admin', 'committee'),
        'reconsider': ('admin', 'committee'),
        'expire': ('admin', 'committee'),
        'activate': ('admin', 'committee'),
        'revoke': ('admin', 'committee'),
        'void': ('admin', 'committee'),
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
        payload = dict(data)
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            extra = custom(actor, payload, lookup) or {}
            payload.update(extra)
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
        patch = dict(data)
        if custom:
            extra = custom(actor, entity, data, lookup) or {}
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None

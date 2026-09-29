import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

FAR_FUTURE = "2099-12-31"
GRANT_END = "2098-12-31"
THREE_REVIEWERS = [
    {"committee_id": "r1"},
    {"committee_id": "r2"},
    {"committee_id": "r3"},
]


class GovernanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_dataset(self):
        return self.service.create(
            self.admin, "dataset",
            {"name": "D", "access_policy": "controlled"},
        )["id"]

    def _submit_application(self, applicant_id="APP-1", institution="Genome Institute",
                            purpose="variant analysis"):
        dataset_id = self._create_dataset()
        application = self.service.create(
            self.admin, "application",
            {
                "dataset_id": dataset_id,
                "applicant_id": applicant_id,
                "institution": institution,
                "purpose": purpose,
            },
        )
        self.service.transition(self.admin, application["id"], "submit", {})
        self.service.transition(
            self.admin, application["id"], "review", {"committee_id": "c-1"}
        )
        return application, dataset_id

    def _approved_application(self, **kwargs):
        """返回 (application, dataset_id)，申请已被三名无利益关联委员批准。"""
        application, dataset_id = self._submit_application(**kwargs)
        approved = self.service.transition(
            self.admin,
            application["id"],
            "approve",
            {"reviewers": THREE_REVIEWERS, "terms": "noncommercial",
             "expires_at": FAR_FUTURE},
        )
        return approved, dataset_id

    def _issue_grant(self, application, dataset_id, institution="Genome Institute",
                     application_version=None):
        data = {
            "application_id": application["id"],
            "dataset_id": dataset_id,
            "recipient": "researcher-1",
            "recipient_institution": institution,
        }
        if application_version is not None:
            data["application_version"] = application_version
        return self.service.create(self.admin, "grant", data)

    def test_approval_records_three_independent_reviewers_and_snapshot(self):
        approved, _ = self._approved_application()
        snapshot = approved["data"]["approved"]
        self.assertEqual(snapshot["version"], approved["version"])
        self.assertEqual(snapshot["purpose"], "variant analysis")
        self.assertEqual(snapshot["purpose_version"], 1)
        self.assertEqual(snapshot["institution"], "Genome Institute")
        self.assertEqual(snapshot["expires_at"], FAR_FUTURE)
        self.assertEqual(
            [item["committee_id"] for item in snapshot["reviewers"]],
            ["r1", "r2", "r3"],
        )
        self.assertTrue(
            all(not item["conflict_of_interest"] for item in snapshot["reviewers"])
        )

    def test_conflicted_or_duplicate_or_applicant_reviewers_are_rejected(self):
        def attempt(reviewers):
            application, _ = self._submit_application()
            self.service.transition(
                self.admin, application["id"], "approve",
                {"reviewers": reviewers, "terms": "t", "expires_at": FAR_FUTURE},
            )

        with self.assertRaises(PermissionDenied):
            attempt([
                {"committee_id": "r1"},
                {"committee_id": "r2", "conflict_of_interest": True},
                {"committee_id": "r3"},
            ])
        with self.assertRaises(ValidationError):
            attempt([{"committee_id": "r1"}, {"committee_id": "r1"}])
        with self.assertRaises(PermissionDenied):
            # 申请人本人不得充当“无利益关联委员”
            attempt([
                {"committee_id": "APP-1"},
                {"committee_id": "r2"},
                {"committee_id": "r3"},
            ])

    def test_grant_cannot_bind_unapproved_or_stale_version(self):
        dataset_id = self._create_dataset()
        draft = self.service.create(
            self.admin, "application",
            {
                "dataset_id": dataset_id,
                "applicant_id": "APP-9",
                "institution": "Lab",
                "purpose": "p",
            },
        )
        with self.assertRaises(ValidationError):
            self._issue_grant(draft, dataset_id, institution="Lab")

        approved, bound_dataset = self._approved_application()
        other_dataset = self._create_dataset()
        with self.assertRaises(ValidationError):  # 数据集必须一致
            self._issue_grant(approved, other_dataset)
        with self.assertRaises(ValidationError):  # 机构必须一致
            self._issue_grant(approved, bound_dataset, institution="Other Lab")
        with self.assertRaises(ConflictError):  # 不能绑定旧版本
            self._issue_grant(
                approved, bound_dataset,
                application_version=approved["version"] - 1,
            )

    def test_activation_rechecks_purpose_drift_after_amendment(self):
        approved, dataset_id = self._approved_application()
        grant = self._issue_grant(approved, dataset_id)
        binding = grant["data"]["bound_approval"]
        self.assertEqual(binding["application_version"], approved["version"])
        self.assertEqual(binding["purpose"], "variant analysis")

        # 批准后修订用途：冻结快照不变，当前数据带新版本号
        amended = self.service.transition(
            self.admin, approved["id"], "amend", {"purpose": "GWAS re-use"}
        )
        self.assertEqual(amended["data"]["purpose_version"], 2)
        self.assertEqual(amended["data"]["approved"]["purpose"], "variant analysis")

        # 已签发授权仍绑定旧用途版本，生效核对必须发现漂移并拒绝
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.admin, grant["id"], "activate",
                {"starts_at": "2026-09-29", "expires_at": GRANT_END},
            )
        message = str(ctx.exception)
        self.assertIn("用途", message)
        self.assertIn("重新申请授权", message)
        self.assertEqual(self.service.get(grant["id"])["status"], "issued")

    def test_activation_window_cannot_exceed_approval_window(self):
        approved, dataset_id = self._approved_application()
        grant = self._issue_grant(approved, dataset_id)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.admin, grant["id"], "activate",
                {"starts_at": "2026-09-29", "expires_at": "2100-01-01"},
            )
        self.assertIn("批准有效期", str(ctx.exception))

    def test_reconsider_then_reapprove_produces_new_version(self):
        approved, dataset_id = self._approved_application()
        old_grant = self._issue_grant(approved, dataset_id)

        # 修订用途后重新审查、重新批准：生成新的冻结快照版本
        self.service.transition(
            self.admin, approved["id"], "amend", {"purpose": "GWAS re-use"}
        )
        reconsidered = self.service.transition(
            self.admin, approved["id"], "reconsider", {}
        )
        self.assertEqual(reconsidered["status"], "under_review")
        reapproved = self.service.transition(
            self.admin, reconsidered["id"], "approve",
            {"reviewers": THREE_REVIEWERS, "terms": "noncommercial",
             "expires_at": FAR_FUTURE},
        )
        self.assertEqual(reapproved["status"], "approved")
        self.assertEqual(reapproved["data"]["approved"]["version"],
                         reapproved["version"])
        self.assertEqual(reapproved["data"]["approved"]["purpose"], "GWAS re-use")

        # 旧授权仍绑定旧版本，生效被拒；按当前版本新签发的授权可以生效
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, old_grant["id"], "activate",
                {"starts_at": "2026-09-29", "expires_at": GRANT_END},
            )
        new_grant = self._issue_grant(reapproved, dataset_id)
        self.assertEqual(new_grant["data"]["bound_approval"]["application_version"],
                         reapproved["data"]["approved"]["version"])
        active = self.service.transition(
            self.admin, new_grant["id"], "activate",
            {"starts_at": "2026-09-29", "expires_at": GRANT_END},
        )
        self.assertEqual(active["status"], "active")

    def test_application_expiry_voids_unused_grant_but_blocks_active(self):
        approved, dataset_id = self._approved_application()
        unused = self._issue_grant(approved, dataset_id)
        active_grant = self._issue_grant(approved, dataset_id)
        active = self.service.transition(
            self.admin, active_grant["id"], "activate",
            {"starts_at": "2026-09-29", "expires_at": GRANT_END},
        )
        self.assertEqual(active["status"], "active")

        # 使用中：申请不能过期，提示管理员先撤销；申请与授权均保持原状
        with self.assertRaises(InvalidTransition) as ctx:
            self.service.transition(self.admin, approved["id"], "expire", {})
        self.assertIn("先撤销", str(ctx.exception))
        self.assertEqual(self.service.get(approved["id"])["status"], "approved")
        self.assertEqual(self.service.get(unused["id"])["status"], "issued")

        # 管理员先撤销使用中的授权，再登记申请过期：未生效授权自动失效
        self.service.transition(
            self.admin, active["id"], "revoke", {"reason": "review"}
        )
        expired_app = self.service.transition(
            self.admin, approved["id"], "expire", {}
        )
        self.assertEqual(expired_app["status"], "expired")
        voided = self.service.get(unused["id"])
        self.assertEqual(voided["status"], "voided")
        self.assertIn("过期", voided["data"]["invalid_reason"])

        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, unused["id"], "activate",
                {"starts_at": "2026-09-29", "expires_at": GRANT_END},
            )

    def test_withdraw_cascades_void_with_audit_reason(self):
        approved, dataset_id = self._approved_application()
        grant = self._issue_grant(approved, dataset_id)
        self.service.transition(
            self.admin, approved["id"], "withdraw", {"reason": "applicant left"}
        )
        voided = self.service.get(grant["id"])
        self.assertEqual(voided["status"], "voided")
        self.assertIn("撤回", voided["data"]["invalid_reason"])
        entries = [item for item in self.service.audit_log(grant["id"])
                   if item["action"] == "void"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"]["application_action"], "withdraw")
        self.assertEqual(entries[0]["actor_id"], "system")

    def test_reject_cascades_void_unused_grant(self):
        approved, dataset_id = self._approved_application()
        grant = self._issue_grant(approved, dataset_id)
        self.service.transition(
            self.admin, approved["id"], "reject", {"reason": "new evidence"}
        )
        voided = self.service.get(grant["id"])
        self.assertEqual(voided["status"], "voided")
        self.assertIn("驳回", voided["data"]["invalid_reason"])

    def test_happy_path_activation_records_recheck(self):
        approved, dataset_id = self._approved_application()
        grant = self._issue_grant(approved, dataset_id)
        active = self.service.transition(
            self.admin, grant["id"], "activate",
            {"starts_at": "2026-09-29", "expires_at": GRANT_END},
        )
        self.assertEqual(active["status"], "active")
        self.assertIn("validated_at", active["data"])
        self.assertEqual(active["data"]["activated_by"], "admin-1")


if __name__ == "__main__":
    unittest.main()

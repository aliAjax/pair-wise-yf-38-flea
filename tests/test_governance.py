import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    GrantsInUseError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


REVIEWERS = [
    {"reviewer_id": "r1", "reviewer_org": "university-a"},
    {"reviewer_id": "r2", "reviewer_org": "university-b"},
    {"reviewer_id": "r3", "reviewer_org": "hospital-c"},
]


class GovernanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.committee = Actor("committee-1", "committee")
        self.applicant = Actor("applicant-1", "applicant")

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_application(
        self,
        purpose="variant analysis",
        expires_at="2099-01-01",
        applicant_org="genomics-lab-x",
    ):
        dataset = self.service.create(
            self.admin,
            "dataset",
            {"name": "Cohort", "access_policy": "controlled"},
        )
        application = self.service.create(
            self.applicant,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "APP-1",
                "applicant_org": applicant_org,
                "purpose": purpose,
            },
        )
        self.service.transition(self.applicant, application["id"], "submit", {})
        self.service.transition(
            self.committee,
            application["id"],
            "review",
            {"committee_id": "committee-a"},
        )
        approved = self.service.transition(
            self.committee,
            application["id"],
            "approve",
            {"reviewers": REVIEWERS, "terms": "noncommercial", "expires_at": expires_at},
        )
        return dataset, approved

    def _issued_grant(self, application, dataset=None, recipient_org="genomics-lab-x"):
        return self.service.create(
            self.admin,
            "grant",
            {
                "application_id": application["id"],
                "dataset_id": (dataset["id"] if dataset else application["data"]["dataset_id"]),
                "recipient": "researcher-1",
                "recipient_org": recipient_org,
            },
        )

    def test_approval_records_three_independent_reviewers_and_snapshot(self):
        _, approved = self._approved_application()
        data = approved["data"]
        self.assertEqual(len(data["reviewers"]), 3)
        self.assertEqual(
            {item["reviewer_org"] for item in data["reviewers"]},
            {"university-a", "university-b", "hospital-c"},
        )
        self.assertEqual(data["approved_application_version"], approved["version"])
        self.assertIn("approval_snapshot", data)
        self.assertEqual(data["approval_snapshot"]["purpose"], "variant analysis")
        self.assertTrue(data["approved_purpose_version"].startswith("sha256:"))

    def test_approval_requires_three_distinct_reviewers(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "C", "access_policy": "controlled"}
        )
        app = self.service.create(
            self.applicant,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "APP-1",
                "applicant_org": "lab-x",
                "purpose": "p",
            },
        )
        self.service.transition(self.applicant, app["id"], "submit", {})
        self.service.transition(self.committee, app["id"], "review", {"committee_id": "c"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.committee,
                app["id"],
                "approve",
                {
                    "reviewers": [
                        {"reviewer_id": "r1", "reviewer_org": "u-a"},
                        {"reviewer_id": "r1", "reviewer_org": "u-b"},
                        {"reviewer_id": "r3", "reviewer_org": "u-c"},
                    ],
                    "terms": "t",
                    "expires_at": "2099-01-01",
                },
            )

    def test_reviewer_from_applicant_org_is_rejected(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "C", "access_policy": "controlled"}
        )
        app = self.service.create(
            self.applicant,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "APP-1",
                "applicant_org": "lab-x",
                "purpose": "p",
            },
        )
        self.service.transition(self.applicant, app["id"], "submit", {})
        self.service.transition(self.committee, app["id"], "review", {"committee_id": "c"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.committee,
                app["id"],
                "approve",
                {
                    "reviewers": [
                        {"reviewer_id": "r1", "reviewer_org": "lab-x"},
                        {"reviewer_id": "r2", "reviewer_org": "u-b"},
                        {"reviewer_id": "r3", "reviewer_org": "u-c"},
                    ],
                    "terms": "t",
                    "expires_at": "2099-01-01",
                },
            )

    def test_declared_conflict_reviewer_is_rejected(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "C", "access_policy": "controlled"}
        )
        app = self.service.create(
            self.applicant,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "APP-1",
                "applicant_org": "lab-x",
                "purpose": "p",
            },
        )
        self.service.transition(self.applicant, app["id"], "submit", {})
        self.service.transition(self.committee, app["id"], "review", {"committee_id": "c"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.committee,
                app["id"],
                "approve",
                {
                    "reviewers": [
                        {"reviewer_id": "r1", "reviewer_org": "u-a", "declared_conflict": True},
                        {"reviewer_id": "r2", "reviewer_org": "u-b"},
                        {"reviewer_id": "r3", "reviewer_org": "u-c"},
                    ],
                    "terms": "t",
                    "expires_at": "2099-01-01",
                },
            )

    def test_grant_binds_to_approved_version(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.assertEqual(grant["status"], "issued")
        self.assertEqual(grant["data"]["bound_application_version"], approved["version"])
        self.assertEqual(
            grant["data"]["bound_purpose_version"],
            approved["data"]["approved_purpose_version"],
        )
        self.assertEqual(grant["data"]["bound_applicant_org"], "genomics-lab-x")

    def test_grant_cannot_bind_unapproved_application(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "C", "access_policy": "controlled"}
        )
        app = self.service.create(
            self.applicant,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "APP-1",
                "applicant_org": "lab-x",
                "purpose": "p",
            },
        )
        with self.assertRaises(ValidationError):
            self._issued_grant(app, dataset)

    def test_grant_dataset_must_match_application(self):
        dataset, approved = self._approved_application()
        other = self.service.create(
            self.admin, "dataset", {"name": "Other", "access_policy": "controlled"}
        )
        with self.assertRaises(ValidationError):
            self._issued_grant(approved, other)

    def test_activation_rechecks_org_purpose_and_window(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        active = self.service.transition(
            self.admin,
            grant["id"],
            "activate",
            {"starts_at": "2026-09-29", "expires_at": "2098-12-31"},
        )
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["data"]["activated_by"], "admin")

    def test_activation_blocks_when_recipient_org_differs(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset, recipient_org="another-institute")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-29", "expires_at": "2098-12-31"},
            )

    def test_activation_blocks_when_purpose_amended(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        # Purpose/expiry changed after approval: version moves on.
        self.service.transition(
            self.admin,
            approved["id"],
            "amend",
            {"reason": "scope changed", "purpose": "completely new purpose"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-29", "expires_at": "2098-12-31"},
            )

    def test_activation_blocks_when_expiry_extended_beyond_approval(self):
        dataset, approved = self._approved_application(expires_at="2030-01-01")
        grant = self._issued_grant(approved, dataset)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-29", "expires_at": "2099-01-01"},
            )

    def test_withdraw_invalidates_unused_grant_immediately(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        withdrawn = self.service.transition(
            self.applicant,
            approved["id"],
            "withdraw",
            {"reason": "applicant cancelled"},
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        updated_grant = self.service.get(grant["id"])
        self.assertEqual(updated_grant["status"], "invalid")
        self.assertEqual(updated_grant["data"]["invalid_reason"], "application_withdrawn")
        self.assertEqual(updated_grant["data"]["invalid_detail"], "applicant cancelled")

    def test_reject_invalidates_unused_grant_immediately(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.service.transition(
            self.committee,
            approved["id"],
            "reject",
            {"reason": "noncompliance found"},
        )
        updated_grant = self.service.get(grant["id"])
        self.assertEqual(updated_grant["status"], "invalid")
        self.assertEqual(updated_grant["data"]["invalid_reason"], "application_rejected")

    def test_expire_application_invalidates_unused_grant(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.service.transition(
            self.admin, approved["id"], "expire", {"reason": "approval lapsed"}
        )
        updated_grant = self.service.get(grant["id"])
        self.assertEqual(updated_grant["status"], "invalid")
        self.assertEqual(updated_grant["data"]["invalid_reason"], "application_expired")

    def test_active_grant_blocks_application_close_until_revoked(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.service.transition(
            self.admin,
            grant["id"],
            "activate",
            {"starts_at": "2026-09-29", "expires_at": "2098-12-31"},
        )
        with self.assertRaises(GrantsInUseError):
            self.service.transition(
                self.applicant,
                approved["id"],
                "withdraw",
                {"reason": "cancel"},
            )
        # Application is untouched while grants are in use.
        self.assertEqual(self.service.get(approved["id"])["status"], "approved")
        self.assertEqual(self.service.get(grant["id"])["status"], "active")

        # Admin revokes the in-use grant, then the close can proceed.
        self.service.transition(
            self.admin, grant["id"], "revoke", {"reason": "manual revocation"}
        )
        withdrawn = self.service.transition(
            self.applicant, approved["id"], "withdraw", {"reason": "cancel"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")

    def test_invalid_grant_cannot_activate(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.service.transition(
            self.admin,
            grant["id"],
            "invalidate",
            {"reason": "credentials suspect"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-29", "expires_at": "2098-12-31"},
            )

    def test_grant_view_carries_source_status_and_reason(self):
        dataset, approved = self._approved_application()
        grant = self._issued_grant(approved, dataset)
        self.service.transition(
            self.applicant,
            approved["id"],
            "withdraw",
            {"reason": "applicant cancelled"},
        )
        view = self.service.get(grant["id"])
        self.assertEqual(view["data"]["application_id"], approved["id"])
        self.assertEqual(view["status"], "invalid")
        self.assertTrue(view["data"]["invalid_reason"])

    def test_amend_is_forbidden_for_viewer(self):
        _, approved = self._approved_application()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                approved["id"],
                "amend",
                {"reason": "x", "purpose": "new"},
            )


if __name__ == "__main__":
    unittest.main()

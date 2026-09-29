import unittest

from src.rules import purpose_version, valid_grant_window
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


REVIEWERS = [
    {"reviewer_id": "r1", "reviewer_org": "university-a"},
    {"reviewer_id": "r2", "reviewer_org": "university-b"},
    {"reviewer_id": "r3", "reviewer_org": "hospital-c"},
]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertTrue(valid_grant_window("2099-01-01", "2026-09-24"))
        self.assertFalse(valid_grant_window("2026-01-01", "2026-09-24"))
        self.assertNotEqual(
            purpose_version("variant analysis"), purpose_version("other work")
        )
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin,
                {"kind": "application", "status": "under_review", "data": {"applicant_org": "lab-x"}, "version": 4},
                "approve",
                {"reviewers": [{"reviewer_id": "a", "reviewer_org": "u1"}], "terms": "x", "expires_at": "2099-01-01"},
            )

    def test_reviewer_must_be_independent(self):
        entity = {
            "kind": "application",
            "status": "under_review",
            "version": 1,
            "data": {"applicant_org": "university-a", "purpose": "p"},
        }
        conflicted = [
            {"reviewer_id": "r1", "reviewer_org": "university-a"},
            {"reviewer_id": "r2", "reviewer_org": "university-b"},
            {"reviewer_id": "r3", "reviewer_org": "hospital-c"},
        ]
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                self.admin, entity, "approve",
                {"reviewers": conflicted, "terms": "x", "expires_at": "2099-01-01"},
            )


if __name__ == "__main__":
    unittest.main()

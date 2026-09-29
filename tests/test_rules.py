import unittest

from src.rules import valid_grant_window
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def _application(self):
        return {
            "kind": "application",
            "status": "under_review",
            "version": 3,
            "data": {
                "applicant_id": "APP-1",
                "institution": "Genome Institute",
                "dataset_id": "ds-1",
                "purpose": "variant analysis",
                "purpose_version": 1,
            },
        }

    def test_rule_calculation_or_validation(self):
        self.assertTrue(valid_grant_window("2099-01-01", "2026-09-24"))
        self.assertFalse(valid_grant_window("2026-01-01", "2026-09-24"))
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin, self._application(), "approve",
                {"terms": "x", "expires_at": "2099-01-01",
                 "reviewers": [{"committee_id": "a"}]},
            )

    def test_approve_snapshot_uses_next_entity_version(self):
        status, patch = self.rules.validate_transition(
            self.admin, self._application(), "approve",
            {"terms": "x", "expires_at": "2099-01-01",
             "reviewers": [
                 {"committee_id": "a"},
                 {"committee_id": "b"},
                 {"committee_id": "c"},
             ]},
        )
        self.assertEqual(status, "approved")
        self.assertEqual(patch["approved"]["version"], 4)
        self.assertEqual(patch["approved"]["purpose_version"], 1)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


REVIEWERS = [
    {"reviewer_id": "r1", "reviewer_org": "university-a"},
    {"reviewer_id": "r2", "reviewer_org": "university-b"},
    {"reviewer_id": "r3", "reviewer_org": "hospital-c"},
]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [
            {
                "op": "create",
                "as": "dataset",
                "kind": "dataset",
                "data": {"name": "Rare Disease Cohort", "access_policy": "controlled"},
            },
            {
                "op": "create",
                "as": "application",
                "kind": "application",
                "data": {
                    "dataset_id": "{dataset}",
                    "applicant_id": "APP-1",
                    "applicant_org": "genomics-lab-x",
                    "purpose": "variant analysis",
                },
            },
            {"op": "transition", "target": "application", "action": "submit", "data": {}, "expect": "submitted"},
            {"op": "transition", "target": "application", "action": "review", "data": {"committee_id": "committee-a"}, "expect": "under_review"},
            {
                "op": "transition",
                "target": "application",
                "action": "approve",
                "data": {"reviewers": REVIEWERS, "terms": "noncommercial", "expires_at": "2099-01-01"},
                "expect": "approved",
            },
            {
                "op": "create",
                "as": "grant",
                "kind": "grant",
                "data": {
                    "application_id": "{application}",
                    "dataset_id": "{dataset}",
                    "recipient": "researcher-1",
                    "recipient_org": "genomics-lab-x",
                },
            },
            {"op": "transition", "target": "grant", "action": "activate", "data": {"starts_at": "2026-09-24", "expires_at": "2099-01-01"}, "expect": "active"},
            {"op": "transition", "target": "grant", "action": "revoke", "data": {"reason": "purpose changed"}, "expect": "revoked"},
        ]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

        grant = self.service.get(created["grant"])
        self.assertEqual(grant["data"]["bound_applicant_org"], "genomics-lab-x")
        self.assertEqual(grant["data"]["bound_purpose_version"][:7], "sha256:")
        self.assertEqual(grant["data"]["invalid_reason"], "admin_revoked")


if __name__ == "__main__":
    unittest.main()

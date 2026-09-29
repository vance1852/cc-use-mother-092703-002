from __future__ import annotations

import json
import sqlite3
import unittest

from joint_project.api import JsonApplication
from joint_project.service import JointProjectService


class JointProjectApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(JointProjectService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _call(self, method: str, path: str, actor: str | None = None, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle(method, path, headers, body)

    def _seed(self) -> None:
        for user_id, role in (
            ("sec", "secretary"),
            ("uni", "party_admin"),
            ("acc", "acceptor"),
            ("fina", "finance"),
            ("finb", "finance"),
        ):
            self._call("POST", "/users", payload={"user_id": user_id, "display_name": user_id, "role": role})
        self._call("POST", "/parties", "sec", {"party_id": "p1", "name": "高校", "kind": "university"})
        self._call("POST", "/projects", "sec", {"project_id": "pr1", "name": "项目"})
        self._call(
            "POST", "/agreements/versions", "sec",
            {"project_id": "pr1", "title": "首版", "body": {"clauses": ["资金"]}},
        )
        self._call(
            "POST", "/commitments", "uni",
            {"project_id": "pr1", "commitment_id": "c1", "party_id": "p1", "kind": "funding", "title": "经费", "detail": {}},
        )
        self._call(
            "POST", "/acceptance-rules", "acc",
            {"project_id": "pr1", "rule_id": "r1", "milestone_code": "M1", "name": "功能", "rule": {"criteria": {"x": 1}}},
        )
        self._call("POST", "/agreements/pr1/versions/1/sign", "sec")
        self._call(
            "POST", "/funding/sources", "fina",
            {"project_id": "pr1", "source_id": "s1", "commitment_id": "c1", "name": "账户", "total_amount_cny": "100000"},
        )
        self._call(
            "POST", "/milestones", "sec",
            {"project_id": "pr1", "code": "M1", "name": "节点", "sequence_no": 1, "planned_date": "2026-10-31"},
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self._call("POST", "/projects", payload={"project_id": "x", "name": "x"})
        self.assertEqual(response.status, 422)

    def test_full_workflow_over_http(self) -> None:
        self._seed()
        submit = self._call(
            "POST", "/milestones/pr1:M1/submit", "uni",
            {"note": "提交", "evidences": [{"kind": "report", "ref": "r1"}], "idempotency_key": "k1"},
        )
        self.assertEqual(submit.status, 201)
        decide = self._call(
            "POST", "/milestones/pr1:M1/decide", "acc",
            {
                "submission_id": submit.body["submission_id"],
                "decisions": [{"rule_id": "r1", "decision": "accept", "reason": "通过"}],
            },
        )
        self.assertEqual(decide.status, 200)
        self.assertEqual(decide.body["state"], "accepted")

        propose = self._call(
            "POST", "/disbursements", "fina",
            {
                "project_id": "pr1",
                "disbursement_id": "pay1",
                "milestone_id": "pr1:M1",
                "source_id": "s1",
                "amount_cny": "50000",
                "idempotency_key": "pk1",
            },
        )
        self.assertEqual(propose.status, 201)
        # 发起人不能复核自己
        forbidden = self._call("POST", "/disbursements/pay1/review", "fina", {"decision": "approve"})
        self.assertEqual(forbidden.status, 403)
        review = self._call("POST", "/disbursements/pay1/review", "finb", {"decision": "approve"})
        self.assertEqual(review.status, 200)
        pay = self._call("POST", "/disbursements/pay1/pay", "finb")
        self.assertEqual(pay.status, 200)

        history = self._call("GET", "/milestones/pr1:M1/history", "sec")
        self.assertEqual(history.status, 200)
        self.assertEqual(history.body["acceptance_rules_in_force"][0]["rule_id"], "r1")

        outstanding = self._call("GET", "/projects/pr1/funding/outstanding", "sec")
        self.assertEqual(outstanding.status, 200)
        self.assertEqual(outstanding.body["paid_total_cny"], "50000.00")

    def test_dispute_freeze_over_http(self) -> None:
        self._seed()
        submit = self._call(
            "POST", "/milestones/pr1:M1/submit", "uni",
            {"note": "n", "evidences": [{"kind": "report", "ref": "r"}], "idempotency_key": "k"},
        )
        self._call(
            "POST", "/milestones/pr1:M1/decide", "acc",
            {"submission_id": submit.body["submission_id"], "decisions": [{"rule_id": "r1", "decision": "accept"}]},
        )
        self._call(
            "POST", "/disbursements", "fina",
            {"project_id": "pr1", "disbursement_id": "p1", "milestone_id": "pr1:M1", "source_id": "s1", "amount_cny": "1000"},
        )
        dispute = self._call("POST", "/disputes", "uni", {"milestone_id": "pr1:M1", "subject": "争议"})
        self.assertEqual(dispute.status, 201)
        review = self._call("POST", "/disbursements/p1/review", "finb", {"decision": "approve"})
        self.assertEqual(review.status, 409)
        resolve = self._call(
            "POST", f"/disputes/{dispute.body['dispute_id']}/resolve", "sec", {"resolution": "补测"}
        )
        self.assertEqual(resolve.status, 200)
        self.assertEqual(resolve.body["state"], "resolved")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "sec"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()

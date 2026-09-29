"""产学研联合研发项目 HTTP 路由测试（不监听真实端口）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from joint_project.api import JsonApplication
from joint_project.clock import FrozenClock
from joint_project.service import ProjectService
from joint_project.storage import connect

from tests.test_joint_project import make_agreement


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        connection = connect(":memory:")
        self.service = ProjectService(
            connection, FrozenClock(datetime(2026, 1, 10, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)

    def req(self, method: str, path: str, body: dict | None = None, actor: str | None = None):
        import json

        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        return self.app.handle(method, path, headers, payload)

    def test_health(self) -> None:
        response = self.req("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "joint-project")

    def test_bootstrap_first_admin_without_actor_header(self) -> None:
        response = self.req("POST", "/users", {
            "user_id": "admin", "display_name": "管理员", "role": "admin"
        })
        self.assertEqual(response.status, 201)
        # 之后无身份头创建用户被拒绝
        denied = self.req("POST", "/users", {"user_id": "x", "display_name": "X", "role": "auditor"})
        self.assertEqual(denied.status, 422)

    def _seed(self) -> None:
        self.req("POST", "/users", {"user_id": "admin", "display_name": "管理员", "role": "admin"})
        for user_id, name, role in (
            ("sec", "秘书", "secretary"), ("tech", "技术", "technical_reviewer"),
            ("fin", "财务", "finance_officer"), ("rep", "代表", "party_representative"),
            ("aud", "审计", "auditor"),
        ):
            self.req("POST", "/users", {"user_id": user_id, "display_name": name, "role": role}, actor="admin")
        self.req("POST", "/projects", {"project_id": "P1", "title": "项目"}, actor="sec")
        self.req("POST", "/agreements", make_agreement(), actor="sec")

    def test_full_flow_over_http(self) -> None:
        self._seed()
        created = self.req("POST", "/projects/P1/milestones/M1/evidence",
                          {"title": "检测报告", "ref": "DOC-1"}, actor="rep")
        self.assertEqual(created.status, 201)
        review = self.req("POST", "/projects/P1/milestones/M1/reviews",
                          {"decision": "accepted", "achieved_ratio_pct": 100, "note": "达标"},
                          actor="tech")
        self.assertEqual(review.status, 201)
        # 同一人不能既验收又拨款
        forbidden = self.req("POST", "/projects/P1/milestones/M1/payments",
                             {"source_id": "s1", "decision": "approved"}, actor="tech")
        self.assertEqual(forbidden.status, 403)
        payment = self.req("POST", "/projects/P1/milestones/M1/payments",
                           {"source_id": "s1", "decision": "approved", "note": "40%"}, actor="fin")
        self.assertEqual(payment.status, 201)
        executed = self.req("POST", f"/payments/{payment.body['payment_id']}/execute", {}, actor="fin")
        self.assertEqual(executed.status, 200)
        self.assertIsNotNone(executed.body["executed_at"])

    def test_dispute_blocks_payment_via_http(self) -> None:
        self._seed()
        self.req("POST", "/projects/P1/milestones/M1/evidence",
                 {"title": "证据", "ref": "D1"}, actor="rep")
        self.req("POST", "/projects/P1/milestones/M1/reviews",
                 {"decision": "accepted", "achieved_ratio_pct": 100, "note": "ok"}, actor="tech")
        payment = self.req("POST", "/projects/P1/milestones/M1/payments",
                           {"source_id": "s1", "decision": "approved"}, actor="fin")
        dispute = self.req("POST", "/projects/P1/milestones/M1/disputes",
                           {"reason": "有异议"}, actor="rep")
        self.assertEqual(dispute.status, 201)
        blocked = self.req("POST", f"/payments/{payment.body['payment_id']}/execute", {}, actor="fin")
        self.assertEqual(blocked.status, 409)
        resolved = self.req("POST", f"/disputes/{dispute.body['dispute_id']}/resolve",
                            {"resolution": "维持"}, actor="sec")
        self.assertEqual(resolved.status, 200)
        self.req("POST", f"/payments/{payment.body['payment_id']}/execute", {}, actor="fin")

    def test_version_and_report_endpoints(self) -> None:
        self._seed()
        versions = self.req("GET", "/projects/P1/versions", actor="aud")
        self.assertEqual(versions.status, 200)
        self.assertEqual(len(versions.body["versions"]), 1)
        version = self.req("GET", "/projects/P1/versions/1", actor="aud")
        self.assertEqual(version.status, 200)
        self.assertEqual(len(version.body["content"]["parties"]), 3)
        report = self.req("GET", "/projects/P1/report", actor="aud")
        self.assertEqual(report.status, 200)
        self.assertEqual(len(report.body["milestones"]), 2)

    def test_unknown_route_and_bad_json(self) -> None:
        self.assertEqual(self.req("GET", "/nope", actor="aud").status, 404)
        response = self.app.handle(
            "POST", "/projects", {"Content-Type": "application/json"}, b"{bad json"
        )
        self.assertEqual(response.status, 422)

    def test_missing_actor_header(self) -> None:
        response = self.req("POST", "/projects", {"project_id": "P9", "title": "X"})
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()

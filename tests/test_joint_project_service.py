from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from joint_project.clock import FrozenClock
from joint_project.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from joint_project.service import JointProjectService


class JointProjectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = JointProjectService(self.connection, self.clock)
        for user_id, role in (
            ("sec", "secretary"),
            ("uni", "party_admin"),
            ("ent", "party_admin"),
            ("park", "party_admin"),
            ("acc", "acceptor"),
            ("fina", "finance"),
            ("finb", "finance"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_party("sec", "p-uni", "南华大学", "university")
        self.service.register_party("sec", "p-ent", "企业", "enterprise")
        self.service.register_party("sec", "p-park", "产业园", "park")

    def tearDown(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------------ 辅助

    def _project_v1(self, project_id: str = "proj-1") -> None:
        self.service.create_project("sec", project_id, "联合研发项目")
        self.service.create_agreement_version("sec", project_id, "首版", {"clauses": ["资金", "验收"]})
        self.service.add_commitment("ent", project_id, f"{project_id}-fund", "p-ent", "funding", "经费 100 万", {})
        self.service.add_commitment(
            "uni", project_id, f"{project_id}-deliv", "p-uni", "deliverable", "原型系统", {}
        )
        self.service.add_acceptance_rule(
            "acc", project_id, f"{project_id}-r-func", "M1", "功能",
            {"criteria": {"modules": ["a", "b"]}},
        )
        self.service.add_acceptance_rule(
            "acc", project_id, f"{project_id}-r-perf", "M1", "性能",
            {"criteria": {"p95_ms_max": 200}},
        )
        self.service.sign_agreement("sec", project_id, 1)
        self.service.register_funding_source(
            "fina", project_id, f"{project_id}-src", f"{project_id}-fund", "企业账户", "1000000"
        )
        self.service.create_milestone("sec", project_id, "M1", "原型节点", 1, "2026-10-31")

    def _submit_and_decide(self, project_id: str = "proj-1", verdicts: tuple[str, ...] = ("accept", "accept")) -> str:
        milestone_id = f"{project_id}:M1"
        submission = self.service.submit_milestone(
            "uni", milestone_id, "提交", [{"kind": "report", "ref": "r1"}], f"key-{project_id}"
        )
        rule_func = f"{project_id}-r-func"
        rule_perf = f"{project_id}-r-perf"
        decisions = []
        for rule_id, verdict in ((rule_func, verdicts[0]), (rule_perf, verdicts[1])):
            item = {"rule_id": rule_id, "decision": verdict, "reason": "x"}
            if verdict == "partial":
                item["accepted_scope"] = ["部分指标"]
            decisions.append(item)
        self.service.decide_acceptance("acc", milestone_id, submission["submission_id"], decisions)
        return submission["submission_id"]

    # ----------------------------------------------------------- 协议与版本

    def test_cannot_sign_empty_agreement(self) -> None:
        self.service.create_project("sec", "p2", "项目")
        self.service.create_agreement_version("sec", "p2", "空版", {})
        with self.assertRaisesRegex(InvalidState, "承诺"):
            self.service.sign_agreement("sec", "p2", 1)

    def test_milestone_requires_signed_version_and_rules(self) -> None:
        self.service.create_project("sec", "p3", "项目")
        self.service.create_agreement_version("sec", "p3", "首版", {})
        self.service.add_commitment("ent", "p3", "p3-fund", "p-ent", "funding", "钱", {})
        with self.assertRaises(InvalidState):
            self.service.create_milestone("sec", "p3", "M1", "节点", 1, "2026-10-31")
        self.service.sign_agreement("sec", "p3", 1)
        with self.assertRaisesRegex(InvalidState, "验收规则"):
            self.service.create_milestone("sec", "p3", "M9", "无规则节点", 1, "2026-10-31")

    def test_revision_does_not_rewrite_confirmed_history(self) -> None:
        self._project_v1()
        submission_id = self._submit_and_decide(verdicts=("accept", "partial"))
        v1_before = self.connection.execute(
            "SELECT body_json,content_sha256,state FROM agreement_versions WHERE project_id='proj-1' AND version_no=1"
        ).fetchone()
        decisions_before = self.connection.execute(
            "SELECT rule_id,decision FROM acceptance_decisions WHERE submission_id=? ORDER BY rule_id",
            (submission_id,),
        ).fetchall()

        # 修订 v2
        self.service.create_agreement_version("sec", "proj-1", "修订版", {"clauses": ["资金", "验收", "知产"]}, "细化")
        self.service.add_commitment("ent", "proj-1", "proj-1-fund2", "p-ent", "funding", "追加", {})
        self.service.add_acceptance_rule(
            "acc", "proj-1", "proj-1-r-perf-v2", "M1", "性能v2", {"criteria": {"p95_ms_max": 180}}
        )
        self.service.sign_agreement("sec", "proj-1", 2)

        v1_after = self.connection.execute(
            "SELECT body_json,content_sha256,state FROM agreement_versions WHERE project_id='proj-1' AND version_no=1"
        ).fetchone()
        self.assertEqual(v1_before["body_json"], v1_after["body_json"])
        self.assertEqual(v1_before["content_sha256"], v1_after["content_sha256"])
        self.assertEqual(v1_after["state"], "superseded")
        decisions_after = self.connection.execute(
            "SELECT rule_id,decision FROM acceptance_decisions WHERE submission_id=? ORDER BY rule_id",
            (submission_id,),
        ).fetchall()
        self.assertEqual([dict(r) for r in decisions_before], [dict(r) for r in decisions_after])

        # 已确认里程碑仍锚定 v1 与 v1 规则
        history = self.service.milestone_history("sec", "proj-1:M1")
        self.assertEqual(history["milestone"]["anchored_version_no"], 1)
        self.assertEqual(
            {r["rule_id"] for r in history["acceptance_rules_in_force"]},
            {"proj-1-r-func", "proj-1-r-perf"},
        )
        # v1 承诺被标记取代但记录仍在
        self.assertEqual(
            self.connection.execute(
                "SELECT state,closed_in_version_no FROM commitments WHERE commitment_id='proj-1-fund'"
            ).fetchone()["state"],
            "closed_by_revision",
        )

    def test_cannot_open_two_drafts_or_sign_twice(self) -> None:
        self._project_v1()
        self.service.create_agreement_version("sec", "proj-1", "修订版", {"x": 1})
        with self.assertRaises(Conflict):
            self.service.create_agreement_version("sec", "proj-1", "另一个草稿", {"x": 2})
        with self.assertRaises(InvalidState):
            self.service.sign_agreement("sec", "proj-1", 1)

    # ----------------------------------------------------------- 验收决定

    def test_acceptance_verdicts(self) -> None:
        self._project_v1()
        submission_id = self._submit_and_decide(verdicts=("accept", "partial"))
        self.assertEqual(
            self.connection.execute("SELECT state FROM milestones WHERE milestone_id='proj-1:M1'").fetchone()["state"],
            "partially_accepted",
        )
        # 部分接受后可再次提交（补证）
        again = self.service.submit_milestone(
            "uni", "proj-1:M1", "补证", [{"kind": "report", "ref": "r2"}], "key-2"
        )
        self.assertEqual(again["attempt"], 2)
        self.service.decide_acceptance(
            "acc", "proj-1:M1", again["submission_id"],
            [
                {"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""},
                {"rule_id": "proj-1-r-perf", "decision": "accept", "reason": ""},
            ],
        )
        self.assertEqual(
            self.connection.execute("SELECT state FROM milestones WHERE milestone_id='proj-1:M1'").fetchone()["state"],
            "accepted",
        )
        self.assertEqual(submission_id, "proj-1:M1:s1")

    def test_returned_and_paused_states(self) -> None:
        self._project_v1()
        submission = self.service.submit_milestone(
            "uni", "proj-1:M1", "提交", [{"kind": "report", "ref": "r1"}], "k"
        )
        self.service.decide_acceptance(
            "acc", "proj-1:M1", submission["submission_id"],
            [
                {"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""},
                {"rule_id": "proj-1-r-perf", "decision": "return", "reason": "数据缺失"},
            ],
        )
        self.assertEqual(
            self.connection.execute("SELECT state FROM milestones WHERE milestone_id='proj-1:M1'").fetchone()["state"],
            "returned",
        )

        submission2 = self.service.submit_milestone(
            "uni", "proj-1:M1", "再提交", [{"kind": "report", "ref": "r2"}], "k2"
        )
        self.service.decide_acceptance(
            "acc", "proj-1:M1", submission2["submission_id"],
            [
                {"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""},
                {"rule_id": "proj-1-r-perf", "decision": "pause", "reason": "等待设备"},
            ],
        )
        self.assertEqual(
            self.connection.execute("SELECT state FROM milestones WHERE milestone_id='proj-1:M1'").fetchone()["state"],
            "paused",
        )
        resumed = self.service.resume_milestone("acc", "proj-1:M1", "设备到位")
        self.assertEqual(resumed["state"], "pending")

    def test_partial_requires_scope_and_all_rules_covered(self) -> None:
        self._project_v1()
        submission = self.service.submit_milestone(
            "uni", "proj-1:M1", "提交", [{"kind": "report", "ref": "r1"}], "k"
        )
        with self.assertRaises(ValidationFailed):
            self.service.decide_acceptance(
                "acc", "proj-1:M1", submission["submission_id"],
                [{"rule_id": "proj-1-r-func", "decision": "partial", "reason": "", "accepted_scope": []}],
            )
        with self.assertRaises(ValidationFailed):
            self.service.decide_acceptance(
                "acc", "proj-1:M1", submission["submission_id"],
                [{"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""}],
            )

    def test_submission_idempotency(self) -> None:
        self._project_v1()
        evidences = [{"kind": "report", "ref": "r1"}]
        first = self.service.submit_milestone("uni", "proj-1:M1", "提交", evidences, "idem")
        second = self.service.submit_milestone("uni", "proj-1:M1", "提交", evidences, "idem")
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.submit_milestone("uni", "proj-1:M1", "不同内容", evidences, "idem")

    # ----------------------------------------------------------- 争议与拨付

    def test_dispute_freezes_only_related_disbursement(self) -> None:
        self._project_v1()
        self._submit_and_decide()
        d1 = self.service.propose_disbursement(
            "fina", "proj-1", "d1", "proj-1:M1", "proj-1-src", "100000", None, "dk1"
        )
        self.assertEqual(d1["state"], "proposed")
        dispute = self.service.open_dispute("ent", "proj-1:M1", "成果争议")
        self.assertEqual(
            self.connection.execute("SELECT state,freeze_reason FROM disbursements WHERE disbursement_id='d1'").fetchone()["state"],
            "frozen",
        )
        # 冻结中不能复核
        with self.assertRaises(InvalidState):
            self.service.review_disbursement("finb", "d1", "approve")
        # 另一个里程碑的拨付不受影响（M2 规则在修订版 v2 中新增）
        self.service.create_agreement_version("sec", "proj-1", "v2", {"x": 1})
        self.service.add_commitment("ent", "proj-1", "proj-1-fund2", "p-ent", "funding", "追加", {})
        self.service.add_acceptance_rule(
            "acc", "proj-1", "proj-1-r-m2-v2", "M2", "中试", {"criteria": {"yield": 90}}
        )
        self.service.sign_agreement("sec", "proj-1", 2)
        self.service.create_milestone("sec", "proj-1", "M2", "中试节点", 2, "2026-12-31")
        m2 = self.service.submit_milestone(
            "ent", "proj-1:M2", "提交", [{"kind": "report", "ref": "m2"}], "m2k"
        )
        self.service.decide_acceptance(
            "acc", "proj-1:M2", m2["submission_id"],
            [{"rule_id": "proj-1-r-m2-v2", "decision": "accept", "reason": ""}],
        )
        d2 = self.service.propose_disbursement("fina", "proj-1", "d2", "proj-1:M2", "proj-1-src", "50000")
        self.assertEqual(d2["state"], "proposed")
        self.service.review_disbursement("finb", "d2", "approve")

        # 解决争议 → d1 解冻 → 重新复核 → 支付
        self.service.resolve_dispute("sec", dispute["dispute_id"], "补测")
        released = self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d1'").fetchone()["state"]
        self.assertEqual(released, "released_after_freeze")
        self.service.review_disbursement("finb", "d1", "approve")
        self.service.pay_disbursement("fina", "d1")
        self.assertEqual(
            self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d1'").fetchone()["state"],
            "paid",
        )

    def test_finance_and_acceptance_are_separate_roles(self) -> None:
        self._project_v1()
        submission = self.service.submit_milestone(
            "uni", "proj-1:M1", "提交", [{"kind": "report", "ref": "r1"}], "k"
        )
        # 财务不能做成果验收
        with self.assertRaises(Forbidden):
            self.service.decide_acceptance(
                "fina", "proj-1:M1", submission["submission_id"],
                [
                    {"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""},
                    {"rule_id": "proj-1-r-perf", "decision": "accept", "reason": ""},
                ],
            )
        self.service.decide_acceptance(
            "acc", "proj-1:M1", submission["submission_id"],
            [
                {"rule_id": "proj-1-r-func", "decision": "accept", "reason": ""},
                {"rule_id": "proj-1-r-perf", "decision": "accept", "reason": ""},
            ],
        )
        # 验收人不能动拨付
        with self.assertRaises(Forbidden):
            self.service.propose_disbursement("acc", "proj-1", "dx", "proj-1:M1", "proj-1-src", "10")

    def test_disbursement_requires_distinct_reviewer(self) -> None:
        self._project_v1()
        self._submit_and_decide()
        self.service.propose_disbursement("fina", "proj-1", "d1", "proj-1:M1", "proj-1-src", "100000")
        with self.assertRaises(Forbidden):
            self.service.review_disbursement("fina", "d1", "approve")

    def test_disbursement_blocked_when_not_accepted(self) -> None:
        self._project_v1()
        self.service.propose_disbursement("fina", "proj-1", "d1", "proj-1:M1", "proj-1-src", "100000")
        with self.assertRaises(InvalidState):
            self.service.review_disbursement("finb", "d1", "approve")

    def test_budget_cap_enforced(self) -> None:
        self._project_v1()
        self._submit_and_decide()
        with self.assertRaises(Conflict):
            self.service.propose_disbursement(
                "fina", "proj-1", "big", "proj-1:M1", "proj-1-src", "1000001"
            )

    # ----------------------------------------------------------- 项目生命周期

    def test_suspend_freezes_all_and_restart_unfreezes(self) -> None:
        self._project_v1()
        self._submit_and_decide()
        self.service.propose_disbursement("fina", "proj-1", "d1", "proj-1:M1", "proj-1-src", "100000")
        self.service.suspend_project("sec", "proj-1", "经费待落实")
        self.assertEqual(
            self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d1'").fetchone()["state"],
            "frozen",
        )
        # 暂停期间不能提交验收
        with self.assertRaises(InvalidState):
            self.service.submit_milestone("uni", "proj-1:M1", "x", [{"kind": "report", "ref": "r"}], "k9")
        self.service.restart_project("sec", "proj-1", "恢复")
        self.assertEqual(
            self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d1'").fetchone()["state"],
            "released_after_freeze",
        )
        self.service.review_disbursement("finb", "d1", "approve")

    def test_restart_query_shows_rules_in_force_and_outstanding_decisions(self) -> None:
        self._project_v1()
        self._submit_and_decide(verdicts=("accept", "partial"))
        self.service.propose_disbursement("fina", "proj-1", "d1", "proj-1:M1", "proj-1-src", "40000")
        self.service.suspend_project("sec", "proj-1", "暂停")
        self.service.restart_project("sec", "proj-1", "重启")
        history = self.service.milestone_history("sec", "proj-1:M1")
        self.assertEqual(history["anchored_agreement_version"]["version_no"], 1)
        self.assertEqual(history["anchored_agreement_version"]["state"], "signed")
        outstanding = self.service.outstanding_funding_decisions("sec", "proj-1")
        self.assertEqual(outstanding["open_count"], 1)
        self.assertEqual(outstanding["open_decisions"][0]["disbursement_id"], "d1")
        self.assertEqual(outstanding["open_decisions"][0]["state"], "released_after_freeze")
        dossier = self.service.project_dossier("aud", "proj-1")
        self.assertEqual([e["action"] for e in dossier["lifecycles"]], ["suspend", "restart"])

    # ----------------------------------------------------------- 权限与审计

    def test_role_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_project("uni", "x", "x")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("fina")
        chain = self.service.audit_chain("aud")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)

    def test_missing_entities(self) -> None:
        with self.assertRaises(NotFound):
            self.service.create_milestone("sec", "nope", "M1", "x", 1, "2026-10-31")
        with self.assertRaises(NotFound):
            self.service.register_funding_source("fina", "proj-x", "s1", "c1", "账户", "100")


if __name__ == "__main__":
    unittest.main()

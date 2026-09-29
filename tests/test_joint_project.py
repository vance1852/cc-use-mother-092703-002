"""产学研联合研发项目管理服务的单元测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from typing import Any

from joint_project.clock import FrozenClock
from joint_project.contracts import AgreementDraft, ValidationError
from joint_project.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from joint_project.service import ProjectService
from joint_project.storage import connect, inspect_schema, initialize


def make_agreement(**overrides: Any) -> dict[str, Any]:
    agreement: dict[str, Any] = {
        "project_id": "P1",
        "title": "衡阳联合研发项目",
        "version_label": "v1.0",
        "note": "首版",
        "parties": [
            {"party_id": "univ", "name": "衡阳理工大学", "kind": "university", "contact": "周老师"},
            {"party_id": "corp", "name": "智聚材料公司", "kind": "enterprise", "contact": "李经理"},
            {"party_id": "park", "name": "高新产业园", "kind": "park", "contact": "王科长"},
        ],
        "commitments": [
            {"commitment_id": "c1", "party_id": "univ", "category": "人力",
             "description": "研发人月", "unit": "人月", "quantity": 36},
        ],
        "funding_sources": [
            {"source_id": "s1", "party_id": "corp", "name": "企业自筹", "amount": "600.00"},
            {"source_id": "s2", "party_id": "park", "name": "园区专项", "amount": "400.00"},
        ],
        "acceptance_rules": [
            {"rule_id": "r1", "title": "规则一", "criterion": "指标达标",
             "evidence_required": "检测报告", "accept_ratio_pct": 90},
            {"rule_id": "r2", "title": "规则二", "criterion": "良率达标",
             "evidence_required": "质检记录", "accept_ratio_pct": 85},
        ],
        "milestones": [
            {"milestone_id": "M1", "title": "原理样机", "plan_date": "2026-03-31",
             "payment_ratio_pct": 40, "rule_ids": ["r1"]},
            {"milestone_id": "M2", "title": "中试验证", "plan_date": "2026-06-30",
             "payment_ratio_pct": 60, "rule_ids": ["r1", "r2"]},
        ],
        "deliverables": [
            {"deliverable_id": "d1", "milestone_id": "M1", "owner_party_id": "univ",
             "title": "样机报告", "description": "第三方检测"},
        ],
    }
    agreement.update(overrides)
    return agreement


class ContractTests(unittest.TestCase):
    def test_valid_agreement(self) -> None:
        draft = AgreementDraft.from_dict(make_agreement())
        self.assertEqual(len(draft.milestones), 2)

    def test_payment_ratio_must_sum_to_100(self) -> None:
        raw = make_agreement()
        raw["milestones"][1]["payment_ratio_pct"] = 50
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)

    def test_dangling_rule_reference(self) -> None:
        raw = make_agreement()
        raw["milestones"][0]["rule_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)

    def test_dangling_party_reference(self) -> None:
        raw = make_agreement()
        raw["deliverables"][0]["owner_party_id"] = "ghost"
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)

    def test_dates_must_be_ascending(self) -> None:
        raw = make_agreement()
        raw["milestones"][0]["plan_date"] = "2026-07-31"
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)

    def test_duplicate_ids_and_empty_funding(self) -> None:
        raw = make_agreement()
        raw["milestones"][1]["milestone_id"] = "M1"
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)
        raw = make_agreement(funding_sources=[])
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)

    def test_money_rejects_negative_and_non_finite(self) -> None:
        raw = make_agreement()
        raw["funding_sources"][0]["amount"] = -1
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)
        raw = make_agreement()
        raw["funding_sources"][0]["amount"] = "NaN"
        with self.assertRaises(ValidationError):
            AgreementDraft.from_dict(raw)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.clock = FrozenClock(datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc))
        self.service = ProjectService(self.connection, self.clock)
        self.service.create_user(None, "admin", "管理员", "admin")
        self.service.create_user("admin", "sec", "项目秘书", "secretary")
        self.service.create_user("admin", "tech", "技术验收人", "technical_reviewer")
        self.service.create_user("admin", "tech2", "第二技术验收人", "technical_reviewer")
        self.service.create_user("admin", "fin", "财务复核人", "finance_officer")
        self.service.create_user("admin", "rep", "合作方代表", "party_representative")
        self.service.create_user("admin", "aud", "审计员", "auditor")
        self.agreement = make_agreement()
        self.service.register_project("sec", "P1", self.agreement["title"])
        self.service.sign_agreement("sec", self.agreement)

    def tearDown(self) -> None:
        self.connection.close()

    def accept_milestone(
        self, milestone: str, *, decision: str = "accepted", ratio: int = 100,
        reviewer: str = "tech", ref: str | None = None,
    ) -> dict[str, Any]:
        self.service.submit_evidence(
            "rep", "P1", milestone, f"{milestone}证据", ref or f"DOC-{milestone}"
        )
        return self.service.review_acceptance(reviewer, "P1", milestone, decision, ratio, "评审意见")

    def pay_and_execute(self, milestone: str, source: str = "s1", **kwargs: Any) -> dict[str, Any]:
        decision = self.service.decide_payment("fin", "P1", milestone, source, "approved", "复核通过", **kwargs)
        self.service.execute_payment("fin", decision["payment_id"])
        return decision


class VersionTests(ServiceTestBase):
    def test_sign_creates_pending_milestones_bound_to_version(self) -> None:
        project = self.service.get_project("P1")
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["current_version_seq"], 1)
        versions = self.service.list_versions("aud", "P1")
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(versions[0]["content_sha256"]), 64)
        milestones = self.service.list_milestones("aud", "P1")
        self.assertEqual([m["status"] for m in milestones], ["pending", "pending"])
        self.assertTrue(all(m["definition_version_seq"] == 1 for m in milestones))

    def test_identical_content_is_rejected(self) -> None:
        with self.assertRaises(Conflict):
            self.service.sign_agreement("sec", make_agreement())

    def test_historical_version_is_not_rewritten(self) -> None:
        self.accept_milestone("M1")
        v2 = make_agreement(version_label="v2.0", note="改期")
        v2["milestones"][1]["plan_date"] = "2026-07-31"
        self.service.sign_agreement("sec", v2)
        v1 = self.service.get_version("P1", 1)
        self.assertEqual(v1["content"]["milestones"][1]["plan_date"], "2026-06-30")
        v2_view = self.service.get_version("P1", 2)
        self.assertEqual(v2_view["content"]["milestones"][1]["plan_date"], "2026-07-31")

    def test_secretary_can_sign_but_representative_cannot(self) -> None:
        draft = make_agreement(project_id="P2", version_label="v1.0")
        self.service.register_project("sec", "P2", "另一项目")
        with self.assertRaises(Forbidden):
            self.service.sign_agreement("rep", draft)


class ReviewTests(ServiceTestBase):
    def test_accepted_requires_full_ratio(self) -> None:
        self.service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        with self.assertRaises(ValidationFailed):
            self.service.review_acceptance("tech", "P1", "M1", "accepted", 90, "未完全达标")
        with self.assertRaises(ValidationFailed):
            self.service.review_acceptance("tech", "P1", "M1", "partially_accepted", 100, "矛盾")
        with self.assertRaises(ValidationFailed):
            self.service.review_acceptance("tech", "P1", "M1", "returned", 100, "矛盾")

    def test_return_then_partial_then_accept(self) -> None:
        self.service.submit_evidence("rep", "P1", "M1", "初版证据", "DOC-1")
        first = self.service.review_acceptance("tech", "P1", "M1", "returned", 30, "资料不足")
        self.assertEqual(first["round"], 1)
        self.assertEqual(first["rule_snapshots"][0]["rule_id"], "r1")
        m1 = self.service.get_milestone("aud", "P1", "M1")
        self.assertEqual(m1["status"], "returned")
        self.service.submit_evidence("rep", "P1", "M1", "补充证据", "DOC-2")
        second = self.service.review_acceptance("tech", "P1", "M1", "partially_accepted", 60, "部分达标")
        self.assertEqual(second["round"], 2)
        self.service.submit_evidence("rep", "P1", "M1", "最终证据", "DOC-3")
        third = self.service.review_acceptance("tech2", "P1", "M1", "accepted", 100, "达标")
        self.assertEqual(third["round"], 3)

    def test_review_without_evidence_is_invalid(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.review_acceptance("tech", "P1", "M1", "accepted", 100, "无证据")

    def test_finance_cannot_review_and_reviewer_cannot_pay(self) -> None:
        self.service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        with self.assertRaises(Forbidden):
            self.service.review_acceptance("fin", "P1", "M1", "accepted", 100, "越权")
        self.service.review_acceptance("tech", "P1", "M1", "accepted", 100, "ok")
        with self.assertRaises(Forbidden):
            self.service.decide_payment("tech", "P1", "M1", "s1", "approved", "越权")

    def test_finance_reviewer_must_differ_from_technical_reviewer(self) -> None:
        # 管理员拥有全部权限：技术验收与财务复核也不能是同一人
        self.service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        self.service.review_acceptance("admin", "P1", "M1", "accepted", 100, "管理员验收")
        with self.assertRaises(Forbidden):
            self.service.decide_payment("admin", "P1", "M1", "s1", "approved", "同一人拨款")

    def test_reviewer_cannot_review_own_evidence(self) -> None:
        self.service.submit_evidence("admin", "P1", "M1", "证据", "DOC-1")
        with self.assertRaises(Forbidden):
            self.service.review_acceptance("admin", "P1", "M1", "accepted", 100, "自评")


class PaymentTests(ServiceTestBase):
    def test_full_payment_amounts(self) -> None:
        self.accept_milestone("M1")
        ent = self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "40%")
        park = self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "40%")
        self.assertEqual(ent["amount_cents"], 24_000)  # 600 × 40%
        self.assertEqual(park["amount_cents"], 16_000)  # 400 × 40%
        self.assertEqual(ent["source_version_seq"], 1)

    def test_partial_payment_then_top_up_after_full_acceptance(self) -> None:
        self.accept_milestone("M1", decision="partially_accepted", ratio=50)
        first = self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "半数")
        self.assertEqual(first["amount_cents"], 12_000)  # 600 × 40% × 50%
        self.service.execute_payment("fin", first["payment_id"])
        # 补交证据后完全接受：只补拨 40% 与 20% 的差额
        self.service.submit_evidence("rep", "P1", "M1", "补证", "DOC-X")
        self.service.review_acceptance("tech", "P1", "M1", "accepted", 100, "补齐")
        top_up = self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "补差额")
        self.assertEqual(top_up["amount_cents"], 12_000)

    def test_payment_requires_review(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "无验收")

    def test_decisions_are_append_only(self) -> None:
        self.accept_milestone("M1")
        self.service.decide_payment("fin", "P1", "M1", "s1", "rejected", "暂缓")
        with self.assertRaises(InvalidState):
            self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "不能翻案")

    def test_expected_amount_conflict(self) -> None:
        self.accept_milestone("M1")
        with self.assertRaises(Conflict):
            self.service.decide_payment(
                "fin", "P1", "M1", "s1", "approved", "金额不符", expected_amount_cents=1
            )

    def test_milestone_locks_after_all_sources_executed(self) -> None:
        self.accept_milestone("M1")
        d1 = self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "x")
        self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "x")
        self.service.execute_payment("fin", d1["payment_id"])
        m1 = self.service.get_milestone("aud", "P1", "M1")
        self.assertFalse(m1["locked"])
        d2 = self.connection.execute(
            "SELECT payment_id FROM payment_decisions WHERE source_id='s2'"
        ).fetchone()[0]
        self.service.execute_payment("fin", d2)
        self.assertTrue(self.service.get_milestone("aud", "P1", "M1")["locked"])


class DisputeTests(ServiceTestBase):
    def _prepare_m1_with_one_outstanding(self) -> None:
        self.accept_milestone("M1")
        self.pay_and_execute("M1", "s1")
        self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "待执行")

    def test_dispute_requires_reviewed_milestone(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.open_dispute("rep", "P1", "M2", "尚未评审")

    def test_dispute_freezes_only_related_payments(self) -> None:
        self._prepare_m1_with_one_outstanding()
        self.service.open_dispute("rep", "P1", "M1", "对结论有异议")
        m1 = self.service.get_milestone("aud", "P1", "M1")
        self.assertTrue(m1["payment_frozen"])
        # M1 未执行的园区资金不能执行
        frozen_payment = self.connection.execute(
            "SELECT payment_id FROM payment_decisions WHERE source_id='s2'"
        ).fetchone()[0]
        with self.assertRaises(InvalidState):
            self.service.execute_payment("fin", frozen_payment)
        # M2 的验收与拨款完全不受影响
        self.accept_milestone("M2")
        other = self.service.decide_payment("fin", "P1", "M2", "s1", "approved", "M2照常")
        self.service.execute_payment("fin", other["payment_id"])
        # 解决争议后 M1 资金恢复
        self.service.resolve_dispute("sec", 1, "复核后维持原结论")
        self.service.execute_payment("fin", frozen_payment)
        self.assertFalse(self.service.get_milestone("aud", "P1", "M1")["payment_frozen"])

    def test_keep_frozen_after_resolution(self) -> None:
        self._prepare_m1_with_one_outstanding()
        dispute = self.service.open_dispute("rep", "P1", "M1", "异议")
        self.service.resolve_dispute("sec", dispute["dispute_id"], "待上级确认", keep_frozen=True)
        with self.assertRaises(InvalidState):
            self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "仍冻结")
        # 上级确认后秘书显式解冻，既有的未结拨付决定可以执行
        released = self.service.release_payment_hold("sec", "P1", "M1", "上级已确认，恢复拨付")
        self.assertFalse(released["payment_frozen"])
        outstanding_id = self.connection.execute(
            "SELECT payment_id FROM payment_decisions WHERE source_id='s2' AND executed_at IS NULL"
        ).fetchone()[0]
        self.service.execute_payment("fin", outstanding_id)

    def test_release_hold_requires_existing_freeze(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.release_payment_hold("sec", "P1", "M2", "本就未冻结")

    def test_open_amendment_blocked_while_dispute_open(self) -> None:
        self._prepare_m1_with_one_outstanding()
        self.service.open_dispute("rep", "P1", "M1", "异议")
        draft = make_agreement(version_label="v2.0", note="改期M2")
        draft["milestones"][1]["plan_date"] = "2026-07-15"
        with self.assertRaises(InvalidState):
            self.service.sign_agreement("sec", draft)


class SuspendTests(ServiceTestBase):
    def test_suspend_and_resume_restores_prior_status(self) -> None:
        self.service.suspend_milestone("sec", "P1", "M1")
        with self.assertRaises(InvalidState):
            self.service.submit_evidence("rep", "P1", "M1", "暂停期证据", "DOC-Z")
        self.service.resume_milestone("sec", "P1", "M1")
        self.assertEqual(self.service.get_milestone("aud", "P1", "M1")["status"], "pending")
        self.service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        self.service.suspend_milestone("sec", "P1", "M1")
        self.service.resume_milestone("sec", "P1", "M1")
        self.assertEqual(self.service.get_milestone("aud", "P1", "M1")["status"], "submitted")

    def test_representative_cannot_suspend(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.suspend_milestone("rep", "P1", "M1")

    def test_payment_blocked_while_suspended(self) -> None:
        self.accept_milestone("M1", decision="partially_accepted", ratio=50)
        decision = self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "x")
        self.service.suspend_milestone("sec", "P1", "M1")
        with self.assertRaises(InvalidState):
            self.service.execute_payment("fin", decision["payment_id"])


class AmendmentTests(ServiceTestBase):
    def _amend(self, **changes: Any) -> dict[str, Any]:
        draft = make_agreement(version_label=changes.pop("version_label", "v2.0"), note="修订")
        for milestone in draft["milestones"]:
            if milestone["milestone_id"] in changes:
                milestone.update(changes[milestone["milestone_id"]])
        return self.service.sign_agreement("sec", draft)

    def test_locked_milestone_cannot_change(self) -> None:
        self.accept_milestone("M1")
        self.pay_and_execute("M1", "s1")
        d2 = self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "x")
        self.service.execute_payment("fin", d2["payment_id"])
        with self.assertRaises(InvalidState):
            self._amend(M1={"plan_date": "2026-04-15"})

        def amend_ratios() -> None:
            draft = make_agreement(version_label="v2.1", note="调整比例")
            for milestone in draft["milestones"]:
                if milestone["milestone_id"] == "M1":
                    milestone["payment_ratio_pct"] = 30
                if milestone["milestone_id"] == "M2":
                    milestone["payment_ratio_pct"] = 70
            self.service.sign_agreement("sec", draft)

        with self.assertRaises(InvalidState):
            amend_ratios()

    def test_reviewed_milestone_cannot_change_but_pending_can(self) -> None:
        # M1 退回补证（已进入评审但未拨款），其定义同样冻结
        self.service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        self.service.review_acceptance("tech", "P1", "M1", "returned", 20, "不足")
        with self.assertRaises(InvalidState):
            self._amend(M1={"plan_date": "2026-04-01"})
        # M2 尚未进入评审，可以顺延
        result = self._amend(M2={"plan_date": "2026-07-31"})
        by_id = {item["milestone_id"]: item for item in result["milestones"]}
        self.assertIsNotNone(by_id["M1"]["carried_from"])
        milestones = {m["milestone_id"]: m for m in self.service.list_milestones("aud", "P1")}
        self.assertEqual(milestones["M2"]["plan_date"], "2026-07-31")
        self.assertEqual(milestones["M2"]["status"], "pending")
        self.assertEqual(milestones["M2"]["definition_version_seq"], 2)
        # M1 的状态延续
        self.assertEqual(milestones["M1"]["status"], "returned")
        self.assertEqual(milestones["M1"]["current_round"], 1)

    def test_reviewed_milestone_cannot_be_deleted(self) -> None:
        self.accept_milestone("M1")
        draft = make_agreement(version_label="v2.0", note="删除M1")
        draft["milestones"] = [m for m in draft["milestones"] if m["milestone_id"] != "M1"]
        draft["deliverables"] = []
        draft["milestones"][0]["payment_ratio_pct"] = 100
        with self.assertRaises(InvalidState):
            self.service.sign_agreement("sec", draft)

    def test_rule_content_change_on_reviewed_milestone_rejected(self) -> None:
        self.accept_milestone("M1")
        draft = make_agreement(version_label="v2.0", note="收紧规则")
        for rule in draft["acceptance_rules"]:
            if rule["rule_id"] == "r1":
                rule["accept_ratio_pct"] = 99
        with self.assertRaises(InvalidState):
            self.service.sign_agreement("sec", draft)

    def test_funding_change_only_affects_future_milestones(self) -> None:
        self.accept_milestone("M1")
        self.pay_and_execute("M1", "s1")
        d2 = self.service.decide_payment("fin", "P1", "M1", "s2", "approved", "x")
        self.service.execute_payment("fin", d2["payment_id"])
        draft = make_agreement(version_label="v2.0", note="增资")
        for source in draft["funding_sources"]:
            if source["source_id"] == "s1":
                source["amount"] = "800.00"
        self.service.sign_agreement("sec", draft)
        self.accept_milestone("M2")
        decision = self.service.decide_payment("fin", "P1", "M2", "s1", "approved", "按v2")
        self.assertEqual(decision["source_version_seq"], 2)
        self.assertEqual(decision["amount_cents"], 48_000)  # 800 × 60%
        # M1 的历史拨付仍按 v1
        m1 = self.service.get_milestone("aud", "P1", "M1")
        self.assertEqual(m1["payments"][0]["source_version_seq"], 1)
        self.assertEqual(m1["payments"][0]["amount_cents"], 24_000)

    def test_dispute_flag_carries_across_amendment(self) -> None:
        self.accept_milestone("M1")
        self.service.decide_payment("fin", "P1", "M1", "s1", "approved", "未执行")
        self.service.open_dispute("rep", "P1", "M1", "异议")
        self.service.resolve_dispute("sec", 1, "维持冻结", keep_frozen=True)
        # M2 仍可修订（未进入评审）；M1 冻结状态随节点延续
        self._amend(M2={"plan_date": "2026-07-31"})
        m1 = self.service.get_milestone("aud", "P1", "M1")
        self.assertTrue(m1["payment_frozen"])


class PersistenceTests(unittest.TestCase):
    def test_file_persistence(self) -> None:
        import tempfile
        from pathlib import Path

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = str(Path(tmpdir.name) / "p.sqlite3")
        service = ProjectService(connect(path))
        service.create_user(None, "admin", "管理员", "admin")
        service.create_user("admin", "sec", "秘书", "secretary")
        service.create_user("admin", "tech", "技术", "technical_reviewer")
        service.create_user("admin", "fin", "财务", "finance_officer")
        service.create_user("admin", "rep", "代表", "party_representative")
        service.create_user("admin", "aud", "审计", "auditor")
        service.register_project("sec", "P1", "项目")
        service.sign_agreement("sec", make_agreement())
        service.submit_evidence("rep", "P1", "M1", "证据", "DOC-1")
        service.review_acceptance("tech", "P1", "M1", "accepted", 100, "ok")
        service.decide_payment("fin", "P1", "M1", "s1", "approved", "未执行")
        service.connection.close()

        reopened = ProjectService(connect(path))
        report = reopened.project_report("aud", "P1")
        self.assertEqual(len(report["outstanding_payments"]), 1)
        m1 = next(m for m in report["milestones"] if m["milestone_id"] == "M1")
        self.assertEqual(m1["reviews"][0]["rule_version_seq"], 1)
        self.assertEqual(m1["reviews"][0]["rule_snapshots"][0]["title"], "规则一")
        reopened.connection.close()


class StorageTests(unittest.TestCase):
    def test_initialize_is_idempotent_and_schema_complete(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize(connection)
        initialize(connection)
        info = inspect_schema(connection)
        self.assertEqual(info["missing_tables"], [])
        self.assertTrue(info["foreign_keys"])
        connection.close()

    def test_foreign_key_enforced(self) -> None:
        connection = connect(":memory:")
        ProjectService(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO parties(version_id,party_id,name,kind,seq) VALUES(999,'x','X','university',0)"
            )
        connection.close()

if __name__ == "__main__":
    unittest.main()

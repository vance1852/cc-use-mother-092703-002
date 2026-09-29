"""联合研发项目管理服务的离线验收。

情景：衡阳联合研发项目由高校、企业和产业园共同投入。验收覆盖
版本锚定、部分接受、争议只冻结相关拨付、协议修订不改历史、
财务复核与成果验收分离，以及重启后追溯当时规则与未结资金决定。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import JointProjectService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
    service = JointProjectService(connection, clock)

    # 用户：项目秘书、三方管理员、成果验收人、两名财务（发起/复核分离）、审计
    for user_id, role in (
        ("sec", "secretary"),
        ("uni", "party_admin"),
        ("ent", "party_admin"),
        ("park", "party_admin"),
        ("acc", "acceptor"),
        ("fin-a", "finance"),
        ("fin-b", "finance"),
        ("aud", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 参与方
    service.register_party("sec", "party-usc", "南华大学", "university")
    service.register_party("sec", "party-ent", "衡阳智造企业", "enterprise")
    service.register_party("sec", "party-park", "衡阳产业园", "park")

    project = service.create_project("sec", "hy-jrd-2026", "衡阳智能装备联合研发项目")

    # 协议 v1：三方承诺
    service.create_agreement_version("sec", "hy-jrd-2026", "联合研发协议首版", {"chapters": ["范围", "资金", "验收"]})
    commitments_v1 = (
        ("c-univ-tech", "party-usc", "deliverable", "原型系统与算法验证报告"),
        ("c-univ-staff", "party-usc", "resource", "派驻 6 名研究人员"),
        ("c-ent-fund", "party-ent", "funding", "企业研发经费 60 万元"),
        ("c-park-fund", "party-park", "funding", "产业园配套经费 40 万元"),
        ("c-park-site", "party-park", "resource", "提供中试场地 300 平方米"),
    )
    for commitment_id, party_id, kind, title in commitments_v1:
        service.add_commitment("uni", "hy-jrd-2026", commitment_id, party_id, kind, title, {"source": "v1"})

    # v1 验收规则：M1 两条（可部分接受），M2 一条
    service.add_acceptance_rule(
        "acc", "hy-jrd-2026", "rule-m1-func", "M1", "原型功能完整性",
        {"criteria": {"modules": ["采集", "推理", "看板"], "pass_ratio": 1.0}},
    )
    service.add_acceptance_rule(
        "acc", "hy-jrd-2026", "rule-m1-perf", "M1", "推理时延指标",
        {"criteria": {"p95_ms_max": 200, "evidence": "third_party_report"}},
    )
    service.add_acceptance_rule(
        "acc", "hy-jrd-2026", "rule-m2-line", "M2", "中试线良率",
        {"criteria": {"yield_min_percent": 92, "batch_count_min": 3}},
    )
    signed_v1 = service.sign_agreement("sec", "hy-jrd-2026", 1)
    assert signed_v1["state"] == "signed" and signed_v1["project_state"] == "active"

    # 资金来源绑定 v1 资金承诺
    service.register_funding_source("fin-a", "hy-jrd-2026", "src-ent", "c-ent-fund", "企业拨款账户", "600000")
    service.register_funding_source("fin-a", "hy-jrd-2026", "src-park", "c-park-fund", "园区配套账户", "400000")
    service.record_funding_receipt("fin-a", "src-ent", "300000")

    # 里程碑锚定签署版本 v1
    service.create_milestone("sec", "hy-jrd-2026", "M1", "原型联调节点", 1, "2026-10-31")
    service.create_milestone("sec", "hy-jrd-2026", "M2", "中试线验证节点", 2, "2026-12-31")

    # M1 提交验收
    submission = service.submit_milestone(
        "uni",
        "hy-jrd-2026:M1",
        "Q4 季度提交",
        [
            {"kind": "report", "ref": "oss://joint/m1/report-v3.pdf", "sha256": "a" * 64},
            {"kind": "testlog", "ref": "oss://joint/m1/logs.zip"},
        ],
        "submit-m1-1",
    )
    # 功能完整接受，时延指标退回补证 → 部分接受
    decision = service.decide_acceptance(
        "acc",
        "hy-jrd-2026:M1",
        submission["submission_id"],
        [
            {"rule_id": "rule-m1-func", "decision": "accept", "reason": "三个模块均演示通过"},
            {
                "rule_id": "rule-m1-perf",
                "decision": "partial",
                "accepted_scope": ["p50 时延 82ms 已达标"],
                "reason": "缺第三方 p95 报告，退回补证",
            },
        ],
    )
    assert decision["state"] == "partially_accepted"

    # 财务按 v1 计划为 M1 拟拨付，由另一名财务复核后支付
    d1 = service.propose_disbursement(
        "fin-a", "hy-jrd-2026", "pay-m1-ent", "hy-jrd-2026:M1", "src-ent", "100000",
        submission["submission_id"], "pay-m1-ent-key",
    )
    reviewed = service.review_disbursement("fin-b", "pay-m1-ent", "approve", "与 v1 节点计划一致")
    assert reviewed["state"] == "approved"

    # M2 已先完成验收并拟拨付（用于证明争议不波及其他节点）
    m2_submission = service.submit_milestone(
        "ent", "hy-jrd-2026:M2", "中试三批次数据",
        [{"kind": "report", "ref": "oss://joint/m2/yield.xlsx"}],
        "submit-m2-1",
    )
    service.decide_acceptance(
        "acc", "hy-jrd-2026:M2", m2_submission["submission_id"],
        [{"rule_id": "rule-m2-line", "decision": "accept", "reason": "三批次良率 94.2%"}],
    )
    d2 = service.propose_disbursement("fin-a", "hy-jrd-2026", "pay-m2-park", "hy-jrd-2026:M2", "src-park", "50000")

    # 合作方就 M1 成果是否达标产生争议：只冻结 M1 相关拨付，M2 不受影响
    dispute = service.open_dispute("ent", "hy-jrd-2026:M1", "p95 抽检口径争议", "企业不认可自测数据")
    d1_frozen = connection.execute("SELECT state,freeze_reason FROM disbursements WHERE disbursement_id='pay-m1-ent'").fetchone()
    d2_row = connection.execute("SELECT state FROM disbursements WHERE disbursement_id='pay-m2-park'").fetchone()
    assert d1_frozen["state"] == "frozen" and d1_frozen["freeze_reason"] == f"dispute:{dispute['dispute_id']}"
    assert d2_row["state"] == "proposed"

    # 争议解决，M1 拨付解冻，需财务再次复核后方可支付
    service.resolve_dispute("sec", dispute["dispute_id"], "以第三方检测报告为准，企业认可补测安排")
    service.review_disbursement("fin-b", "pay-m1-ent", "approve", "争议已解决，复核通过")
    service.pay_disbursement("fin-b", "pay-m1-ent")

    # 协议修订 v2：不能改写 v1 已确认的历史节点
    service.create_agreement_version(
        "sec", "hy-jrd-2026", "联合研发协议修订版",
        {"chapters": ["范围", "资金", "验收", "知识产权"], "note": "细化时延口径"},
        "依据季度会争议处理结果修订验收口径",
    )
    service.add_commitment("uni", "hy-jrd-2026", "c-ent-fund-2", "party-ent", "funding", "企业追加经费 20 万元", {"source": "v2"})
    service.add_acceptance_rule(
        "acc", "hy-jrd-2026", "rule-m1-perf-v2", "M1", "推理时延指标（修订口径）",
        {"criteria": {"p95_ms_max": 180, "evidence": "third_party_report", "sampling": "rev2"}},
    )
    service.sign_agreement("sec", "hy-jrd-2026", 2)
    history_m1 = service.milestone_history("sec", "hy-jrd-2026:M1")
    assert history_m1["milestone"]["anchored_version_no"] == 1
    assert {rule["rule_id"] for rule in history_m1["acceptance_rules_in_force"]} == {"rule-m1-func", "rule-m1-perf"}

    # 项目暂停：所有未结拨付冻结；重启后解冻，规则与决定仍可查
    service.suspend_project("sec", "hy-jrd-2026", "等待三方追加经费落实")
    assert connection.execute("SELECT state FROM disbursements WHERE disbursement_id='pay-m2-park'").fetchone()["state"] == "frozen"
    service.restart_project("sec", "hy-jrd-2026", "追加经费落实，恢复执行")
    service.review_disbursement("fin-b", "pay-m2-park", "approve", "重启后复核")
    service.pay_disbursement("fin-b", "pay-m2-park")

    # 重启后：秘书查询当时采用的验收规则与未结资金决定
    restart_history = service.milestone_history("sec", "hy-jrd-2026:M1")
    outstanding = service.outstanding_funding_decisions("sec", "hy-jrd-2026")
    dossier = service.project_dossier("sec", "hy-jrd-2026")
    audit = service.audit_chain("aud")
    assert audit["valid"]
    assert outstanding["open_count"] == 0

    result = {
        "status": "ok",
        "project": project,
        "agreement_signed": {"v1": signed_v1["signed_at"]},
        "m1_partial_acceptance": decision,
        "dispute": dispute,
        "m1_history_after_revision": {
            "anchored_version_no": restart_history["milestone"]["anchored_version_no"],
            "rules_in_force": [rule["rule_id"] for rule in restart_history["acceptance_rules_in_force"]],
            "submission_attempts": len(restart_history["submissions"]),
            "disputes": len(restart_history["disputes"]),
        },
        "outstanding_after_restart": outstanding,
        "dossier": {
            "state": dossier["project"]["state"],
            "versions": [(v["version_no"], v["state"]) for v in dossier["agreement_versions"]],
            "milestones": [(m["code"], m["state"], m["anchored_version_no"]) for m in dossier["milestones"]],
            "funding_sources": len(dossier["funding_sources"]),
            "lifecycle_events": [e["action"] for e in dossier["lifecycles"]],
        },
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行产学研联合研发项目管理服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

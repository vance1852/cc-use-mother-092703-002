"""容器和快照检查使用的冒烟验收命令。

复现衡阳联合研发的典型协作：
1. 三方签署 v1 协议，承诺、交付物、资金来源与验收规则全部绑定该版本；
2. M1 验收通过后双来源拨款完成并锁定；
3. M2 部分接受、仅部分来源执行后产生争议，只冻结 M2 相关拨付，M3 不受影响；
4. 争议解决后补齐拨付，再签署 v2 修订（只动尚未确认的 M3）；
5. 关闭数据库连接后重新打开，仍能查到当时的验收规则快照与未结资金决定。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import ProjectService
from .storage import connect

ROOT = Path(__file__).resolve().parents[2]


def _load_agreement() -> dict:
    return json.loads((ROOT / "fixtures" / "demo_joint_agreement.json").read_text(encoding="utf-8"))


def run(database_path: str | None = None) -> dict:
    tmpdir: tempfile.TemporaryDirectory | None = None
    if database_path is None:
        tmpdir = tempfile.TemporaryDirectory(prefix="joint-project-")
        database_path = str(Path(tmpdir.name) / "joint.sqlite3")

    service = ProjectService(connect(database_path))
    service.create_user(None, "admin", "平台管理员", "admin")
    service.create_user("admin", "sec", "项目秘书", "secretary")
    service.create_user("admin", "tech", "技术验收人", "technical_reviewer")
    service.create_user("admin", "fin", "财务复核人", "finance_officer")
    service.create_user("admin", "rep", "合作方代表", "party_representative")
    service.create_user("admin", "aud", "审计员", "auditor")

    agreement = _load_agreement()
    project_id = agreement["project_id"]
    service.register_project("sec", project_id, agreement["title"])
    v1 = service.sign_agreement("sec", agreement)
    assert v1["version_seq"] == 1

    # M1：提交证据 → 技术验收 100% → 财务按两个资金来源分别复核并执行
    service.submit_evidence("rep", project_id, "M1", "原理样机检测报告", "DOC-M1-01", note="72小时连续运行达标")
    review_m1 = service.review_acceptance("tech", project_id, "M1", "accepted", 100, "指标达标率96%")
    pay_m1_ent = service.decide_payment("fin", project_id, "M1", "sf-enterprise", "approved", "按v1计划40%")
    pay_m1_park = service.decide_payment("fin", project_id, "M1", "sf-park", "approved", "按v1计划40%")
    assert pay_m1_ent["amount_cents"] == 24_000_000  # 60万 × 40%，单位分
    assert pay_m1_park["amount_cents"] == 16_000_000  # 40万 × 40%
    service.execute_payment("fin", pay_m1_ent["payment_id"])
    service.execute_payment("fin", pay_m1_park["payment_id"])
    m1 = service.get_milestone("aud", project_id, "M1")
    assert m1["locked"] is True

    # M2：只达成 90%，部分接受 → 按比例折算；企业来源先执行
    service.submit_evidence("rep", project_id, "M2", "三批次中试质检记录", "DOC-M2-01")
    review_m2 = service.review_acceptance(
        "tech", project_id, "M2", "partially_accepted", 90, "第三批次良率86%，其余资料待补"
    )
    pay_m2_ent = service.decide_payment(
        "fin", project_id, "M2", "sf-enterprise", "approved", "部分接受按36%折算"
    )
    # 40% × 90% = 36%
    assert pay_m2_ent["amount_cents"] == 21_600_000
    service.execute_payment("fin", pay_m2_ent["payment_id"])

    # 合作方对 M2 技术结论有争议：仅冻结 M2 的相关拨付
    dispute = service.open_dispute("rep", project_id, "M2", "第三批次良率统计口径存在分歧")
    blocked = False
    try:
        service.decide_payment("fin", project_id, "M2", "sf-park", "approved", "尝试在争议期间拨款")
    except Exception:
        blocked = True
    assert blocked is True

    # 未进入评审的 M3 与 M2 无关：争议期间其证据提交等准备工作不受影响
    m3_during_dispute = service.get_milestone("aud", project_id, "M3")
    assert m3_during_dispute["payment_frozen"] is False

    # 争议解决 → M2 产业园来源解冻并执行
    service.resolve_dispute("sec", dispute["dispute_id"], "按加权口径复核，维持90%达成")
    pay_m2_park = service.decide_payment(
        "fin", project_id, "M2", "sf-park", "approved", "争议解决后按36%执行"
    )
    service.execute_payment("fin", pay_m2_park["payment_id"])

    # v2 修订：M1 已锁定、M2 已评审，二者都不能改写；仅尚未进入评审的 M3 允许顺延
    v2_draft = json.loads(json.dumps(agreement))
    v2_draft["version_label"] = "v1.1"
    v2_draft["note"] = "M3 结题节点顺延一个月"
    for milestone in v2_draft["milestones"]:
        if milestone["milestone_id"] == "M3":
            milestone["plan_date"] = "2026-10-31"
    v2 = service.sign_agreement("sec", v2_draft)
    assert v2["version_seq"] == 2
    carried = {item["milestone_id"]: item for item in v2["milestones"]}
    assert carried["M1"]["locked"] is True

    # 试图在修订中改动 M1 必须失败
    bad = json.loads(json.dumps(v2_draft))
    bad["version_label"] = "v1.2-bad"
    for milestone in bad["milestones"]:
        if milestone["milestone_id"] == "M1":
            milestone["plan_date"] = "2026-04-15"
    try:
        service.sign_agreement("sec", bad)
        raise AssertionError("已确认里程碑不应被改写")
    except Exception as exc:
        assert "不能被修订改写" in str(exc)

    # 试图改动已评审但尚未完全付款的 M2 同样必须失败
    bad2 = json.loads(json.dumps(v2_draft))
    bad2["version_label"] = "v1.3-bad"
    for milestone in bad2["milestones"]:
        if milestone["milestone_id"] == "M2":
            milestone["payment_ratio_pct"] = 30
            for other in bad2["milestones"]:
                if other["milestone_id"] == "M3":
                    other["payment_ratio_pct"] = 30
    try:
        service.sign_agreement("sec", bad2)
        raise AssertionError("已评审里程碑的拨付比例不应被改写")
    except Exception as exc:
        assert "不能被修订改写" in str(exc)

    # M3 在 v2 下完成验收与拨款（其规则快照标记为 v2；M1/M2 的历史仍指向 v1）
    service.submit_evidence("rep", project_id, "M3", "结题资料汇编", "DOC-M3-01")
    review_m3 = service.review_acceptance("tech", project_id, "M3", "accepted", 100, "资料齐全")
    pay_m3 = service.decide_payment("fin", project_id, "M3", "sf-park", "approved", "按v2计划20%")
    assert pay_m3["amount_cents"] == 8_000_000
    assert pay_m3["source_version_seq"] == 2
    assert review_m3["rule_version_seq"] == 2
    service.execute_payment("fin", pay_m3["payment_id"])

    # 模拟进程重启：关闭并重新打开同一数据库
    service.connection.close()
    reopened = ProjectService(connect(database_path))

    report = reopened.project_report("aud", project_id)
    assert report["project"]["current_version_seq"] == 2
    m1_after = next(item for item in report["milestones"] if item["milestone_id"] == "M1")
    assert m1_after["definition_version_seq"] == 2  # 节点延续到 v2
    # 但当时的验收依据仍是 v1：规则快照与资金决定都记录了采用版本
    assert m1_after["reviews"][0]["rule_version_seq"] == 1
    assert m1_after["payments"][0]["source_version_seq"] == 1
    assert m1_after["reviews"][0]["rule_snapshots"][0]["title"] == "样机性能验证"
    m2_after = next(item for item in report["milestones"] if item["milestone_id"] == "M2")
    assert m2_after["status"] == "partially_accepted"
    outstanding = reopened.project_report("aud", project_id)["outstanding_payments"]
    result = {
        "status": "ok",
        "project_id": project_id,
        "versions": len(report["versions"]),
        "milestones": len(report["milestones"]),
        "m1_locked": m1_after["locked"],
        "m1_rule_version_at_review": m1_after["reviews"][0]["rule_version_seq"],
        "m1_payment_version": m1_after["payments"][0]["source_version_seq"],
        "m2_status": m2_after["status"],
        "m2_outstanding_decisions": len(outstanding),
        "m2_rule_snapshot": review_m2["rule_snapshots"][0]["title"],
        "events": len(reopened.audit_trail("aud")),
    }
    if tmpdir is not None:
        reopened.connection.close()
        tmpdir.cleanup()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.database), ensure_ascii=False))


if __name__ == "__main__":
    main()

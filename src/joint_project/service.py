"""产学研联合研发项目的事务用例。

关键不变量：
- 承诺、可交付成果、验收规则、资金来源都锚定签署时的协议版本；
- 协议修订只产生新版本，旧版本与已确认的里程碑历史不可改写；
- 里程碑可被完全接受、部分接受、退回补证或暂停；
- 争议与暂停只冻结相关拨付，不影响项目其他资金；
- 成果验收（acceptor）与财务复核（finance）由不同角色完成，且财务拨付
  需经发起人之外的第二人复核。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .storage import initialize, transaction

ZERO = Decimal("0")
MONEY_QUANTUM = Decimal("0.01")

ROLE_PERMISSIONS = {
    "secretary": {
        "project.write",
        "party.write",
        "agreement.prepare",
        "agreement.sign",
        "milestone.write",
        "project.lifecycle",
        "dispute.resolve",
        "report.read",
        "audit.read",
    },
    "party_admin": {
        "party.write",
        "commitment.write",
        "submission.write",
        "dispute.write",
        "report.read",
    },
    "acceptor": {"rule.write", "acceptance.write", "report.read"},
    "finance": {"funding.write", "disbursement.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


def money(value: object) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except Exception as exc:  # noqa: BLE001 - 输入可能是任意类型
        raise ValidationFailed("金额必须是数字") from exc
    if not amount.is_finite() or amount <= ZERO:
        raise ValidationFailed("金额必须为正数")
    return amount


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class JointProjectService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM jp_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM jp_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO jp_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotency(self, scope: str, key: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
        request_sha = digest(request)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM jp_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_sha:
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _save_idempotency(self, scope: str, key: str, request: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO jp_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(request), canonical_json(response), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO jp_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # -------------------------------------------------------------- 参与方

    def register_party(self, actor_id: str, party_id: str, name: str, kind: str) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        if kind not in {"university", "enterprise", "park", "institute", "other"}:
            raise ValidationFailed("未知参与方类型")
        if not party_id.strip() or not name.strip():
            raise ValidationFailed("参与方编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO parties(party_id,name,kind,created_by,created_at) VALUES(?,?,?,?,?)",
                    (party_id.strip(), name.strip(), kind, actor_id, self._now()),
                )
                self._audit("party", party_id, "party.registered", actor_id, {"name": name, "kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("参与方编号已经存在") from exc
        return {"party_id": party_id.strip(), "name": name.strip(), "kind": kind}

    # -------------------------------------------------------------- 项目

    def create_project(self, actor_id: str, project_id: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        if not project_id.strip() or not name.strip():
            raise ValidationFailed("项目编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO projects(project_id,name,created_by,created_at) VALUES(?,?,?,?)",
                    (project_id.strip(), name.strip(), actor_id, self._now()),
                )
                self._audit("project", project_id, "project.created", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号已经存在") from exc
        return {"project_id": project_id.strip(), "state": "preparing", "current_version_no": 0}

    def _get_project(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        return row

    def _get_version(self, project_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM agreement_versions WHERE project_id=? AND version_no=?",
            (project_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return row

    def _draft_version(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM agreement_versions WHERE project_id=? AND state='draft' "
            "ORDER BY version_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            raise InvalidState("项目没有待签署的协议版本草稿")
        return row

    # ---------------------------------------------------------- 协议版本

    def create_agreement_version(
        self,
        actor_id: str,
        project_id: str,
        title: str,
        body: Mapping[str, Any],
        change_summary: str = "",
    ) -> dict[str, Any]:
        """起草协议新版本。首版无前置版本；修订版以前一签署版为基础。"""
        self._require(actor_id, "agreement.prepare")
        project = self._get_project(project_id)
        if project["state"] in {"completed", "cancelled"}:
            raise InvalidState("项目已结束，不能再修订协议")
        open_draft = self.connection.execute(
            "SELECT version_no FROM agreement_versions WHERE project_id=? AND state='draft'",
            (project_id,),
        ).fetchone()
        if open_draft is not None:
            raise Conflict(f"版本 {open_draft['version_no']} 尚未签署，不能同时起草新版本")
        next_no = int(project["current_version_no"]) + 1
        supersedes = None if next_no == 1 else int(project["current_version_no"])
        if next_no > 1:
            prior = self._get_version(project_id, int(project["current_version_no"]))
            if prior["state"] != "signed":
                raise InvalidState("只能在已签署版本基础上修订")
        definition = canonical_json(body)
        content_sha = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO agreement_versions(project_id,version_no,title,body_json,content_sha256,"
                    "supersedes_version_no,change_summary,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        next_no,
                        title,
                        definition,
                        content_sha,
                        supersedes,
                        change_summary,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("协议内容与已有版本完全相同") from exc
            self._audit(
                "agreement",
                f"{project_id}:v{next_no}",
                "agreement.drafted",
                actor_id,
                {"version_no": next_no, "supersedes": supersedes, "sha256": content_sha},
            )
        return {"project_id": project_id, "version_no": next_no, "state": "draft", "sha256": content_sha}

    def sign_agreement(self, actor_id: str, project_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "agreement.sign")
        project = self._get_project(project_id)
        version = self._get_version(project_id, version_no)
        if version["state"] != "draft":
            raise InvalidState("该版本不是待签署草稿")
        commitment_count = self.connection.execute(
            "SELECT count(*) FROM commitments WHERE project_id=? AND version_no=?",
            (project_id, version_no),
        ).fetchone()[0]
        if commitment_count == 0:
            raise InvalidState("协议版本没有登记任何参与方承诺，不能签署")
        with transaction(self.connection, immediate=True):
            if version_no > 1:
                prior = self._get_version(project_id, version_no - 1)
                if prior["state"] != "signed":
                    raise InvalidState("前一版本尚未签署")
                self.connection.execute(
                    "UPDATE agreement_versions SET state='superseded' WHERE project_id=? AND version_no=? AND state='signed'",
                    (project_id, version_no - 1),
                )
                # 旧版本内容保持不变，仅把其中的承诺与规则标记为已被修订取代
                self.connection.execute(
                    "UPDATE commitments SET state='closed_by_revision',closed_in_version_no=? "
                    "WHERE project_id=? AND version_no=? AND state='active'",
                    (version_no, project_id, version_no - 1),
                )
                self.connection.execute(
                    "UPDATE acceptance_rules SET state='retired',retired_in_version_no=? "
                    "WHERE project_id=? AND version_no=? AND state='active'",
                    (version_no, project_id, version_no - 1),
                )
            signed_at = self._now()
            cursor = self.connection.execute(
                "UPDATE agreement_versions SET state='signed',signed_at=?,signed_by=? "
                "WHERE project_id=? AND version_no=? AND state='draft'",
                (signed_at, actor_id, project_id, version_no),
            )
            if cursor.rowcount != 1:
                raise InvalidState("协议版本状态已变化")
            project_state = project["state"]
            if project_state == "preparing":
                project_state = "active"
            self.connection.execute(
                "UPDATE projects SET current_version_no=?,state=?,revision=revision+1 WHERE project_id=?",
                (version_no, project_state, project_id),
            )
            self._audit(
                "agreement",
                f"{project_id}:v{version_no}",
                "agreement.signed",
                actor_id,
                {"version_no": version_no, "sha256": version["content_sha256"]},
            )
        return {
            "project_id": project_id,
            "version_no": version_no,
            "state": "signed",
            "signed_at": signed_at,
            "project_state": project_state,
        }

    def agreement_version(self, actor_id: str, project_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        version = self._get_version(project_id, version_no)
        return {
            "project_id": project_id,
            "version_no": version_no,
            "title": version["title"],
            "body": json.loads(version["body_json"]),
            "content_sha256": version["content_sha256"],
            "supersedes_version_no": version["supersedes_version_no"],
            "change_summary": version["change_summary"],
            "state": version["state"],
            "signed_at": version["signed_at"],
            "signed_by": version["signed_by"],
            "commitments": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM commitments WHERE project_id=? AND version_no=? ORDER BY commitment_id",
                    (project_id, version_no),
                )
            ],
            "acceptance_rules": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM acceptance_rules WHERE project_id=? AND version_no=? ORDER BY milestone_code,rule_id",
                    (project_id, version_no),
                )
            ],
        }

    # ---------------------------------------------------------- 承诺/规则

    def add_commitment(
        self,
        actor_id: str,
        project_id: str,
        commitment_id: str,
        party_id: str,
        kind: str,
        title: str,
        detail: Mapping[str, Any],
        quantity: str = "",
        unit: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        if kind not in {"funding", "resource", "deliverable", "service", "other"}:
            raise ValidationFailed("未知承诺类型")
        party = self.connection.execute("SELECT * FROM parties WHERE party_id=?", (party_id,)).fetchone()
        if party is None:
            raise NotFound("参与方不存在")
        with transaction(self.connection, immediate=True):
            version = self._draft_version(project_id)
            try:
                self.connection.execute(
                    "INSERT INTO commitments(commitment_id,project_id,version_no,party_id,kind,title,"
                    "detail_json,quantity,unit,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        commitment_id,
                        project_id,
                        version["version_no"],
                        party_id,
                        kind,
                        title,
                        canonical_json(detail),
                        quantity,
                        unit,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("承诺编号冲突") from exc
            self._audit(
                "commitment",
                commitment_id,
                "commitment.added",
                actor_id,
                {"project_id": project_id, "version_no": version["version_no"], "party_id": party_id, "kind": kind},
            )
        return {
            "commitment_id": commitment_id,
            "project_id": project_id,
            "version_no": version["version_no"],
            "state": "active",
        }

    def add_acceptance_rule(
        self,
        actor_id: str,
        project_id: str,
        rule_id: str,
        milestone_code: str,
        name: str,
        rule: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        criteria = rule.get("criteria")
        if not isinstance(criteria, Mapping) or not criteria:
            raise ValidationFailed("验收规则必须包含非空 criteria")
        with transaction(self.connection, immediate=True):
            version = self._draft_version(project_id)
            try:
                self.connection.execute(
                    "INSERT INTO acceptance_rules(rule_id,project_id,version_no,milestone_code,name,"
                    "rule_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        rule_id,
                        project_id,
                        version["version_no"],
                        milestone_code,
                        name,
                        canonical_json(rule),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("验收规则编号冲突") from exc
            self._audit(
                "acceptance_rule",
                rule_id,
                "rule.added",
                actor_id,
                {"project_id": project_id, "version_no": version["version_no"], "milestone_code": milestone_code},
            )
        return {"rule_id": rule_id, "version_no": version["version_no"], "milestone_code": milestone_code}

    # -------------------------------------------------------------- 里程碑

    def create_milestone(
        self,
        actor_id: str,
        project_id: str,
        code: str,
        name: str,
        sequence_no: int,
        planned_date: str,
        version_no: int | None = None,
        milestone_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "milestone.write")
        try:
            parse_utc(planned_date + "T00:00:00Z", "planned_date")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        project = self._get_project(project_id)
        if version_no is None:
            if int(project["current_version_no"]) == 0:
                raise InvalidState("项目尚无已签署的协议版本")
            anchor = int(project["current_version_no"])
        else:
            anchor = version_no
        anchored = self._get_version(project_id, anchor)
        if anchored["state"] != "signed":
            raise InvalidState("里程碑只能锚定已签署的协议版本")
        rule_count = self.connection.execute(
            "SELECT count(*) FROM acceptance_rules WHERE project_id=? AND version_no=? AND milestone_code=?",
            (project_id, anchor, code),
        ).fetchone()[0]
        if rule_count == 0:
            raise InvalidState("锚定版本中没有该里程碑的验收规则，无法建立节点")
        milestone_id = milestone_id or f"{project_id}:{code}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO milestones(milestone_id,project_id,code,name,sequence_no,anchored_version_no,"
                    "planned_date,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        milestone_id,
                        project_id,
                        code,
                        name,
                        sequence_no,
                        anchor,
                        planned_date,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("里程碑编号或代码冲突") from exc
            self._audit(
                "milestone",
                milestone_id,
                "milestone.created",
                actor_id,
                {"code": code, "anchored_version_no": anchor},
            )
        return {
            "milestone_id": milestone_id,
            "project_id": project_id,
            "code": code,
            "anchored_version_no": anchor,
            "state": "pending",
        }

    def _get_milestone(self, milestone_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,)).fetchone()
        if row is None:
            raise NotFound("里程碑不存在")
        return row

    def _rules_for_milestone(self, milestone: sqlite3.Row) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM acceptance_rules WHERE project_id=? AND version_no=? AND milestone_code=? "
                "ORDER BY rule_id",
                (milestone["project_id"], milestone["anchored_version_no"], milestone["code"]),
            )
        )

    # -------------------------------------------------------------- 资金来源

    def register_funding_source(
        self,
        actor_id: str,
        project_id: str,
        source_id: str,
        commitment_id: str,
        name: str,
        total_amount_cny: object,
    ) -> dict[str, Any]:
        self._require(actor_id, "funding.write")
        total = money(total_amount_cny)
        commitment = self.connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=? AND project_id=?",
            (commitment_id, project_id),
        ).fetchone()
        if commitment is None:
            raise NotFound("承诺不存在")
        if commitment["kind"] != "funding":
            raise ValidationFailed("资金来源必须绑定资金类承诺")
        version = self._get_version(project_id, commitment["version_no"])
        if version["state"] != "signed":
            raise InvalidState("承诺所属协议版本尚未签署")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO funding_sources(source_id,project_id,commitment_id,version_no,party_id,name,"
                    "total_amount_cny,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        project_id,
                        commitment_id,
                        commitment["version_no"],
                        commitment["party_id"],
                        name,
                        format(total, "f"),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("资金来源编号冲突") from exc
            self._audit(
                "funding_source",
                source_id,
                "funding.registered",
                actor_id,
                {"project_id": project_id, "commitment_id": commitment_id, "version_no": commitment["version_no"]},
            )
        return {
            "source_id": source_id,
            "project_id": project_id,
            "commitment_id": commitment_id,
            "version_no": commitment["version_no"],
            "party_id": commitment["party_id"],
            "total_amount_cny": format(total, "f"),
            "received_amount_cny": "0.00",
        }

    def record_funding_receipt(self, actor_id: str, source_id: str, amount_cny: object) -> dict[str, Any]:
        self._require(actor_id, "funding.write")
        amount = money(amount_cny)
        source = self.connection.execute("SELECT * FROM funding_sources WHERE source_id=?", (source_id,)).fetchone()
        if source is None:
            raise NotFound("资金来源不存在")
        new_received = Decimal(source["received_amount_cny"]) + amount
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE funding_sources SET received_amount_cny=?,revision=revision+1 WHERE source_id=?",
                (format(new_received, "f"), source_id),
            )
            self._audit(
                "funding_source",
                source_id,
                "funding.received",
                actor_id,
                {"amount_cny": format(amount, "f"), "received_total": format(new_received, "f")},
            )
        return {"source_id": source_id, "received_amount_cny": format(new_received, "f")}

    # ---------------------------------------------------------- 验收提交

    def submit_milestone(
        self,
        actor_id: str,
        milestone_id: str,
        note: str,
        evidences: Sequence[Mapping[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "submission.write")
        if not isinstance(evidences, Sequence) or isinstance(evidences, (str, bytes)) or not evidences:
            raise ValidationFailed("至少提交一份验收证据")
        request = {"milestone_id": milestone_id, "note": note, "evidences": list(evidences)}
        cached = self._idempotency("submission", idempotency_key, request)
        if cached is not None:
            return cached
        milestone = self._get_milestone(milestone_id)
        project = self._get_project(milestone["project_id"])
        if project["state"] in {"suspended", "completed", "cancelled"}:
            raise InvalidState("项目暂停或结束期间不能提交验收")
        if milestone["state"] not in {"pending", "returned", "partially_accepted"}:
            raise InvalidState(f"里程碑当前状态 {milestone['state']} 不接受提交")
        clean_evidences: list[dict[str, str]] = []
        for item in evidences:
            kind = str(item.get("kind", "")).strip()
            ref = str(item.get("ref", "")).strip()
            if not kind or not ref:
                raise ValidationFailed("每份证据必须包含 kind 和 ref")
            evidence_hash = str(item.get("sha256", "")).strip()
            if evidence_hash and (len(evidence_hash) != 64 or not all(c in "0123456789abcdef" for c in evidence_hash)):
                raise ValidationFailed("证据 sha256 必须是 64 位小写十六进制")
            clean_evidences.append({"kind": kind, "ref": ref, "note": str(item.get("note", "")), "sha256": evidence_hash})
        attempt_row = self.connection.execute(
            "SELECT coalesce(max(attempt),0) attempt FROM milestone_submissions WHERE milestone_id=?",
            (milestone_id,),
        ).fetchone()
        attempt = int(attempt_row["attempt"]) + 1
        submission_id = f"{milestone_id}:s{attempt}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO milestone_submissions(submission_id,milestone_id,attempt,evidence_note,"
                    "evidence_json,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        submission_id,
                        milestone_id,
                        attempt,
                        note,
                        canonical_json(clean_evidences),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("提交编号冲突") from exc
            for index, evidence in enumerate(clean_evidences, start=1):
                self.connection.execute(
                    "INSERT INTO acceptance_evidences(evidence_id,submission_id,kind,ref,note,sha256,"
                    "uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"{submission_id}:e{index}",
                        submission_id,
                        evidence["kind"],
                        evidence["ref"],
                        evidence["note"],
                        evidence["sha256"],
                        actor_id,
                        self._now(),
                    ),
                )
            self.connection.execute(
                "UPDATE milestones SET state='in_review',revision=revision+1 WHERE milestone_id=?",
                (milestone_id,),
            )
            self._audit(
                "milestone",
                milestone_id,
                "milestone.submitted",
                actor_id,
                {"submission_id": submission_id, "attempt": attempt, "evidence_count": len(clean_evidences)},
            )
            response = {
                "submission_id": submission_id,
                "milestone_id": milestone_id,
                "attempt": attempt,
                "state": "in_review",
            }
            self._save_idempotency("submission", idempotency_key, request, response)
        return response

    # ---------------------------------------------------------- 验收决定

    def decide_acceptance(
        self,
        actor_id: str,
        milestone_id: str,
        submission_id: str,
        decisions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """成果验收人按签署时的规则逐项给出结论，并汇总里程碑状态。"""
        self._require(actor_id, "acceptance.write")
        milestone = self._get_milestone(milestone_id)
        project = self._get_project(milestone["project_id"])
        if project["state"] in {"completed", "cancelled"}:
            raise InvalidState("项目已结束，不能再验收")
        if milestone["state"] != "in_review":
            raise InvalidState("里程碑不在评审中")
        submission = self.connection.execute(
            "SELECT * FROM milestone_submissions WHERE submission_id=? AND milestone_id=?",
            (submission_id, milestone_id),
        ).fetchone()
        if submission is None:
            raise NotFound("验收提交不存在")
        rules = self._rules_for_milestone(milestone)
        rule_map = {row["rule_id"]: row for row in rules}
        if len(decisions) != len(rules):
            raise ValidationFailed(f"必须覆盖锚定版本的全部 {len(rules)} 条验收规则")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for item in decisions:
            rule_id = str(item.get("rule_id", ""))
            verdict = str(item.get("decision", ""))
            if rule_id not in rule_map or rule_id in seen:
                raise ValidationFailed(f"验收规则 {rule_id} 不属于本里程碑锚定版本或重复出现")
            if verdict not in {"accept", "partial", "return", "pause"}:
                raise ValidationFailed("验收结论必须是 accept/partial/return/pause")
            seen.add(rule_id)
            accepted_scope = item.get("accepted_scope", [])
            if verdict == "partial":
                if not isinstance(accepted_scope, list) or not accepted_scope:
                    raise ValidationFailed("部分接受必须给出已接受范围 accepted_scope")
            elif accepted_scope:
                raise ValidationFailed("仅部分接受可以填写 accepted_scope")
            normalized.append(
                {
                    "rule_id": rule_id,
                    "decision": verdict,
                    "accepted_scope": accepted_scope,
                    "reason": str(item.get("reason", "")),
                }
            )
        verdicts = {item["decision"] for item in normalized}
        if "return" in verdicts:
            new_state = "returned"
        elif "pause" in verdicts:
            new_state = "paused"
        elif "partial" in verdicts:
            new_state = "partially_accepted"
        else:
            new_state = "accepted"
        decided_at = self._now()
        with transaction(self.connection, immediate=True):
            for item in normalized:
                try:
                    self.connection.execute(
                        "INSERT INTO acceptance_decisions(milestone_id,submission_id,rule_id,decision,"
                        "accepted_scope_json,reason,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            milestone_id,
                            submission_id,
                            item["rule_id"],
                            item["decision"],
                            canonical_json(item["accepted_scope"]),
                            item["reason"],
                            actor_id,
                            decided_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("该提交已有验收决定") from exc
            self.connection.execute(
                "UPDATE milestones SET state=?,revision=revision+1 WHERE milestone_id=? AND state='in_review'",
                (new_state, milestone_id),
            )
            self._reconcile_disbursements(milestone["project_id"], milestone_id)
            self._audit(
                "milestone",
                milestone_id,
                "milestone.acceptance_decided",
                actor_id,
                {
                    "submission_id": submission_id,
                    "anchored_version_no": milestone["anchored_version_no"],
                    "verdicts": sorted(verdicts),
                    "result_state": new_state,
                },
            )
        return {
            "milestone_id": milestone_id,
            "submission_id": submission_id,
            "state": new_state,
            "anchored_version_no": milestone["anchored_version_no"],
            "decisions": normalized,
        }

    def resume_milestone(self, actor_id: str, milestone_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "acceptance.write")
        milestone = self._get_milestone(milestone_id)
        if milestone["state"] != "paused":
            raise InvalidState("只有暂停中的里程碑可以恢复")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE milestones SET state='pending',revision=revision+1 WHERE milestone_id=? AND state='paused'",
                (milestone_id,),
            )
            self._reconcile_disbursements(milestone["project_id"], milestone_id)
            self._audit("milestone", milestone_id, "milestone.resumed", actor_id, {"reason": reason})
        return {"milestone_id": milestone_id, "state": "pending"}

    # -------------------------------------------------------------- 争议

    def open_dispute(self, actor_id: str, milestone_id: str, subject: str, detail: str = "") -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        milestone = self._get_milestone(milestone_id)
        existing = self.connection.execute(
            "SELECT count(*) FROM disputes WHERE milestone_id=? AND state='open'",
            (milestone_id,),
        ).fetchone()[0]
        if existing:
            raise Conflict("该里程碑已有未解决争议")
        dispute_id = f"disp-{milestone_id}-{secrets.token_hex(6)}"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO disputes(dispute_id,project_id,milestone_id,subject,detail,raised_by,raised_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (dispute_id, milestone["project_id"], milestone_id, subject, detail, actor_id, self._now()),
            )
            self._reconcile_disbursements(milestone["project_id"], milestone_id)
            self._audit(
                "dispute",
                dispute_id,
                "dispute.opened",
                actor_id,
                {"milestone_id": milestone_id, "subject": subject},
            )
        return {"dispute_id": dispute_id, "milestone_id": milestone_id, "state": "open"}

    def resolve_dispute(self, actor_id: str, dispute_id: str, resolution: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.resolve")
        row = self.connection.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        if row["state"] != "open":
            raise InvalidState("争议已关闭")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE disputes SET state='resolved',resolution=?,resolved_by=?,resolved_at=?,"
                "revision=revision+1 WHERE dispute_id=? AND state='open'",
                (resolution, actor_id, self._now(), dispute_id),
            )
            self._reconcile_disbursements(row["project_id"], row["milestone_id"])
            self._audit(
                "dispute",
                dispute_id,
                "dispute.resolved",
                actor_id,
                {"milestone_id": row["milestone_id"], "resolution": resolution},
            )
        return {"dispute_id": dispute_id, "state": "resolved"}

    # -------------------------------------------------------------- 拨付

    def propose_disbursement(
        self,
        actor_id: str,
        project_id: str,
        disbursement_id: str,
        milestone_id: str,
        source_id: str,
        amount_cny: object,
        submission_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "disbursement.write")
        amount = money(amount_cny)
        milestone = self.connection.execute(
            "SELECT * FROM milestones WHERE milestone_id=? AND project_id=?",
            (milestone_id, project_id),
        ).fetchone()
        if milestone is None:
            raise NotFound("里程碑不存在")
        source = self.connection.execute(
            "SELECT * FROM funding_sources WHERE source_id=? AND project_id=?",
            (source_id, project_id),
        ).fetchone()
        if source is None:
            raise NotFound("资金来源不存在")
        request = {
            "project_id": project_id,
            "milestone_id": milestone_id,
            "source_id": source_id,
            "amount_cny": format(amount, "f"),
            "submission_id": submission_id,
        }
        if idempotency_key:
            cached = self._idempotency("disbursement", idempotency_key, request)
            if cached is not None:
                return cached
        committed = Decimal(source["total_amount_cny"])
        used_row = self.connection.execute(
            "SELECT coalesce(sum(CAST(amount_cny AS REAL)),0) used FROM disbursements "
            "WHERE source_id=? AND state IN ('proposed','frozen','approved','released_after_freeze','paid')",
            (source_id,),
        ).fetchone()
        if Decimal(str(used_row["used"])) + amount > committed:
            raise Conflict("拨付额超过该资金来源承诺总额")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO disbursements(disbursement_id,project_id,milestone_id,source_id,submission_id,"
                    "amount_cny,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        disbursement_id,
                        project_id,
                        milestone_id,
                        source_id,
                        submission_id,
                        format(amount, "f"),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("拨付编号冲突") from exc
            self._reconcile_disbursements(project_id, milestone_id)
            state = self.connection.execute(
                "SELECT state FROM disbursements WHERE disbursement_id=?", (disbursement_id,)
            ).fetchone()["state"]
            self._audit(
                "disbursement",
                disbursement_id,
                "disbursement.proposed",
                actor_id,
                {"milestone_id": milestone_id, "source_id": source_id, "amount_cny": format(amount, "f"), "state": state},
            )
            response = {
                "disbursement_id": disbursement_id,
                "milestone_id": milestone_id,
                "source_id": source_id,
                "amount_cny": format(amount, "f"),
                "state": state,
            }
            if idempotency_key:
                self._save_idempotency("disbursement", idempotency_key, request, response)
        return response

    def review_disbursement(
        self, actor_id: str, disbursement_id: str, decision: str, note: str = ""
    ) -> dict[str, Any]:
        """财务复核：复核人不得是拨付发起人，也不得是成果验收人（角色隔离）。"""
        self._require(actor_id, "disbursement.write")
        if decision not in {"approve", "reject"}:
            raise ValidationFailed("复核结论必须是 approve 或 reject")
        row = self.connection.execute("SELECT * FROM disbursements WHERE disbursement_id=?", (disbursement_id,)).fetchone()
        if row is None:
            raise NotFound("拨付不存在")
        if row["created_by"] == actor_id:
            raise Forbidden("拨付发起人与复核人必须是不同的财务授权人")
        if row["state"] not in {"proposed", "released_after_freeze"}:
            raise InvalidState(f"拨付当前状态 {row['state']} 不允许复核")
        if decision == "approve":
            milestone = self._get_milestone(row["milestone_id"])
            project = self._get_project(row["project_id"])
            if project["state"] != "active" and project["state"] != "restarted":
                raise InvalidState("项目未在执行中，不能批准拨付")
            if milestone["state"] not in {"accepted", "partially_accepted"}:
                raise InvalidState("只有已接受或部分接受的里程碑才能批准拨付")
            open_dispute = self.connection.execute(
                "SELECT count(*) FROM disputes WHERE milestone_id=? AND state='open'",
                (row["milestone_id"],),
            ).fetchone()[0]
            if open_dispute:
                raise InvalidState("里程碑存在未解决争议，相关拨付冻结中")
            used_row = self.connection.execute(
                "SELECT coalesce(sum(CAST(amount_cny AS REAL)),0) used FROM disbursements "
                "WHERE source_id=? AND state IN ('frozen','approved','released_after_freeze','paid') "
                "AND disbursement_id<>?",
                (row["source_id"], disbursement_id),
            ).fetchone()
            source = self.connection.execute(
                "SELECT total_amount_cny FROM funding_sources WHERE source_id=?", (row["source_id"],)
            ).fetchone()
            if Decimal(str(used_row["used"])) + Decimal(row["amount_cny"]) > Decimal(source["total_amount_cny"]):
                raise Conflict("批准后将超过资金来源承诺总额")
        new_state = "approved" if decision == "approve" else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE disbursements SET state=?,reviewed_by=?,reviewed_at=?,revision=revision+1 "
                "WHERE disbursement_id=?",
                (new_state, actor_id, self._now(), disbursement_id),
            )
            self._audit(
                "disbursement",
                disbursement_id,
                f"disbursement.{new_state}",
                actor_id,
                {"milestone_id": row["milestone_id"], "note": note, "amount_cny": row["amount_cny"]},
            )
        return {"disbursement_id": disbursement_id, "state": new_state, "reviewed_by": actor_id}

    def pay_disbursement(self, actor_id: str, disbursement_id: str) -> dict[str, Any]:
        self._require(actor_id, "disbursement.write")
        row = self.connection.execute("SELECT * FROM disbursements WHERE disbursement_id=?", (disbursement_id,)).fetchone()
        if row is None:
            raise NotFound("拨付不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准的拨付可以支付")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE disbursements SET state='paid',paid_at=?,revision=revision+1 "
                "WHERE disbursement_id=? AND state='approved'",
                (self._now(), disbursement_id),
            )
            self._audit(
                "disbursement",
                disbursement_id,
                "disbursement.paid",
                actor_id,
                {"milestone_id": row["milestone_id"], "amount_cny": row["amount_cny"], "source_id": row["source_id"]},
            )
        return {"disbursement_id": disbursement_id, "state": "paid", "paid_at": self._now()}

    def _freeze_reason(self, project_id: str, milestone_id: str) -> str | None:
        project = self._get_project(project_id)
        if project["state"] == "suspended":
            return "project_suspended"
        dispute = self.connection.execute(
            "SELECT dispute_id FROM disputes WHERE milestone_id=? AND state='open' ORDER BY raised_at DESC LIMIT 1",
            (milestone_id,),
        ).fetchone()
        if dispute is not None:
            return f"dispute:{dispute['dispute_id']}"
        milestone = self._get_milestone(milestone_id)
        if milestone["state"] == "paused":
            return "milestone_paused"
        return None

    def _reconcile_disbursements(self, project_id: str, milestone_id: str) -> None:
        """按争议/暂停状态冻结或解冻相关拨付；已支付/已拒绝的不动。"""
        reason = self._freeze_reason(project_id, milestone_id)
        rows = self.connection.execute(
            "SELECT * FROM disbursements WHERE milestone_id=? AND state IN "
            "('proposed','approved','released_after_freeze','frozen')",
            (milestone_id,),
        ).fetchall()
        for row in rows:
            if reason is not None:
                if row["state"] != "frozen":
                    self.connection.execute(
                        "UPDATE disbursements SET state='frozen',freeze_reason=?,reviewed_by=NULL,reviewed_at=NULL,"
                        "revision=revision+1 WHERE disbursement_id=?",
                        (reason, row["disbursement_id"]),
                    )
            elif row["state"] == "frozen":
                self.connection.execute(
                    "UPDATE disbursements SET state='released_after_freeze',revision=revision+1 "
                    "WHERE disbursement_id=?",
                    (row["disbursement_id"],),
                )

    # -------------------------------------------------------------- 项目生命周期

    def _lifecycle(self, actor_id: str, project_id: str, action: str, reason: str) -> None:
        self.connection.execute(
            "INSERT INTO project_lifecycles(project_id,action,reason,acted_by,acted_at) VALUES(?,?,?,?,?)",
            (project_id, action, reason, actor_id, self._now()),
        )

    def suspend_project(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.lifecycle")
        project = self._get_project(project_id)
        if project["state"] not in {"active", "restarted"}:
            raise InvalidState("只有执行中的项目可以暂停")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE projects SET state='suspended',revision=revision+1 WHERE project_id=?",
                (project_id,),
            )
            self._lifecycle(actor_id, project_id, "suspend", reason)
            milestone_ids = [
                row["milestone_id"]
                for row in self.connection.execute(
                    "SELECT milestone_id FROM milestones WHERE project_id=?", (project_id,)
                )
            ]
            for milestone_id in milestone_ids:
                self._reconcile_disbursements(project_id, milestone_id)
            self._audit("project", project_id, "project.suspended", actor_id, {"reason": reason})
        return {"project_id": project_id, "state": "suspended"}

    def restart_project(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.lifecycle")
        project = self._get_project(project_id)
        if project["state"] != "suspended":
            raise InvalidState("只有暂停中的项目可以重启")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE projects SET state='restarted',revision=revision+1 WHERE project_id=?",
                (project_id,),
            )
            self._lifecycle(actor_id, project_id, "restart", reason)
            milestone_ids = [
                row["milestone_id"]
                for row in self.connection.execute(
                    "SELECT milestone_id FROM milestones WHERE project_id=?", (project_id,)
                )
            ]
            for milestone_id in milestone_ids:
                self._reconcile_disbursements(project_id, milestone_id)
            self._audit("project", project_id, "project.restarted", actor_id, {"reason": reason})
        return {"project_id": project_id, "state": "restarted"}

    def close_project(self, actor_id: str, project_id: str, action: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.lifecycle")
        if action not in {"complete", "cancel"}:
            raise ValidationFailed("结项动作必须是 complete 或 cancel")
        project = self._get_project(project_id)
        if action == "complete" and project["state"] not in {"active", "restarted"}:
            raise InvalidState("执行中的项目才能结项")
        if action == "cancel" and project["state"] in {"completed", "cancelled"}:
            raise InvalidState("项目已结束")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE projects SET state=?,revision=revision+1 WHERE project_id=?",
                ("completed" if action == "complete" else "cancelled", project_id),
            )
            self._lifecycle(actor_id, project_id, action, reason)
            self._audit("project", project_id, f"project.{action}d", actor_id, {"reason": reason})
        return {"project_id": project_id, "state": "completed" if action == "complete" else "cancelled"}

    # -------------------------------------------------------------- 历史查询

    def milestone_history(self, actor_id: str, milestone_id: str) -> dict[str, Any]:
        """返回里程碑锚定版本、当时采用的验收规则、提交/证据/决定全链路。"""
        self._require(actor_id, "report.read")
        milestone = self._get_milestone(milestone_id)
        rules = [dict(row) for row in self._rules_for_milestone(milestone)]
        submissions: list[dict[str, Any]] = []
        for submission in self.connection.execute(
            "SELECT * FROM milestone_submissions WHERE milestone_id=? ORDER BY attempt",
            (milestone_id,),
        ):
            item = dict(submission)
            item["evidences"] = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM acceptance_evidences WHERE submission_id=? ORDER BY evidence_id",
                    (submission["submission_id"],),
                )
            ]
            item["decisions"] = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM acceptance_decisions WHERE submission_id=? ORDER BY decision_id",
                    (submission["submission_id"],),
                )
            ]
            submissions.append(item)
        disputes = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM disputes WHERE milestone_id=? ORDER BY raised_at", (milestone_id,)
            )
        ]
        return {
            "milestone": dict(milestone),
            "anchored_agreement_version": dict(self._get_version(milestone["project_id"], milestone["anchored_version_no"])),
            "acceptance_rules_in_force": [
                {
                    "rule_id": row["rule_id"],
                    "name": row["name"],
                    "rule": json.loads(row["rule_json"]),
                    "version_no": row["version_no"],
                }
                for row in rules
            ],
            "submissions": submissions,
            "disputes": disputes,
        }

    def outstanding_funding_decisions(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """未结资金决定：拟拨付、冻结中、解冻待复核、已批准未支付。"""
        self._require(actor_id, "report.read")
        self._get_project(project_id)
        rows = self.connection.execute(
            "SELECT * FROM disbursements WHERE project_id=? AND state IN "
            "('proposed','frozen','released_after_freeze','approved') ORDER BY created_at,disbursement_id",
            (project_id,),
        ).fetchall()
        paid = self.connection.execute(
            "SELECT coalesce(sum(CAST(amount_cny AS REAL)),0) v FROM disbursements "
            "WHERE project_id=? AND state='paid'",
            (project_id,),
        ).fetchone()["v"]
        return {
            "project_id": project_id,
            "open_decisions": [dict(row) for row in rows],
            "open_count": len(rows),
            "open_total_cny": format(sum((Decimal(r["amount_cny"]) for r in rows), ZERO).quantize(MONEY_QUANTUM), "f"),
            "paid_total_cny": format(Decimal(str(paid)).quantize(MONEY_QUANTUM), "f"),
        }

    def project_dossier(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        project = self._get_project(project_id)
        versions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT project_id,version_no,title,content_sha256,supersedes_version_no,state,"
                "signed_at,signed_by,change_summary,created_at FROM agreement_versions "
                "WHERE project_id=? ORDER BY version_no",
                (project_id,),
            )
        ]
        lifecycles = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM project_lifecycles WHERE project_id=? ORDER BY lifecycle_id",
                (project_id,),
            )
        ]
        milestones = [
            dict(row)
            for row in self.connection.execute(
                "SELECT milestone_id,code,name,sequence_no,anchored_version_no,planned_date,state,revision "
                "FROM milestones WHERE project_id=? ORDER BY sequence_no",
                (project_id,),
            )
        ]
        funding = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM funding_sources WHERE project_id=? ORDER BY source_id",
                (project_id,),
            )
        ]
        return {
            "project": dict(project),
            "agreement_versions": versions,
            "lifecycles": lifecycles,
            "milestones": milestones,
            "funding_sources": funding,
            "outstanding_funding": self.outstanding_funding_decisions(actor_id, project_id),
        }

    # -------------------------------------------------------------- 审计

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM jp_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

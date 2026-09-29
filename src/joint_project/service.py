"""产学研联合研发项目管理的领域用例。

关键规则：
- 协议只能整体签署为新版本；版本内容只增不改；
- 修订时，已确认（已评审或已形成资金决定）的里程碑被锁定，
  其标题、计划日期、拨付比例与验收规则在新版本中必须保持一致；
- 里程碑可接受、部分接受、退回补证或暂停；争议只冻结该节点的拨付；
- 技术验收与财务复核必须由不同授权人完成；
- 每次验收冗余保存当时规则全文快照，资金决定记录所依据的版本。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from .contracts import AgreementDraft, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "secretary": {
        "project.register", "agreement.sign", "milestone.suspend",
        "project.close", "dispute.resolve", "report.read", "audit.read",
    },
    "technical_reviewer": {"review.write", "report.read", "audit.read"},
    "finance_officer": {"payment.decide", "payment.execute", "report.read", "audit.read"},
    "party_representative": {"evidence.submit", "dispute.open", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

REVIEWABLE_STATES = {"submitted", "partially_accepted"}
SUSPENDABLE_STATES = {"pending", "submitted", "partially_accepted", "returned"}
PAYABLE_STATES = {"accepted", "partially_accepted"}

DECISION_TO_STATE = {
    "accepted": "accepted",
    "partially_accepted": "partially_accepted",
    "returned": "returned",
}


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


class ProjectService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if user["role"] != "admin" and permission not in ROLE_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(
        self, actor_id: str | None, user_id: str, display_name: str, role: str, org_name: str | None = None
    ) -> dict[str, Any]:
        if actor_id is not None:
            self._require(actor_id, "user.manage")
        if role not in ROLE_PERMISSIONS and role != "admin":
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,org_name) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, org_name),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "display_name": display_name.strip(), "role": role}

    # ------------------------------------------------------------------ 项目

    def register_project(self, actor_id: str, project_id: str, title: str) -> dict[str, Any]:
        self._require(actor_id, "project.register")
        if not project_id.strip() or not title.strip():
            raise ValidationFailed("项目编号和名称不能为空")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO projects(project_id,title,status,current_version_id,created_by,created_at,updated_at) "
                    "VALUES(?,?, 'draft', NULL,?,?,?)",
                    (project_id.strip(), title.strip(), actor_id, now, now),
                )
                self._audit("project", project_id, "project.registered", actor_id, {"title": title.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"项目已存在: {project_id}") from exc
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        result = dict(row)
        if result["current_version_id"] is not None:
            version = self.connection.execute(
                "SELECT version_seq, version_label, content_sha256 FROM agreement_versions WHERE version_id=?",
                (result["current_version_id"],),
            ).fetchone()
            result["current_version_seq"] = version["version_seq"]
            result["current_version_label"] = version["version_label"]
            result["current_version_sha256"] = version["content_sha256"]
        return result

    def close_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "project.close")
        with transaction(self.connection, immediate=True):
            project = self._locked_project(project_id)
            if project["status"] != "active":
                raise InvalidState("只有执行中的项目可以关闭")
            open_disputes = self.connection.execute(
                "SELECT count(*) FROM disputes WHERE project_id=? AND status='open'", (project_id,)
            ).fetchone()[0]
            if open_disputes:
                raise InvalidState("仍有未解决的争议，不能关闭")
            pending = self.connection.execute(
                "SELECT count(*) FROM milestone_instances WHERE project_id=? AND active_version_seq IS NOT NULL "
                "AND status NOT IN ('accepted')",
                (project_id,),
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有未完成或暂停的里程碑，不能关闭")
            outstanding = self.connection.execute(
                "SELECT count(*) FROM payment_decisions p "
                "JOIN milestone_instances m ON m.instance_id=p.instance_id "
                "WHERE m.project_id=? AND p.decision='approved' AND p.executed_at IS NULL",
                (project_id,),
            ).fetchone()[0]
            if outstanding:
                raise InvalidState("仍有已批准未执行的拨付，不能关闭")
            self.connection.execute(
                "UPDATE projects SET status='closed',updated_at=? WHERE project_id=?", (self._now(), project_id)
            )
            self._audit("project", project_id, "project.closed", actor_id, {})
        return self.get_project(project_id)

    def _locked_project(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        return row

    # ------------------------------------------------------------------ 协议

    def sign_agreement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """签署协议首版或提交一次修订；修订永不覆盖历史。"""

        self._require(actor_id, "agreement.sign")
        try:
            draft = AgreementDraft.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(draft.to_canonical())
        digest = content_digest([draft.to_canonical()])

        with transaction(self.connection, immediate=True):
            project = self._locked_project(draft.project_id)
            if project["status"] not in ("draft", "active"):
                raise InvalidState("项目已关闭，不能再修订协议")
            is_first = project["current_version_id"] is None
            if not is_first and self.connection.execute(
                "SELECT 1 FROM disputes d JOIN milestone_instances m ON m.instance_id=d.instance_id "
                "WHERE d.project_id=? AND d.status='open' AND m.active_version_seq IS NOT NULL",
                (draft.project_id,),
            ).fetchone():
                raise InvalidState("项目存在未解决的争议，请先处理再修订协议")
            if self.connection.execute(
                "SELECT 1 FROM agreement_versions WHERE content_sha256=?", (digest,)
            ).fetchone():
                raise Conflict("内容完全相同的协议版本已经存在")
            next_seq = 1 if is_first else self.connection.execute(
                "SELECT MAX(version_seq)+1 FROM agreement_versions WHERE project_id=?", (draft.project_id,)
            ).fetchone()[0]
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO agreement_versions"
                "(project_id,version_seq,version_label,note,canonical_json,content_sha256,signed_by,signed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (draft.project_id, next_seq, draft.version_label, draft.note, text, digest, actor_id, now),
            )
            version_id = cursor.lastrowid
            self._insert_version_rows(version_id, draft)
            materialized = self._materialize_milestones(
                draft, version_id, next_seq, create_fresh=is_first
            )
            self.connection.execute(
                "UPDATE projects SET current_version_id=?, updated_at=? WHERE project_id=?",
                (version_id, now, draft.project_id),
            )
            if is_first:
                self.connection.execute(
                    "UPDATE projects SET status='active' WHERE project_id=?", (draft.project_id,)
                )
            self._audit(
                "agreement", f"{draft.project_id}@v{next_seq}",
                "agreement.signed" if is_first else "agreement.amended",
                actor_id,
                {"version_seq": next_seq, "sha256": digest, "milestones": materialized},
            )
        return {
            "project_id": draft.project_id,
            "version_id": version_id,
            "version_seq": next_seq,
            "version_label": draft.version_label,
            "content_sha256": digest,
            "milestones": materialized,
        }

    def _insert_version_rows(self, version_id: int, draft: AgreementDraft) -> None:
        for seq, party in enumerate(draft.parties):
            self.connection.execute(
                "INSERT INTO parties(version_id,party_id,name,kind,contact,seq) VALUES(?,?,?,?,?,?)",
                (version_id, party.party_id, party.name, party.kind, party.contact, seq),
            )
        for item in draft.commitments:
            self.connection.execute(
                "INSERT INTO commitments(version_id,commitment_id,party_id,category,description,unit,quantity_cents) "
                "VALUES(?,?,?,?,?,?,?)",
                (version_id, item.commitment_id, item.party_id, item.category, item.description,
                 item.unit, _cents(item.quantity)),
            )
        for seq, item in enumerate(draft.funding_sources):
            self.connection.execute(
                "INSERT INTO funding_sources(version_id,source_id,party_id,name,amount_cents,seq) "
                "VALUES(?,?,?,?,?,?)",
                (version_id, item.source_id, item.party_id, item.name, _cents(item.amount), seq),
            )
        for seq, rule in enumerate(draft.rules):
            self.connection.execute(
                "INSERT INTO acceptance_rules(version_id,rule_id,title,criterion,evidence_required,"
                "accept_ratio_pct,seq) VALUES(?,?,?,?,?,?,?)",
                (version_id, rule.rule_id, rule.title, rule.criterion, rule.evidence_required,
                 rule.accept_ratio_pct, seq),
            )
        for seq, item in enumerate(draft.deliverables):
            self.connection.execute(
                "INSERT INTO deliverables(version_id,deliverable_id,milestone_id,owner_party_id,title,"
                "description,seq) VALUES(?,?,?,?,?,?,?)",
                (version_id, item.deliverable_id, item.milestone_id, item.owner_party_id, item.title,
                 item.description, seq),
            )

    def _materialize_milestones(
        self, draft: AgreementDraft, version_id: int, version_seq: int, *, create_fresh: bool
    ) -> list[dict[str, Any]]:
        now = self._now()
        result: list[dict[str, Any]] = []
        if create_fresh:
            for seq, item in enumerate(draft.milestones):
                cursor = self.connection.execute(
                    "INSERT INTO milestone_instances(project_id,milestone_id,definition_version_id,"
                    "first_version_id,carry_of_id,active_version_seq,seq_in_version,title,plan_date,"
                    "payment_ratio_pct,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?)",
                    (draft.project_id, item.milestone_id, version_id, version_id, None, version_seq,
                     seq, item.title, item.plan_date, item.payment_ratio_pct, now),
                )
                result.append({
                    "milestone_id": item.milestone_id, "instance_id": cursor.lastrowid,
                    "carried_from": None, "locked": False,
                })
            return result

        active_rows = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE project_id=? AND active_version_seq IS NOT NULL",
            (draft.project_id,),
        ).fetchall()
        active = {row["milestone_id"]: row for row in active_rows}
        defined = {item.milestone_id: item for item in draft.milestones}
        new_rules = {rule.rule_id: rule for rule in draft.rules}

        for milestone_id, row in active.items():
            if milestone_id not in defined and (row["locked"] or row["current_round"] > 0):
                raise InvalidState(f"已进入评审的里程碑 {milestone_id} 不能在修订中删除")
        for seq, item in enumerate(draft.milestones):
            previous = active.get(item.milestone_id)
            if previous is None:
                cursor = self.connection.execute(
                    "INSERT INTO milestone_instances(project_id,milestone_id,definition_version_id,"
                    "first_version_id,carry_of_id,active_version_seq,seq_in_version,title,plan_date,"
                    "payment_ratio_pct,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?)",
                    (draft.project_id, item.milestone_id, version_id, version_id, None, version_seq,
                     seq, item.title, item.plan_date, item.payment_ratio_pct, now),
                )
                result.append({
                    "milestone_id": item.milestone_id, "instance_id": cursor.lastrowid,
                    "carried_from": None, "locked": False,
                })
                continue
            if previous["locked"] or previous["current_round"] > 0:
                # 已评审或已拨付的节点其历史不能被改写：标题/日期/比例/规则集
                # 必须与原定义一致，操作状态（含部分接受、暂停、冻结）原样延续。
                self._assert_locked_unchanged(previous, item, new_rules)
                cursor = self.connection.execute(
                    "INSERT INTO milestone_instances(project_id,milestone_id,definition_version_id,"
                    "first_version_id,carry_of_id,active_version_seq,seq_in_version,title,plan_date,"
                    "payment_ratio_pct,status,suspended_from,current_round,locked,payment_frozen,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (draft.project_id, item.milestone_id, version_id, previous["first_version_id"],
                     previous["instance_id"], version_seq, seq,
                     previous["title"], previous["plan_date"], previous["payment_ratio_pct"],
                     previous["status"], previous["suspended_from"], previous["current_round"],
                     previous["locked"], previous["payment_frozen"], now),
                )
            else:
                # 尚未进入评审的节点：允许在新版本中重新定义，旧实例仍保留历史
                cursor = self.connection.execute(
                    "INSERT INTO milestone_instances(project_id,milestone_id,definition_version_id,"
                    "first_version_id,carry_of_id,active_version_seq,seq_in_version,title,plan_date,"
                    "payment_ratio_pct,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?)",
                    (draft.project_id, item.milestone_id, version_id, previous["first_version_id"],
                     previous["instance_id"], version_seq, seq,
                     item.title, item.plan_date, item.payment_ratio_pct, now),
                )
            self.connection.execute(
                "UPDATE milestone_instances SET active_version_seq=NULL WHERE instance_id=?",
                (previous["instance_id"],),
            )
            result.append({
                "milestone_id": item.milestone_id, "instance_id": cursor.lastrowid,
                "carried_from": previous["instance_id"], "locked": bool(previous["locked"]),
            })
        for milestone_id, row in active.items():
            if milestone_id not in defined:
                self.connection.execute(
                    "UPDATE milestone_instances SET active_version_seq=NULL WHERE instance_id=?",
                    (row["instance_id"],),
                )
                result.append({
                    "milestone_id": milestone_id, "instance_id": row["instance_id"],
                    "carried_from": None, "locked": False, "retired": True,
                })
        return result

    def _assert_locked_unchanged(
        self, previous: sqlite3.Row, item: Any, new_rules: Mapping[str, Any]
    ) -> None:
        changes: list[str] = []
        if previous["title"] != item.title:
            changes.append("标题")
        if previous["plan_date"] != item.plan_date:
            changes.append("计划日期")
        if previous["payment_ratio_pct"] != item.payment_ratio_pct:
            changes.append("拨付比例")
        old_rule_ids = self._milestone_rule_ids(previous["definition_version_id"], previous["milestone_id"])
        placeholders = ",".join("?" for _ in old_rule_ids)
        old_rules = {
            row["rule_id"]: row for row in self.connection.execute(
                f"SELECT rule_id,title,criterion,evidence_required,accept_ratio_pct "
                f"FROM acceptance_rules WHERE version_id=? AND rule_id IN ({placeholders})",
                (previous["definition_version_id"], *old_rule_ids),
            ).fetchall()
        }
        if set(old_rules) != set(item.rule_ids):
            changes.append("验收规则集")
        else:
            for rule_id in item.rule_ids:
                old, new = old_rules[rule_id], new_rules[rule_id]
                if (old["title"], old["criterion"], old["evidence_required"], old["accept_ratio_pct"]) != (
                    new.title, new.criterion, new.evidence_required, new.accept_ratio_pct
                ):
                    changes.append(f"验收规则 {rule_id} 的内容")
                    break
        if changes:
            raise InvalidState(
                f"已确认里程碑 {item.milestone_id} 的{'、'.join(changes)}不能被修订改写"
            )

    def _milestone_rule_ids(self, version_id: int, milestone_id: str) -> list[str]:
        row = self.connection.execute(
            "SELECT canonical_json FROM agreement_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        content = json.loads(row["canonical_json"])
        for milestone in content["milestones"]:
            if milestone["milestone_id"] == milestone_id:
                return list(milestone["rule_ids"])
        raise NotFound("里程碑定义不存在")

    def list_versions(self, actor_id: str, project_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        self._locked_project(project_id)
        rows = self.connection.execute(
            "SELECT version_id,version_seq,version_label,note,content_sha256,signed_by,signed_at "
            "FROM agreement_versions WHERE project_id=? ORDER BY version_seq",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_version(self, project_id: str, version_seq: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM agreement_versions WHERE project_id=? AND version_seq=?",
            (project_id, version_seq),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return {
            "version_id": row["version_id"],
            "project_id": row["project_id"],
            "version_seq": row["version_seq"],
            "version_label": row["version_label"],
            "note": row["note"],
            "content_sha256": row["content_sha256"],
            "signed_by": row["signed_by"],
            "signed_at": row["signed_at"],
            "content": json.loads(row["canonical_json"]),
        }

    # ------------------------------------------------------------- 里程碑/证据

    def _active_instance(self, project_id: str, milestone_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE project_id=? AND milestone_id=? "
            "AND active_version_seq IS NOT NULL",
            (project_id, milestone_id),
        ).fetchone()
        if row is None:
            raise NotFound("当前协议版本中没有该里程碑")
        return row

    def list_milestones(self, actor_id: str, project_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE project_id=? AND active_version_seq IS NOT NULL "
            "ORDER BY seq_in_version",
            (project_id,),
        ).fetchall()
        return [self._milestone_view(row) for row in rows]

    def get_milestone(self, actor_id: str, project_id: str, milestone_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        instance = self._active_instance(project_id, milestone_id)
        return self._milestone_view(instance, history=True)

    def _milestone_view(self, row: sqlite3.Row, *, history: bool = False) -> dict[str, Any]:
        version = self.connection.execute(
            "SELECT version_seq,version_label FROM agreement_versions WHERE version_id=?",
            (row["definition_version_id"],),
        ).fetchone()
        view = {
            "milestone_id": row["milestone_id"],
            "instance_id": row["instance_id"],
            "title": row["title"],
            "plan_date": row["plan_date"],
            "payment_ratio_pct": row["payment_ratio_pct"],
            "status": row["status"],
            "suspended_from": row["suspended_from"],
            "current_round": row["current_round"],
            "locked": bool(row["locked"]),
            "payment_frozen": bool(row["payment_frozen"]),
            "definition_version_seq": version["version_seq"],
            "definition_version_label": version["version_label"],
            "carry_of_id": row["carry_of_id"],
            "first_version_id": row["first_version_id"],
        }
        if history:
            chain_ids = self._chain_instance_ids(row["instance_id"])
            placeholders = ",".join("?" for _ in chain_ids)
            view["evidence"] = [dict(item) for item in self.connection.execute(
                f"SELECT evidence_id,instance_id,submitter,title,ref,content_sha256,note,submitted_at "
                f"FROM evidence WHERE instance_id IN ({placeholders}) ORDER BY evidence_id",
                chain_ids,
            ).fetchall()]
            view["reviews"] = [self._review_view(item) for item in self.connection.execute(
                f"SELECT r.* FROM acceptance_reviews r WHERE r.instance_id IN ({placeholders}) "
                f"ORDER BY r.round, r.review_id",
                chain_ids,
            ).fetchall()]
            view["payments"] = [self._payment_view(item["payment_id"]) for item in self.connection.execute(
                f"SELECT payment_id FROM payment_decisions WHERE instance_id IN ({placeholders}) ORDER BY payment_id",
                chain_ids,
            ).fetchall()]
            view["disputes"] = [dict(item) for item in self.connection.execute(
                f"SELECT * FROM disputes WHERE instance_id IN ({placeholders}) ORDER BY dispute_id",
                chain_ids,
            ).fetchall()]
            view["ancestors"] = self._carry_chain(row["instance_id"])
        return view

    def _carry_chain(self, instance_id: int) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        current = self.connection.execute(
            "SELECT carry_of_id FROM milestone_instances WHERE instance_id=?", (instance_id,)
        ).fetchone()["carry_of_id"]
        while current is not None:
            row = self.connection.execute(
                "SELECT instance_id,milestone_id,definition_version_id,status,current_round "
                "FROM milestone_instances WHERE instance_id=?", (current,)
            ).fetchone()
            version = self.connection.execute(
                "SELECT version_seq,version_label FROM agreement_versions WHERE version_id=?",
                (row["definition_version_id"],),
            ).fetchone()
            chain.append({
                "instance_id": row["instance_id"],
                "version_seq": version["version_seq"],
                "version_label": version["version_label"],
                "status": row["status"],
                "round": row["current_round"],
            })
            current = self.connection.execute(
                "SELECT carry_of_id FROM milestone_instances WHERE instance_id=?", (current,)
            ).fetchone()["carry_of_id"]
        return chain

    def _chain_instance_ids(self, instance_id: int) -> list[int]:
        """当前实例在前、历代实例在后的完整 carry 链。"""

        return [instance_id, *[item["instance_id"] for item in self._carry_chain(instance_id)]]

    def submit_evidence(
        self, actor_id: str, project_id: str, milestone_id: str,
        title: str, ref: str, content_sha256: str | None = None, note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence.submit")
        if not title.strip() or not ref.strip():
            raise ValidationFailed("证据标题与存档编号不能为空")
        if content_sha256 is not None and len(content_sha256) != 64:
            raise ValidationFailed("证据摘要必须是 64 位 SHA-256")
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] == "suspended":
                raise InvalidState("里程碑已暂停，暂停期间不能提交证据")
            if instance["status"] not in ("pending", "submitted", "returned", "partially_accepted"):
                raise InvalidState(f"当前状态 {instance['status']} 不接受证据提交")
            cursor = self.connection.execute(
                "INSERT INTO evidence(instance_id,submitter,title,ref,content_sha256,note,submitted_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (instance["instance_id"], actor_id, title.strip(), ref.strip(),
                 content_sha256, note, self._now()),
            )
            if instance["status"] in ("pending", "returned"):
                self.connection.execute(
                    "UPDATE milestone_instances SET status='submitted' WHERE instance_id=?",
                    (instance["instance_id"],),
                )
                resulting_status = "submitted"
            else:
                resulting_status = instance["status"]
            self._audit(
                "milestone", f"{project_id}:{milestone_id}", "evidence.submitted", actor_id,
                {"instance_id": instance["instance_id"], "evidence_id": cursor.lastrowid, "ref": ref.strip()},
            )
        return {
            "evidence_id": cursor.lastrowid, "instance_id": instance["instance_id"],
            "status": resulting_status,
        }

    def _review_view(self, row: sqlite3.Row) -> dict[str, Any]:
        version = self.connection.execute(
            "SELECT version_seq,version_label FROM agreement_versions WHERE version_id=?",
            (row["definition_version_id"],),
        ).fetchone()
        return {
            "review_id": row["review_id"],
            "instance_id": row["instance_id"],
            "round": row["round"],
            "reviewer": row["reviewer"],
            "decision": row["decision"],
            "achieved_ratio_pct": row["achieved_ratio_pct"],
            "note": row["note"],
            "reviewed_at": row["reviewed_at"],
            "evidence_ids": json.loads(row["evidence_ids_json"]),
            "rule_version_seq": version["version_seq"],
            "rule_version_label": version["version_label"],
            "rule_snapshots": json.loads(row["rule_snapshots_json"]),
        }

    def review_acceptance(
        self, actor_id: str, project_id: str, milestone_id: str, decision: str,
        achieved_ratio_pct: int, note: str = "", evidence_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "review.write")
        if decision not in DECISION_TO_STATE:
            raise ValidationFailed("验收结论必须是 accepted、partially_accepted 或 returned")
        if isinstance(achieved_ratio_pct, bool) or not isinstance(achieved_ratio_pct, int):
            raise ValidationFailed("达成比例必须是整数")
        self._check_ratio(decision, achieved_ratio_pct)
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] not in REVIEWABLE_STATES:
                raise InvalidState(f"里程碑状态 {instance['status']} 不具备评审条件")
            evidence_rows = self.connection.execute(
                "SELECT * FROM evidence WHERE instance_id=? ORDER BY evidence_id",
                (instance["instance_id"],),
            ).fetchall()
            if not evidence_rows:
                raise InvalidState("尚未收到任何验收证据")
            by_id = {row["evidence_id"]: row for row in evidence_rows}
            chosen_ids = sorted(evidence_ids) if evidence_ids else sorted(by_id)
            if not chosen_ids or any(eid not in by_id for eid in chosen_ids):
                raise ValidationFailed("引用的证据不存在或不属于该里程碑")
            if any(by_id[eid]["submitter"] == actor_id for eid in chosen_ids):
                raise Forbidden("评审人不能复核自己提交的证据")
            # 轮次沿 carry 链延续：修订不重置已进行的评审轮次
            round_no = self.connection.execute(
                "WITH RECURSIVE chain(id, parent) AS ("
                "SELECT instance_id, carry_of_id FROM milestone_instances WHERE instance_id=?"
                " UNION ALL "
                "SELECT m.instance_id, m.carry_of_id FROM milestone_instances m "
                "JOIN chain c ON m.instance_id=c.parent) "
                "SELECT COALESCE(MAX(r.round),0)+1 FROM acceptance_reviews r "
                "JOIN chain c ON c.id=r.instance_id",
                (instance["instance_id"],),
            ).fetchone()[0]
            rule_ids = self._milestone_rule_ids(instance["definition_version_id"], milestone_id)
            snapshots = [dict(row) for row in self.connection.execute(
                "SELECT rule_id,title,criterion,evidence_required,accept_ratio_pct "
                "FROM acceptance_rules WHERE version_id=? AND rule_id IN (%s) ORDER BY seq"
                % ",".join("?" for _ in rule_ids),
                (instance["definition_version_id"], *rule_ids),
            ).fetchall()]
            if len(snapshots) != len(rule_ids):
                raise InvalidState("里程碑引用的验收规则在当前版本中缺失")
            cursor = self.connection.execute(
                "INSERT INTO acceptance_reviews(instance_id,round,reviewer,decision,achieved_ratio_pct,"
                "rule_snapshots_json,evidence_ids_json,definition_version_id,note,reviewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (instance["instance_id"], round_no, actor_id, decision, achieved_ratio_pct,
                 canonical_json(snapshots), canonical_json(chosen_ids),
                 instance["definition_version_id"], note, self._now()),
            )
            self.connection.execute(
                "UPDATE milestone_instances SET status=?,current_round=? WHERE instance_id=?",
                (DECISION_TO_STATE[decision], round_no, instance["instance_id"]),
            )
            self._audit(
                "milestone", f"{project_id}:{milestone_id}", "acceptance.reviewed", actor_id,
                {"instance_id": instance["instance_id"], "round": round_no, "decision": decision,
                 "achieved_ratio_pct": achieved_ratio_pct, "review_id": cursor.lastrowid},
            )
            review = self.connection.execute(
                "SELECT * FROM acceptance_reviews WHERE review_id=?", (cursor.lastrowid,)
            ).fetchone()
        return self._review_view(review)

    @staticmethod
    def _check_ratio(decision: str, ratio: int) -> None:
        if not 0 <= ratio <= 100:
            raise ValidationFailed("达成比例必须在 0-100 之间")
        if decision == "accepted" and ratio != 100:
            raise ValidationFailed("完全接受要求达成比例为 100，否则应选择部分接受")
        if decision == "partially_accepted" and not 1 <= ratio <= 99:
            raise ValidationFailed("部分接受的达成比例必须在 1-99 之间")
        if decision == "returned" and not 0 <= ratio <= 99:
            raise ValidationFailed("退回补证的达成比例必须小于 100")

    # ------------------------------------------------------------- 暂停与恢复

    def suspend_milestone(self, actor_id: str, project_id: str, milestone_id: str) -> dict[str, Any]:
        self._require(actor_id, "milestone.suspend")
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] == "suspended":
                raise InvalidState("里程碑已经处于暂停状态")
            if instance["status"] not in SUSPENDABLE_STATES:
                raise InvalidState(f"里程碑状态 {instance['status']} 不能暂停")
            self.connection.execute(
                "UPDATE milestone_instances SET status='suspended',suspended_from=? WHERE instance_id=?",
                (instance["status"], instance["instance_id"]),
            )
            self._audit(
                "milestone", f"{project_id}:{milestone_id}", "milestone.suspended", actor_id,
                {"instance_id": instance["instance_id"], "from": instance["status"]},
            )
        return self._active_instance_simple(project_id, milestone_id)

    def resume_milestone(self, actor_id: str, project_id: str, milestone_id: str) -> dict[str, Any]:
        self._require(actor_id, "milestone.suspend")
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] != "suspended":
                raise InvalidState("里程碑未处于暂停状态")
            restored = instance["suspended_from"] or "submitted"
            self.connection.execute(
                "UPDATE milestone_instances SET status=?,suspended_from=NULL WHERE instance_id=?",
                (restored, instance["instance_id"]),
            )
            self._audit(
                "milestone", f"{project_id}:{milestone_id}", "milestone.resumed", actor_id,
                {"instance_id": instance["instance_id"], "to": restored},
            )
        return self._active_instance_simple(project_id, milestone_id)

    def _active_instance_simple(self, project_id: str, milestone_id: str) -> dict[str, Any]:
        return self._milestone_view(self._active_instance(project_id, milestone_id))

    # ------------------------------------------------------------------ 争议

    def open_dispute(
        self, actor_id: str, project_id: str, milestone_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.open")
        if not reason.strip():
            raise ValidationFailed("争议理由不能为空")
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] == "suspended":
                raise InvalidState("里程碑暂停中，请先恢复再登记争议")
            if self.connection.execute(
                "SELECT 1 FROM open_dispute_per_instance WHERE instance_id=?",
                (instance["instance_id"],),
            ).fetchone():
                raise Conflict("该里程碑已有未解决的争议")
            if instance["current_round"] == 0:
                raise InvalidState("里程碑尚未进入评审，没有可冻结的资金决定")
            chain_ids = self._chain_instance_ids(instance["instance_id"])
            chain_sql = ",".join("?" for _ in chain_ids)
            current = self.get_project(project_id)
            source_count = self.connection.execute(
                "SELECT count(*) FROM funding_sources WHERE version_id=?",
                (current["current_version_id"],),
            ).fetchone()[0]
            outstanding = self.connection.execute(
                f"SELECT count(*) FROM payment_decisions WHERE instance_id IN ({chain_sql}) "
                "AND round=? AND decision='approved' AND executed_at IS NULL",
                (*chain_ids, instance["current_round"]),
            ).fetchone()[0]
            executed = self.connection.execute(
                f"SELECT count(*) FROM payment_decisions WHERE instance_id IN ({chain_sql}) "
                "AND round=? AND executed_at IS NOT NULL",
                (*chain_ids, instance["current_round"]),
            ).fetchone()[0]
            if outstanding == 0 and executed >= source_count:
                raise InvalidState("该节点各资金来源均已拨付，没有可冻结的未结资金")
            cursor = self.connection.execute(
                "INSERT INTO disputes(project_id,instance_id,opened_by,reason,status,opened_at) "
                "VALUES(?,?,?,?,'open',?)",
                (project_id, instance["instance_id"], actor_id, reason.strip(), self._now()),
            )
            dispute_id = cursor.lastrowid
            self.connection.execute(
                "INSERT INTO open_dispute_per_instance(instance_id,dispute_id) VALUES(?,?)",
                (instance["instance_id"], dispute_id),
            )
            self.connection.execute(
                "UPDATE milestone_instances SET payment_frozen=1 WHERE instance_id=?",
                (instance["instance_id"],),
            )
            self._audit(
                "dispute", str(dispute_id), "dispute.opened", actor_id,
                {"project_id": project_id, "milestone_id": milestone_id,
                 "instance_id": instance["instance_id"], "scope": "payments_only"},
            )
        return {"dispute_id": dispute_id, "instance_id": instance["instance_id"],
                "payment_frozen": True, "status": "open"}

    def resolve_dispute(
        self, actor_id: str, dispute_id: int, resolution: str, *, keep_frozen: bool = False
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.resolve")
        if not resolution.strip():
            raise ValidationFailed("处理结论不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("争议不存在")
            if row["status"] != "open":
                raise InvalidState("争议已经处理")
            self.connection.execute(
                "UPDATE disputes SET status='resolved',resolution=?,resolved_by=?,resolved_at=? "
                "WHERE dispute_id=?",
                (resolution.strip(), actor_id, self._now(), dispute_id),
            )
            self.connection.execute(
                "DELETE FROM open_dispute_per_instance WHERE dispute_id=?", (dispute_id,)
            )
            if not keep_frozen:
                self._set_frozen_along_chain(row["instance_id"], False)
            self._audit(
                "dispute", str(dispute_id), "dispute.resolved", actor_id,
                {"instance_id": row["instance_id"], "keep_frozen": keep_frozen},
            )
        return {"dispute_id": dispute_id, "status": "resolved", "payment_frozen": keep_frozen}

    def _set_frozen_along_chain(self, instance_id: int, frozen: bool) -> None:
        chain_ids = self._chain_instance_ids(instance_id)
        placeholders = ",".join("?" for _ in chain_ids)
        self.connection.execute(
            f"UPDATE milestone_instances SET payment_frozen=? WHERE instance_id IN ({placeholders})",
            (1 if frozen else 0, *chain_ids),
        )

    def release_payment_hold(
        self, actor_id: str, project_id: str, milestone_id: str, note: str
    ) -> dict[str, Any]:
        """解除以 keep_frozen 方式延续的拨付冻结（争议本身必须已解决）。"""

        self._require(actor_id, "dispute.resolve")
        if not note.strip():
            raise ValidationFailed("解冻说明不能为空")
        with transaction(self.connection, immediate=True):
            instance = self._active_instance(project_id, milestone_id)
            if not instance["payment_frozen"]:
                raise InvalidState("该里程碑的拨付当前未被冻结")
            open_row = self.connection.execute(
                "SELECT 1 FROM open_dispute_per_instance o "
                "JOIN milestone_instances m ON m.instance_id=o.instance_id "
                "WHERE m.project_id=? AND m.milestone_id=?",
                (project_id, milestone_id),
            ).fetchone()
            if open_row is not None:
                raise InvalidState("争议仍未解决，不能解冻")
            self._set_frozen_along_chain(instance["instance_id"], False)
            self._audit(
                "milestone", f"{project_id}:{milestone_id}", "payment.released", actor_id,
                {"instance_id": instance["instance_id"], "note": note.strip()},
            )
        return {"project_id": project_id, "milestone_id": milestone_id, "payment_frozen": False}

    # ------------------------------------------------------------------ 资金

    def decide_payment(
        self, actor_id: str, project_id: str, milestone_id: str, source_id: str,
        decision: str, note: str = "", expected_amount_cents: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "payment.decide")
        if decision not in ("approved", "rejected"):
            raise ValidationFailed("资金决定必须是 approved 或 rejected")
        with transaction(self.connection, immediate=True):
            project = self._locked_project(project_id)
            if project["status"] != "active":
                raise InvalidState("项目不在执行中，不能形成资金决定")
            instance = self._active_instance(project_id, milestone_id)
            if instance["status"] not in PAYABLE_STATES:
                raise InvalidState(f"里程碑状态 {instance['status']} 不具备拨款条件")
            chain_ids = self._chain_instance_ids(instance["instance_id"])
            chain_sql = ",".join("?" for _ in chain_ids)
            if self.connection.execute(
                f"SELECT 1 FROM open_dispute_per_instance WHERE instance_id IN ({chain_sql})",
                chain_ids,
            ).fetchone():
                raise InvalidState("争议未决，该里程碑的相关拨付已冻结")
            if instance["payment_frozen"]:
                raise InvalidState("该里程碑的拨付处于冻结状态")
            basis = self.connection.execute(
                f"SELECT * FROM acceptance_reviews WHERE instance_id IN ({chain_sql}) AND round=? "
                "ORDER BY review_id DESC LIMIT 1",
                (*chain_ids, instance["current_round"]),
            ).fetchone()
            if basis is None:
                raise InvalidState("缺少本轮验收结论，不能进行财务复核")
            if basis["reviewer"] == actor_id:
                raise Forbidden("财务复核必须由验收人之外的授权人完成")
            # 资金方案以验收发生时的协议版本为准：事后修订资金金额不影响
            # 已验收节点应拨金额，避免"财务按旧版计划拨款"变成隐性差错。
            funding_version_id = basis["definition_version_id"]
            source = self.connection.execute(
                "SELECT * FROM funding_sources WHERE version_id=? AND source_id=?",
                (funding_version_id, source_id),
            ).fetchone()
            if source is None:
                raise NotFound("验收所依据的协议版本中没有该资金来源")
            if self.connection.execute(
                f"SELECT 1 FROM payment_decisions WHERE instance_id IN ({chain_sql}) "
                "AND source_id=? AND round=?",
                (*chain_ids, source_id, instance["current_round"]),
            ).fetchone():
                # 资金决定只追加：无论批准还是驳回，同一轮次同一来源不得改写
                raise InvalidState("该资金来源本轮已有决定，不能重复或改写")
            # 本轮目标比例：完全接受按里程碑比例；部分接受再按达成比例折算
            target_ratio_pct = instance["payment_ratio_pct"]
            if instance["status"] == "partially_accepted":
                target_ratio_pct = target_ratio_pct * basis["achieved_ratio_pct"] // 100
            target_amount = source["amount_cents"] * target_ratio_pct // 100
            # 扣除该来源在既往轮次已批准的累计金额，部分接受转完全接受时只补差额
            prior = self.connection.execute(
                f"SELECT COALESCE(SUM(amount_cents),0) FROM payment_decisions "
                f"WHERE instance_id IN ({chain_sql}) AND source_id=? AND decision='approved' AND round<>?",
                (*chain_ids, source_id, instance["current_round"]),
            ).fetchone()[0]
            amount = target_amount - prior
            if amount <= 0:
                raise InvalidState("该来源按当前验收结论已无可补拨金额")
            if expected_amount_cents is not None and expected_amount_cents != amount:
                raise Conflict(
                    f"期望金额 {expected_amount_cents} 与按当前版本计算的 {amount}（分）不一致"
                )
            cursor = self.connection.execute(
                "INSERT INTO payment_decisions(project_id,instance_id,round,source_version_id,source_id,"
                "reviewer,decision,amount_cents,basis_review_id,note,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, instance["instance_id"], instance["current_round"],
                 funding_version_id, source_id, actor_id, decision,
                 amount, basis["review_id"], note, self._now()),
            )
            payment_id = cursor.lastrowid
            self._audit(
                "payment", str(payment_id), "payment.decided", actor_id,
                {"project_id": project_id, "milestone_id": milestone_id,
                 "instance_id": instance["instance_id"], "source_id": source_id,
                 "decision": decision, "amount_cents": amount,
                 "version_seq": self.get_project(project_id)["current_version_seq"],
                 "basis_review_id": basis["review_id"]},
            )
        return self._payment_view(payment_id)

    def execute_payment(self, actor_id: str, payment_id: int) -> dict[str, Any]:
        self._require(actor_id, "payment.execute")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM payment_decisions WHERE payment_id=?", (payment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("资金决定不存在")
            if row["decision"] != "approved":
                raise InvalidState("只有批准的拨付可以执行")
            if row["executed_at"] is not None:
                raise InvalidState("拨付已经执行")
            # 拨付执行时按里程碑当前活动实例判断是否被争议冻结或暂停
            milestone_ref = self.connection.execute(
                "SELECT project_id,milestone_id FROM milestone_instances WHERE instance_id=?",
                (row["instance_id"],),
            ).fetchone()
            active = self.connection.execute(
                "SELECT instance_id,status,payment_frozen FROM milestone_instances "
                "WHERE project_id=? AND milestone_id=? AND active_version_seq IS NOT NULL",
                (milestone_ref["project_id"], milestone_ref["milestone_id"]),
            ).fetchone()
            chain_ids = self._chain_instance_ids(row["instance_id"])
            chain_sql = ",".join("?" for _ in chain_ids)
            if self.connection.execute(
                f"SELECT 1 FROM open_dispute_per_instance WHERE instance_id IN ({chain_sql})",
                chain_ids,
            ).fetchone():
                raise InvalidState("争议未决，相关拨付保持冻结，不能执行")
            if active["payment_frozen"]:
                raise InvalidState("该里程碑的拨付保持冻结，不能执行")
            if active["status"] == "suspended":
                raise InvalidState("里程碑已暂停，不能执行拨付")
            self.connection.execute(
                "UPDATE payment_decisions SET executed_at=?,executed_by=? WHERE payment_id=?",
                (self._now(), actor_id, payment_id),
            )
            self._mark_locked_if_complete(row)
            self._audit(
                "payment", str(payment_id), "payment.executed", actor_id,
                {"instance_id": row["instance_id"], "amount_cents": row["amount_cents"]},
            )
        return self._payment_view(payment_id)

    def _mark_locked_if_complete(self, payment: sqlite3.Row) -> None:
        """完全接受的节点在各来源拨付全部执行后锁定，此后修订不得改写。"""

        instance = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE instance_id=?", (payment["instance_id"],)
        ).fetchone()
        if instance["status"] != "accepted":
            return
        project = self.get_project(instance["project_id"])
        source_count = self.connection.execute(
            "SELECT count(*) FROM funding_sources WHERE version_id=?",
            (project["current_version_id"],),
        ).fetchone()[0]
        executed = self.connection.execute(
            "SELECT count(*) FROM payment_decisions WHERE instance_id=? AND round=? "
            "AND decision='approved' AND executed_at IS NOT NULL",
            (payment["instance_id"], instance["current_round"]),
        ).fetchone()[0]
        if executed >= source_count:
            self.connection.execute(
                "UPDATE milestone_instances SET locked=1 WHERE instance_id=?",
                (payment["instance_id"],)
            )

    def _payment_view(self, payment_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM payment_decisions WHERE payment_id=?", (payment_id,)
        ).fetchone()
        version = self.connection.execute(
            "SELECT version_seq,version_label FROM agreement_versions WHERE version_id=?",
            (row["source_version_id"],),
        ).fetchone()
        result = dict(row)
        result["source_version_seq"] = version["version_seq"]
        result["source_version_label"] = version["version_label"]
        return result

    # ------------------------------------------------------------------ 报告

    def project_report(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """完整报告：版本历史、规则快照、资金决定与争议，供重启后追溯。"""

        self._require(actor_id, "report.read")
        project = self.get_project(project_id)
        active_rows = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE project_id=? AND active_version_seq IS NOT NULL "
            "ORDER BY seq_in_version",
            (project_id,),
        ).fetchall()
        retired_rows = self.connection.execute(
            "SELECT * FROM milestone_instances WHERE project_id=? AND active_version_seq IS NULL "
            "ORDER BY instance_id",
            (project_id,),
        ).fetchall()
        payments = self.connection.execute(
            "SELECT * FROM payment_decisions WHERE project_id=? ORDER BY payment_id", (project_id,)
        ).fetchall()
        disputes = self.connection.execute(
            "SELECT * FROM disputes WHERE project_id=? ORDER BY dispute_id", (project_id,)
        ).fetchall()
        return {
            "project": project,
            "versions": self.list_versions(actor_id, project_id),
            "current_agreement": (
                self.get_version(project_id, project["current_version_seq"])
                if project.get("current_version_seq") else None
            ),
            "milestones": [self._milestone_view(row, history=True) for row in active_rows],
            "retired_milestones": [self._milestone_view(row, history=True) for row in retired_rows],
            "outstanding_payments": [self._payment_view(row["payment_id"]) for row in payments
                                     if row["decision"] == "approved" and row["executed_at"] is None],
            "disputes": [dict(row) for row in disputes],
        }

    def audit_trail(
        self, actor_id: str, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        query = "SELECT * FROM audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id"
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in self.connection.execute(query, params).fetchall()
        ]

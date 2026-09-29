"""联合研发协议的严格数据契约。

协议版本是整套协作的事实来源：参与方承诺、可交付成果、资金来源、
里程碑与验收规则都在签署时冻结为一个不可变版本。后续修订只能新增
版本，不能改写已经确认的历史节点。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _text(value, path)


def _money(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是金额数值")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制金额") from exc
    if not amount.is_finite() or amount < 0:
        raise ValidationError(f"{path} 必须是不小于零的有限金额")
    return amount.quantize(Decimal("0.01"))


def _int_rate(value: object, path: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{path} 必须是 0-100 的整数百分比")
    low = 0 if allow_zero else 1
    if not low <= value <= 100:
        raise ValidationError(f"{path} 必须在 {low}-100 之间")
    return value


@dataclass(frozen=True, slots=True)
class Party:
    """签署协议的一方：高校、企业或产业园等。"""

    party_id: str
    name: str
    kind: str
    contact: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Party":
        data = _mapping(raw, path)
        kind = _text(data.get("kind"), f"{path}.kind")
        if kind not in {"university", "enterprise", "park", "institute", "other"}:
            raise ValidationError(f"{path}.kind 取值非法")
        return cls(
            party_id=_text(data.get("party_id"), f"{path}.party_id"),
            name=_text(data.get("name"), f"{path}.name"),
            kind=kind,
            contact=_optional_text(data.get("contact"), f"{path}.contact"),
        )


@dataclass(frozen=True, slots=True)
class Commitment:
    """某一参与方在本版本协议下的承诺（人月、设备、场地、配套资金等）。"""

    commitment_id: str
    party_id: str
    category: str
    description: str
    unit: str
    quantity: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Commitment":
        data = _mapping(raw, path)
        return cls(
            commitment_id=_text(data.get("commitment_id"), f"{path}.commitment_id"),
            party_id=_text(data.get("party_id"), f"{path}.party_id"),
            category=_text(data.get("category"), f"{path}.category"),
            description=_text(data.get("description"), f"{path}.description"),
            unit=_text(data.get("unit"), f"{path}.unit"),
            quantity=_money(data.get("quantity"), f"{path}.quantity"),
        )


@dataclass(frozen=True, slots=True)
class FundingSource:
    """一笔资金来源，出资方与金额在签署时冻结。"""

    source_id: str
    party_id: str
    name: str
    amount: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "FundingSource":
        data = _mapping(raw, path)
        return cls(
            source_id=_text(data.get("source_id"), f"{path}.source_id"),
            party_id=_text(data.get("party_id"), f"{path}.party_id"),
            name=_text(data.get("name"), f"{path}.name"),
            amount=_money(data.get("amount"), f"{path}.amount"),
        )


@dataclass(frozen=True, slots=True)
class Deliverable:
    """绑定到某个里程碑的可交付成果。"""

    deliverable_id: str
    milestone_id: str
    owner_party_id: str
    title: str
    description: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Deliverable":
        data = _mapping(raw, path)
        return cls(
            deliverable_id=_text(data.get("deliverable_id"), f"{path}.deliverable_id"),
            milestone_id=_text(data.get("milestone_id"), f"{path}.milestone_id"),
            owner_party_id=_text(data.get("owner_party_id"), f"{path}.owner_party_id"),
            title=_text(data.get("title"), f"{path}.title"),
            description=_optional_text(data.get("description"), f"{path}.description"),
        )


@dataclass(frozen=True, slots=True)
class AcceptanceRule:
    """里程碑评审时采用的验收规则；接受比例表示满足到何种程度算达成。"""

    rule_id: str
    title: str
    criterion: str
    evidence_required: str
    accept_ratio_pct: int

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "AcceptanceRule":
        data = _mapping(raw, path)
        return cls(
            rule_id=_text(data.get("rule_id"), f"{path}.rule_id"),
            title=_text(data.get("title"), f"{path}.title"),
            criterion=_text(data.get("criterion"), f"{path}.criterion"),
            evidence_required=_text(data.get("evidence_required"), f"{path}.evidence_required"),
            accept_ratio_pct=_int_rate(data.get("accept_ratio_pct"), f"{path}.accept_ratio_pct"),
        )


@dataclass(frozen=True, slots=True)
class Milestone:
    """一个计划节点：计划日期、拨付比例、绑定的验收规则。"""

    milestone_id: str
    title: str
    plan_date: str
    payment_ratio_pct: int
    rule_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Milestone":
        data = _mapping(raw, path)
        rule_ids = tuple(
            _text(item, f"{path}.rule_ids[]") for item in _sequence(data.get("rule_ids", ()), f"{path}.rule_ids")
        )
        if not rule_ids:
            raise ValidationError(f"{path}.rule_ids 至少包含一条验收规则")
        plan_date = _text(data.get("plan_date"), f"{path}.plan_date")
        return cls(
            milestone_id=_text(data.get("milestone_id"), f"{path}.milestone_id"),
            title=_text(data.get("title"), f"{path}.title"),
            plan_date=plan_date,
            payment_ratio_pct=_int_rate(data.get("payment_ratio_pct"), f"{path}.payment_ratio_pct"),
            rule_ids=rule_ids,
        )


@dataclass(frozen=True, slots=True)
class AgreementDraft:
    """一次签署或修订提交的完整协议草案。"""

    project_id: str
    title: str
    version_label: str
    note: str
    parties: tuple[Party, ...]
    commitments: tuple[Commitment, ...]
    funding_sources: tuple[FundingSource, ...]
    milestones: tuple[Milestone, ...]
    rules: tuple[AcceptanceRule, ...]
    deliverables: tuple[Deliverable, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "AgreementDraft":
        data = _mapping(raw, "agreement")
        parties = tuple(Party.from_dict(item, "agreement.parties[]") for item in _sequence(data.get("parties"), "agreement.parties"))
        commitments = tuple(
            Commitment.from_dict(item, "agreement.commitments[]")
            for item in _sequence(data.get("commitments", ()), "agreement.commitments")
        )
        funding = tuple(
            FundingSource.from_dict(item, "agreement.funding_sources[]")
            for item in _sequence(data.get("funding_sources"), "agreement.funding_sources")
        )
        rules = tuple(
            AcceptanceRule.from_dict(item, "agreement.acceptance_rules[]")
            for item in _sequence(data.get("acceptance_rules"), "agreement.acceptance_rules")
        )
        milestones = tuple(
            Milestone.from_dict(item, "agreement.milestones[]")
            for item in _sequence(data.get("milestones"), "agreement.milestones")
        )
        deliverables = tuple(
            Deliverable.from_dict(item, "agreement.deliverables[]")
            for item in _sequence(data.get("deliverables", ()), "agreement.deliverables")
        )
        draft = cls(
            project_id=_text(data.get("project_id"), "agreement.project_id"),
            title=_text(data.get("title"), "agreement.title"),
            version_label=_text(data.get("version_label"), "agreement.version_label"),
            note=_optional_text(data.get("note"), "agreement.note") or "",
            parties=parties,
            commitments=commitments,
            funding_sources=funding,
            milestones=milestones,
            rules=rules,
            deliverables=deliverables,
        )
        draft.validate_cross_refs()
        return draft

    def validate_cross_refs(self) -> None:
        party_ids = {item.party_id for item in self.parties}
        if len(party_ids) != len(self.parties):
            raise ValidationError("参与方编号重复")
        if len(party_ids) < 2:
            raise ValidationError("联合研发协议至少需要两个参与方")

        def _unique(ids: list[str], label: str) -> None:
            if len(ids) != len(set(ids)):
                raise ValidationError(f"{label}编号重复")

        commitment_ids: list[str] = []
        for item in self.commitments:
            commitment_ids.append(item.commitment_id)
            if item.party_id not in party_ids:
                raise ValidationError(f"承诺 {item.commitment_id} 引用了不存在的参与方")
        _unique(commitment_ids, "承诺")

        source_ids: list[str] = []
        for item in self.funding_sources:
            source_ids.append(item.source_id)
            if item.party_id not in party_ids:
                raise ValidationError(f"资金来源 {item.source_id} 引用了不存在的参与方")
        _unique(source_ids, "资金来源")
        if not self.funding_sources or sum((item.amount for item in self.funding_sources), Decimal("0")) <= 0:
            raise ValidationError("协议必须声明总额大于零的资金来源")

        rule_ids_all = {item.rule_id for item in self.rules}
        if len(rule_ids_all) != len(self.rules):
            raise ValidationError("验收规则编号重复")
        if not self.rules:
            raise ValidationError("协议必须声明验收规则")

        milestone_ids: list[str] = []
        total_ratio = 0
        seen_dates: set[str] = set()
        for item in self.milestones:
            milestone_ids.append(item.milestone_id)
            total_ratio += item.payment_ratio_pct
            if item.plan_date in seen_dates:
                raise ValidationError(f"里程碑 {item.milestone_id} 计划日期重复")
            seen_dates.add(item.plan_date)
            for rule_id in item.rule_ids:
                if rule_id not in rule_ids_all:
                    raise ValidationError(f"里程碑 {item.milestone_id} 引用了不存在的验收规则 {rule_id}")
        _unique(milestone_ids, "里程碑")
        if not self.milestones:
            raise ValidationError("协议至少包含一个里程碑")
        if total_ratio != 100:
            raise ValidationError(f"里程碑拨付比例之和必须为 100（当前 {total_ratio}）")
        # 计划日期必须单调，避免阶段顺序歧义
        ordered = sorted(self.milestones, key=lambda item: item.plan_date)
        if [item.milestone_id for item in ordered] != [item.milestone_id for item in self.milestones]:
            raise ValidationError("里程碑必须按 plan_date 升序排列")

        deliverable_ids: list[str] = []
        milestone_set = set(milestone_ids)
        for item in self.deliverables:
            deliverable_ids.append(item.deliverable_id)
            if item.milestone_id not in milestone_set:
                raise ValidationError(f"交付物 {item.deliverable_id} 引用了不存在的里程碑")
            if item.owner_party_id not in party_ids:
                raise ValidationError(f"交付物 {item.deliverable_id} 的责任方不存在")
        _unique(deliverable_ids, "可交付成果")

    def to_canonical(self) -> dict[str, Any]:
        """序列化为摘要用的规范化结构（金额转为字符串，避免浮点误差）。"""

        return {
            "project_id": self.project_id,
            "title": self.title,
            "version_label": self.version_label,
            "note": self.note,
            "parties": [asdict(item) for item in self.parties],
            "commitments": [
                asdict(item) | {"quantity": format(item.quantity, "f")} for item in self.commitments
            ],
            "funding_sources": [
                asdict(item) | {"amount": format(item.amount, "f")} for item in self.funding_sources
            ],
            "milestones": [asdict(item) for item in self.milestones],
            "acceptance_rules": [asdict(item) for item in self.rules],
            "deliverables": [asdict(item) for item in self.deliverables],
        }
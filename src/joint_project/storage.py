"""联合研发项目服务的 SQLite 模式和事务辅助。

所有业务表都带 revision 乐观锁；协议版本、承诺、里程碑、验收规则、
拨付决定与审计事件一经确认即只追加，不就地改写历史。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jp_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('secretary','party_admin','acceptor','finance','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 参与方：高校 / 企业 / 产业园等
CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('university','enterprise','park','institute','other')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_version_no INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'preparing'
        CHECK(state IN ('preparing','active','suspended','restarted','completed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL
);

-- 协议版本：只追加。签署后内容不可变；修订只能生成新版本
CREATE TABLE IF NOT EXISTS agreement_versions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    supersedes_version_no INTEGER,
    change_summary TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','signed','superseded')),
    signed_at TEXT,
    signed_by TEXT REFERENCES jp_users(user_id),
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, version_no),
    UNIQUE(project_id, content_sha256),
    CHECK(version_no >= 1),
    CHECK(state = 'draft' OR signed_at IS NOT NULL),
    CHECK(state <> 'draft' OR signed_at IS NULL)
);

-- 承诺 / 可交付成果：签署版本内不可变；修订版本可关闭并另开新版本记录
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version_no INTEGER NOT NULL,
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    kind TEXT NOT NULL CHECK(kind IN ('funding','resource','deliverable','service','other')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','fulfilled','closed_by_revision')),
    closed_in_version_no INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version_no, commitment_id),
    FOREIGN KEY(project_id, version_no) REFERENCES agreement_versions(project_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_commitments_project
ON commitments(project_id, party_id, kind);

-- 里程碑的验收规则，定义在某个签署版本上，验收时按当时规则解释
CREATE TABLE IF NOT EXISTS acceptance_rules (
    rule_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version_no INTEGER NOT NULL,
    milestone_code TEXT NOT NULL,
    name TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','retired')),
    retired_in_version_no INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version_no, milestone_code, rule_id),
    FOREIGN KEY(project_id, version_no) REFERENCES agreement_versions(project_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_rules_milestone
ON acceptance_rules(project_id, milestone_code, state);

-- 里程碑：锚定其创建时的签署版本
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    anchored_version_no INTEGER NOT NULL,
    planned_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','in_review','partially_accepted','accepted','returned','paused')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, code),
    FOREIGN KEY(project_id, anchored_version_no) REFERENCES agreement_versions(project_id, version_no)
);

-- 资金来源 / 拨付包：绑定承诺（funding 类），并锚定签署版本
CREATE TABLE IF NOT EXISTS funding_sources (
    source_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    version_no INTEGER NOT NULL,
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    name TEXT NOT NULL,
    total_amount_cny TEXT NOT NULL,
    received_amount_cny TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id, version_no) REFERENCES agreement_versions(project_id, version_no)
);

-- 里程碑提交验收（每次退回后重新提交产生新提交）
CREATE TABLE IF NOT EXISTS milestone_submissions (
    submission_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    attempt INTEGER NOT NULL,
    evidence_note TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES jp_users(user_id),
    submitted_at TEXT NOT NULL,
    UNIQUE(milestone_id, attempt)
);

-- 验收证据
CREATE TABLE IF NOT EXISTS acceptance_evidences (
    evidence_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES milestone_submissions(submission_id),
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL REFERENCES jp_users(user_id),
    uploaded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_submission
ON acceptance_evidences(submission_id);

-- 验收决定（成果验收人）：只追加，部分接受时逐项给出结论
CREATE TABLE IF NOT EXISTS acceptance_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    submission_id TEXT NOT NULL REFERENCES milestone_submissions(submission_id),
    rule_id TEXT NOT NULL REFERENCES acceptance_rules(rule_id),
    decision TEXT NOT NULL CHECK(decision IN ('accept','partial','return','pause')),
    accepted_scope_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES jp_users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE(submission_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_milestone
ON acceptance_decisions(milestone_id, decision_id);

-- 争议：冻结与争议里程碑相关的拨付，不影响项目其他拨付
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    subject TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved','withdrawn')),
    resolution TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    raised_by TEXT NOT NULL REFERENCES jp_users(user_id),
    raised_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES jp_users(user_id),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_disputes_milestone
ON disputes(milestone_id, state);

-- 拨付决定（财务复核人）：只追加。pending 在争议/暂停期间保持冻结
CREATE TABLE IF NOT EXISTS disbursements (
    disbursement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    source_id TEXT NOT NULL REFERENCES funding_sources(source_id),
    submission_id TEXT REFERENCES milestone_submissions(submission_id),
    amount_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','frozen','approved','rejected','paid','released_after_freeze')),
    freeze_reason TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT REFERENCES jp_users(user_id),
    reviewed_at TEXT,
    paid_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES jp_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_disbursements_milestone
ON disbursements(milestone_id, state);

-- 项目级暂停 / 重启记录（项目重启后仍可追溯当时的规则与未结资金决定）
CREATE TABLE IF NOT EXISTS project_lifecycles (
    lifecycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    action TEXT NOT NULL CHECK(action IN ('suspend','restart','complete','cancel')),
    reason TEXT NOT NULL,
    acted_by TEXT NOT NULL REFERENCES jp_users(user_id),
    acted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lifecycles_project
ON project_lifecycles(project_id, lifecycle_id);

CREATE TABLE IF NOT EXISTS jp_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS jp_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jp_audit_entity
ON jp_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)

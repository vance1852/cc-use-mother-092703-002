"""联合研发项目管理的 SQLite 模式与事务辅助。

设计原则：
- ``agreement_versions`` 及其子表只插入、不更新、不删除，协议历史永久可查；
- 里程碑实例在修订时通过 ``carry_of_id`` 链接延续，旧实例行冻结不变；
- 验收评审冗余保存当时的验收规则全文快照；
- 资金决定记录做出决定时的协议版本与来源版本。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'admin', 'secretary', 'technical_reviewer',
        'finance_officer', 'party_representative', 'auditor')),
    org_name TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'suspended', 'closed')),
    current_version_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreement_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version_seq INTEGER NOT NULL CHECK (version_seq >= 1),
    version_label TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    signed_by TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    UNIQUE (project_id, version_seq),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS parties (
    version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    party_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    contact TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (version_id, party_id)
);

CREATE TABLE IF NOT EXISTS commitments (
    version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    commitment_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL,
    quantity_cents INTEGER NOT NULL CHECK (quantity_cents >= 0),
    PRIMARY KEY (version_id, commitment_id)
);

CREATE TABLE IF NOT EXISTS funding_sources (
    version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    source_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    seq INTEGER NOT NULL,
    PRIMARY KEY (version_id, source_id)
);

CREATE TABLE IF NOT EXISTS acceptance_rules (
    version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    rule_id TEXT NOT NULL,
    title TEXT NOT NULL,
    criterion TEXT NOT NULL,
    evidence_required TEXT NOT NULL,
    accept_ratio_pct INTEGER NOT NULL CHECK (accept_ratio_pct BETWEEN 1 AND 100),
    seq INTEGER NOT NULL,
    PRIMARY KEY (version_id, rule_id)
);

CREATE TABLE IF NOT EXISTS milestone_instances (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_id TEXT NOT NULL,
    definition_version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    first_version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    carry_of_id INTEGER REFERENCES milestone_instances(instance_id),
    active_version_seq INTEGER,
    seq_in_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    payment_ratio_pct INTEGER NOT NULL CHECK (payment_ratio_pct BETWEEN 1 AND 100),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'submitted', 'accepted', 'partially_accepted',
        'returned', 'suspended')),
    suspended_from TEXT,
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round >= 0),
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    payment_frozen INTEGER NOT NULL DEFAULT 0 CHECK (payment_frozen IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (project_id, milestone_id, definition_version_id)
);

CREATE INDEX IF NOT EXISTS idx_milestone_active
ON milestone_instances(project_id, active_version_seq);

CREATE INDEX IF NOT EXISTS idx_milestone_carry ON milestone_instances(carry_of_id);

CREATE TABLE IF NOT EXISTS deliverables (
    version_id INTEGER NOT NULL REFERENCES agreement_versions(version_id),
    deliverable_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    owner_party_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (version_id, deliverable_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES milestone_instances(instance_id),
    submitter TEXT NOT NULL,
    title TEXT NOT NULL,
    ref TEXT NOT NULL,
    content_sha256 TEXT,
    note TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acceptance_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES milestone_instances(instance_id),
    round INTEGER NOT NULL CHECK (round >= 1),
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'partially_accepted', 'returned')),
    achieved_ratio_pct INTEGER NOT NULL CHECK (achieved_ratio_pct BETWEEN 0 AND 100),
    rule_snapshots_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    definition_version_id INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL,
    UNIQUE (instance_id, round)
);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    instance_id INTEGER NOT NULL REFERENCES milestone_instances(instance_id),
    opened_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    resolution TEXT NOT NULL DEFAULT '',
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS open_dispute_per_instance (
    instance_id INTEGER PRIMARY KEY REFERENCES milestone_instances(instance_id),
    dispute_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_decisions (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    instance_id INTEGER NOT NULL REFERENCES milestone_instances(instance_id),
    round INTEGER NOT NULL CHECK (round >= 1),
    source_version_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'frozen')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    basis_review_id INTEGER NOT NULL REFERENCES acceptance_reviews(review_id),
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    frozen_at TEXT,
    executed_at TEXT,
    executed_by TEXT,
    UNIQUE (instance_id, source_id, round),
    FOREIGN KEY (source_version_id, source_id) REFERENCES funding_sources(version_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_project ON payment_decisions(project_id);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "projects", "agreement_versions", "parties",
    "commitments", "funding_sources", "acceptance_rules", "milestone_instances",
    "deliverables", "evidence", "acceptance_reviews", "disputes",
    "open_dispute_per_instance", "payment_decisions", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用外键与显式事务模式。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化全部表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }

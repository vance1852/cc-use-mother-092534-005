"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    subject_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('heritor','school','community','student','guardian')),
    name TEXT NOT NULL,
    organization_id TEXT REFERENCES organizations(organization_id),
    guardian_id TEXT REFERENCES subjects(subject_id),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('source_pattern','sketch','structure_plan','display','correction')),
    title TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    parent_id TEXT REFERENCES works(work_id),
    change_note TEXT,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_family ON works(family_id);
CREATE TABLE IF NOT EXISTS work_contributors (
    work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
    contribution_role TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (work_id, subject_id)
);
CREATE TABLE IF NOT EXISTS work_authorizations (
    auth_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
    scope TEXT NOT NULL CHECK(scope IN ('public_display','commercial')),
    decision TEXT NOT NULL CHECK(decision IN ('allow','deny')),
    is_minor INTEGER NOT NULL CHECK(is_minor IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    valid_from TEXT,
    valid_until TEXT,
    granted_by TEXT NOT NULL REFERENCES actors(actor_id),
    granted_at TEXT NOT NULL,
    revoked_by TEXT REFERENCES actors(actor_id),
    revoked_at TEXT,
    UNIQUE(work_id, subject_id, scope)
);
CREATE TABLE IF NOT EXISTS authorization_confirmations (
    auth_id TEXT NOT NULL REFERENCES work_authorizations(auth_id) ON DELETE CASCADE,
    confirmer_type TEXT NOT NULL CHECK(confirmer_type IN ('guardian','institution')),
    confirmer_id TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (auth_id, confirmer_type)
);
CREATE TABLE IF NOT EXISTS exhibitions (
    exhibition_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    purpose TEXT NOT NULL CHECK(purpose IN ('public_display','commercial')),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('approved','blocked')),
    decided_by TEXT NOT NULL REFERENCES actors(actor_id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exhibitions_work ON exhibitions(work_id);
CREATE TABLE IF NOT EXISTS supersessions (
    replacement_work_id TEXT NOT NULL REFERENCES works(work_id),
    superseded_work_id TEXT NOT NULL REFERENCES works(work_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (replacement_work_id, superseded_work_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()

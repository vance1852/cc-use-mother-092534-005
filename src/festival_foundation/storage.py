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
CREATE TABLE IF NOT EXISTS designs (
    design_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS design_versions (
    version_id TEXT PRIMARY KEY,
    design_id TEXT NOT NULL REFERENCES designs(design_id),
    kind TEXT NOT NULL CHECK(kind IN ('sketch','structure','public','correction')),
    content_summary_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    parent_version_id TEXT REFERENCES design_versions(version_id),
    change_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_sources (
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(version_id, source_key)
);
CREATE TABLE IF NOT EXISTS version_contributors (
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    contributor_actor_id TEXT NOT NULL,
    contribution_kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(version_id, contributor_actor_id)
);
CREATE TABLE IF NOT EXISTS license_grants (
    grant_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    contributor_actor_id TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    is_minor INTEGER NOT NULL CHECK(is_minor IN (0, 1)),
    guardian_actor_id TEXT,
    institution_org_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','effective','revoked')),
    effective_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, contributor_actor_id)
);
CREATE TABLE IF NOT EXISTS grant_confirmations (
    grant_id TEXT NOT NULL REFERENCES license_grants(grant_id),
    confirmation_kind TEXT NOT NULL CHECK(confirmation_kind IN ('guardian','institution')),
    confirmer_actor_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(grant_id, confirmation_kind)
);
CREATE TABLE IF NOT EXISTS version_supersessions (
    supersession_id TEXT PRIMARY KEY,
    old_version_id TEXT NOT NULL UNIQUE REFERENCES design_versions(version_id),
    new_version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exhibitions (
    exhibition_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    purpose TEXT NOT NULL,
    commercial INTEGER NOT NULL CHECK(commercial IN (0, 1)),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    exhibitor_actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('approved','blocked')),
    blockers_json TEXT NOT NULL,
    permit_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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

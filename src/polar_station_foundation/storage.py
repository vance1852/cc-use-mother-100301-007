"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('machine', 'component')),
    parent_id TEXT REFERENCES equipment(equipment_id),
    name TEXT NOT NULL,
    site_id TEXT REFERENCES sites(site_id),
    status TEXT NOT NULL CHECK(status IN ('available', 'in_maintenance', 'failed', 'missing', 'in_transit', 'retired')),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    crate_status TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK(crate_status IN ('sealed', 'unsealed', 'damaged', 'not_applicable')),
    attached INTEGER NOT NULL DEFAULT 1 CHECK(attached IN (0, 1)),
    config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    certificate_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    calibration_version TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'revoked')),
    covered_signature TEXT NOT NULL,
    covered_components_json TEXT NOT NULL,
    revoked_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    capability TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'revoked')),
    created_at TEXT NOT NULL,
    UNIQUE(actor_id, capability)
);
CREATE TABLE IF NOT EXISTS priority_freezes (
    freeze_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    ranking_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_requests (
    request_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    components_json TEXT NOT NULL,
    required_capabilities_json TEXT NOT NULL,
    requesting_site_id TEXT NOT NULL REFERENCES sites(site_id),
    origin_site_id TEXT REFERENCES sites(site_id),
    operator_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    commitment_key TEXT NOT NULL,
    freeze_id TEXT NOT NULL REFERENCES priority_freezes(freeze_id),
    priority_rank INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested', 'waitlisted', 'confirmed', 'in_transit',
                                          'pending_acceptance', 'active', 'completed', 'interrupted',
                                          'rejected', 'cancelled')),
    phases_json TEXT NOT NULL,
    last_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(equipment_id, requesting_site_id, commitment_key, start_at, end_at)
);
CREATE INDEX IF NOT EXISTS idx_loans_equipment_status ON loan_requests(equipment_id, status, priority_rank);
CREATE TABLE IF NOT EXISTS slot_occupancy (
    occupancy_id TEXT PRIMARY KEY,
    loan_request_id TEXT NOT NULL REFERENCES loan_requests(request_id),
    equipment_id TEXT NOT NULL,
    unit_type TEXT NOT NULL CHECK(unit_type IN ('machine', 'component')),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slot_equipment ON slot_occupancy(equipment_id, start_at, end_at);
CREATE TABLE IF NOT EXISTS experiment_data (
    data_id TEXT PRIMARY KEY,
    loan_request_id TEXT NOT NULL REFERENCES loan_requests(request_id),
    payload_json TEXT NOT NULL,
    equipment_status TEXT NOT NULL,
    calibration_certificate_id TEXT,
    calibration_version TEXT,
    config_signature TEXT,
    capabilities_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    incident_type TEXT NOT NULL CHECK(incident_type IN ('equipment_failure', 'overdue_return',
                                                       'transport_damage', 'capability_downgrade',
                                                       'calibration_revoked', 'component_missing')),
    equipment_id TEXT REFERENCES equipment(equipment_id),
    loan_request_id TEXT REFERENCES loan_requests(request_id),
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_type ON incidents(incident_type);
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
        # 单个连接被 HTTP 工作线程共享，进程内写事务需要串行；
        # BEGIN IMMEDIATE 仍然负责跨进程的并发互斥。
        self._write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self._write_lock:
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

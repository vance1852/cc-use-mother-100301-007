"""跨站设备借调平台在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Equipment:
    """整机或可拆组件。组件通过 parent_id 挂在整机下。"""

    equipment_id: str
    kind: str
    parent_id: str | None
    name: str
    site_id: str | None
    status: str
    capabilities: frozenset[str]
    crate_status: str
    attached: bool
    config_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Calibration:
    """校准证书；covered_signature 冻结发证书时的组件配置。"""

    certificate_id: str
    equipment_id: str
    calibration_version: str
    valid_from: str
    valid_until: str
    status: str
    covered_signature: str
    covered_components: tuple[str, ...]
    revoked_reason: str | None
    created_at: str


@dataclass(frozen=True)
class Qualification:
    """操作员针对某一能力的资格。"""

    qualification_id: str
    actor_id: str
    capability: str
    valid_from: str
    valid_until: str
    status: str


@dataclass(frozen=True)
class PriorityFreeze:
    """冻结的科研优先级；rank 越小优先级越高，顺序在冻结时固化。"""

    freeze_id: str
    label: str
    ranking: tuple[str, ...]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class LoanRequestView:
    """一次借调申请的完整视图。"""

    request_id: str
    equipment_id: str
    components: tuple[str, ...]
    required_capabilities: frozenset[str]
    requesting_site_id: str
    origin_site_id: str | None
    operator_actor_id: str
    start_at: str
    end_at: str
    commitment_key: str
    freeze_id: str
    priority_rank: int
    status: str
    phases: dict[str, str]
    last_reason: str | None
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SlotDecision:
    """某个时段被谁占用、某个申请为何落选的解释。"""

    request_id: str
    equipment_id: str
    status: str
    start_at: str
    end_at: str
    priority_rank: int
    phases: dict[str, str]
    reason: str


@dataclass(frozen=True)
class ExperimentDataView:
    """实验数据及其采集时有效的设备/校准状态快照。"""

    data_id: str
    loan_request_id: str
    payload: dict[str, Any]
    equipment_status: str
    calibration_certificate_id: str | None
    calibration_version: str | None
    config_signature: str | None
    capabilities: frozenset[str]
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class IncidentView:
    """事故与追责记录。"""

    incident_id: str
    incident_type: str
    equipment_id: str | None
    loan_request_id: str | None
    detail: dict[str, Any]
    created_by: str
    occurred_at: str

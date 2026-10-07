"""跨站设备借调平台的领域常量与状态枚举。"""

from __future__ import annotations

# 借调申请在确认前需要分阶段通过的三个前置条件。
PHASE_EQUIPMENT = "equipment_capability"
PHASE_OPERATOR = "operator_qualification"
PHASE_PACKING = "packing_transport"
LOAN_PHASES = (PHASE_EQUIPMENT, PHASE_OPERATOR, PHASE_PACKING)

# 申请生命周期状态。
STATUS_REQUESTED = "requested"
STATUS_WAITLISTED = "waitlisted"
STATUS_CONFIRMED = "confirmed"
STATUS_IN_TRANSIT = "in_transit"
STATUS_PENDING_ACCEPTANCE = "pending_acceptance"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_INTERRUPTED = "interrupted"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

# 已经真正占用设备时间、会与后续申请冲突的状态。
OCCUPYING_STATUSES = frozenset({
    STATUS_CONFIRMED,
    STATUS_IN_TRANSIT,
    STATUS_PENDING_ACCEPTANCE,
    STATUS_ACTIVE,
})

# 仍可被事故或取消调整的“尚未完成”状态。
ADJUSTABLE_STATUSES = frozenset({
    STATUS_REQUESTED,
    STATUS_WAITLISTED,
    STATUS_CONFIRMED,
})

# 已完成或已经开始执行、事故不可再调整的状态；实验数据继续引用当时有效状态。
LOCKED_STATUSES = frozenset({
    STATUS_IN_TRANSIT,
    STATUS_PENDING_ACCEPTANCE,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
})

TERMINAL_STATUSES = frozenset({
    STATUS_REJECTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
})

# 重启后必须保留、不得丢回候补的“流程在途”状态。
IN_FLIGHT_STATUSES = frozenset({STATUS_IN_TRANSIT, STATUS_PENDING_ACCEPTANCE})

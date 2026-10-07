"""跨站设备借调平台。

在基础服务的 SQLite 事务、幂等回执与哈希审计链之上，独立维护：

- 整机与可拆组件（位置、能力范围、维护状态、版本修订）；
- 校准证书（覆盖的能力与组件修订，可撤销）；
- 运输箱（包装与运输前置条件）；
- 操作员资格与冻结科研优先级的研究承诺；
- 分阶段确认的借调申请：设备能力、人员资格、包装、运输四项前置条件
  同时满足后才真正占用预约时段，否则按冻结优先级稳定候补；
- 故障、延期归还、运输损伤、能力降级、校准撤销只影响尚未完成的预约，
  已产生的实验数据快照继续引用当时有效的状态；
- 逾期归还与组件失联的追责记录。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError

# --- 常量 -------------------------------------------------------------------

RESOURCE_UNIT = "unit"
RESOURCE_COMPONENT = "component"

PHASE_CAPABILITY = "capability"
PHASE_QUALIFICATION = "qualification"
PHASE_PACKAGING = "packaging"
PHASE_TRANSPORT = "transport"
PHASES = (PHASE_CAPABILITY, PHASE_QUALIFICATION, PHASE_PACKAGING, PHASE_TRANSPORT)

# 申请生命周期。
WAITLISTED = "waitlisted"
CONFIRMED = "confirmed"
PACKED = "packed"
IN_TRANSIT = "in_transit"
PENDING_ACCEPTANCE = "pending_acceptance"
ACTIVE = "active"
RETURN_IN_TRANSIT = "return_in_transit"
PENDING_RETURN = "pending_return"
COMPLETED = "completed"
RELEASED = "released"

# 尚未开始执行实验、仍可被事件调整的预约状态。
ADJUSTABLE_STATUSES = (CONFIRMED, PACKED, IN_TRANSIT, PENDING_ACCEPTANCE)
# 占用查询中视为仍占着资源的状态（completed/released 不再阻挡后续预约）。
OCCUPYING_STATUSES = (
    CONFIRMED, PACKED, IN_TRANSIT, PENDING_ACCEPTANCE, ACTIVE,
    RETURN_IN_TRANSIT, PENDING_RETURN,
)
TERMINAL_STATUSES = (COMPLETED, RELEASED)

EVENT_FAULT = "fault"
EVENT_DELAYED_RETURN = "delayed_return"
EVENT_TRANSPORT_DAMAGE = "transport_damage"
EVENT_CAPABILITY_DEGRADED = "capability_degraded"
EVENT_CALIBRATION_REVOKED = "calibration_revoked"
REPORTABLE_EVENTS = frozenset({
    EVENT_FAULT, EVENT_DELAYED_RETURN, EVENT_TRANSPORT_DAMAGE, EVENT_CAPABILITY_DEGRADED,
})

LOAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS loan_resources (
    resource_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('unit','component')),
    parent_id TEXT REFERENCES loan_resources(resource_id),
    name TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    location_note TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'operational'
        CHECK(status IN ('operational','degraded','faulty','maintenance','missing')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_calibrations (
    calibration_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    certificate_ref TEXT NOT NULL,
    version TEXT NOT NULL,
    covers_capabilities_json TEXT NOT NULL,
    covers_components_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','revoked','superseded')),
    created_at TEXT NOT NULL,
    UNIQUE(resource_id, version)
);
CREATE TABLE IF NOT EXISTS loan_crates (
    crate_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    fits_resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    status TEXT NOT NULL DEFAULT 'available'
        CHECK(status IN ('available','packed','in_transit','inspection','damaged')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_qualifications (
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    capability TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    PRIMARY KEY (actor_id, capability)
);
CREATE TABLE IF NOT EXISTS loan_commitments (
    commitment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority >= 1),
    frozen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_requests (
    loan_id TEXT PRIMARY KEY,
    request_site_id TEXT NOT NULL REFERENCES sites(site_id),
    owner_site_id TEXT NOT NULL REFERENCES sites(site_id),
    commitment_id TEXT NOT NULL REFERENCES loan_commitments(commitment_id),
    resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    required_capabilities_json TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    planned_return_at TEXT NOT NULL,
    frozen_priority INTEGER NOT NULL,
    queued_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'waitlisted','confirmed','packed','in_transit','pending_acceptance','active',
        'return_in_transit','pending_return','completed','released')),
    cap_ready INTEGER NOT NULL DEFAULT 0,
    qual_ready INTEGER NOT NULL DEFAULT 0,
    pack_ready INTEGER NOT NULL DEFAULT 0,
    transport_ready INTEGER NOT NULL DEFAULT 0,
    pack_crate_id TEXT,
    transport_json TEXT NOT NULL DEFAULT '{}',
    confirmed_at TEXT,
    completed_at TEXT,
    dedupe_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_loan_dedupe_open
    ON loan_requests(dedupe_hash) WHERE status NOT IN ('completed','released');
CREATE INDEX IF NOT EXISTS idx_loan_status ON loan_requests(status);
CREATE TABLE IF NOT EXISTS loan_occupancies (
    occupancy_id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL REFERENCES loan_requests(loan_id),
    resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loan_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_occupancy_resource_window
    ON loan_occupancies(resource_id, window_start, window_end);
CREATE TABLE IF NOT EXISTS loan_phase_checks (
    check_id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL REFERENCES loan_requests(loan_id),
    phase TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_phase_checks_lookup ON loan_phase_checks(loan_id, phase, evaluated_at);
CREATE TABLE IF NOT EXISTS loan_manifests (
    line_id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL REFERENCES loan_requests(loan_id),
    resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    packed_by TEXT, packed_at TEXT,
    accepted_by TEXT, accepted_at TEXT, condition_in TEXT,
    return_shipped_by TEXT, return_shipped_at TEXT,
    return_accepted_by TEXT, return_accepted_at TEXT, condition_back TEXT,
    returned INTEGER NOT NULL DEFAULT 0 CHECK(returned IN (0,1)),
    UNIQUE(loan_id, resource_id)
);
CREATE TABLE IF NOT EXISTS loan_resource_events (
    event_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES loan_resources(resource_id),
    kind TEXT NOT NULL CHECK(kind IN (
        'fault','delayed_return','transport_damage','capability_degraded','calibration_revoked')),
    effective_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_experiment_runs (
    run_id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL REFERENCES loan_requests(loan_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state_snapshot_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL REFERENCES loan_requests(loan_id),
    reason_event_id TEXT REFERENCES loan_resource_events(event_id),
    kind TEXT NOT NULL,
    explanation TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_accountability (
    issue_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('overdue_return','component_missing','transport_damage')),
    loan_id TEXT REFERENCES loan_requests(loan_id),
    resource_id TEXT REFERENCES loan_resources(resource_id),
    site_id TEXT REFERENCES sites(site_id),
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    created_at TEXT NOT NULL
);
"""


class LoanService:
    """实现跨站借调的登记、候补、占用、交接与追责规则。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.database.connection.executescript(LOAN_SCHEMA)

    # --- 通用辅助 -----------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _ts(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO-8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return dict(row)

    def _require_role(self, actor: dict[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return dict(row)

    def _require_site_member(self, actor: dict[str, Any], site: dict[str, Any]) -> None:
        if actor["role"] != "admin" and actor["organization_id"] != site["organization_id"]:
            raise PermissionDenied("不能代表其他组织的站点操作")

    def _resource(self, connection, resource_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM loan_resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备或组件不存在")
        item = dict(row)
        item["capabilities"] = json.loads(item.pop("capabilities_json"))
        return item

    def _loan(self, connection, loan_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM loan_requests WHERE loan_id=?", (loan_id,)).fetchone()
        if row is None:
            raise NotFoundError("借调不存在")
        return self._loan_dict(row)

    @staticmethod
    def _loan_dict(row) -> dict[str, Any]:
        item = dict(row)
        item["required_capabilities"] = json.loads(item.pop("required_capabilities_json"))
        item["transport"] = json.loads(item.pop("transport_json"))
        return item

    def _all_resources(self, connection) -> dict[str, dict[str, Any]]:
        resources = {}
        for row in connection.execute("SELECT * FROM loan_resources"):
            resources[row["resource_id"]] = self._resource(connection, row["resource_id"])
        return resources

    def _closure(self, resources: dict[str, dict[str, Any]], root_id: str) -> list[str]:
        """返回资源本身、祖先整机与全部后代组件的编号集合（有序）。"""

        ids = {root_id}
        current = root_id
        while parent := resources.get(current, {}).get("parent_id"):
            ids.add(parent)
            current = parent
        changed = True
        while changed:
            changed = False
            for rid, resource in resources.items():
                if rid not in ids and resource.get("parent_id") in ids:
                    ids.add(rid)
                    changed = True
        return [root_id] + sorted(ids - {root_id})

    def _travel_set(self, resources: dict[str, dict[str, Any]], root_id: str) -> list[str]:
        """借调整机时整机与全部在装组件随行；借调组件时仅该组件随行。"""

        root = resources[root_id]
        if root["resource_type"] == RESOURCE_UNIT:
            return [root_id] + [
                rid for rid, item in resources.items()
                if item["parent_id"] == root_id
            ]
        return [root_id]

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> dict[str, Any]:
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "replayed": False}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _record_check(self, connection, *, loan_id: str, phase: str, passed: bool,
                      reason: str, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO loan_phase_checks(check_id,loan_id,phase,passed,reason,detail_json,evaluated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, loan_id, phase, 1 if passed else 0, reason,
             canonical_json(detail), self._now()),
        )

    # --- 登记：资源 / 校准 / 运输箱 / 资格 / 承诺 ---------------------------

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str,
                          resource_type: str, name: str, site_id: str,
                          capabilities: list[str], parent_id: str | None = None,
                          location_note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "resource_id": resource_id, "resource_type": resource_type,
                   "name": name, "site_id": site_id, "capabilities": capabilities, "parent_id": parent_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            site = self._site(connection, site_id)
            if resource_type not in (RESOURCE_UNIT, RESOURCE_COMPONENT):
                raise ValidationError("resource_type 只能是 unit 或 component")
            if not isinstance(capabilities, list) or not all(isinstance(c, str) and c for c in capabilities):
                raise ValidationError("capabilities 必须是非空字符串列表")
            if resource_type == RESOURCE_COMPONENT and not parent_id:
                raise ValidationError("可拆组件必须指定所属整机 parent_id")
            if resource_type == RESOURCE_UNIT and parent_id:
                raise ValidationError("整机不能挂在其他资源下")
            if parent_id:
                parent = self._resource(connection, parent_id)
                if parent["resource_type"] != RESOURCE_UNIT:
                    raise ValidationError("组件只能挂在整机下")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO loan_resources(resource_id,resource_type,parent_id,name,site_id,"
                        "location_note,capabilities_json,status,revision,created_at) "
                        "VALUES(?,?,?,?,?,?,?, 'operational', 1, ?)",
                        (resource_id, resource_type, parent_id, name, site_id, location_note,
                         canonical_json(capabilities), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在或引用无效") from exc
                self._audit(connection, actor_id=actor_id, action="loan_resource.registered",
                            resource_type="loan_resource", resource_id=resource_id,
                            detail={"resource_type": resource_type, "site_id": site_id,
                                    "capabilities": capabilities, "parent_id": parent_id})
                response = {"resource_id": resource_id, "status": "operational", "revision": 1}
                return "loan_resource", resource_id, response

            return self._idempotent(connection, request_id=request_id, action="register_loan_resource",
                                    payload=payload, create=create)

    def register_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                             resource_id: str, certificate_ref: str, version: str,
                             covers_capabilities: list[str], covers_components: list[dict[str, Any]],
                             valid_from: str, valid_until: str) -> dict[str, Any]:
        valid_from = self._ts(valid_from, "valid_from")
        valid_until = self._ts(valid_until, "valid_until")
        if not valid_from < valid_until:
            raise ValidationError("校准有效期开始必须早于结束")
        payload = {"actor_id": actor_id, "calibration_id": calibration_id, "resource_id": resource_id,
                   "certificate_ref": certificate_ref, "version": version,
                   "covers_capabilities": covers_capabilities, "covers_components": covers_components,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            target = self._resource(connection, resource_id)
            if not isinstance(covers_capabilities, list) or not all(isinstance(c, str) for c in covers_capabilities):
                raise ValidationError("covers_capabilities 必须是字符串列表")
            if not isinstance(covers_components, list):
                raise ValidationError("covers_components 必须是列表")
            for item in covers_components:
                if not isinstance(item, dict) or "component_id" not in item or "revision" not in item:
                    raise ValidationError("covers_components 项必须包含 component_id 与 revision")
                component = self._resource(connection, item["component_id"])
                if target["resource_type"] == RESOURCE_UNIT and component["parent_id"] != resource_id:
                    raise ValidationError("整机证书只能覆盖其下属组件")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO loan_calibrations(calibration_id,resource_id,certificate_ref,version,"
                        "covers_capabilities_json,covers_components_json,valid_from,valid_until,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'active',?)",
                        (calibration_id, resource_id, certificate_ref, version,
                         canonical_json(covers_capabilities), canonical_json(covers_components),
                         valid_from, valid_until, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("校准编号或同资源版本号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="loan_calibration.registered",
                            resource_type="loan_calibration", resource_id=calibration_id,
                            detail={"resource_id": resource_id, "version": version,
                                    "certificate_ref": certificate_ref,
                                    "covers_capabilities": covers_capabilities,
                                    "covers_components": covers_components,
                                    "valid_from": valid_from, "valid_until": valid_until})
                response = {"calibration_id": calibration_id, "status": "active", "version": version}
                return "loan_calibration", calibration_id, response

            return self._idempotent(connection, request_id=request_id, action="register_loan_calibration",
                                    payload=payload, create=create)

    def register_crate(self, *, request_id: str, actor_id: str, crate_id: str, name: str,
                       site_id: str, fits_resource_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "crate_id": crate_id, "name": name, "site_id": site_id,
                   "fits_resource_id": fits_resource_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            self._site(connection, site_id)
            self._resource(connection, fits_resource_id)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO loan_crates(crate_id,name,site_id,fits_resource_id,status,created_at) "
                        "VALUES(?,?,?,?, 'available', ?)",
                        (crate_id, name, site_id, fits_resource_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运输箱编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="loan_crate.registered",
                            resource_type="loan_crate", resource_id=crate_id,
                            detail={"site_id": site_id, "fits_resource_id": fits_resource_id})
                response = {"crate_id": crate_id, "status": "available"}
                return "loan_crate", crate_id, response

            return self._idempotent(connection, request_id=request_id, action="register_loan_crate",
                                    payload=payload, create=create)

    def register_qualification(self, *, request_id: str, actor_id: str, qualified_actor_id: str,
                               capability: str, valid_until: str) -> dict[str, Any]:
        valid_until = self._ts(valid_until, "valid_until")
        payload = {"actor_id": actor_id, "qualified_actor_id": qualified_actor_id,
                   "capability": capability, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            qualified = self._actor(connection, qualified_actor_id)
            if qualified["role"] != "operator":
                raise ValidationError("资格只能授予操作员")

            def create():
                connection.execute(
                    "INSERT INTO loan_qualifications(actor_id,capability,valid_until,active) "
                    "VALUES(?,?,?,1) ON CONFLICT(actor_id,capability) DO UPDATE SET "
                    "valid_until=excluded.valid_until, active=1",
                    (qualified_actor_id, capability, valid_until),
                )
                self._audit(connection, actor_id=actor_id, action="loan_qualification.registered",
                            resource_type="loan_qualification", resource_id=qualified_actor_id,
                            detail={"capability": capability, "valid_until": valid_until})
                response = {"actor_id": qualified_actor_id, "capability": capability,
                            "valid_until": valid_until, "active": True}
                return "loan_qualification", f"{qualified_actor_id}:{capability}", response

            return self._idempotent(connection, request_id=request_id,
                                    action="register_loan_qualification", payload=payload, create=create)

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            site_id: str, title: str, priority: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "site_id": site_id,
                   "title": title, "priority": priority}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            self._site(connection, site_id)
            if not isinstance(priority, int) or priority < 1:
                raise ValidationError("priority 必须是不小于 1 的整数，数字越小优先级越高")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO loan_commitments(commitment_id,site_id,title,priority,frozen_at,active,created_at) "
                        "VALUES(?,?,?,?,?,1,?)",
                        (commitment_id, site_id, title, priority, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("研究承诺编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="loan_commitment.registered",
                            resource_type="loan_commitment", resource_id=commitment_id,
                            detail={"site_id": site_id, "title": title, "priority": priority})
                response = {"commitment_id": commitment_id, "priority": priority, "frozen": True}
                return "loan_commitment", commitment_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="register_loan_commitment", payload=payload, create=create)

    # --- 前置条件评估 -------------------------------------------------------

    def _lost_capabilities(self, connection, resource_id: str) -> set[str]:
        lost: set[str] = set()
        rows = connection.execute(
            "SELECT kind,detail_json FROM loan_resource_events WHERE resource_id=? "
            "AND kind IN ('fault','capability_degraded','transport_damage')",
            (resource_id,),
        ).fetchall()
        for row in rows:
            lost.update(json.loads(row["detail_json"]).get("lost_capabilities", []))
        return lost

    def _effective_capabilities(self, connection, resources, travel_ids: list[str]) -> set[str]:
        available: set[str] = set()
        for rid in travel_ids:
            available.update(resources[rid]["capabilities"])
        for rid in travel_ids:
            available -= self._lost_capabilities(connection, rid)
        return available

    def _select_calibration(self, connection, loan, resources, travel_ids: list[str]):
        """挑选覆盖当前借调的有效证书；找不到时返回 (None, 原因列表)。"""

        root = resources[loan["resource_id"]]
        owner_candidates = [loan["resource_id"]]
        current = root.get("parent_id")
        while current:
            owner_candidates.append(current)
            current = resources.get(current, {}).get("parent_id")
        reasons = []
        certificates = connection.execute(
            f"SELECT * FROM loan_calibrations WHERE status='active' AND resource_id IN "
            f"({','.join('?' for _ in owner_candidates)})",
            owner_candidates,
        ).fetchall()
        if not certificates:
            reasons.append("资源及其所属整机没有处于 active 状态的校准证书")
            return None, reasons
        required = set(loan["required_capabilities"])
        for row in certificates:
            covers_caps = set(json.loads(row["covers_capabilities_json"]))
            covered_parts = {
                item["component_id"]: item["revision"]
                for item in json.loads(row["covers_components_json"])
            }
            missing_caps = required - covers_caps
            missing_window = not (row["valid_from"] <= loan["window_start"]
                                  and row["valid_until"] >= loan["window_end"])
            uncovered = []
            for rid in travel_ids:
                if rid == row["resource_id"]:
                    continue
                if covered_parts.get(rid) != resources[rid]["revision"]:
                    uncovered.append({"resource_id": rid, "current_revision": resources[rid]["revision"],
                                      "covered_revision": covered_parts.get(rid)})
            if not missing_caps and not missing_window and not uncovered:
                return dict(row), reasons
            problems = []
            if missing_caps:
                problems.append(f"证书不覆盖能力：{sorted(missing_caps)}")
            if missing_window:
                problems.append("证书有效期不覆盖整个预约时段")
            if uncovered:
                problems.append(f"证书不覆盖当前组件修订：{uncovered}")
            reasons.append(f"证书 {row['calibration_id']}（版本 {row['version']}）" + "；".join(problems))
        return None, reasons

    def _evaluate_capability(self, connection, loan) -> tuple[bool, str, dict[str, Any]]:
        resources = self._all_resources(connection)
        travel_ids = self._travel_set(resources, loan["resource_id"])
        blocked = [rid for rid in travel_ids
                   if resources[rid]["status"] in ("faulty", "missing", "maintenance")]
        if blocked:
            reason = "随行资源处于故障或失联状态：" + ",".join(blocked)
            return False, reason, {"blocked_resources": blocked}
        effective = self._effective_capabilities(connection, resources, travel_ids)
        missing_caps = sorted(set(loan["required_capabilities"]) - effective)
        if missing_caps:
            return False, "设备当前能力不满足借调需要：" + ",".join(missing_caps), {
                "required": loan["required_capabilities"], "effective": sorted(effective),
                "missing_capabilities": missing_caps}
        certificate, reasons = self._select_calibration(connection, loan, resources, travel_ids)
        if certificate is None:
            detail = {"certificate_reasons": reasons}
            if reasons:
                return False, "没有覆盖当前组件与所需能力的有效校准证书：" + " | ".join(reasons), detail
            return False, "没有覆盖当前组件与所需能力的有效校准证书", detail
        return True, "设备能力与校准证书均满足", {
            "calibration_id": certificate["calibration_id"], "version": certificate["version"],
            "travel_resources": travel_ids, "effective_capabilities": sorted(effective)}

    def _evaluate_qualification(self, connection, loan) -> tuple[bool, str, dict[str, Any]]:
        destination = self._site(connection, loan["request_site_id"])
        rows = connection.execute(
            "SELECT q.capability FROM loan_qualifications q JOIN actors a ON a.actor_id=q.actor_id "
            "WHERE a.active=1 AND q.active=1 AND a.organization_id=? AND q.valid_until>=?",
            (destination["organization_id"], loan["window_end"]),
        ).fetchall()
        covered = {row["capability"] for row in rows}
        missing = sorted(set(loan["required_capabilities"]) - covered)
        if missing:
            return False, "借入站没有在归还期前持续具备全部所需能力资格的操作员：" + ",".join(missing), {
                "site_id": loan["request_site_id"], "covered": sorted(covered), "missing": missing}
        return True, "借入站操作员资格覆盖全部所需能力", {
            "site_id": loan["request_site_id"], "covered": sorted(covered)}

    def _evaluate_packaging(self, connection, loan) -> tuple[bool, str, dict[str, Any]]:
        if not loan["pack_ready"] or not loan["pack_crate_id"]:
            return False, "包装前置条件尚未确认：未指定可用运输箱", {}
        row = connection.execute("SELECT * FROM loan_crates WHERE crate_id=?",
                                 (loan["pack_crate_id"],)).fetchone()
        if row is None:
            return False, "已登记的运输箱不存在", {"crate_id": loan["pack_crate_id"]}
        if row["status"] != "available":
            return False, f"运输箱当前状态为 {row['status']}，不能用于包装", {"crate_status": row["status"]}
        resources = self._all_resources(connection)
        root = resources[loan["resource_id"]]
        fitting = {loan["resource_id"]}
        current = root.get("parent_id")
        while current:
            fitting.add(current)
            current = resources.get(current, {}).get("parent_id")
        if row["fits_resource_id"] not in fitting:
            return False, "运输箱不是为该整机或组件配备的", {
                "crate_id": row["crate_id"], "fits_resource_id": row["fits_resource_id"]}
        return True, "运输箱可用且与设备匹配", {
            "crate_id": row["crate_id"], "crate_status": row["status"]}

    def _evaluate_transport(self, connection, loan) -> tuple[bool, str, dict[str, Any]]:
        if not loan["transport_ready"] or not loan["transport"]:
            return False, "运输前置条件尚未确认：未安排承运与发运时间", {}
        detail = dict(loan["transport"])
        if detail.get("scheduled_dispatch_at", "") > loan["window_start"]:
            return False, "发运时间晚于预约开始时间", detail
        return True, "运输已经安排", detail

    def _evaluate_all(self, connection, loan):
        cap = self._evaluate_capability(connection, loan)
        qual = self._evaluate_qualification(connection, loan)
        pack = self._evaluate_packaging(connection, loan)
        transport = self._evaluate_transport(connection, loan)
        return {PHASE_CAPABILITY: cap, PHASE_QUALIFICATION: qual,
                PHASE_PACKAGING: pack, PHASE_TRANSPORT: transport}

    # --- 占用冲突 -----------------------------------------------------------

    def _overlapping_occupancies(self, connection, resource_ids, start: str, end: str):
        marks = ",".join("?" for _ in resource_ids)
        placeholders_status = ",".join("?" for _ in OCCUPYING_STATUSES)
        return connection.execute(
            f"SELECT o.*, l.status AS loan_status, l.frozen_priority, l.request_site_id "
            f"FROM loan_occupancies o JOIN loan_requests l ON l.loan_id=o.loan_id "
            f"WHERE o.resource_id IN ({marks}) AND l.status IN ({placeholders_status}) "
            f"AND o.window_start < ? AND o.window_end > ?",
            (*resource_ids, *OCCUPYING_STATUSES, end, start),
        ).fetchall()

    def _try_confirm_one(self, connection, loan, *, actor_id: str) -> str:
        """尝试把一条候补申请转为确认占用，返回结果状态。"""

        results = self._evaluate_all(connection, loan)
        connection.execute(
            "UPDATE loan_requests SET cap_ready=?, qual_ready=? WHERE loan_id=?",
            (1 if results[PHASE_CAPABILITY][0] else 0,
             1 if results[PHASE_QUALIFICATION][0] else 0, loan["loan_id"]),
        )
        for phase, (passed, reason, detail) in results.items():
            self._record_check(connection, loan_id=loan["loan_id"], phase=phase,
                               passed=passed, reason=reason, detail=detail)
        if not all(passed for passed, _, _ in results.values()):
            failed = [phase for phase, (passed, _, _) in results.items() if not passed]
            self._record_check(connection, loan_id=loan["loan_id"], phase="overall", passed=False,
                               reason="前置条件未全部满足，继续候补：" + ",".join(failed),
                               detail={"failed_phases": failed})
            return "waitlisted"
        resources = self._all_resources(connection)
        closure = self._closure(resources, loan["resource_id"])
        conflicts = self._overlapping_occupancies(
            connection, closure, loan["window_start"], loan["window_end"])
        if conflicts:
            winner = conflicts[0]
            reason = f"时段已被借调 {winner['loan_id']}（站点 {winner['request_site_id']}，" \
                     f"冻结优先级 {winner['frozen_priority']}）占用"
            self._record_check(connection, loan_id=loan["loan_id"], phase="overall", passed=False,
                               reason=reason,
                               detail={"blocked_by": [row["loan_id"] for row in conflicts]})
            return "waitlisted"
        # 稳定候补：冻结优先级更高（或同序更早入队）且争用同一整机/组件时段的申请
        # 仍在候补时，本申请即使前置条件就绪也不能越过它占用。
        self_root = resources.get(loan["resource_id"], {}).get("parent_id") or loan["resource_id"]
        ahead_rows = connection.execute(
            "SELECT * FROM loan_requests WHERE status='waitlisted' AND loan_id<>?",
            (loan["loan_id"],)).fetchall()
        blocked_by_priority = []
        for row in ahead_rows:
            other = self._loan_dict(row)
            other_root = resources.get(other["resource_id"], {}).get("parent_id") \
                or other["resource_id"]
            if other_root != self_root:
                continue
            if not (other["window_start"] < loan["window_end"]
                    and other["window_end"] > loan["window_start"]):
                continue
            other_key = (other["frozen_priority"], other["queued_at"], other["loan_id"])
            own_key = (loan["frozen_priority"], loan["queued_at"], loan["loan_id"])
            if other_key < own_key:
                blocked_by_priority.append(other["loan_id"])
        if blocked_by_priority:
            self._record_check(connection, loan_id=loan["loan_id"], phase="overall", passed=False,
                               reason="冻结优先级更高的候补申请尚未获得确认，按稳定候补顺序等待："
                                      + ",".join(blocked_by_priority),
                               detail={"ahead_loan_ids": blocked_by_priority})
            return "waitlisted"
        travel_ids = self._travel_set(resources, loan["resource_id"])
        for rid in travel_ids:
            connection.execute(
                "INSERT OR IGNORE INTO loan_occupancies(occupancy_id,loan_id,resource_id,"
                "window_start,window_end,created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, loan["loan_id"], rid, loan["window_start"], loan["window_end"],
                 self._now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO loan_manifests(line_id,loan_id,resource_id) VALUES(?,?,?)",
                (uuid.uuid4().hex, loan["loan_id"], rid),
            )
        connection.execute(
            "UPDATE loan_requests SET status='confirmed', confirmed_at=? WHERE loan_id=?",
            (self._now(), loan["loan_id"]),
        )
        self._record_check(connection, loan_id=loan["loan_id"], phase="overall", passed=True,
                           reason="四项前置条件同时满足，时段已占用",
                           detail={"occupied_resources": travel_ids,
                                   "capability": results[PHASE_CAPABILITY][2],
                                   "qualification": results[PHASE_QUALIFICATION][2],
                                   "packaging": results[PHASE_PACKAGING][2],
                                   "transport": results[PHASE_TRANSPORT][2]})
        self._audit(connection, actor_id=actor_id, action="loan.confirmed",
                    resource_type="loan", resource_id=loan["loan_id"],
                    detail={"resource_id": loan["resource_id"], "window_start": loan["window_start"],
                            "window_end": loan["window_end"], "frozen_priority": loan["frozen_priority"]})
        return CONFIRMED

    def _promote_waitlist(self, connection, *, actor_id: str) -> list[str]:
        """按冻结优先级稳定地尝试候补队列；争用同一时段只有一个胜者。"""

        promoted = []
        rows = connection.execute(
            "SELECT * FROM loan_requests WHERE status='waitlisted' "
            "ORDER BY frozen_priority ASC, queued_at ASC, loan_id ASC"
        ).fetchall()
        for row in rows:
            loan = self._loan_dict(row)
            if self._try_confirm_one(connection, loan, actor_id=actor_id) == CONFIRMED:
                promoted.append(loan["loan_id"])
        return promoted

    # --- 申请与分阶段确认 ---------------------------------------------------

    def submit_loan_request(self, *, request_id: str, actor_id: str, site_id: str,
                            commitment_id: str, resource_id: str, required_capabilities: list[str],
                            window_start: str, window_end: str, planned_return_at: str) -> dict[str, Any]:
        window_start = self._ts(window_start, "window_start")
        window_end = self._ts(window_end, "window_end")
        planned_return_at = self._ts(planned_return_at, "planned_return_at")
        if not window_start < window_end:
            raise ValidationError("预约开始必须早于结束")
        if not window_start <= planned_return_at <= window_end:
            raise ValidationError("计划归还时间必须落在预约时段内")
        if not isinstance(required_capabilities, list) or not required_capabilities \
                or not all(isinstance(c, str) for c in required_capabilities):
            raise ValidationError("required_capabilities 必须是非空字符串列表")
        payload = {"actor_id": actor_id, "site_id": site_id, "commitment_id": commitment_id,
                   "resource_id": resource_id, "required_capabilities": required_capabilities,
                   "window_start": window_start, "window_end": window_end,
                   "planned_return_at": planned_return_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "operator", "admin")
            site = self._site(connection, site_id)
            self._require_site_member(actor, site)
            resource = self._resource(connection, resource_id)
            owner_site = self._site(connection, resource["site_id"])
            commitment_row = connection.execute(
                "SELECT * FROM loan_commitments WHERE commitment_id=?", (commitment_id,)
            ).fetchone()
            if commitment_row is None:
                raise NotFoundError("研究承诺不存在")
            if not commitment_row["active"]:
                raise ValidationError("研究承诺已经停用")
            if commitment_row["site_id"] != site_id:
                raise ValidationError("研究承诺不属于申请站点")
            missing_caps = sorted(set(required_capabilities) - set(resource["capabilities"]))
            if missing_caps:
                raise ValidationError("资源本身不具备这些能力：" + ",".join(missing_caps))
            dedupe_hash = digest({
                "site_id": site_id, "commitment_id": commitment_id, "resource_id": resource_id,
                "required_capabilities": sorted(required_capabilities), "window_start": window_start,
                "window_end": window_end, "planned_return_at": planned_return_at,
            })
            loan_id = "loan-" + uuid.uuid4().hex[:16]
            queued_at = self._now()

            def create():
                duplicate = connection.execute(
                    "SELECT loan_id FROM loan_requests WHERE dedupe_hash=? "
                    "AND status NOT IN ('completed','released')",
                    (dedupe_hash,),
                ).fetchone()
                if duplicate:
                    raise ConflictError(
                        f"相同请求已经生成借调 {duplicate['loan_id']}，不得重复借调")
                connection.execute(
                    "INSERT INTO loan_requests(loan_id,request_site_id,owner_site_id,commitment_id,"
                    "resource_id,required_capabilities_json,window_start,window_end,planned_return_at,"
                    "frozen_priority,queued_at,status,dedupe_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'waitlisted',?)",
                    (loan_id, site_id, owner_site["site_id"], commitment_id, resource_id,
                     canonical_json(required_capabilities), window_start, window_end,
                     planned_return_at, commitment_row["priority"], queued_at, dedupe_hash),
                )
                self._audit(connection, actor_id=actor_id, action="loan.submitted",
                            resource_type="loan", resource_id=loan_id,
                            detail={"site_id": site_id, "resource_id": resource_id,
                                    "commitment_id": commitment_id,
                                    "frozen_priority": commitment_row["priority"],
                                    "window_start": window_start, "window_end": window_end})
                return "loan", loan_id, {"loan_id": loan_id, "status": WAITLISTED,
                                         "frozen_priority": commitment_row["priority"]}

            receipt = self._idempotent(connection, request_id=request_id, action="submit_loan_request",
                                       payload=payload, create=create)
            if not receipt.get("replayed"):
                # 统一由候补晋升流程评估四项前置条件：满足则占用，否则稳定候补。
                self._promote_waitlist(connection, actor_id=actor_id)
            return self._loan(connection, receipt["loan_id"]) | {"replayed": receipt["replayed"]}

    def confirm_phase(self, *, actor_id: str, loan_id: str, phase: str,
                      evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValidationError("phase 必须是 capability/qualification/packaging/transport 之一")
        evidence = evidence or {}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            loan = self._loan(connection, loan_id)
            if loan["status"] != WAITLISTED:
                raise ConflictError("只有候补中的申请可以补充阶段确认")
            if phase in (PHASE_CAPABILITY, PHASE_QUALIFICATION):
                self._require_role(actor, "admin", "reviewer", "operator")
            else:
                self._require_role(actor, "admin", "operator")
                owner_site = self._site(connection, loan["owner_site_id"])
                self._require_site_member(actor, owner_site)

            if phase == PHASE_PACKAGING:
                crate_id = str(evidence.get("crate_id", "")).strip()
                if not crate_id:
                    raise ValidationError("包装确认必须提供 crate_id")
                crate_row = connection.execute("SELECT * FROM loan_crates WHERE crate_id=?",
                                               (crate_id,)).fetchone()
                if crate_row is None:
                    raise NotFoundError("运输箱不存在")
                if crate_row["status"] != "available":
                    raise ConflictError(f"运输箱当前状态为 {crate_row['status']}，不能作为包装前置条件")
                connection.execute(
                    "UPDATE loan_requests SET pack_ready=1, pack_crate_id=? WHERE loan_id=?",
                    (crate_id, loan_id))
            elif phase == PHASE_TRANSPORT:
                handler = str(evidence.get("handler", "")).strip()
                scheduled = self._ts(evidence.get("scheduled_dispatch_at", ""), "scheduled_dispatch_at")
                if not handler:
                    raise ValidationError("运输确认必须提供 handler 与 scheduled_dispatch_at")
                if scheduled > loan["window_start"]:
                    raise ValidationError("发运时间不能晚于预约开始时间")
                connection.execute(
                    "UPDATE loan_requests SET transport_ready=1, transport_json=? WHERE loan_id=?",
                    (canonical_json({"handler": handler, "scheduled_dispatch_at": scheduled}), loan_id))

            loan = self._loan(connection, loan_id)
            promoted = self._promote_waitlist(connection, actor_id=actor_id)
            result = self._loan(connection, loan_id)
            result["promoted_loan_ids"] = promoted
            self._audit(connection, actor_id=actor_id, action="loan.phase_confirmed",
                        resource_type="loan", resource_id=loan_id,
                        detail={"phase": phase, "evidence": evidence, "confirmed": loan_id in promoted})
            return result

    def cancel_loan(self, *, actor_id: str, loan_id: str, reason: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            loan = self._loan(connection, loan_id)
            if loan["status"] != WAITLISTED:
                raise ConflictError("只能撤销尚在候补的申请")
            connection.execute(
                "UPDATE loan_requests SET status='released', completed_at=? WHERE loan_id=?",
                (self._now(), loan_id))
            connection.execute(
                "INSERT INTO loan_adjustments(adjustment_id,loan_id,reason_event_id,kind,explanation,created_at) "
                "VALUES(?,?,NULL,'released',?,?)",
                (uuid.uuid4().hex, loan_id, reason, self._now()))
            self._audit(connection, actor_id=actor_id, action="loan.cancelled",
                        resource_type="loan", resource_id=loan_id, detail={"reason": reason})
            promoted = self._promote_waitlist(connection, actor_id=actor_id)
            result = self._loan(connection, loan_id)
            result["promoted_loan_ids"] = promoted
            return result

    # --- 物流与验收交接 -----------------------------------------------------

    def _require_status(self, loan, *statuses: str) -> None:
        if loan["status"] not in statuses:
            raise ConflictError(
                f"借调当前状态为 {loan['status']}，该操作要求状态为 {'/'.join(statuses)}")

    def _manifest_lines(self, connection, loan_id: str):
        return connection.execute("SELECT * FROM loan_manifests WHERE loan_id=? ORDER BY line_id",
                                  (loan_id,)).fetchall()

    def _set_crate(self, connection, loan, status: str) -> None:
        if loan["pack_crate_id"]:
            connection.execute("UPDATE loan_crates SET status=? WHERE crate_id=?",
                               (status, loan["pack_crate_id"]))

    def pack_for_shipment(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, CONFIRMED)
            owner_site = self._site(connection, loan["owner_site_id"])
            self._require_site_member(actor, owner_site)
            now = self._now()
            connection.execute("UPDATE loan_manifests SET packed_by=?, packed_at=? WHERE loan_id=?",
                               (actor_id, now, loan_id))
            self._set_crate(connection, loan, "packed")
            connection.execute("UPDATE loan_requests SET status='packed' WHERE loan_id=?", (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.packed",
                        resource_type="loan", resource_id=loan_id, detail={})
            return self._loan(connection, loan_id)

    def ship(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, PACKED)
            self._set_crate(connection, loan, "in_transit")
            connection.execute("UPDATE loan_requests SET status='in_transit' WHERE loan_id=?",
                               (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.shipped",
                        resource_type="loan", resource_id=loan_id,
                        detail={"transport": loan["transport"]})
            return self._loan(connection, loan_id)

    def arrive(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, IN_TRANSIT)
            connection.execute("UPDATE loan_requests SET status='pending_acceptance' WHERE loan_id=?",
                               (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.arrived",
                        resource_type="loan", resource_id=loan_id, detail={})
            return self._loan(connection, loan_id)

    def _apply_damage(self, connection, *, actor_id: str, resource_id: str,
                      lost_capabilities: list[str], note: str) -> str:
        resource = self._resource(connection, resource_id)
        connection.execute(
            "UPDATE loan_resources SET status='degraded', revision=revision+1 WHERE resource_id=?",
            (resource_id,))
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO loan_resource_events(event_id,resource_id,kind,effective_at,detail_json,"
            "reported_by,created_at) VALUES(?,?, 'transport_damage', ?,?,?,?)",
            (event_id, resource_id, self._now(),
             canonical_json({"lost_capabilities": lost_capabilities, "note": note,
                             "revision_before": resource["revision"],
                             "revision_after": resource["revision"] + 1}),
             actor_id, self._now()))
        self._audit(connection, actor_id=actor_id, action="loan_resource.damaged",
                    resource_type="loan_resource", resource_id=resource_id,
                    detail={"event_id": event_id, "lost_capabilities": lost_capabilities, "note": note})
        return event_id

    def accept_delivery(self, *, actor_id: str, loan_id: str,
                        observed_damage: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """目的地验收：核对运输后校准证书是否仍覆盖当前组件修订。"""

        observed_damage = observed_damage or []
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, PENDING_ACCEPTANCE)
            destination = self._site(connection, loan["request_site_id"])
            self._require_site_member(actor, destination)
            now = self._now()
            damage_events = []
            for damage in observed_damage:
                damage_events.append(self._apply_damage(
                    connection, actor_id=actor_id, resource_id=damage["resource_id"],
                    lost_capabilities=list(damage.get("lost_capabilities", [])),
                    note=str(damage.get("note", ""))))

            lines = self._manifest_lines(connection, loan_id)
            for line in lines:
                connection.execute(
                    "UPDATE loan_manifests SET accepted_by=?, accepted_at=?, condition_in=? "
                    "WHERE line_id=?",
                    (actor_id, now, "damaged" if any(
                        d["resource_id"] == line["resource_id"] for d in observed_damage) else "ok",
                     line["line_id"]))
            loan = self._loan(connection, loan_id)
            cap_ok, cap_reason, cap_detail = self._evaluate_capability(connection, loan)
            self._record_check(connection, loan_id=loan_id, phase=PHASE_CAPABILITY,
                               passed=cap_ok, reason=cap_reason, detail=cap_detail)
            if not cap_ok:
                # 验收发现能力或校准覆盖不再满足：本预约退回候补，仅影响尚未产生的数据。
                connection.execute(
                    "UPDATE loan_requests SET status='waitlisted', cap_ready=0 WHERE loan_id=?",
                    (loan_id,))
                connection.execute("DELETE FROM loan_occupancies WHERE loan_id=?", (loan_id,))
                connection.execute(
                    "INSERT INTO loan_adjustments(adjustment_id,loan_id,reason_event_id,kind,explanation,created_at) "
                    "VALUES(?,?,NULL,'rewaitlisted',?,?)",
                    (uuid.uuid4().hex, loan_id, "验收未通过：" + cap_reason, now))
                self._set_crate(connection, loan, "inspection")
                self._audit(connection, actor_id=actor_id, action="loan.adjusted",
                            resource_type="loan", resource_id=loan_id,
                            detail={"at": "delivery_acceptance", "reason": cap_reason,
                                    "damage_events": damage_events})
                self._adjust_future(connection, damage_events, actor_id=actor_id)
                self._promote_waitlist(connection, actor_id=actor_id)
                return self._loan(connection, loan_id) | {"acceptance": "rejected_waitlisted",
                                                          "reason": cap_reason}
            connection.execute("UPDATE loan_requests SET status='active' WHERE loan_id=?", (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.accepted",
                        resource_type="loan", resource_id=loan_id,
                        detail={"damage_events": damage_events})
            if damage_events:
                self._adjust_future(connection, damage_events, actor_id=actor_id)
                self._promote_waitlist(connection, actor_id=actor_id)
            return self._loan(connection, loan_id) | {"acceptance": "active"}

    def start_experiment_run(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, ACTIVE)
            destination = self._site(connection, loan["request_site_id"])
            self._require_site_member(actor, destination)
            open_run = connection.execute(
                "SELECT 1 FROM loan_experiment_runs WHERE loan_id=? AND finished_at IS NULL",
                (loan_id,)).fetchone()
            if open_run:
                raise ConflictError("该借调已有未结束的实验")
            resources = self._all_resources(connection)
            travel_ids = self._travel_set(resources, loan["resource_id"])
            certificate, _ = self._select_calibration(connection, loan, resources, travel_ids)
            snapshot = {
                "snapshot_at": self._now(),
                "loan_id": loan_id,
                "resource_id": loan["resource_id"],
                "required_capabilities": loan["required_capabilities"],
                "calibration": None if certificate is None else {
                    "calibration_id": certificate["calibration_id"],
                    "version": certificate["version"],
                    "certificate_ref": certificate["certificate_ref"],
                    "valid_from": certificate["valid_from"], "valid_until": certificate["valid_until"],
                },
                "resources": [
                    {"resource_id": rid, "status": resources[rid]["status"],
                     "revision": resources[rid]["revision"],
                     "capabilities": resources[rid]["capabilities"]}
                    for rid in travel_ids
                ],
            }
            run_id = "run-" + uuid.uuid4().hex[:16]
            connection.execute(
                "INSERT INTO loan_experiment_runs(run_id,loan_id,started_at,state_snapshot_json,recorded_by) "
                "VALUES(?,?,?,?,?)",
                (run_id, loan_id, self._now(), canonical_json(snapshot), actor_id))
            self._audit(connection, actor_id=actor_id, action="loan_run.started",
                        resource_type="loan_run", resource_id=run_id,
                        detail={"loan_id": loan_id, "snapshot": snapshot})
            return {"run_id": run_id, "loan_id": loan_id, "started_at": snapshot["snapshot_at"],
                    "state_snapshot": snapshot}

    def finish_experiment_run(self, *, actor_id: str, run_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            row = connection.execute("SELECT * FROM loan_experiment_runs WHERE run_id=?",
                                     (run_id,)).fetchone()
            if row is None:
                raise NotFoundError("实验不存在")
            if row["finished_at"]:
                raise ConflictError("实验已经结束")
            connection.execute("UPDATE loan_experiment_runs SET finished_at=? WHERE run_id=?",
                               (self._now(), run_id))
            self._audit(connection, actor_id=actor_id, action="loan_run.finished",
                        resource_type="loan_run", resource_id=run_id,
                        detail={"loan_id": row["loan_id"]})
            return {"run_id": run_id, "loan_id": row["loan_id"], "finished_at": self._now()}

    def begin_return(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, ACTIVE)
            destination = self._site(connection, loan["request_site_id"])
            self._require_site_member(actor, destination)
            open_run = connection.execute(
                "SELECT 1 FROM loan_experiment_runs WHERE loan_id=? AND finished_at IS NULL",
                (loan_id,)).fetchone()
            if open_run:
                raise ConflictError("尚有实验未结束，不能归还")
            now = self._now()
            connection.execute(
                "UPDATE loan_manifests SET return_shipped_by=?, return_shipped_at=? WHERE loan_id=?",
                (actor_id, now, loan_id))
            self._set_crate(connection, loan, "in_transit")
            connection.execute("UPDATE loan_requests SET status='return_in_transit' WHERE loan_id=?",
                               (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.return_started",
                        resource_type="loan", resource_id=loan_id, detail={})
            return self._loan(connection, loan_id)

    def arrive_return(self, *, actor_id: str, loan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, RETURN_IN_TRANSIT)
            connection.execute("UPDATE loan_requests SET status='pending_return' WHERE loan_id=?",
                               (loan_id,))
            self._audit(connection, actor_id=actor_id, action="loan.return_arrived",
                        resource_type="loan", resource_id=loan_id, detail={})
            return self._loan(connection, loan_id)

    def accept_return(self, *, actor_id: str, loan_id: str,
                      conditions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._loan(connection, loan_id)
            self._require_status(loan, PENDING_RETURN)
            owner_site = self._site(connection, loan["owner_site_id"])
            self._require_site_member(actor, owner_site)
            now = self._now()
            lines = self._manifest_lines(connection, loan_id)
            damage_events, missing_resources = [], []
            for line in lines:
                observed = conditions.get(line["resource_id"])
                if observed is None:
                    raise ValidationError(f"归还验收缺少组件 {line['resource_id']} 的状态")
                if not observed.get("returned", False):
                    missing_resources.append(line["resource_id"])
                    connection.execute(
                        "UPDATE loan_resources SET status='missing' WHERE resource_id=?",
                        (line["resource_id"],))
                    connection.execute(
                        "UPDATE loan_manifests SET return_accepted_by=?, return_accepted_at=?, "
                        "condition_back='missing', returned=0 WHERE line_id=?",
                        (actor_id, now, line["line_id"]))
                    self._open_issue(connection, kind="component_missing", loan_id=loan_id,
                                     resource_id=line["resource_id"], site_id=loan["request_site_id"],
                                     detail={"note": str(observed.get("note", ""))})
                    continue
                condition = "damaged" if observed.get("condition") == "damaged" else "ok"
                connection.execute(
                    "UPDATE loan_manifests SET return_accepted_by=?, return_accepted_at=?, "
                    "condition_back=?, returned=1 WHERE line_id=?",
                    (actor_id, now, condition, line["line_id"]))
                if condition == "damaged":
                    event_id = self._apply_damage(
                        connection, actor_id=actor_id, resource_id=line["resource_id"],
                        lost_capabilities=list(observed.get("lost_capabilities", [])),
                        note=str(observed.get("note", "")))
                    damage_events.append(event_id)
                    self._open_issue(connection, kind="transport_damage", loan_id=loan_id,
                                     resource_id=line["resource_id"], site_id=loan["request_site_id"],
                                     detail={"event_id": event_id, "note": str(observed.get("note", ""))})
            overdue = now > loan["planned_return_at"]
            if overdue:
                self._open_issue(connection, kind="overdue_return", loan_id=loan_id,
                                 resource_id=loan["resource_id"], site_id=loan["request_site_id"],
                                 detail={"loan_id": loan_id,
                                         "planned_return_at": loan["planned_return_at"],
                                         "returned_at": now})
            if missing_resources:
                self._set_crate(connection, loan, "inspection")
            elif damage_events:
                self._set_crate(connection, loan, "damaged")
            else:
                self._set_crate(connection, loan, "available")
            connection.execute(
                "UPDATE loan_requests SET status='completed', completed_at=? WHERE loan_id=?",
                (now, loan_id))
            self._audit(connection, actor_id=actor_id, action="loan.completed",
                        resource_type="loan", resource_id=loan_id,
                        detail={"overdue": overdue, "missing_resources": missing_resources,
                                "damage_events": damage_events})
            self._adjust_future(connection, damage_events, actor_id=actor_id)
            self._promote_waitlist(connection, actor_id=actor_id)
            return self._loan(connection, loan_id) | {
                "overdue": overdue, "missing_resources": missing_resources,
                "damage_event_ids": damage_events}

    # --- 事件、调整、追责 ---------------------------------------------------

    def _open_issue(self, connection, *, kind: str, loan_id: str | None, resource_id: str | None,
                    site_id: str | None, detail: dict[str, Any]) -> str:
        existing = connection.execute(
            "SELECT issue_id FROM loan_accountability WHERE kind=? AND "
            "COALESCE(loan_id,'')=COALESCE(?,'') AND COALESCE(resource_id,'')=COALESCE(?,'') "
            "AND status='open'",
            (kind, loan_id, resource_id),
        ).fetchone()
        if existing:
            return existing["issue_id"]
        issue_id = "issue-" + uuid.uuid4().hex[:16]
        connection.execute(
            "INSERT INTO loan_accountability(issue_id,kind,loan_id,resource_id,site_id,detail_json,"
            "status,created_at) VALUES(?,?,?,?,?,?,'open',?)",
            (issue_id, kind, loan_id, resource_id, site_id, canonical_json(detail), self._now()))
        self._audit(connection, actor_id="system", action=f"loan_issue.{kind}_opened",
                    resource_type="loan_accountability", resource_id=issue_id,
                    detail={"kind": kind, "loan_id": loan_id, "resource_id": resource_id})
        return issue_id

    def _adjust_future(self, connection, reason_event_ids: list[str] | None, *,
                       actor_id: str, extra_loan_ids: list[str] | None = None,
                       explanation: str | None = None) -> list[str]:
        """把受事件影响、尚未开始执行的预约退回稳定候补；已完成数据不动。"""

        reason_event_ids = reason_event_ids or []
        adjusted = []
        rows = connection.execute(
            f"SELECT * FROM loan_requests WHERE status IN "
            f"({','.join('?' for _ in ADJUSTABLE_STATUSES)}) ORDER BY frozen_priority, queued_at",
            ADJUSTABLE_STATUSES,
        ).fetchall()
        for row in rows:
            loan = self._loan_dict(row)
            if extra_loan_ids and loan["loan_id"] not in extra_loan_ids:
                continue
            cap = self._evaluate_capability(connection, loan)
            qual = self._evaluate_qualification(connection, loan)
            if extra_loan_ids or not cap[0] or not qual[0]:
                why = explanation or "；".join(
                    text for passed, text, _ in (cap, qual) if not passed)
                connection.execute("DELETE FROM loan_occupancies WHERE loan_id=?",
                                   (loan["loan_id"],))
                connection.execute(
                    "UPDATE loan_requests SET status='waitlisted', cap_ready=?, qual_ready=? "
                    "WHERE loan_id=?",
                    (1 if cap[0] else 0, 1 if qual[0] else 0, loan["loan_id"]))
                connection.execute(
                    "INSERT INTO loan_adjustments(adjustment_id,loan_id,reason_event_id,kind,"
                    "explanation,created_at) VALUES(?,?,?, 'rewaitlisted', ?,?)",
                    (uuid.uuid4().hex, loan["loan_id"], reason_event_ids[0] if reason_event_ids else None,
                     why, self._now()))
                self._record_check(connection, loan_id=loan["loan_id"], phase="overall", passed=False,
                                   reason="事件导致预约被调整回候补：" + why,
                                   detail={"reason_event_ids": reason_event_ids})
                self._audit(connection, actor_id=actor_id, action="loan.adjusted",
                            resource_type="loan", resource_id=loan["loan_id"],
                            detail={"reason_event_ids": reason_event_ids, "explanation": why})
                adjusted.append(loan["loan_id"])
        return adjusted

    def report_resource_event(self, *, actor_id: str, resource_id: str, kind: str,
                              detail: dict[str, Any] | None = None) -> dict[str, Any]:
        if kind not in REPORTABLE_EVENTS:
            raise ValidationError("不支持的事件类型")
        detail = detail or {}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            resource = self._resource(connection, resource_id)
            now = self._now()
            event_id = uuid.uuid4().hex
            extra_loans = None
            explanation = None
            if kind == EVENT_FAULT:
                connection.execute(
                    "UPDATE loan_resources SET status='faulty' WHERE resource_id=?", (resource_id,))
            elif kind == EVENT_CAPABILITY_DEGRADED:
                connection.execute(
                    "UPDATE loan_resources SET status='degraded' WHERE resource_id=?", (resource_id,))
            elif kind == EVENT_TRANSPORT_DAMAGE:
                connection.execute(
                    "UPDATE loan_resources SET status='degraded', revision=revision+1 WHERE resource_id=?",
                    (resource_id,))
                detail = {**detail, "revision_before": resource["revision"],
                          "revision_after": resource["revision"] + 1}
            elif kind == EVENT_DELAYED_RETURN:
                loan_id = detail.get("loan_id")
                new_return_at = self._ts(detail.get("new_return_at", ""), "new_return_at")
                loan = self._loan(connection, loan_id)
                if loan["status"] not in (ACTIVE, RETURN_IN_TRANSIT, PENDING_RETURN):
                    raise ConflictError("只有执行中的借调可以上报延期归还")
                detail = {"loan_id": loan_id, "planned_return_at": loan["planned_return_at"],
                          "new_return_at": new_return_at}
                self._open_issue(connection, kind="overdue_return", loan_id=loan_id,
                                 resource_id=resource_id, site_id=loan["request_site_id"],
                                 detail=detail)
                # 找出在新归还时间之前就要开始、且与该资源互斥的已排预约。
                resources = self._all_resources(connection)
                closure = self._closure(resources, resource_id)
                blocked_rows = self._overlapping_occupancies(connection, closure, now, new_return_at)
                extra_loans = [r["loan_id"] for r in blocked_rows
                               if r["loan_id"] != loan_id and r["loan_status"] in ADJUSTABLE_STATUSES]
                explanation = f"设备延期至 {new_return_at} 才能归还"
            connection.execute(
                "INSERT INTO loan_resource_events(event_id,resource_id,kind,effective_at,detail_json,"
                "reported_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, resource_id, kind, now, canonical_json(detail), actor_id, now))
            self._audit(connection, actor_id=actor_id, action=f"loan_resource.{kind}",
                        resource_type="loan_resource", resource_id=resource_id,
                        detail={"event_id": event_id, "detail": detail})
            adjusted = self._adjust_future(connection, [event_id], actor_id=actor_id,
                                           extra_loan_ids=extra_loans, explanation=explanation)
            promoted = self._promote_waitlist(connection, actor_id=actor_id)
            return {"event_id": event_id, "resource_id": resource_id, "kind": kind,
                    "adjusted_loan_ids": adjusted, "promoted_loan_ids": promoted}

    def revoke_calibration(self, *, actor_id: str, calibration_id: str, reason: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin")
            row = connection.execute("SELECT * FROM loan_calibrations WHERE calibration_id=?",
                                     (calibration_id,)).fetchone()
            if row is None:
                raise NotFoundError("校准证书不存在")
            if row["status"] != "active":
                raise ConflictError("证书已经不是 active 状态")
            now = self._now()
            connection.execute("UPDATE loan_calibrations SET status='revoked' WHERE calibration_id=?",
                               (calibration_id,))
            event_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO loan_resource_events(event_id,resource_id,kind,effective_at,detail_json,"
                "reported_by,created_at) VALUES(?,?,'calibration_revoked',?,?,?,?)",
                (event_id, row["resource_id"], now,
                 canonical_json({"calibration_id": calibration_id, "version": row["version"],
                                 "reason": reason}), actor_id, now))
            self._audit(connection, actor_id=actor_id, action="loan_calibration.revoked",
                        resource_type="loan_calibration", resource_id=calibration_id,
                        detail={"resource_id": row["resource_id"], "version": row["version"],
                                "reason": reason})
            adjusted = self._adjust_future(connection, [event_id], actor_id=actor_id)
            promoted = self._promote_waitlist(connection, actor_id=actor_id)
            return {"calibration_id": calibration_id, "status": "revoked",
                    "adjusted_loan_ids": adjusted, "promoted_loan_ids": promoted}

    def detect_overdue(self, *, actor_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "reviewer")
            now = self._now()
            rows = connection.execute(
                "SELECT * FROM loan_requests WHERE status IN ('active','return_in_transit','pending_return') "
                "AND planned_return_at < ? ORDER BY planned_return_at", (now,)).fetchall()
            issues = []
            for row in rows:
                loan = self._loan_dict(row)
                issue_id = self._open_issue(
                    connection, kind="overdue_return", loan_id=loan["loan_id"],
                    resource_id=loan["resource_id"], site_id=loan["request_site_id"],
                    detail={"loan_id": loan["loan_id"],
                            "planned_return_at": loan["planned_return_at"], "detected_at": now,
                            "current_status": loan["status"]})
                issues.append(issue_id)
            return {"detected_at": now, "issue_ids": issues, "overdue_count": len(issues)}

    # --- 查询与解释 ---------------------------------------------------------

    def list_resources(self, site_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT r.*, (SELECT json_group_array(c.version) FROM loan_calibrations c "
                 "WHERE c.resource_id=r.resource_id AND c.status='active') AS active_versions "
                 "FROM loan_resources r")
        parameters: list[Any] = []
        if site_id:
            query += " WHERE r.site_id=?"
            parameters.append(site_id)
        query += " ORDER BY r.resource_id"
        items = []
        for row in self.database.connection.execute(query, parameters):
            item = dict(row)
            item["capabilities"] = json.loads(item.pop("capabilities_json"))
            item["active_calibration_versions"] = [v for v in json.loads(item.pop("active_versions")) if v]
            items.append(item)
        return items

    def list_waitlist(self, resource_id: str | None = None) -> list[dict[str, Any]]:
        resources = self._all_resources(self.database.connection)
        query = "SELECT * FROM loan_requests WHERE status='waitlisted'"
        parameters: list[Any] = []
        if resource_id:
            closure = self._closure(resources, resource_id)
            query += f" AND resource_id IN ({','.join('?' for _ in closure)})"
            parameters.extend(closure)
        query += " ORDER BY frozen_priority ASC, queued_at ASC, loan_id ASC"
        return [self._loan_dict(row) for row in
                self.database.connection.execute(query, parameters).fetchall()]

    def _latest_checks(self, connection, loan_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM loan_phase_checks WHERE loan_id=? ORDER BY evaluated_at ASC, rowid ASC",
            (loan_id,)).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row["phase"]] = {"passed": bool(row["passed"]), "reason": row["reason"],
                                    "detail": json.loads(row["detail_json"]),
                                    "evaluated_at": row["evaluated_at"]}
        return latest

    def explain_slot(self, *, resource_id: str, start: str, end: str) -> dict[str, Any]:
        start = self._ts(start, "start")
        end = self._ts(end, "end")
        if not start < end:
            raise ValidationError("时段开始必须早于结束")
        connection = self.database.connection
        resource = self._resource(connection, resource_id)
        resources = self._all_resources(connection)
        closure = self._closure(resources, resource_id)
        occupants_by_loan: dict[str, dict[str, Any]] = {}
        for row in self._overlapping_occupancies(connection, closure, start, end):
            loan = self._loan(connection, row["loan_id"])
            entry = occupants_by_loan.get(loan["loan_id"])
            if entry is None:
                checks = self._latest_checks(connection, loan["loan_id"])
                entry = {
                    "loan_id": loan["loan_id"], "site_id": loan["request_site_id"],
                    "commitment_id": loan["commitment_id"], "frozen_priority": loan["frozen_priority"],
                    "status": loan["status"], "window_start": loan["window_start"],
                    "window_end": loan["window_end"], "occupied_resource_ids": [],
                    "explanation": "四项前置条件同时满足后于 "
                                   f"{loan['confirmed_at']} 占用；证据："
                                   f"{json.dumps(checks.get('overall', {}).get('detail', {}), ensure_ascii=False)}",
                }
                occupants_by_loan[loan["loan_id"]] = entry
            entry["occupied_resource_ids"].append(row["resource_id"])
        occupants = list(occupants_by_loan.values())
        candidates = connection.execute(
            "SELECT * FROM loan_requests WHERE status='waitlisted' AND window_start < ? AND window_end > ? "
            "ORDER BY frozen_priority ASC, queued_at ASC, loan_id ASC",
            (end, start)).fetchall()
        waiting = []
        for row in candidates:
            loan = self._loan_dict(row)
            if loan["resource_id"] not in closure and resource_id not in self._closure(
                    resources, loan["resource_id"]):
                continue
            checks = self._latest_checks(connection, loan["loan_id"])
            reasons = [f"[{phase}] {entry['reason']}" for phase, entry in checks.items()
                       if phase != "overall" and not entry["passed"]]
            for occupant in occupants:
                reasons.append(
                    f"时段已被借调 {occupant['loan_id']} 占用（冻结优先级 "
                    f"{occupant['frozen_priority']}）；候补按冻结优先级 {loan['frozen_priority']} "
                    f"排队，不会抢占已确认占用")
            if not occupants and not reasons:
                reasons.append("前置条件评估通过，等待下次候补晋升时占用")
            waiting.append({"loan_id": loan["loan_id"], "site_id": loan["request_site_id"],
                            "frozen_priority": loan["frozen_priority"], "queued_at": loan["queued_at"],
                            "window_start": loan["window_start"], "window_end": loan["window_end"],
                            "not_selected_reasons": reasons, "latest_checks": checks})
        return {"resource_id": resource_id, "start": start, "end": end,
                "occupied_by": occupants, "waitlisted": waiting}

    def explain_loan(self, loan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        loan = self._loan(connection, loan_id)
        checks = [
            {"phase": row["phase"], "passed": bool(row["passed"]), "reason": row["reason"],
             "detail": json.loads(row["detail_json"]), "evaluated_at": row["evaluated_at"]}
            for row in connection.execute(
                "SELECT * FROM loan_phase_checks WHERE loan_id=? ORDER BY evaluated_at, rowid",
                (loan_id,)).fetchall()
        ]
        adjustments = [dict(row) for row in connection.execute(
            "SELECT * FROM loan_adjustments WHERE loan_id=? ORDER BY created_at", (loan_id,)).fetchall()]
        manifest = []
        for row in self._manifest_lines(connection, loan_id):
            item = dict(row)
            manifest.append(item)
        runs = [
            {"run_id": row["run_id"], "started_at": row["started_at"], "finished_at": row["finished_at"],
             "state_snapshot": json.loads(row["state_snapshot_json"])}
            for row in connection.execute(
                "SELECT * FROM loan_experiment_runs WHERE loan_id=? ORDER BY started_at",
                (loan_id,)).fetchall()
        ]
        issues = [dict(row) for row in connection.execute(
            "SELECT * FROM loan_accountability WHERE loan_id=? ORDER BY created_at",
            (loan_id,)).fetchall()]
        for issue in issues:
            issue["detail"] = json.loads(issue.pop("detail_json"))
        timeline = [
            {"sequence": row["sequence"], "action": row["action"], "actor_id": row["actor_id"],
             "occurred_at": row["occurred_at"], "detail": json.loads(row["detail_json"])}
            for row in connection.execute(
                "SELECT * FROM audit_events WHERE resource_type='loan' AND resource_id=? "
                "UNION ALL SELECT * FROM audit_events WHERE resource_type='loan_run' "
                "AND json_extract(detail_json,'$.loan_id')=? ORDER BY occurred_at",
                (loan_id, loan_id)).fetchall()
        ]
        return {"loan": loan, "phase_checks": checks, "adjustments": adjustments,
                "manifest": manifest, "experiment_runs": runs, "accountability_issues": issues,
                "timeline": timeline}

    def list_issues(self, kind: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM loan_accountability WHERE kind=? ORDER BY created_at", (kind,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            items.append(item)
        return items

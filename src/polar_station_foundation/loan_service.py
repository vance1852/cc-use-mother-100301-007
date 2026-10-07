"""实现跨站设备借调的核心业务规则。

所有写入都在 ``BEGIN IMMEDIATE`` 事务内完成，因此并发接受同一时段时只会有
一个胜者；业务状态全部落在 SQLite，服务重启后运输中与等待验收的流程继续保留。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .loan_domain import (
    LOAN_PHASES,
    PHASE_EQUIPMENT,
    PHASE_OPERATOR,
    PHASE_PACKING,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_IN_TRANSIT,
    STATUS_INTERRUPTED,
    STATUS_PENDING_ACCEPTANCE,
    STATUS_REQUESTED,
    STATUS_WAITLISTED,
)
from .loan_models import (
    Calibration,
    Equipment,
    ExperimentDataView,
    IncidentView,
    LoanRequestView,
    SlotDecision,
)
from .storage import Database

EQUIPMENT_STATUSES = frozenset({"available", "in_maintenance", "failed", "missing", "in_transit", "retired"})
CRATE_STATUSES = frozenset({"sealed", "unsealed", "damaged", "not_applicable"})


class LoanService:
    """协调设备登记、分阶段确认、占位、候补、事故与追责。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return self._stamp(self._now_dt())

    def _stamp(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _parse_time(self, value: str, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _time_field(self, value: str, field: str) -> str:
        return self._stamp(self._parse_time(value, field))

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_role(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create) -> tuple[str, bool]:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return row["resource_id"], True
        resource_id = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, "loan_resource", resource_id,
             canonical_json({"resource_id": resource_id}), self._now()),
        )
        return resource_id, False

    def _capabilities(self, value: Iterable[str] | None, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"{field} 必须是数组")
        result = sorted({str(item).strip() for item in value})
        if any(not item for item in result):
            raise ValidationError(f"{field} 不能包含空值")
        return result

    # ------------------------------------------------------------------ 设备与组件

    def register_equipment(self, *, request_id: str, actor_id: str, equipment_id: str, kind: str,
                           name: str, site_id: str | None = None, parent_id: str | None = None,
                           capabilities: Iterable[str] | None = None,
                           crate_status: str = "not_applicable") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "kind": kind, "name": name,
                   "site_id": site_id, "parent_id": parent_id, "capabilities": list(capabilities or []),
                   "crate_status": crate_status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            equipment_id = str(equipment_id).strip()
            name = str(name).strip()
            if not equipment_id or not name:
                raise ValidationError("equipment_id 与 name 不能为空")
            if kind not in ("machine", "component"):
                raise ValidationError("kind 必须是 machine 或 component")
            caps = self._capabilities(capabilities, "capabilities")
            if crate_status not in CRATE_STATUSES:
                raise ValidationError("crate_status 不合法")
            if kind == "component":
                if not parent_id:
                    raise ValidationError("组件必须指定 parent_id")
                parent = connection.execute("SELECT * FROM equipment WHERE equipment_id=?", (parent_id,)).fetchone()
                if parent is None or parent["kind"] != "machine":
                    raise ValidationError("parent_id 必须指向已登记整机")
                if crate_status == "not_applicable":
                    crate_status = "unsealed"
            elif parent_id:
                raise ValidationError("整机不能指定 parent_id")
            if site_id and connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create() -> str:
                try:
                    connection.execute(
                        "INSERT INTO equipment(equipment_id,kind,parent_id,name,site_id,status,capabilities_json,"
                        "crate_status,attached,config_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                        (equipment_id, kind, parent_id, name, site_id, "available", canonical_json(caps),
                         crate_status, 1, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                if parent_id:
                    self._bump_config(connection, parent_id, actor_id)
                self._audit(connection, actor_id=actor_id, action="equipment.registered",
                            resource_type="equipment", resource_id=equipment_id,
                            detail={"kind": kind, "parent_id": parent_id, "name": name,
                                    "capabilities": caps, "site_id": site_id})
                return equipment_id

            resource_id, replayed = self._idempotent(
                connection, request_id=request_id, action="register_equipment", payload=payload, create=create)
            return {"equipment_id": resource_id, "replayed": replayed}

    def _bump_config(self, connection, machine_id: str | None, actor_id: str) -> None:
        if not machine_id:
            return
        connection.execute("UPDATE equipment SET config_version=config_version+1, updated_at=? WHERE equipment_id=?",
                           (self._now(), machine_id))

    def update_equipment_status(self, *, actor_id: str, equipment_id: str, status: str,
                                site_id: str | None = None) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            if status not in EQUIPMENT_STATUSES:
                raise ValidationError("status 不合法")
            row = self.get_equipment_row(connection, equipment_id)
            if site_id is not None and connection.execute(
                    "SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            connection.execute("UPDATE equipment SET status=?, site_id=COALESCE(?, site_id), updated_at=? WHERE equipment_id=?",
                               (status, site_id, self._now(), equipment_id))
            self._audit(connection, actor_id=actor_id, action="equipment.status_changed",
                        resource_type="equipment", resource_id=equipment_id,
                        detail={"status": status, "site_id": site_id})
            if status == "available":
                self._promote_waitlist(connection, actor_id, row["parent_id"] or equipment_id)

    def set_crate_status(self, *, actor_id: str, equipment_id: str, crate_status: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            if crate_status not in CRATE_STATUSES:
                raise ValidationError("crate_status 不合法")
            row = self.get_equipment_row(connection, equipment_id)
            connection.execute("UPDATE equipment SET crate_status=?, updated_at=? WHERE equipment_id=?",
                               (crate_status, self._now(), equipment_id))
            self._audit(connection, actor_id=actor_id, action="equipment.crate_changed",
                        resource_type="equipment", resource_id=equipment_id,
                        detail={"crate_status": crate_status})
            if crate_status == "sealed":
                self._promote_waitlist(connection, actor_id, row["parent_id"] or equipment_id)

    def get_equipment_row(self, connection, equipment_id: str):
        row = connection.execute("SELECT * FROM equipment WHERE equipment_id=?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return row

    def _equipment_view(self, row) -> Equipment:
        return Equipment(row["equipment_id"], row["kind"], row["parent_id"], row["name"], row["site_id"],
                         row["status"], frozenset(json.loads(row["capabilities_json"])), row["crate_status"],
                         bool(row["attached"]), row["config_version"], row["created_at"], row["updated_at"])

    def get_equipment(self, equipment_id: str) -> Equipment:
        row = self.get_equipment_row(self.database.connection, equipment_id)
        return self._equipment_view(row)

    def list_components(self, machine_id: str) -> list[Equipment]:
        rows = self.database.connection.execute(
            "SELECT * FROM equipment WHERE parent_id=? ORDER BY equipment_id", (machine_id,)).fetchall()
        return [self._equipment_view(row) for row in rows]

    def _config_signature(self, connection, machine_id: str, component_ids: Iterable[str]) -> tuple[str, list[str]]:
        """按给定组件配置版本计算签名；运输后重算即可判断证书是否仍覆盖。"""
        ids = sorted(set(component_ids))
        material = [f"machine:{machine_id}"]
        for component_id in ids:
            row = self.get_equipment_row(connection, component_id)
            if row["kind"] != "component" or row["parent_id"] != machine_id:
                raise ValidationError(f"{component_id} 不是 {machine_id} 的组件")
            material.append(f"{component_id}:{row['config_version']}:{int(row['attached'])}")
        return digest(material), ids

    def _full_config_signature(self, connection, machine_id: str) -> tuple[str, list[str]]:
        """对整机当前全部在装组件计算签名，作为校准证书是否仍覆盖当前配置的依据。"""
        rows = connection.execute(
            "SELECT equipment_id FROM equipment WHERE parent_id=? AND attached=1 ORDER BY equipment_id",
            (machine_id,)).fetchall()
        return self._config_signature(connection, machine_id, [row["equipment_id"] for row in rows])

    # ------------------------------------------------------------------ 校准与资格

    def issue_calibration(self, *, request_id: str, actor_id: str, certificate_id: str, equipment_id: str,
                          calibration_version: str, valid_from: str, valid_until: str) -> dict[str, Any]:
        start = self._time_field(valid_from, "valid_from")
        end = self._time_field(valid_until, "valid_until")
        if end <= start:
            raise ValidationError("valid_until 必须晚于 valid_from")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            machine = self.get_equipment_row(connection, equipment_id)
            if machine["kind"] != "machine":
                raise ValidationError("校准证书只能签发给整机")
            component_rows = connection.execute(
                "SELECT equipment_id FROM equipment WHERE parent_id=? AND attached=1", (equipment_id,)).fetchall()
            component_ids = [row["equipment_id"] for row in component_rows]
            signature, covered = self._config_signature(connection, equipment_id, component_ids)
            payload = {"actor_id": actor_id, "certificate_id": certificate_id, "equipment_id": equipment_id,
                       "calibration_version": calibration_version, "valid_from": start,
                       "valid_until": end, "covered_signature": signature}

            def create() -> str:
                try:
                    connection.execute(
                        "INSERT INTO calibrations(certificate_id,equipment_id,calibration_version,valid_from,"
                        "valid_until,status,covered_signature,covered_components_json,created_at) "
                        "VALUES(?,?,?,?,?,'valid',?,?,?)",
                        (certificate_id, equipment_id, str(calibration_version).strip(), start, end,
                         signature, canonical_json(covered), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("证书编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="calibration.issued",
                            resource_type="calibration", resource_id=certificate_id,
                            detail={"equipment_id": equipment_id, "calibration_version": calibration_version,
                                    "valid_from": start, "valid_until": end, "covered_signature": signature})
                return certificate_id

            resource_id, replayed = self._idempotent(
                connection, request_id=request_id, action="issue_calibration", payload=payload, create=create)
            self._promote_waitlist(connection, actor_id, equipment_id)
            return {"certificate_id": resource_id, "replayed": replayed}

    def get_calibration(self, certificate_id: str) -> Calibration:
        row = self.database.connection.execute(
            "SELECT * FROM calibrations WHERE certificate_id=?", (certificate_id,)).fetchone()
        if row is None:
            raise NotFoundError("校准证书不存在")
        return self._calibration_view(row)

    def _calibration_view(self, row) -> Calibration:
        return Calibration(row["certificate_id"], row["equipment_id"], row["calibration_version"],
                           row["valid_from"], row["valid_until"], row["status"], row["covered_signature"],
                           tuple(json.loads(row["covered_components_json"])), row["revoked_reason"],
                           row["created_at"])

    def register_qualification(self, *, request_id: str, actor_id: str, qualification_id: str,
                               target_actor_id: str, capability: str,
                               valid_from: str, valid_until: str) -> dict[str, Any]:
        start = self._time_field(valid_from, "valid_from")
        end = self._time_field(valid_until, "valid_until")
        capability = str(capability).strip()
        if not capability:
            raise ValidationError("capability 不能为空")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            self._actor(connection, target_actor_id)
            payload = {"actor_id": actor_id, "qualification_id": qualification_id,
                       "target_actor_id": target_actor_id, "capability": capability,
                       "valid_from": start, "valid_until": end}

            def create() -> str:
                try:
                    connection.execute(
                        "INSERT INTO qualifications(qualification_id,actor_id,capability,valid_from,valid_until,"
                        "status,created_at) VALUES(?,?,?,?,?,'valid',?)",
                        (qualification_id, target_actor_id, capability, start, end, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资格编号已存在或该操作者已有相同能力资格") from exc
                self._audit(connection, actor_id=actor_id, action="qualification.registered",
                            resource_type="qualification", resource_id=qualification_id,
                            detail={"target_actor_id": target_actor_id, "capability": capability,
                                    "valid_from": start, "valid_until": end})
                return qualification_id

            resource_id, replayed = self._idempotent(
                connection, request_id=request_id, action="register_qualification",
                payload=payload, create=create)
            return {"qualification_id": resource_id, "replayed": replayed}

    def revoke_qualification(self, *, actor_id: str, qualification_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "reviewer")
            row = connection.execute("SELECT * FROM qualifications WHERE qualification_id=?",
                                     (qualification_id,)).fetchone()
            if row is None:
                raise NotFoundError("资格不存在")
            connection.execute("UPDATE qualifications SET status='revoked' WHERE qualification_id=?",
                               (qualification_id,))
            self._audit(connection, actor_id=actor_id, action="qualification.revoked",
                        resource_type="qualification", resource_id=qualification_id,
                        detail={"target_actor_id": row["actor_id"], "capability": row["capability"]})
            self._reevaluate_future(connection, actor_id, reason="操作员资格被撤销")

    # ------------------------------------------------------------------ 科研优先级冻结

    def freeze_priorities(self, *, request_id: str, actor_id: str, freeze_id: str, label: str,
                          ranking: Iterable[str]) -> dict[str, Any]:
        ranking = [str(item).strip() for item in ranking]
        if not ranking or any(not item for item in ranking):
            raise ValidationError("ranking 必须是非空承诺键数组")
        if len(set(ranking)) != len(ranking):
            raise ValidationError("ranking 中不能出现重复承诺键")
        payload = {"actor_id": actor_id, "freeze_id": freeze_id, "label": label, "ranking": ranking}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "reviewer")

            def create() -> str:
                try:
                    connection.execute(
                        "INSERT INTO priority_freezes(freeze_id,label,ranking_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (freeze_id, str(label).strip(), canonical_json(ranking), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("优先级冻结编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="priority.frozen",
                            resource_type="priority_freeze", resource_id=freeze_id,
                            detail={"label": label, "ranking": ranking})
                return freeze_id

            resource_id, replayed = self._idempotent(
                connection, request_id=request_id, action="freeze_priorities",
                payload=payload, create=create)
            return {"freeze_id": resource_id, "replayed": replayed}

    # ------------------------------------------------------------------ 前置条件评估

    def _active_calibration(self, connection, machine_id: str, signature: str,
                            start: str, end: str):
        return connection.execute(
            "SELECT * FROM calibrations WHERE equipment_id=? AND status='valid' AND covered_signature=? "
            "AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC LIMIT 1",
            (machine_id, signature, start, end)).fetchone()

    def _evaluate_equipment(self, connection, machine_id: str, component_ids: list[str],
                            required: frozenset[str], start: str, end: str) -> tuple[bool, str | None, str | None]:
        """返回 (是否通过, 失败原因, 命中的证书编号)。"""
        machine = self.get_equipment_row(connection, machine_id)
        if machine["status"] != "available":
            return False, f"整机当前状态为 {machine['status']}，不具备出借条件", None
        available_caps = set(json.loads(machine["capabilities_json"]))
        for component_id in component_ids:
            component = self.get_equipment_row(connection, component_id)
            if component["parent_id"] != machine_id:
                return False, f"组件 {component_id} 不属于整机 {machine_id}", None
            if not component["attached"] or component["status"] != "available":
                return False, f"组件 {component_id} 当前不可用（status={component['status']}）", None
            available_caps.update(json.loads(component["capabilities_json"]))
        missing = required - available_caps
        if missing:
            return False, f"设备能力不满足，缺少：{', '.join(sorted(missing))}", None
        signature, _ = self._full_config_signature(connection, machine_id)
        calibration = self._active_calibration(connection, machine_id, signature, start, end)
        if calibration is None:
            current = connection.execute(
                "SELECT * FROM calibrations WHERE equipment_id=? AND status='valid' "
                "AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC LIMIT 1",
                (machine_id, start, end)).fetchone()
            if current is not None and current["covered_signature"] != signature:
                return False, "运输后的校准证书不再覆盖当前组件配置（配置签名不一致）", None
            return False, "预约时段内没有覆盖当前组件配置的有效校准证书", None
        return True, None, calibration["certificate_id"]

    def _evaluate_operator(self, connection, operator_actor_id: str, required: frozenset[str],
                           start: str, end: str) -> tuple[bool, str | None]:
        for capability in sorted(required):
            row = connection.execute(
                "SELECT 1 FROM qualifications WHERE actor_id=? AND capability=? AND status='valid' "
                "AND valid_from<=? AND valid_until>=? LIMIT 1",
                (operator_actor_id, capability, start, end)).fetchone()
            if row is None:
                return False, f"操作员缺少时段内有效的资格：{capability}"
        return True, None

    def _evaluate_packing(self, connection, machine_id: str, component_ids: list[str]) -> tuple[bool, str | None]:
        for component_id in component_ids:
            component = self.get_equipment_row(connection, component_id)
            if component["crate_status"] == "damaged":
                return False, f"组件 {component_id} 的运输箱已损坏"
            if component["crate_status"] != "sealed":
                return False, f"组件 {component_id} 的运输箱尚未封箱"
        machine = self.get_equipment_row(connection, machine_id)
        if machine["crate_status"] == "damaged":
            return False, "整机运输箱已损坏"
        return True, None

    # ------------------------------------------------------------------ 借调申请

    def submit_loan_request(self, *, request_id: str, actor_id: str, equipment_id: str,
                            required_capabilities: Iterable[str], requesting_site_id: str,
                            operator_actor_id: str, start_at: str, end_at: str,
                            commitment_key: str, freeze_id: str,
                            component_ids: Iterable[str] | None = None) -> dict[str, Any]:
        start = self._time_field(start_at, "start_at")
        end = self._time_field(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        required = frozenset(self._capabilities(required_capabilities, "required"))
        if not required:
            raise ValidationError("required_capabilities 必须是非空数组")
        components = sorted(set(component_ids or []))
        commitment_key = str(commitment_key).strip()
        if not commitment_key:
            raise ValidationError("commitment_key 不能为空")
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "components": components,
                   "required_capabilities": sorted(required), "requesting_site_id": requesting_site_id,
                   "operator_actor_id": operator_actor_id, "start_at": start, "end_at": end,
                   "commitment_key": commitment_key, "freeze_id": freeze_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            machine = self.get_equipment_row(connection, equipment_id)
            if machine["kind"] != "machine":
                raise ValidationError("借调对象必须是整机")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (requesting_site_id,)).fetchone() is None:
                raise NotFoundError("申请场所不存在")
            self._actor(connection, operator_actor_id)
            freeze = connection.execute("SELECT * FROM priority_freezes WHERE freeze_id=?", (freeze_id,)).fetchone()
            if freeze is None:
                raise NotFoundError("科研优先级冻结不存在")
            ranking = json.loads(freeze["ranking_json"])
            if commitment_key not in ranking:
                raise ValidationError("研究承诺键不在冻结的优先级列表中")
            priority_rank = ranking.index(commitment_key)
            for component_id in components:
                component = self.get_equipment_row(connection, component_id)
                if component["parent_id"] != equipment_id:
                    raise ValidationError(f"组件 {component_id} 不属于该整机")

            def create() -> str:
                phases = {phase: "pending" for phase in LOAN_PHASES}
                equip_ok, equip_reason, _ = self._evaluate_equipment(
                    connection, equipment_id, components, required, start, end)
                phases[PHASE_EQUIPMENT] = "passed" if equip_ok else "failed"
                op_ok, op_reason = self._evaluate_operator(connection, operator_actor_id, required, start, end)
                phases[PHASE_OPERATOR] = "passed" if op_ok else "failed"
                reason = equip_reason or op_reason
                try:
                    connection.execute(
                        "INSERT INTO loan_requests(request_id,equipment_id,components_json,required_capabilities_json,"
                        "requesting_site_id,origin_site_id,operator_actor_id,start_at,end_at,commitment_key,"
                        "freeze_id,priority_rank,status,phases_json,last_reason,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'requested',?,?,?,?,?)",
                        (request_id, equipment_id, canonical_json(components), canonical_json(sorted(required)),
                         requesting_site_id, machine["site_id"], operator_actor_id, start, end, commitment_key,
                         freeze_id, priority_rank, canonical_json(phases), reason, actor_id,
                         self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("相同请求已经存在，不得生成第二次借调") from exc
                self._audit(connection, actor_id=actor_id, action="loan.submitted",
                            resource_type="loan_request", resource_id=request_id,
                            detail={"equipment_id": equipment_id, "components": components,
                                    "requesting_site_id": requesting_site_id, "start_at": start,
                                    "end_at": end, "commitment_key": commitment_key,
                                    "priority_rank": priority_rank, "phases": phases, "reason": reason})
                return request_id

            resource_id, replayed = self._idempotent(
                connection, request_id=request_id, action="submit_loan", payload=payload, create=create)
            view = self._load_loan(connection, resource_id)
            return {"request_id": resource_id, "replayed": replayed, "status": view.status,
                    "priority_rank": view.priority_rank, "phases": view.phases, "last_reason": view.last_reason}

    def _load_loan(self, connection, request_id: str) -> LoanRequestView:
        row = connection.execute("SELECT * FROM loan_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise NotFoundError("借调申请不存在")
        return LoanRequestView(
            row["request_id"], row["equipment_id"], tuple(json.loads(row["components_json"])),
            frozenset(json.loads(row["required_capabilities_json"])), row["requesting_site_id"],
            row["origin_site_id"], row["operator_actor_id"], row["start_at"], row["end_at"],
            row["commitment_key"], row["freeze_id"], row["priority_rank"], row["status"],
            json.loads(row["phases_json"]), row["last_reason"], row["created_by"],
            row["created_at"], row["updated_at"])

    def get_loan(self, request_id: str) -> LoanRequestView:
        return self._load_loan(self.database.connection, request_id)

    def advance_packing_phase(self, *, actor_id: str, request_id: str, passed: bool,
                              reason: str | None = None) -> dict[str, Any]:
        """由现场确认包装与运输前置条件；通过后申请才参与占位。"""
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._load_loan(connection, request_id)
            if loan.status not in (STATUS_REQUESTED, STATUS_WAITLISTED):
                raise ConflictError(f"申请处于 {loan.status}，不能再修改包装阶段")
            phases = dict(loan.phases)
            if passed:
                pack_ok, pack_reason = self._evaluate_packing(
                    connection, loan.equipment_id, list(loan.components))
                if not pack_ok:
                    raise ConflictError(pack_reason)
                phases[PHASE_PACKING] = "passed"
            else:
                phases[PHASE_PACKING] = "failed"
                reason = reason or "包装与运输前置条件未满足"
            self._save_phases(connection, request_id, phases, reason)
            self._audit(connection, actor_id=actor_id,
                        action="loan.packing_passed" if passed else "loan.packing_failed",
                        resource_type="loan_request", resource_id=request_id,
                        detail={"phases": phases, "reason": reason})
            self._promote_waitlist(connection, actor_id, loan.equipment_id)
            return {"request_id": request_id, "status": self._load_loan(connection, request_id).status,
                    "phases": phases}

    def _save_phases(self, connection, request_id: str, phases: dict[str, str], reason: str | None) -> None:
        connection.execute("UPDATE loan_requests SET phases_json=?, last_reason=COALESCE(?, last_reason), "
                           "updated_at=? WHERE request_id=?",
                           (canonical_json(phases), reason, self._now(), request_id))

    # ------------------------------------------------------------------ 占位与候补

    def _overlaps(self, connection, unit_ids: Iterable[str], start: str, end: str,
                  exclude_request: str | None = None) -> list[dict[str, Any]]:
        blockers = []
        for unit_id in unit_ids:
            rows = connection.execute(
                "SELECT o.*, l.status AS loan_status, l.priority_rank AS loan_rank FROM slot_occupancy o "
                "JOIN loan_requests l ON l.request_id=o.loan_request_id WHERE o.equipment_id=? "
                "AND o.start_at < ? AND ? < o.end_at", (unit_id, end, start)).fetchall()
            for row in rows:
                if exclude_request and row["loan_request_id"] == exclude_request:
                    continue
                blockers.append({"unit_id": unit_id, "request_id": row["loan_request_id"],
                                 "status": row["loan_status"], "rank": row["loan_rank"],
                                 "start_at": row["start_at"], "end_at": row["end_at"]})
        return blockers

    def _priority_ahead(self, connection, machine_id: str, rank: int, created_at: str,
                        request_id: str, start: str, end: str) -> str | None:
        """返回排在当前申请之前、时段重叠且尚未终结的申请编号。

        排序依据是冻结的优先级 rank（相同 rank 按提交时间与编号），保证候补顺序
        稳定，不会因服务重启或反复评估而抖动。
        """
        row = connection.execute(
            "SELECT request_id FROM loan_requests WHERE equipment_id=? AND status IN (?, ?) "
            "AND request_id!=? AND start_at < ? AND ? < end_at "
            "AND json_extract(phases_json, '$.packing_transport') != 'failed' "
            "AND (priority_rank<? OR (priority_rank=? AND (created_at<? OR (created_at=? AND request_id<?)))) "
            "ORDER BY priority_rank, created_at, request_id LIMIT 1",
            (machine_id, STATUS_REQUESTED, STATUS_WAITLISTED, request_id, end, start,
             rank, rank, created_at, created_at, request_id)).fetchone()
        return row["request_id"] if row else None

    def _promote_waitlist(self, connection, actor_id: str, machine_id: str) -> None:
        """按冻结优先级与提交次序确定性递补；只有三阶段全部通过才真正占位。"""
        candidates = connection.execute(
            "SELECT * FROM loan_requests WHERE equipment_id=? AND status IN (?, ?) "
            "ORDER BY priority_rank, created_at, request_id",
            (machine_id, STATUS_REQUESTED, STATUS_WAITLISTED)).fetchall()
        for row in candidates:
            request_id = row["request_id"]
            phases = json.loads(row["phases_json"])
            components = json.loads(row["components_json"])
            required = frozenset(json.loads(row["required_capabilities_json"]))
            if phases.get(PHASE_PACKING) != "passed":
                continue
            equip_ok, equip_reason, _ = self._evaluate_equipment(
                connection, machine_id, components, required, row["start_at"], row["end_at"])
            phases[PHASE_EQUIPMENT] = "passed" if equip_ok else "failed"
            op_ok, op_reason = self._evaluate_operator(
                connection, row["operator_actor_id"], required, row["start_at"], row["end_at"])
            phases[PHASE_OPERATOR] = "passed" if op_ok else "failed"
            if not (equip_ok and op_ok):
                reason = equip_reason or op_reason
                connection.execute(
                    "UPDATE loan_requests SET status=?, phases_json=?, last_reason=?, updated_at=? WHERE request_id=?",
                    (STATUS_WAITLISTED, canonical_json(phases), reason, self._now(), request_id))
                continue
            blockers = self._overlaps(
                connection, [machine_id, *components], row["start_at"], row["end_at"],
                exclude_request=request_id)
            ahead = self._priority_ahead(
                connection, machine_id, row["priority_rank"], row["created_at"], request_id,
                row["start_at"], row["end_at"])
            if blockers or ahead:
                if blockers:
                    winner = blockers[0]
                    reason = f"时段已被申请 {winner['request_id']}（状态 {winner['status']}）占用"
                else:
                    reason = (f"更高优先级申请 {ahead} 已在候补队列中，按冻结优先级稳定候补")
                connection.execute(
                    "UPDATE loan_requests SET status=?, phases_json=?, last_reason=?, updated_at=? WHERE request_id=?",
                    (STATUS_WAITLISTED, canonical_json(phases), reason, self._now(), request_id))
                continue
            occupancy_id = uuid.uuid4().hex
            for unit_id in [machine_id, *components]:
                unit_type = "machine" if unit_id == machine_id else "component"
                connection.execute(
                    "INSERT INTO slot_occupancy(occupancy_id,loan_request_id,equipment_id,unit_type,"
                    "start_at,end_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, request_id, unit_id, unit_type, row["start_at"], row["end_at"]))
            connection.execute(
                "UPDATE loan_requests SET status=?, phases_json=?, last_reason=NULL, updated_at=? WHERE request_id=?",
                (STATUS_CONFIRMED, canonical_json(phases), self._now(), request_id))
            self._audit(connection, actor_id=actor_id, action="loan.confirmed",
                        resource_type="loan_request", resource_id=request_id,
                        detail={"occupancy_id": occupancy_id, "start_at": row["start_at"],
                                "end_at": row["end_at"], "components": components})

    def list_waitlist(self, machine_id: str) -> list[LoanRequestView]:
        rows = self.database.connection.execute(
            "SELECT * FROM loan_requests WHERE equipment_id=? AND status IN (?, ?) "
            "ORDER BY priority_rank, created_at, request_id",
            (machine_id, STATUS_REQUESTED, STATUS_WAITLISTED)).fetchall()
        return [self._load_loan(self.database.connection, row["request_id"]) for row in rows]

    # ------------------------------------------------------------------ 运输、验收、归还

    def _require_loan_status(self, loan: LoanRequestView, *statuses: str) -> None:
        if loan.status not in statuses:
            raise ConflictError(f"申请当前为 {loan.status}，不能执行该动作")

    def mark_in_transit(self, *, actor_id: str, request_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._load_loan(connection, request_id)
            self._require_loan_status(loan, STATUS_CONFIRMED)
            pack_ok, pack_reason = self._evaluate_packing(connection, loan.equipment_id, list(loan.components))
            if not pack_ok:
                raise ConflictError(pack_reason)
            connection.execute(
                "UPDATE loan_requests SET status=?, updated_at=? WHERE request_id=?",
                (STATUS_IN_TRANSIT, self._now(), request_id))
            for unit_id in [loan.equipment_id, *loan.components]:
                connection.execute("UPDATE equipment SET status='in_transit', updated_at=? WHERE equipment_id=?",
                                   (self._now(), unit_id))
            self._audit(connection, actor_id=actor_id, action="loan.in_transit",
                        resource_type="loan_request", resource_id=request_id,
                        detail={"equipment_id": loan.equipment_id, "components": list(loan.components)})

    def accept_delivery(self, *, actor_id: str, request_id: str, accepted: bool,
                        damage_note: str | None = None) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._load_loan(connection, request_id)
            self._require_loan_status(loan, STATUS_IN_TRANSIT, STATUS_PENDING_ACCEPTANCE)
            connection.execute(
                "UPDATE loan_requests SET status=?, updated_at=? WHERE request_id=?",
                (STATUS_PENDING_ACCEPTANCE, self._now(), request_id))
            signature, _ = self._full_config_signature(connection, loan.equipment_id)
            calibration = self._active_calibration(
                connection, loan.equipment_id, signature, loan.start_at, loan.end_at)
            damaged_units = [unit for unit in [loan.equipment_id, *loan.components]
                             if self.get_equipment_row(connection, unit)["crate_status"] == "damaged"]
            failed_units = [unit for unit in [loan.equipment_id, *loan.components]
                            if self.get_equipment_row(connection, unit)["status"] in ("failed", "missing")]
            missing_components = [unit for unit in loan.components
                                  if not self.get_equipment_row(connection, unit)["attached"]]
            if accepted and calibration is not None and not damaged_units and not failed_units \
                    and not missing_components:
                connection.execute(
                    "UPDATE loan_requests SET status=?, updated_at=? WHERE request_id=?",
                    (STATUS_ACTIVE, self._now(), request_id))
                for unit_id in [loan.equipment_id, *loan.components]:
                    connection.execute(
                        "UPDATE equipment SET status='available', site_id=?, updated_at=? WHERE equipment_id=?",
                        (loan.requesting_site_id, self._now(), unit_id))
                self._audit(connection, actor_id=actor_id, action="loan.accepted",
                            resource_type="loan_request", resource_id=request_id,
                            detail={"certificate_id": calibration["certificate_id"],
                                    "config_signature": signature, "site_id": loan.requesting_site_id})
                return {"request_id": request_id, "status": STATUS_ACTIVE,
                        "certificate_id": calibration["certificate_id"]}
            reasons = []
            if calibration is None:
                reasons.append("到货后校准证书不再覆盖当前组件配置")
            if damaged_units:
                reasons.append(f"运输箱损伤：{', '.join(damaged_units)}")
            if failed_units:
                reasons.append(f"设备或组件处于不可用状态：{', '.join(failed_units)}")
            if missing_components:
                reasons.append(f"验收时组件失联：{', '.join(missing_components)}")
            if damage_note:
                reasons.append(damage_note)
            if not reasons:
                reasons.append("到货验收未通过")
            detail = {"reasons": reasons, "config_signature": signature}
            connection.execute(
                "INSERT INTO incidents(incident_id,incident_type,equipment_id,loan_request_id,detail_json,"
                "created_by,occurred_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "transport_damage", loan.equipment_id, request_id,
                 canonical_json(detail), actor_id, self._now()))
            self._finish_occupancy(connection, request_id, STATUS_INTERRUPTED)
            for unit_id in set(damaged_units + failed_units + missing_components) or [loan.equipment_id]:
                unit = self.get_equipment_row(connection, unit_id)
                new_status = "missing" if unit_id in missing_components else "failed"
                connection.execute("UPDATE equipment SET status=?, updated_at=? WHERE equipment_id=?",
                                   (new_status, self._now(), unit_id))
            self._audit(connection, actor_id=actor_id, action="loan.acceptance_rejected",
                        resource_type="loan_request", resource_id=request_id, detail=detail)
            self._release_future_confirmed(connection, actor_id, loan.equipment_id,
                                           reason="运输损伤导致设备不可用")
            self._promote_waitlist(connection, actor_id, loan.equipment_id)
            return {"request_id": request_id, "status": STATUS_INTERRUPTED, "reasons": reasons}

    def _finish_occupancy(self, connection, request_id: str, status: str) -> None:
        connection.execute("DELETE FROM slot_occupancy WHERE loan_request_id=?", (request_id,))
        connection.execute("UPDATE loan_requests SET status=?, updated_at=? WHERE request_id=?",
                           (status, self._now(), request_id))

    def record_experiment_data(self, *, actor_id: str, request_id: str, data_id: str,
                               payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            loan = self._load_loan(connection, request_id)
            self._require_loan_status(loan, STATUS_ACTIVE)
            signature, _ = self._full_config_signature(connection, loan.equipment_id)
            calibration = self._active_calibration(
                connection, loan.equipment_id, signature, loan.start_at, loan.end_at)
            machine = self.get_equipment_row(connection, loan.equipment_id)
            capabilities = set(json.loads(machine["capabilities_json"]))
            for component_id in loan.components:
                capabilities.update(json.loads(
                    self.get_equipment_row(connection, component_id)["capabilities_json"]))
            try:
                connection.execute(
                    "INSERT INTO experiment_data(data_id,loan_request_id,payload_json,equipment_status,"
                    "calibration_certificate_id,calibration_version,config_signature,capabilities_json,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (data_id, request_id, canonical_json(payload), machine["status"],
                     calibration["certificate_id"] if calibration else None,
                     calibration["calibration_version"] if calibration else None,
                     signature, canonical_json(sorted(capabilities)), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("data_id 已经存在") from exc
            self._audit(connection, actor_id=actor_id, action="experiment_data.recorded",
                        resource_type="experiment_data", resource_id=data_id,
                        detail={"loan_request_id": request_id,
                                "calibration_certificate_id": calibration["certificate_id"] if calibration else None,
                                "config_signature": signature})
            return {"data_id": data_id, "recorded_at": self._now()}

    def list_experiment_data(self, request_id: str) -> list[ExperimentDataView]:
        rows = self.database.connection.execute(
            "SELECT * FROM experiment_data WHERE loan_request_id=? ORDER BY recorded_at, data_id",
            (request_id,)).fetchall()
        return [ExperimentDataView(
            row["data_id"], row["loan_request_id"], json.loads(row["payload_json"]),
            row["equipment_status"], row["calibration_certificate_id"], row["calibration_version"],
            row["config_signature"], frozenset(json.loads(row["capabilities_json"])),
            row["recorded_by"], row["recorded_at"]) for row in rows]

    def complete_return(self, *, actor_id: str, request_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            loan = self._load_loan(connection, request_id)
            self._require_loan_status(loan, STATUS_ACTIVE, STATUS_IN_TRANSIT, STATUS_PENDING_ACCEPTANCE)
            now = self._now()
            incidents = []
            if self._now_dt() > self._parse_time(loan.end_at, "end_at"):
                connection.execute(
                    "INSERT INTO incidents(incident_id,incident_type,equipment_id,loan_request_id,detail_json,"
                    "created_by,occurred_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "overdue_return", loan.equipment_id, request_id,
                     canonical_json({"expected_end_at": loan.end_at, "returned_at": now}),
                     actor_id, now))
                incidents.append("overdue_return")
            for component_id in loan.components:
                component = self.get_equipment_row(connection, component_id)
                if not component["attached"] or component["status"] in ("missing", "failed"):
                    already = connection.execute(
                        "SELECT 1 FROM incidents WHERE incident_type='component_missing' "
                        "AND equipment_id=? AND loan_request_id=? LIMIT 1",
                        (component_id, request_id)).fetchone()
                    if already:
                        continue
                    connection.execute(
                        "INSERT INTO incidents(incident_id,incident_type,equipment_id,loan_request_id,detail_json,"
                        "created_by,occurred_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, "component_missing", component_id, request_id,
                         canonical_json({"status": component["status"], "attached": bool(component["attached"])}),
                         actor_id, now))
                    incidents.append(f"component_missing:{component_id}")
            self._finish_occupancy(connection, request_id, STATUS_COMPLETED)
            for unit_id in [loan.equipment_id, *loan.components]:
                unit = self.get_equipment_row(connection, unit_id)
                if unit["status"] in ("in_transit", "available"):
                    connection.execute(
                        "UPDATE equipment SET status='available', crate_status='unsealed', "
                        "site_id=?, updated_at=? WHERE equipment_id=?",
                        (loan.origin_site_id, now, unit_id))
            self._audit(connection, actor_id=actor_id, action="loan.returned",
                        resource_type="loan_request", resource_id=request_id,
                        detail={"incidents": incidents, "returned_at": now})
            self._promote_waitlist(connection, actor_id, loan.equipment_id)
            return {"request_id": request_id, "status": STATUS_COMPLETED, "incidents": incidents}

    def cancel_request(self, *, actor_id: str, request_id: str, reason: str | None = None) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            loan = self._load_loan(connection, request_id)
            if loan.status not in (STATUS_REQUESTED, STATUS_WAITLISTED, STATUS_CONFIRMED):
                raise ConflictError(f"申请处于 {loan.status}，不能取消")
            self._finish_occupancy(connection, request_id, STATUS_CANCELLED)
            connection.execute("UPDATE loan_requests SET last_reason=? WHERE request_id=?",
                               (reason or "申请被取消", request_id))
            self._audit(connection, actor_id=actor_id, action="loan.cancelled",
                        resource_type="loan_request", resource_id=request_id,
                        detail={"reason": reason, "previous_status": loan.status})
            self._promote_waitlist(connection, actor_id, loan.equipment_id)

    # ------------------------------------------------------------------ 事故与调整

    def report_incident(self, *, actor_id: str, incident_type: str, equipment_id: str,
                        detail: dict[str, Any] | None = None, loan_request_id: str | None = None) -> dict[str, Any]:
        detail = detail or {}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator")
            row = self.get_equipment_row(connection, equipment_id)
            machine_id = row["parent_id"] or equipment_id
            incident_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO incidents(incident_id,incident_type,equipment_id,loan_request_id,detail_json,"
                "created_by,occurred_at) VALUES(?,?,?,?,?,?,?)",
                (incident_id, incident_type, equipment_id, loan_request_id,
                 canonical_json(detail), actor_id, self._now()))
            reason_map = {
                "equipment_failure": "设备故障，尚未完成的预约被释放",
                "capability_downgrade": "设备部分能力降级，不再满足预约要求",
                "transport_damage": "运输损伤，尚未完成的预约被释放",
                "calibration_revoked": "校准被撤销，尚未完成的预约被释放",
                "component_missing": "组件失联，相关预约被释放",
            }
            if incident_type == "equipment_failure":
                connection.execute("UPDATE equipment SET status='failed', updated_at=? WHERE equipment_id=?",
                                   (self._now(), equipment_id))
            elif incident_type == "component_missing" and row["kind"] == "component":
                connection.execute("UPDATE equipment SET status='missing', attached=0, updated_at=? WHERE equipment_id=?",
                                   (self._now(), equipment_id))
                self._bump_config(connection, machine_id, actor_id)
            elif incident_type == "capability_downgrade":
                new_caps = self._capabilities(detail.get("capabilities", []), "capabilities")
                connection.execute(
                    "UPDATE equipment SET capabilities_json=?, updated_at=? WHERE equipment_id=?",
                    (canonical_json(new_caps), self._now(), equipment_id))
            elif incident_type == "transport_damage":
                connection.execute("UPDATE equipment SET status='failed', crate_status='damaged', updated_at=? "
                                   "WHERE equipment_id=?", (self._now(), equipment_id))
            self._audit(connection, actor_id=actor_id, action=f"incident.{incident_type}",
                        resource_type="incident", resource_id=incident_id,
                        detail={"equipment_id": equipment_id, "loan_request_id": loan_request_id, "detail": detail})
            if incident_type in ("equipment_failure", "transport_damage", "component_missing"):
                self._release_future_confirmed(connection, actor_id, machine_id, reason=reason_map[incident_type])
                self._promote_waitlist(connection, actor_id, machine_id)
            elif incident_type in ("capability_downgrade", "calibration_revoked"):
                # 能力降级或校准撤销只影响确实不再满足条件的预约，其余保持占用。
                self._release_future_failing(connection, actor_id, machine_id)
            return {"incident_id": incident_id}

    def revoke_calibration(self, *, actor_id: str, certificate_id: str, reason: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "reviewer")
            row = connection.execute("SELECT * FROM calibrations WHERE certificate_id=?",
                                     (certificate_id,)).fetchone()
            if row is None:
                raise NotFoundError("校准证书不存在")
            connection.execute("UPDATE calibrations SET status='revoked', revoked_reason=? WHERE certificate_id=?",
                               (reason, certificate_id))
            connection.execute(
                "INSERT INTO incidents(incident_id,incident_type,equipment_id,loan_request_id,detail_json,"
                "created_by,occurred_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "calibration_revoked", row["equipment_id"], None,
                 canonical_json({"certificate_id": certificate_id, "reason": reason}),
                 actor_id, self._now()))
            self._audit(connection, actor_id=actor_id, action="calibration.revoked",
                        resource_type="calibration", resource_id=certificate_id,
                        detail={"equipment_id": row["equipment_id"], "reason": reason})
            self._release_future_failing(connection, actor_id, row["equipment_id"])

    def _release_future_confirmed(self, connection, actor_id: str, machine_id: str, *, reason: str) -> None:
        """事故只调整尚未开始的已确认预约；在途、待验收、进行中与已完成一律保留。"""
        now = self._now()
        rows = connection.execute(
            "SELECT * FROM loan_requests WHERE equipment_id=? AND status=? AND start_at>?",
            (machine_id, STATUS_CONFIRMED, now)).fetchall()
        for row in rows:
            phases = json.loads(row["phases_json"])
            phases[PHASE_EQUIPMENT] = "failed"
            connection.execute("DELETE FROM slot_occupancy WHERE loan_request_id=?", (row["request_id"],))
            connection.execute(
                "UPDATE loan_requests SET status=?, phases_json=?, last_reason=?, updated_at=? WHERE request_id=?",
                (STATUS_WAITLISTED, canonical_json(phases), reason, self._now(), row["request_id"]))
            self._audit(connection, actor_id=actor_id, action="loan.released_by_incident",
                        resource_type="loan_request", resource_id=row["request_id"], detail={"reason": reason})

    def _release_future_failing(self, connection, actor_id: str, machine_id: str) -> None:
        """能力降级/校准撤销后，仅释放重新评估不再通过的未来已确认预约。"""
        now = self._now()
        rows = connection.execute(
            "SELECT * FROM loan_requests WHERE equipment_id=? AND status=? AND start_at>?",
            (machine_id, STATUS_CONFIRMED, now)).fetchall()
        for row in rows:
            components = json.loads(row["components_json"])
            required = frozenset(json.loads(row["required_capabilities_json"]))
            equip_ok, equip_reason, _ = self._evaluate_equipment(
                connection, machine_id, components, required, row["start_at"], row["end_at"])
            if equip_ok:
                continue
            phases = json.loads(row["phases_json"])
            phases[PHASE_EQUIPMENT] = "failed"
            connection.execute("DELETE FROM slot_occupancy WHERE loan_request_id=?", (row["request_id"],))
            connection.execute(
                "UPDATE loan_requests SET status=?, phases_json=?, last_reason=?, updated_at=? WHERE request_id=?",
                (STATUS_WAITLISTED, canonical_json(phases), equip_reason, self._now(), row["request_id"]))
            self._audit(connection, actor_id=actor_id, action="loan.released_by_incident",
                        resource_type="loan_request", resource_id=row["request_id"],
                        detail={"reason": equip_reason})
        self._promote_waitlist(connection, actor_id, machine_id)

    def _reevaluate_future(self, connection, actor_id: str, *, reason: str) -> None:
        """资格变化等事件：重新评估所有未完成预约，未通过的退回候补。"""
        rows = connection.execute(
            "SELECT * FROM loan_requests WHERE status IN (?, ?, ?)",
            (STATUS_REQUESTED, STATUS_WAITLISTED, STATUS_CONFIRMED)).fetchall()
        machines = {row["equipment_id"] for row in rows}
        for machine_id in machines:
            self._promote_waitlist(connection, actor_id, machine_id)
        for row in rows:
            if row["status"] != STATUS_CONFIRMED:
                continue
            required = frozenset(json.loads(row["required_capabilities_json"]))
            ok, why = self._evaluate_operator(
                connection, row["operator_actor_id"], required, row["start_at"], row["end_at"])
            if not ok:
                phases = json.loads(row["phases_json"])
                phases[PHASE_OPERATOR] = "failed"
                connection.execute("DELETE FROM slot_occupancy WHERE loan_request_id=?", (row["request_id"],))
                connection.execute(
                    "UPDATE loan_requests SET status=?, phases_json=?, last_reason=?, updated_at=? WHERE request_id=?",
                    (STATUS_WAITLISTED, canonical_json(phases), f"{reason}：{why}", self._now(), row["request_id"]))
                self._audit(connection, actor_id=actor_id, action="loan.released_by_incident",
                            resource_type="loan_request", resource_id=row["request_id"],
                            detail={"reason": reason, "detail": why})

    # ------------------------------------------------------------------ 解释与追责

    def explain_slot(self, equipment_id: str, start_at: str, end_at: str,
                     request_id: str | None = None) -> dict[str, Any]:
        start = self._time_field(start_at, "start_at")
        end = self._time_field(end_at, "end_at")
        connection = self.database.connection
        self.get_equipment_row(connection, equipment_id)
        occupants: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT DISTINCT l.* FROM loan_requests l JOIN slot_occupancy o ON o.loan_request_id=l.request_id "
            "WHERE l.equipment_id=? AND o.start_at < ? AND ? < o.end_at ORDER BY l.priority_rank, l.start_at",
            (equipment_id, end, start)).fetchall()
        for row in rows:
            note = "已确认占用"
            if row["status"] == STATUS_IN_TRANSIT:
                note = "设备运输中，服务重启后流程保留"
            elif row["status"] == STATUS_PENDING_ACCEPTANCE:
                note = "设备已到货等待验收，服务重启后流程保留"
            elif row["status"] == STATUS_ACTIVE:
                note = "实验正在进行，事故不得调整已产生的占用"
            occupants.append(SlotDecision(row["request_id"], row["equipment_id"], row["status"],
                                          row["start_at"], row["end_at"], row["priority_rank"],
                                          json.loads(row["phases_json"]), note).__dict__)
        waitlisted: list[dict[str, Any]] = []
        waiting = connection.execute(
            "SELECT * FROM loan_requests WHERE equipment_id=? AND status IN (?, ?) "
            "AND start_at < ? AND ? < end_at ORDER BY priority_rank, created_at, request_id",
            (equipment_id, STATUS_REQUESTED, STATUS_WAITLISTED, end, start)).fetchall()
        target = None
        for row in waiting:
            phases = json.loads(row["phases_json"])
            pending = [name for name, value in phases.items() if value != "passed"]
            if pending:
                why = f"前置阶段未全部通过：{', '.join(pending)}" + (
                    f"（{row['last_reason']}）" if row["last_reason"] else "")
            else:
                why = row["last_reason"] or "候补等待时段释放"
            item = SlotDecision(row["request_id"], row["equipment_id"], row["status"], row["start_at"],
                                row["end_at"], row["priority_rank"], phases, why).__dict__
            waitlisted.append(item)
            if request_id and row["request_id"] == request_id:
                target = item
        result = {"equipment_id": equipment_id, "start_at": start, "end_at": end,
                  "occupants": occupants, "waitlisted": waitlisted}
        if request_id and target is None:
            loan = connection.execute("SELECT * FROM loan_requests WHERE request_id=?", (request_id,)).fetchone()
            if loan is None:
                raise NotFoundError("借调申请不存在")
            blockers = self._overlaps(connection, [equipment_id, *json.loads(loan["components_json"])],
                                      loan["start_at"], loan["end_at"], exclude_request=request_id)
            if loan["status"] in ("confirmed", "in_transit", "pending_acceptance", "active", "completed"):
                reason = f"该申请处于 {loan['status']}，已占用此时段"
            else:
                reason = loan["last_reason"] or "未占用时段"
            target = {"request_id": request_id, "status": loan["status"],
                     "reason": reason,
                     "blocked_by": [{"request_id": b["request_id"], "status": b["status"]} for b in blockers]}
            result["target"] = target
        elif request_id:
            target["blocked_by"] = [{"request_id": item["request_id"], "status": item["status"]}
                                    for item in occupants]
            result["target"] = target
        return result

    def list_incidents(self, incident_type: str | None = None) -> list[IncidentView]:
        query = "SELECT * FROM incidents"
        parameters: list[Any] = []
        if incident_type:
            query += " WHERE incident_type=?"
            parameters.append(incident_type)
        query += " ORDER BY occurred_at, incident_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [IncidentView(row["incident_id"], row["incident_type"], row["equipment_id"],
                             row["loan_request_id"], json.loads(row["detail_json"]), row["created_by"],
                             row["occurred_at"]) for row in rows]

    def accountability_report(self) -> dict[str, Any]:
        """汇总逾期归还与组件失联，供平台主管追责。"""
        connection = self.database.connection
        overdue = []
        missing = []
        for row in connection.execute(
                "SELECT i.*, l.requesting_site_id, l.operator_actor_id, l.commitment_key, l.end_at "
                "FROM incidents i LEFT JOIN loan_requests l ON l.request_id=i.loan_request_id "
                "WHERE i.incident_type IN ('overdue_return','component_missing') ORDER BY i.occurred_at"):
            entry = {"incident_id": row["incident_id"], "incident_type": row["incident_type"],
                     "equipment_id": row["equipment_id"], "loan_request_id": row["loan_request_id"],
                     "requesting_site_id": row["requesting_site_id"], "operator_actor_id": row["operator_actor_id"],
                     "commitment_key": row["commitment_key"], "detail": json.loads(row["detail_json"]),
                     "occurred_at": row["occurred_at"]}
            (overdue if row["incident_type"] == "overdue_return" else missing).append(entry)
        return {"overdue_returns": overdue, "missing_components": missing}

"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .loan_service import LoanService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        loan_result = _run_loan_flow(database)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "loan": loan_result}
        database.close()
        return result


def _run_loan_flow(database: Database) -> dict[str, object]:
    """走通一次跨站借调：登记设备、校准、资格、冻结优先级、分阶段确认到归还。"""

    clock = FixedClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
    loan = LoanService(database, clock)
    service = DomainService(database, clock)
    service.register_actor(request_id="req-op2", actor_id="admin-001", new_actor_id="operator-002",
                           display_name="二站操作员", role="operator", organization_id="org-001")
    service.register_site(request_id="req-site2", actor_id="admin-001", site_id="site-002",
                          organization_id="org-001", name="二号科考站点", timezone_name="Asia/Shanghai")
    loan.register_equipment(request_id="req-machine", actor_id="admin-001", equipment_id="ms-1",
                            kind="machine", name="高精度质谱仪", site_id="site-001",
                            capabilities=["mass_basic"])
    loan.register_equipment(request_id="req-component", actor_id="admin-001", equipment_id="ion-1",
                            kind="component", parent_id="ms-1", name="离子源", capabilities=["hr_mode"])
    loan.register_qualification(request_id="req-qual1", actor_id="admin-001", qualification_id="qf-1",
                                target_actor_id="operator-001", capability="mass_basic",
                                valid_from="2026-01-01T00:00Z", valid_until="2027-01-01T00:00Z")
    loan.register_qualification(request_id="req-qual2", actor_id="admin-001", qualification_id="qf-2",
                                target_actor_id="operator-001", capability="hr_mode",
                                valid_from="2026-01-01T00:00Z", valid_until="2027-01-01T00:00Z")
    loan.freeze_priorities(request_id="req-freeze", actor_id="admin-001", freeze_id="fz-1",
                           label="Q4科研优先级", ranking=["ocean_samples"])
    loan.issue_calibration(request_id="req-cal", actor_id="admin-001", certificate_id="cal-1",
                           equipment_id="ms-1", calibration_version="v3",
                           valid_from="2026-09-01T00:00Z", valid_until="2026-12-31T00:00Z")
    loan.set_crate_status(actor_id="admin-001", equipment_id="ion-1", crate_status="sealed")
    submitted = loan.submit_loan_request(
        request_id="req-loan", actor_id="operator-001", equipment_id="ms-1",
        required_capabilities=["mass_basic", "hr_mode"], requesting_site_id="site-002",
        operator_actor_id="operator-001", start_at="2026-10-10T00:00Z", end_at="2026-10-12T00:00Z",
        commitment_key="ocean_samples", freeze_id="fz-1", component_ids=["ion-1"])
    loan.advance_packing_phase(actor_id="admin-001", request_id="req-loan", passed=True)
    confirmed = loan.get_loan("req-loan").status
    loan.mark_in_transit(actor_id="admin-001", request_id="req-loan")
    accepted = loan.accept_delivery(actor_id="admin-001", request_id="req-loan", accepted=True)
    loan.record_experiment_data(actor_id="operator-001", request_id="req-loan", data_id="data-1",
                                payload={"sample": "ocean-7"})
    returned = loan.complete_return(actor_id="admin-001", request_id="req-loan")
    return {"submitted_status": submitted["status"], "confirmed_status": confirmed,
            "accepted_status": accepted["status"], "returned_status": returned["status"],
            "data_records": len(loan.list_experiment_data("req-loan"))}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

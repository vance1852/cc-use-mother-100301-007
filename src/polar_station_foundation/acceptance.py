"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .loan import LoanService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
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

        # --- 跨站借调平台：第二座站申请唯一质谱仪 ---
        loans = LoanService(database, clock)
        service.register_organization(request_id="req-org-b", actor_id="admin-001",
                                      organization_id="org-002", name="海洋样品机构")
        service.register_actor(request_id="req-op-b", actor_id="admin-001", new_actor_id="operator-002",
                               display_name="借入站操作员", role="operator", organization_id="org-002")
        service.register_site(request_id="req-site-b", actor_id="operator-002", site_id="site-002",
                              organization_id="org-002", name="海洋样品站", timezone_name="UTC")
        loans.register_resource(request_id="req-res", actor_id="admin-001", resource_id="mass-spec",
                                resource_type="unit", name="高精度质谱仪", site_id="site-001",
                                capabilities=["mass-analysis"])
        loans.register_calibration(request_id="req-cal", actor_id="admin-001", calibration_id="cal-001",
                                   resource_id="mass-spec", certificate_ref="CERT-MS-001", version="v1",
                                   covers_capabilities=["mass-analysis"], covers_components=[],
                                   valid_from="2026-09-01T00:00:00Z",
                                   valid_until="2026-12-31T00:00:00Z")
        loans.register_crate(request_id="req-crate", actor_id="admin-001", crate_id="crate-001",
                             name="整机运输箱", site_id="site-001", fits_resource_id="mass-spec")
        loans.register_qualification(request_id="req-qual", actor_id="admin-001",
                                     qualified_actor_id="operator-002", capability="mass-analysis",
                                     valid_until="2027-01-01T00:00:00Z")
        loans.register_commitment(request_id="req-com", actor_id="admin-001", commitment_id="com-001",
                                  site_id="site-002", title="海洋样品实验", priority=10)
        request = loans.submit_loan_request(
            request_id="req-loan", actor_id="operator-002", site_id="site-002",
            commitment_id="com-001", resource_id="mass-spec",
            required_capabilities=["mass-analysis"],
            window_start="2026-11-10T00:00:00Z", window_end="2026-11-12T00:00:00Z",
            planned_return_at="2026-11-11T00:00:00Z")
        loans.confirm_phase(actor_id="operator-001", loan_id=request["loan_id"], phase="packaging",
                            evidence={"crate_id": "crate-001"})
        confirmed = loans.confirm_phase(
            actor_id="operator-001", loan_id=request["loan_id"], phase="transport",
            evidence={"handler": "极地物流", "scheduled_dispatch_at": "2026-11-09T00:00:00Z"})
        loans.pack_for_shipment(actor_id="operator-001", loan_id=request["loan_id"])
        loans.ship(actor_id="operator-001", loan_id=request["loan_id"])
        loans.arrive(actor_id="operator-002", loan_id=request["loan_id"])
        loans.accept_delivery(actor_id="operator-002", loan_id=request["loan_id"])
        run = loans.start_experiment_run(actor_id="operator-002", loan_id=request["loan_id"])

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "loan_status": confirmed["status"],
                  "run_calibration": run["state_snapshot"]["calibration"]["calibration_id"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

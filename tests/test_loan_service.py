import threading
import unittest
from datetime import datetime, timezone

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import ConflictError, PermissionDenied, ValidationError
from polar_station_foundation.loan_service import LoanService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class LoanCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 7, 8, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.loan = LoanService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="极地中心")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                                    display_name="主管", role="admin", organization_id="o1")
        for actor_id, name in [("op1", "甲站员"), ("op2", "乙站员"), ("au1", "审计员")]:
            role = "auditor" if actor_id == "au1" else "operator"
            self.service.register_actor(request_id="r-" + actor_id, actor_id="adm",
                                        new_actor_id=actor_id, display_name=name, role=role,
                                        organization_id="o1")
        self.service.register_site(request_id="s1", actor_id="adm", site_id="s1",
                                   organization_id="o1", name="甲站", timezone_name="UTC")
        self.service.register_site(request_id="s2", actor_id="adm", site_id="s2",
                                   organization_id="o1", name="乙站", timezone_name="UTC")
        self.loan.register_equipment(request_id="em", actor_id="adm", equipment_id="ms1",
                                     kind="machine", name="质谱仪", site_id="s1",
                                     capabilities=["mass_basic"])
        self.loan.register_equipment(request_id="ec", actor_id="adm", equipment_id="ion1",
                                     kind="component", parent_id="ms1", name="离子源",
                                     capabilities=["hr_mode"])
        self.loan.register_qualification(request_id="q1", actor_id="adm", qualification_id="qf1",
                                         target_actor_id="op1", capability="mass_basic",
                                         valid_from="2026-01-01T00:00Z",
                                         valid_until="2027-01-01T00:00Z")
        self.loan.register_qualification(request_id="q2", actor_id="adm", qualification_id="qf2",
                                         target_actor_id="op1", capability="hr_mode",
                                         valid_from="2026-01-01T00:00Z",
                                         valid_until="2027-01-01T00:00Z")
        self.loan.freeze_priorities(request_id="fz", actor_id="adm", freeze_id="fz1", label="P",
                                    ranking=["ocean", "repair"])
        self.loan.issue_calibration(request_id="cal", actor_id="adm", certificate_id="cal1",
                                    equipment_id="ms1", calibration_version="v3",
                                    valid_from="2026-09-01T00:00Z",
                                    valid_until="2026-12-31T00:00Z")

    def tearDown(self):
        self.database.close()

    def _submit(self, request_id="LA", *, actor="op1", operator="op1", caps=("mass_basic", "hr_mode"),
                commitment="ocean", start="2026-10-10T00:00Z", end="2026-10-12T00:00Z",
                components=("ion1",), site="s2"):
        return self.loan.submit_loan_request(
            request_id=request_id, actor_id=actor, equipment_id="ms1",
            required_capabilities=list(caps), requesting_site_id=site, operator_actor_id=operator,
            start_at=start, end_at=end, commitment_key=commitment, freeze_id="fz1",
            component_ids=list(components))

    def test_three_phases_must_all_pass_before_occupancy(self):
        result = self._submit()
        self.assertEqual("requested", result["status"])
        self.assertEqual("pending", result["phases"]["packing_transport"])
        # 未封箱不能通过包装阶段
        with self.assertRaises(ConflictError):
            self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self.assertEqual("confirmed", self.loan.get_loan("LA").status)

    def test_missing_capability_blocks_equipment_phase(self):
        result = self._submit("LX", caps=("mass_basic", "unknown_cap"))
        self.assertEqual("failed", result["phases"]["equipment_capability"])
        self.assertIn("缺少", result["last_reason"])

    def test_operator_without_qualification_waitlists(self):
        result = self._submit("LB", operator="op2", caps=("mass_basic",), commitment="repair",
                              start="2026-10-10T01:00Z", end="2026-10-11T00:00Z")
        self.assertEqual("failed", result["phases"]["operator_qualification"])

    def test_duplicate_request_never_creates_second_loan(self):
        first = self._submit()
        replay = self._submit()
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        rows = self.database.connection.execute("SELECT COUNT(*) AS c FROM loan_requests").fetchone()["c"]
        self.assertEqual(1, rows)

    def test_concurrent_acceptance_picks_single_winner(self):
        self._submit("LA", start="2026-10-10T00:00Z", end="2026-10-12T00:00Z")
        self._submit("LB", operator="op2", caps=("mass_basic",), commitment="repair",
                     start="2026-10-10T06:00Z", end="2026-10-11T00:00Z")
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        errors = []

        def confirm(rid):
            try:
                self.loan.advance_packing_phase(actor_id="adm", request_id=rid, passed=True)
            except Exception as exc:  # pragma: no cover - 串行化下不应抛出
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=(rid,)) for rid in ("LA", "LB")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        statuses = {rid: self.loan.get_loan(rid).status for rid in ("LA", "LB")}
        self.assertEqual("confirmed", statuses["LA"])
        self.assertEqual("waitlisted", statuses["LB"])
        occupancy = self.database.connection.execute(
            "SELECT COUNT(DISTINCT loan_request_id) AS c FROM slot_occupancy").fetchone()["c"]
        self.assertEqual(1, occupancy)

    def test_waitlist_promotes_after_cancel(self):
        # 高优先级先占
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit("LA", start="2026-10-10T00:00Z", end="2026-10-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        # 低优先级候补（给 op2 资格）
        self.loan.register_qualification(request_id="q3", actor_id="adm", qualification_id="qf3",
                                         target_actor_id="op2", capability="mass_basic",
                                         valid_from="2026-01-01T00:00Z",
                                         valid_until="2027-01-01T00:00Z")
        self._submit("LB", operator="op2", caps=("mass_basic",), commitment="repair",
                     start="2026-10-10T06:00Z", end="2026-10-11T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LB", passed=True)
        self.assertEqual("waitlisted", self.loan.get_loan("LB").status)
        self.loan.cancel_request(actor_id="adm", request_id="LA", reason="计划变更")
        self.assertEqual("confirmed", self.loan.get_loan("LB").status)

    def test_incident_only_adjusts_unconfirmed_future_loans(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit("LA", start="2026-10-10T00:00Z", end="2026-10-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self._submit("LF", start="2026-11-10T00:00Z", end="2026-11-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LF", passed=True)
        self.loan.mark_in_transit(actor_id="adm", request_id="LA")
        self.loan.report_incident(actor_id="adm", incident_type="equipment_failure",
                                  equipment_id="ms1")
        # 在途流程不动；未来已确认预约被释放回候补
        self.assertEqual("in_transit", self.loan.get_loan("LA").status)
        self.assertEqual("waitlisted", self.loan.get_loan("LF").status)

    def test_experiment_data_keeps_state_snapshot_after_revocation(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit()
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self.loan.mark_in_transit(actor_id="adm", request_id="LA")
        self.loan.accept_delivery(actor_id="adm", request_id="LA", accepted=True)
        self.loan.record_experiment_data(actor_id="op1", request_id="LA", data_id="d1",
                                         payload={"sample": "ocean-7"})
        self.loan.revoke_calibration(actor_id="adm", certificate_id="cal1", reason="批次异常")
        data = self.loan.list_experiment_data("LA")[0]
        self.assertEqual("cal1", data.calibration_certificate_id)
        self.assertEqual("v3", data.calibration_version)
        self.assertIsNotNone(data.config_signature)

    def test_in_transit_state_survives_restart(self):
        import tempfile
        from pathlib import Path
        path = Path(tempfile.mkdtemp()) / "restart.sqlite3"
        database = Database(path)
        base = DomainService(database, self.clock)
        loan = LoanService(database, self.clock)
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="中心")
        base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                            display_name="主管", role="admin", organization_id="o1")
        base.register_actor(request_id="op1", actor_id="adm", new_actor_id="op1",
                            display_name="甲站员", role="operator", organization_id="o1")
        base.register_site(request_id="s1", actor_id="adm", site_id="s1",
                           organization_id="o1", name="甲站", timezone_name="UTC")
        base.register_site(request_id="s2", actor_id="adm", site_id="s2",
                           organization_id="o1", name="乙站", timezone_name="UTC")
        loan.register_equipment(request_id="em", actor_id="adm", equipment_id="ms2",
                                kind="machine", name="备用仪", site_id="s1",
                                capabilities=["mass_basic"])
        loan.register_qualification(request_id="q9", actor_id="adm", qualification_id="qf9",
                                    target_actor_id="op1", capability="mass_basic",
                                    valid_from="2026-01-01T00:00Z",
                                    valid_until="2027-01-01T00:00Z")
        loan.freeze_priorities(request_id="fz9", actor_id="adm", freeze_id="fz9", label="P",
                               ranking=["k"])
        loan.issue_calibration(request_id="c9", actor_id="adm", certificate_id="cal9",
                               equipment_id="ms2", calibration_version="v1",
                               valid_from="2026-09-01T00:00Z", valid_until="2026-12-31T00:00Z")
        loan.submit_loan_request(request_id="LR", actor_id="op1", equipment_id="ms2",
                                 required_capabilities=["mass_basic"], requesting_site_id="s2",
                                 operator_actor_id="op1", start_at="2026-10-10T00:00Z",
                                 end_at="2026-10-12T00:00Z", commitment_key="k",
                                 freeze_id="fz9", component_ids=[])
        loan.advance_packing_phase(actor_id="adm", request_id="LR", passed=True)
        loan.mark_in_transit(actor_id="adm", request_id="LR")
        database.close()
        reopened = Database(path)
        revived = LoanService(reopened, self.clock)
        self.assertEqual("in_transit", revived.get_loan("LR").status)
        self.assertEqual("in_transit", revived.get_equipment("ms2").status)
        reopened.close()

    def test_acceptance_rejects_when_config_no_longer_covered(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit()
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self.loan.mark_in_transit(actor_id="adm", request_id="LA")
        # 运输途中新增组件，整机配置版本改变
        self.loan.register_equipment(request_id="en", actor_id="adm", equipment_id="det2",
                                     kind="component", parent_id="ms1", name="新检测器",
                                     capabilities=["x"])
        result = self.loan.accept_delivery(actor_id="adm", request_id="LA", accepted=True)
        self.assertEqual("interrupted", result["status"])
        self.assertTrue(any("校准" in reason for reason in result["reasons"]))

    def test_auditor_cannot_register_equipment(self):
        with self.assertRaises(PermissionDenied):
            self.loan.register_equipment(request_id="x", actor_id="au1", equipment_id="z",
                                         kind="machine", name="禁")

    def test_explains_occupancy_and_waitlist_reason(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit()
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        explanation = self.loan.explain_slot(
            "ms1", "2026-10-10T00:00Z", "2026-10-12T00:00Z")
        self.assertEqual("LA", explanation["occupants"][0]["request_id"])

    def test_overdue_return_and_missing_component_are_accountable(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit()
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self.loan.mark_in_transit(actor_id="adm", request_id="LA")
        self.loan.accept_delivery(actor_id="adm", request_id="LA", accepted=True)
        self.clock._value = datetime(2026, 10, 13, tzinfo=timezone.utc)
        self.loan.report_incident(actor_id="adm", incident_type="component_missing",
                                  equipment_id="ion1", loan_request_id="LA")
        self.loan.complete_return(actor_id="adm", request_id="LA")
        report = self.loan.accountability_report()
        self.assertEqual(1, len(report["overdue_returns"]))
        self.assertEqual(1, len(report["missing_components"]))
        entry = report["missing_components"][0]
        self.assertEqual("op1", entry["operator_actor_id"])
        self.assertEqual("ocean", entry["commitment_key"])

    def test_commitment_must_belong_to_frozen_priority(self):
        with self.assertRaises(ValidationError):
            self._submit("LX", commitment="not_in_freeze")

    def test_capability_downgrade_only_releases_affected_loan(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        # 需要 hr_mode 的预约与只需 mass_basic 的预约
        self._submit("LA", caps=("mass_basic", "hr_mode"),
                     start="2026-11-10T00:00Z", end="2026-11-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        self._submit("LB", caps=("mass_basic",), commitment="repair",
                     start="2026-12-10T00:00Z", end="2026-12-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LB", passed=True)
        self.assertEqual("confirmed", self.loan.get_loan("LA").status)
        self.assertEqual("confirmed", self.loan.get_loan("LB").status)
        # 离子源丢失 hr_mode：LA 落回候补，LB 不受影响
        self.loan.report_incident(actor_id="adm", incident_type="capability_downgrade",
                                  equipment_id="ion1", detail={"capabilities": []})
        self.assertEqual("waitlisted", self.loan.get_loan("LA").status)
        self.assertEqual("confirmed", self.loan.get_loan("LB").status)

    def test_calibration_revocation_keeps_loan_with_other_valid_certificate(self):
        self.loan.set_crate_status(actor_id="adm", equipment_id="ion1", crate_status="sealed")
        self._submit("LA", start="2026-11-10T00:00Z", end="2026-11-12T00:00Z")
        self.loan.advance_packing_phase(actor_id="adm", request_id="LA", passed=True)
        # 再签发一张覆盖同一配置、更晚到期的证书
        self.loan.issue_calibration(request_id="cal2", actor_id="adm", certificate_id="cal2",
                                    equipment_id="ms1", calibration_version="v4",
                                    valid_from="2026-10-01T00:00Z", valid_until="2027-12-31T00:00Z")
        self.loan.revoke_calibration(actor_id="adm", certificate_id="cal1", reason="批次异常")
        # 仍被 cal2 覆盖，保持确认
        self.assertEqual("confirmed", self.loan.get_loan("LA").status)


if __name__ == "__main__":
    unittest.main()

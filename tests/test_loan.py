import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import ConflictError, ValidationError
from polar_station_foundation.loan import LoanService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


def ts(day, hour=0):
    return f"2026-11-{day:02d}T{hour:02d}:00:00Z"


class LoanWorld:
    """两座科考站、一台质谱仪整机加两个可拆组件的标准夹具。"""

    def __init__(self, database=None, clock=None, seed=True):
        self.database = database or Database()
        self.clock = clock or FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.loan = LoanService(self.database, self.clock)
        b = self.base
        existing = self.database.connection.execute("SELECT COUNT(*) AS c FROM actors").fetchone()["c"]
        if existing:
            return
        b.register_organization(request_id="org-a", actor_id="bootstrap",
                                organization_id="org-owner", name="设备所有机构")
        b.register_organization(request_id="org-b", actor_id="bootstrap",
                                organization_id="org-borrow", name="海洋样品机构")
        b.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                         display_name="平台主管", role="admin", organization_id="org-owner")
        b.register_actor(request_id="op-own", actor_id="admin-1", new_actor_id="op-own",
                         display_name="所有站操作员", role="operator", organization_id="org-owner")
        b.register_actor(request_id="op-bor", actor_id="admin-1", new_actor_id="op-bor",
                         display_name="借入站操作员", role="operator", organization_id="org-borrow")
        b.register_site(request_id="site-own", actor_id="op-own", site_id="site-own",
                        organization_id="org-owner", name="质谱仪所在站", timezone_name="UTC")
        b.register_site(request_id="site-bor", actor_id="op-bor", site_id="site-bor",
                        organization_id="org-borrow", name="海洋样品站", timezone_name="UTC")
        L = self.loan
        L.register_resource(request_id="res-unit", actor_id="admin-1", resource_id="mass-spec",
                            resource_type="unit", name="高精度质谱仪", site_id="site-own",
                            capabilities=["mass-analysis", "isotope-ratio"])
        L.register_resource(request_id="res-ion", actor_id="admin-1", resource_id="ion-source",
                            resource_type="component", name="离子源", site_id="site-own",
                            capabilities=["ionization"], parent_id="mass-spec")
        L.register_resource(request_id="res-det", actor_id="admin-1", resource_id="detector",
                            resource_type="component", name="检测器", site_id="site-own",
                            capabilities=["detection"], parent_id="mass-spec")
        L.register_calibration(request_id="cal-1", actor_id="admin-1", calibration_id="cal-2026",
                               resource_id="mass-spec", certificate_ref="CERT-MS-2026-09",
                               version="v3",
                               covers_capabilities=["mass-analysis", "isotope-ratio",
                                                    "ionization", "detection"],
                               covers_components=[{"component_id": "ion-source", "revision": 1},
                                                  {"component_id": "detector", "revision": 1}],
                               valid_from="2026-09-01T00:00:00Z", valid_until="2026-12-31T00:00:00Z")
        L.register_crate(request_id="crate-a", actor_id="admin-1", crate_id="crate-a",
                         name="整机运输箱甲", site_id="site-own", fits_resource_id="mass-spec")
        L.register_crate(request_id="crate-b", actor_id="admin-1", crate_id="crate-b",
                         name="整机运输箱乙", site_id="site-own", fits_resource_id="mass-spec")
        L.register_qualification(request_id="qual", actor_id="admin-1",
                                 qualified_actor_id="op-bor", capability="mass-analysis",
                                 valid_until="2027-01-01T00:00:00Z")
        L.register_commitment(request_id="com-1", actor_id="admin-1", commitment_id="com-ocean",
                              site_id="site-bor", title="海洋样品同位素实验", priority=10)
        L.register_commitment(request_id="com-2", actor_id="admin-1", commitment_id="com-ice",
                              site_id="site-bor", title="冰芯备份实验", priority=20)


class LoanServiceTest(unittest.TestCase):
    def setUp(self):
        self.world = LoanWorld()
        self.loan = self.world.loan

    def tearDown(self):
        self.world.database.close()

    def _ready(self, loan_id, crate="crate-a"):
        self.loan.confirm_phase(actor_id="op-own", loan_id=loan_id, phase="packaging",
                                evidence={"crate_id": crate})
        return self.loan.confirm_phase(actor_id="op-own", loan_id=loan_id, phase="transport",
                                       evidence={"handler": "极地物流",
                                                 "scheduled_dispatch_at": ts(9)})

    def _request(self, request_id, commitment="com-ocean", caps=("mass-analysis",),
                 start=10, end=12, ret=11, ret_hour=0):
        return self.loan.submit_loan_request(
            request_id=request_id, actor_id="op-bor", site_id="site-bor",
            commitment_id=commitment, resource_id="mass-spec",
            required_capabilities=list(caps),
            window_start=ts(start), window_end=ts(end), planned_return_at=ts(ret, ret_hour))

    def test_stays_waitlisted_until_all_four_gates_pass(self):
        result = self._request("req-1")
        self.assertEqual("waitlisted", result["status"])
        explanation = self.loan.explain_loan(result["loan_id"])
        phases = {check["phase"]: check for check in explanation["phase_checks"]}
        self.assertTrue(phases["capability"]["passed"])
        self.assertTrue(phases["qualification"]["passed"])
        self.assertFalse(phases["packaging"]["passed"])
        self.assertFalse(phases["transport"]["passed"])
        self.assertIn("运输箱", phases["packaging"]["reason"])

        ready = self._ready(result["loan_id"])
        self.assertEqual("confirmed", ready["status"])
        slot = self.loan.explain_slot(resource_id="mass-spec", start=ts(10), end=ts(12))
        self.assertEqual(1, len(slot["occupied_by"]))
        self.assertEqual(result["loan_id"], slot["occupied_by"][0]["loan_id"])

    def test_missing_qualification_keeps_request_waitlisted(self):
        result = self._request("req-1", caps=("mass-analysis", "isotope-ratio"))
        explanation = self.loan.explain_loan(result["loan_id"])
        qual = [c for c in explanation["phase_checks"] if c["phase"] == "qualification"][-1]
        self.assertFalse(qual["passed"])
        self.assertIn("isotope-ratio", qual["reason"])

    def test_concurrent_requests_only_one_winner_by_frozen_priority(self):
        high = self._request("req-high", commitment="com-ocean")
        low = self._request("req-low", commitment="com-ice")
        # 两个申请都还缺包装与运输，先完成低优先级的前置条件也不能抢时段。
        self._ready(low["loan_id"], crate="crate-b")
        low = self.loan.explain_loan(low["loan_id"])["loan"]
        self.assertEqual("waitlisted", low["status"])
        # 高优先级补齐前置条件后立即占用。
        self._ready(high["loan_id"], crate="crate-a")
        # 低优先级再次触发评估仍然落选，原因可解释。
        self.loan.confirm_phase(actor_id="op-own", loan_id=low["loan_id"], phase="transport",
                                evidence={"handler": "极地物流", "scheduled_dispatch_at": ts(9)})
        slot = self.loan.explain_slot(resource_id="mass-spec", start=ts(10), end=ts(12))
        self.assertEqual([high["loan_id"]], [item["loan_id"] for item in slot["occupied_by"]])
        losers = [item for item in slot["waitlisted"] if item["loan_id"] == low["loan_id"]]
        self.assertTrue(any("占用" in reason for reason in losers[0]["not_selected_reasons"]))

    def test_duplicate_request_is_rejected_and_same_request_id_replays(self):
        first = self._request("req-dup")
        with self.assertRaises(ConflictError):
            self._request("req-dup-2")
        replay = self._request("req-dup")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["loan_id"], replay["loan_id"])

    def test_full_lifecycle_packing_transit_acceptance_and_return(self):
        req = self._request("req-life")
        self._ready(req["loan_id"])
        self.assertEqual("packed", self.loan.pack_for_shipment(actor_id="op-own", loan_id=req["loan_id"])["status"])
        self.assertEqual("in_transit", self.loan.ship(actor_id="op-own", loan_id=req["loan_id"])["status"])
        self.assertEqual("pending_acceptance",
                         self.loan.arrive(actor_id="op-bor", loan_id=req["loan_id"])["status"])
        accepted = self.loan.accept_delivery(actor_id="op-bor", loan_id=req["loan_id"])
        self.assertEqual("active", accepted["status"])
        run = self.loan.start_experiment_run(actor_id="op-bor", loan_id=req["loan_id"])
        self.assertEqual("cal-2026", run["state_snapshot"]["calibration"]["calibration_id"])
        self.assertEqual(1, run["state_snapshot"]["resources"][0]["revision"])
        self.loan.finish_experiment_run(actor_id="op-bor", run_id=run["run_id"])
        self.assertEqual("return_in_transit",
                         self.loan.begin_return(actor_id="op-bor", loan_id=req["loan_id"])["status"])
        self.assertEqual("pending_return",
                         self.loan.arrive_return(actor_id="op-own", loan_id=req["loan_id"])["status"])
        done = self.loan.accept_return(
            actor_id="op-own", loan_id=req["loan_id"],
            conditions={"mass-spec": {"returned": True, "condition": "ok"},
                        "ion-source": {"returned": True, "condition": "ok"},
                        "detector": {"returned": True, "condition": "ok"}})
        self.assertEqual("completed", done["status"])
        self.assertFalse(done["overdue"])

    def test_transport_damage_revisions_component_and_certificate_stops_covering(self):
        # 离子源单独借调，证书覆盖 revision 1。
        self.loan.register_qualification(request_id="qual-ion", actor_id="admin-1",
                                         qualified_actor_id="op-bor", capability="ionization",
                                         valid_until="2027-01-01T00:00:00Z")
        req = self.loan.submit_loan_request(
            request_id="req-dmg", actor_id="op-bor", site_id="site-bor",
            commitment_id="com-ocean", resource_id="ion-source",
            required_capabilities=["ionization"],
            window_start=ts(10), window_end=ts(12), planned_return_at=ts(11, 12))
        self.loan.confirm_phase(actor_id="op-own", loan_id=req["loan_id"], phase="packaging",
                                evidence={"crate_id": "crate-a"})
        self.loan.confirm_phase(actor_id="op-own", loan_id=req["loan_id"], phase="transport",
                                evidence={"handler": "极地物流", "scheduled_dispatch_at": ts(9)})
        self.assertEqual("confirmed", self.loan.explain_loan(req["loan_id"])["loan"]["status"])
        self.loan.pack_for_shipment(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.ship(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.arrive(actor_id="op-bor", loan_id=req["loan_id"])
        result = self.loan.accept_delivery(
            actor_id="op-bor", loan_id=req["loan_id"],
            observed_damage=[{"resource_id": "ion-source", "lost_capabilities": [],
                              "note": "运输后密封面损伤，更换备件，修订升至 2"}])
        # 证书只覆盖 revision 1：验收不通过，退回候补，占用释放，但无实验数据受影响。
        self.assertEqual("waitlisted", result["status"])
        self.assertIn("revision", result["reason"])
        issues = self.loan.list_issues("component_missing")
        self.assertEqual(0, len(issues))

    def test_fault_and_calibration_revoke_adjust_only_unstarted_bookings(self):
        active_req = self._request("req-active", start=10, end=12, ret=11)
        self._ready(active_req["loan_id"])
        self.loan.pack_for_shipment(actor_id="op-own", loan_id=active_req["loan_id"])
        self.loan.ship(actor_id="op-own", loan_id=active_req["loan_id"])
        self.loan.arrive(actor_id="op-bor", loan_id=active_req["loan_id"])
        self.loan.accept_delivery(actor_id="op-bor", loan_id=active_req["loan_id"])
        run = self.loan.start_experiment_run(actor_id="op-bor", loan_id=active_req["loan_id"])

        future = self._request("req-future", start=20, end=22, ret=21)
        self._ready(future["loan_id"], crate="crate-b")
        self.assertEqual("confirmed", self.loan.explain_loan(future["loan_id"])["loan"]["status"])

        event = self.loan.report_resource_event(actor_id="admin-1", resource_id="mass-spec",
                                                kind="fault", detail={"note": "真空系统故障"})
        self.assertIn(future["loan_id"], event["adjusted_loan_ids"])
        self.assertNotIn(active_req["loan_id"], event["adjusted_loan_ids"])
        self.assertEqual("active", self.loan.explain_loan(active_req["loan_id"])["loan"]["status"])
        self.assertEqual("waitlisted", self.loan.explain_loan(future["loan_id"])["loan"]["status"])

        # 即使随后撤销校准，已经产生的实验数据快照仍引用当时有效的证书与修订。
        self.loan.revoke_calibration(actor_id="admin-1", calibration_id="cal-2026",
                                     reason="发现证书程序瑕疵")
        self.loan.finish_experiment_run(actor_id="op-bor", run_id=run["run_id"])
        runs = self.loan.explain_loan(active_req["loan_id"])["experiment_runs"]
        self.assertEqual("cal-2026", runs[0]["state_snapshot"]["calibration"]["calibration_id"])
        self.assertEqual("v3", runs[0]["state_snapshot"]["calibration"]["version"])

    def test_delayed_return_reopens_future_booking_and_files_issue(self):
        first = self._request("req-first", start=10, end=11, ret=11)
        self._ready(first["loan_id"], crate="crate-a")
        self.loan.pack_for_shipment(actor_id="op-own", loan_id=first["loan_id"])
        self.loan.ship(actor_id="op-own", loan_id=first["loan_id"])
        self.loan.arrive(actor_id="op-bor", loan_id=first["loan_id"])
        self.loan.accept_delivery(actor_id="op-bor", loan_id=first["loan_id"])

        second = self._request("req-second", start=11, end=13, ret=12)
        self._ready(second["loan_id"], crate="crate-b")
        self.assertEqual("confirmed", self.loan.explain_loan(second["loan_id"])["loan"]["status"])

        event = self.loan.report_resource_event(
            actor_id="admin-1", resource_id="mass-spec", kind="delayed_return",
            detail={"loan_id": first["loan_id"], "new_return_at": ts(12, 12)})
        self.assertIn(second["loan_id"], event["adjusted_loan_ids"])
        issues = self.loan.list_issues("overdue_return")
        self.assertTrue(any(item["detail"]["loan_id"] == first["loan_id"] for item in issues))

    def test_overdue_return_and_missing_component_create_accountability(self):
        req = self._request("req-acc")
        self._ready(req["loan_id"])
        self.loan.pack_for_shipment(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.ship(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.arrive(actor_id="op-bor", loan_id=req["loan_id"])
        self.loan.accept_delivery(actor_id="op-bor", loan_id=req["loan_id"])
        run = self.loan.start_experiment_run(actor_id="op-bor", loan_id=req["loan_id"])
        self.loan.finish_experiment_run(actor_id="op-bor", run_id=run["run_id"])
        self.loan.begin_return(actor_id="op-bor", loan_id=req["loan_id"])
        self.loan.arrive_return(actor_id="op-own", loan_id=req["loan_id"])

        self.world.clock._value = datetime(2026, 11, 20, tzinfo=timezone.utc)
        result = self.loan.accept_return(
            actor_id="op-own", loan_id=req["loan_id"],
            conditions={"mass-spec": {"returned": True, "condition": "ok"},
                        "ion-source": {"returned": False, "note": "去向不明"},
                        "detector": {"returned": True, "condition": "ok"}})
        self.assertTrue(result["overdue"])
        self.assertEqual(["ion-source"], result["missing_resources"])
        missing = self.loan.list_issues("component_missing")
        overdue = self.loan.list_issues("overdue_return")
        self.assertEqual(1, len(missing))
        self.assertEqual(1, len(overdue))
        resources = {r["resource_id"]: r for r in self.loan.list_resources()}
        self.assertEqual("missing", resources["ion-source"]["status"])

    def test_detect_overdue_flags_active_loans_past_planned_return(self):
        req = self._request("req-late")
        self._ready(req["loan_id"])
        self.loan.pack_for_shipment(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.ship(actor_id="op-own", loan_id=req["loan_id"])
        self.loan.arrive(actor_id="op-bor", loan_id=req["loan_id"])
        self.loan.accept_delivery(actor_id="op-bor", loan_id=req["loan_id"])
        self.world.clock._value = datetime(2026, 11, 30, tzinfo=timezone.utc)
        result = self.loan.detect_overdue(actor_id="admin-1")
        self.assertEqual(1, result["overdue_count"])
        # 重复检测不会制造重复工单。
        again = self.loan.detect_overdue(actor_id="admin-1")
        self.assertEqual(1, again["overdue_count"])

    def test_invalid_calibration_window_rejected(self):
        with self.assertRaises(ValidationError):
            self.loan.register_calibration(
                request_id="cal-bad", actor_id="admin-1", calibration_id="cal-bad",
                resource_id="mass-spec", certificate_ref="X", version="vX",
                covers_capabilities=["mass-analysis"], covers_components=[],
                valid_from="2027-01-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z")


class LoanPersistenceTest(unittest.TestCase):
    def test_in_transit_and_waitlist_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loan.sqlite3"

            def fresh_world():
                database = Database(path)
                clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
                return LoanWorld(database, clock)

            world = fresh_world()
            moving = world.loan.submit_loan_request(
                request_id="req-move", actor_id="op-bor", site_id="site-bor",
                commitment_id="com-ocean", resource_id="mass-spec",
                required_capabilities=["mass-analysis"],
                window_start=ts(10), window_end=ts(12), planned_return_at=ts(11, 12))
            world.loan.confirm_phase(actor_id="op-own", loan_id=moving["loan_id"], phase="packaging",
                                    evidence={"crate_id": "crate-a"})
            world.loan.confirm_phase(actor_id="op-own", loan_id=moving["loan_id"], phase="transport",
                                    evidence={"handler": "极地物流", "scheduled_dispatch_at": ts(9)})
            world.loan.pack_for_shipment(actor_id="op-own", loan_id=moving["loan_id"])
            world.loan.ship(actor_id="op-own", loan_id=moving["loan_id"])
            waiting = world.loan.submit_loan_request(
                request_id="req-wait", actor_id="op-bor", site_id="site-bor",
                commitment_id="com-ice", resource_id="mass-spec",
                required_capabilities=["mass-analysis"],
                window_start=ts(10), window_end=ts(12), planned_return_at=ts(11, 12))
            world.database.close()

            restarted = fresh_world()
            self.assertEqual("in_transit",
                             restarted.loan.explain_loan(moving["loan_id"])["loan"]["status"])
            waitlist = restarted.loan.list_waitlist("mass-spec")
            self.assertEqual([waiting["loan_id"]], [item["loan_id"] for item in waitlist])
            valid, count = restarted.base.verify_audit()
            self.assertTrue(valid)
            self.assertGreater(count, 0)
            restarted.database.close()


class LoanConcurrencyTest(unittest.TestCase):
    def test_parallel_phase_confirmation_picks_single_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loan.sqlite3"

            def make_service():
                database = Database(path)
                clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
                world = LoanWorld(database, clock)
                return database, world.loan

            setup_db, setup = make_service()
            loan_ids = []
            for index, request_id in enumerate(("req-a", "req-b")):
                loan = setup.submit_loan_request(
                    request_id=request_id, actor_id="op-bor", site_id="site-bor",
                    commitment_id="com-ocean" if index == 0 else "com-ice",
                    resource_id="mass-spec", required_capabilities=["mass-analysis"],
                    window_start=ts(10), window_end=ts(12), planned_return_at=ts(11, 12))
                loan_ids.append(loan["loan_id"])
            setup_db.close()

            outcomes = []
            barrier = threading.Barrier(2)

            def worker(loan_id, crate):
                database, service = make_service()
                try:
                    barrier.wait(timeout=10)
                    service.confirm_phase(actor_id="op-own", loan_id=loan_id, phase="packaging",
                                          evidence={"crate_id": crate})
                    service.confirm_phase(actor_id="op-own", loan_id=loan_id, phase="transport",
                                          evidence={"handler": "极地物流", "scheduled_dispatch_at": ts(9)})
                    outcomes.append(service.explain_loan(loan_id)["loan"]["status"])
                finally:
                    database.close()

            threads = [threading.Thread(target=worker, args=(loan_ids[0], "crate-a")),
                       threading.Thread(target=worker, args=(loan_ids[1], "crate-b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(2, len(outcomes))
            self.assertEqual(1, outcomes.count("confirmed"))
            self.assertEqual(1, outcomes.count("waitlisted"))


if __name__ == "__main__":
    unittest.main()

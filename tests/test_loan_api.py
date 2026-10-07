import unittest

from polar_station_foundation.api import route
from polar_station_foundation.loan_service import LoanService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


def call(service, loan, method, path, body=None, actor="adm"):
    return route(service, method, path, body or {}, {"X-Actor-Id": actor},
                 loan_service=loan)


class LoanApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.loan = LoanService(self.database)
        call(self.service, self.loan, "POST", "/organizations",
             {"request_id": "org", "organization_id": "o1", "name": "中心"}, actor="bootstrap")
        call(self.service, self.loan, "POST", "/actors",
             {"request_id": "adm", "new_actor_id": "adm", "display_name": "主管",
              "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        call(self.service, self.loan, "POST", "/actors",
             {"request_id": "op1", "new_actor_id": "op1", "display_name": "甲",
              "role": "operator", "organization_id": "o1"})
        call(self.service, self.loan, "POST", "/sites",
             {"request_id": "s1", "site_id": "s1", "organization_id": "o1",
              "name": "甲站", "timezone_name": "UTC"})
        call(self.service, self.loan, "POST", "/sites",
             {"request_id": "s2", "site_id": "s2", "organization_id": "o1",
              "name": "乙站", "timezone_name": "UTC"})

    def tearDown(self):
        self.database.close()

    def _ready_machine(self):
        status, body = call(self.service, self.loan, "POST", "/equipment",
                            {"request_id": "em", "equipment_id": "ms1", "kind": "machine",
                             "name": "质谱仪", "site_id": "s1", "capabilities": ["mass"]})
        self.assertEqual(201, status, body)
        call(self.service, self.loan, "POST", "/equipment",
             {"request_id": "ec", "equipment_id": "ion1", "kind": "component",
              "parent_id": "ms1", "name": "离子源", "capabilities": ["hr"]})
        call(self.service, self.loan, "POST", "/qualifications",
             {"request_id": "q1", "qualification_id": "qf1", "target_actor_id": "op1",
              "capability": "mass", "valid_from": "2026-01-01T00:00Z",
              "valid_until": "2027-01-01T00:00Z"})
        call(self.service, self.loan, "POST", "/qualifications",
             {"request_id": "q2", "qualification_id": "qf2", "target_actor_id": "op1",
              "capability": "hr", "valid_from": "2026-01-01T00:00Z",
              "valid_until": "2027-01-01T00:00Z"})
        call(self.service, self.loan, "POST", "/priority-freezes",
             {"request_id": "fz", "freeze_id": "fz1", "label": "P", "ranking": ["ocean"]})
        call(self.service, self.loan, "POST", "/calibrations",
             {"request_id": "cal", "certificate_id": "cal1", "equipment_id": "ms1",
              "calibration_version": "v3", "valid_from": "2026-09-01T00:00Z",
              "valid_until": "2026-12-31T00:00Z"})
        call(self.service, self.loan, "POST", "/equipment/crate",
             {"equipment_id": "ion1", "crate_status": "sealed"})

    def test_full_loan_flow_over_http(self):
        self._ready_machine()
        status, body = call(self.service, self.loan, "POST", "/loan-requests",
                            {"request_id": "LA", "equipment_id": "ms1",
                             "required_capabilities": ["mass", "hr"], "requesting_site_id": "s2",
                             "operator_actor_id": "op1", "start_at": "2026-10-10T00:00Z",
                             "end_at": "2026-10-12T00:00Z", "commitment_key": "ocean",
                             "freeze_id": "fz1", "component_ids": ["ion1"]}, actor="op1")
        self.assertEqual(201, status, body)
        status, body = call(self.service, self.loan, "POST", "/loan-requests/LA/packing",
                            {"passed": True})
        self.assertEqual(200, status, body)
        status, body = call(self.service, self.loan, "GET", "/loan-requests/LA")
        self.assertEqual(200, status)
        self.assertEqual("confirmed", body["status"])
        call(self.service, self.loan, "POST", "/loan-requests/LA/transit")
        status, body = call(self.service, self.loan, "POST", "/loan-requests/LA/accept",
                            {"accepted": True})
        self.assertEqual("active", body["status"])
        status, body = call(self.service, self.loan, "POST", "/experiment-data",
                            {"request_id": "LA", "data_id": "d1", "payload": {"s": 1}},
                            actor="op1")
        self.assertEqual(201, status, body)
        status, body = call(self.service, self.loan, "POST", "/loan-requests/LA/return")
        self.assertEqual(200, status, body)
        self.assertEqual("completed", body["status"])

    def test_slot_explanation_endpoint(self):
        self._ready_machine()
        call(self.service, self.loan, "POST", "/loan-requests",
             {"request_id": "LA", "equipment_id": "ms1", "required_capabilities": ["mass", "hr"],
              "requesting_site_id": "s2", "operator_actor_id": "op1",
              "start_at": "2026-10-10T00:00Z", "end_at": "2026-10-12T00:00Z",
              "commitment_key": "ocean", "freeze_id": "fz1", "component_ids": ["ion1"]},
             actor="op1")
        call(self.service, self.loan, "POST", "/loan-requests/LA/packing", {"passed": True})
        status, body = call(self.service, self.loan, "GET",
                            "/slots?equipment_id=ms1&start_at=2026-10-10T00:00Z"
                            "&end_at=2026-10-12T00:00Z")
        self.assertEqual(200, status)
        self.assertEqual("LA", body["occupants"][0]["request_id"])

    def test_idempotent_equipment_registration(self):
        self._ready_machine()
        status, body = call(self.service, self.loan, "POST", "/equipment",
                            {"request_id": "em", "equipment_id": "ms1", "kind": "machine",
                             "name": "质谱仪", "site_id": "s1", "capabilities": ["mass"]})
        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])

    def test_accountability_endpoint(self):
        status, body = call(self.service, self.loan, "GET", "/accountability")
        self.assertEqual(200, status)
        self.assertIn("overdue_returns", body)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timezone

from polar_station_foundation.api import route
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.loan import LoanService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database

from tests.test_loan import LoanWorld, ts


class LoanApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.world = LoanWorld(self.database, clock)
        self.loan = self.world.loan
        self.base = self.world.base

    def tearDown(self):
        self.database.close()

    def call(self, method, path, actor="op-bor", body=None):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor}, loan=self.loan)

    def test_request_confirm_and_explain_over_http(self):
        status, payload = self.call("POST", "/loan/requests", body={
            "request_id": "http-1", "site_id": "site-bor", "commitment_id": "com-ocean",
            "resource_id": "mass-spec", "required_capabilities": ["mass-analysis"],
            "window_start": ts(10), "window_end": ts(12), "planned_return_at": ts(11)})
        self.assertEqual(201, status)
        loan_id = payload["loan_id"]
        self.assertEqual("waitlisted", payload["status"])

        status, payload = self.call("POST", "/loan/phases/confirm", actor="op-own", body={
            "loan_id": loan_id, "phase": "packaging", "evidence": {"crate_id": "crate-a"}})
        self.assertEqual(200, status)
        status, payload = self.call("POST", "/loan/phases/confirm", actor="op-own", body={
            "loan_id": loan_id, "phase": "transport",
            "evidence": {"handler": "极地物流", "scheduled_dispatch_at": ts(9)}})
        self.assertEqual(200, status)
        self.assertEqual("confirmed", payload["status"])

        status, payload = self.call("GET", "/loan/explain-slot?resource_id=mass-spec"
                                           f"&start={ts(10)}&end={ts(12)}")
        self.assertEqual(200, status)
        self.assertEqual(loan_id, payload["occupied_by"][0]["loan_id"])

        status, payload = self.call("GET", f"/loan/loans/{loan_id}/explanation", actor="admin-1")
        self.assertEqual(200, status)
        self.assertIn("phase_checks", payload)

    def test_duplicate_returns_conflict_and_resources_listing(self):
        body = {"request_id": "http-dup", "site_id": "site-bor", "commitment_id": "com-ocean",
                "resource_id": "mass-spec", "required_capabilities": ["mass-analysis"],
                "window_start": ts(10), "window_end": ts(12), "planned_return_at": ts(11)}
        first_status, _ = self.call("POST", "/loan/requests", body=body)
        self.assertEqual(201, first_status)
        second_status, payload = self.call("POST", "/loan/requests", body={**body, "request_id": "http-dup-2"})
        self.assertEqual(409, second_status)
        self.assertIn("相同请求", payload["message"])

        status, payload = self.call("GET", "/loan/resources?site_id=site-own", actor="admin-1")
        self.assertEqual(200, status)
        ids = {item["resource_id"] for item in payload["items"]}
        self.assertEqual({"mass-spec", "ion-source", "detector"}, ids)

    def test_loan_routes_absent_without_loan_service(self):
        status, payload = route(self.base, "GET", "/loan/resources", {}, {})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()

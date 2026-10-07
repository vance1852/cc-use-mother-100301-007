"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .loan_service import LoanService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, loan_service: LoanService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    if loan_service is None and parsed.path != "/health":
        loan_service = LoanService(service.database)
    query = parse_qs(parsed.query)

    def parameter(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ----------------------------------------------------- 跨站设备借调平台
        assert loan_service is not None
        if method == "POST" and parsed.path == "/equipment":
            result = loan_service.register_equipment(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/equipment/status":
            loan_service.update_equipment_status(actor_id=actor_id, **body)
            return 200, {"status": "ok"}
        if method == "POST" and parsed.path == "/equipment/crate":
            loan_service.set_crate_status(actor_id=actor_id, **body)
            return 200, {"status": "ok"}
        if method == "GET" and parsed.path.startswith("/equipment/"):
            equipment_id = parsed.path.split("/")[2]
            return 200, _equipment_payload(loan_service.get_equipment(equipment_id))
        if method == "POST" and parsed.path == "/calibrations":
            result = loan_service.issue_calibration(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path.startswith("/calibrations/") and parsed.path.endswith("/revoke"):
            certificate_id = parsed.path.split("/")[2]
            loan_service.revoke_calibration(actor_id=actor_id, certificate_id=certificate_id,
                                            reason=body.get("reason", ""))
            return 200, {"status": "ok"}
        if method == "POST" and parsed.path == "/qualifications":
            result = loan_service.register_qualification(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/qualifications/revoke":
            loan_service.revoke_qualification(actor_id=actor_id,
                                              qualification_id=body["qualification_id"])
            return 200, {"status": "ok"}
        if method == "POST" and parsed.path == "/priority-freezes":
            result = loan_service.freeze_priorities(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan-requests":
            result = loan_service.submit_loan_request(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and parsed.path == "/loan-requests":
            machine_id = parameter("equipment_id", "")
            if not machine_id:
                raise ValidationError("equipment_id 不能为空")
            items = [_loan_payload(item) for item in loan_service.list_waitlist(machine_id)]
            return 200, {"items": items}
        if method == "GET" and parsed.path.startswith("/loan-requests/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 2:
                return 200, _loan_payload(loan_service.get_loan(parts[1]))
        if method == "POST" and parsed.path.endswith("/packing"):
            request_id = parsed.path.split("/")[2]
            result = loan_service.advance_packing_phase(
                actor_id=actor_id, request_id=request_id, passed=bool(body.get("passed", False)),
                reason=body.get("reason"))
            return 200, result
        if method == "POST" and parsed.path.endswith("/transit"):
            request_id = parsed.path.split("/")[2]
            loan_service.mark_in_transit(actor_id=actor_id, request_id=request_id)
            return 200, {"status": "ok"}
        if method == "POST" and parsed.path.endswith("/accept"):
            request_id = parsed.path.split("/")[2]
            result = loan_service.accept_delivery(
                actor_id=actor_id, request_id=request_id, accepted=bool(body.get("accepted", True)),
                damage_note=body.get("damage_note"))
            return 200, result
        if method == "POST" and parsed.path.endswith("/return"):
            request_id = parsed.path.split("/")[2]
            return 200, loan_service.complete_return(actor_id=actor_id, request_id=request_id)
        if method == "POST" and parsed.path.endswith("/cancel"):
            request_id = parsed.path.split("/")[2]
            loan_service.cancel_request(actor_id=actor_id, request_id=request_id,
                                        reason=body.get("reason"))
            return 200, {"status": "ok"}
        if method == "POST" and parsed.path == "/experiment-data":
            result = loan_service.record_experiment_data(actor_id=actor_id, **body)
            return 201, result
        if method == "GET" and parsed.path == "/experiment-data":
            request_id = parameter("request_id", "")
            if not request_id:
                raise ValidationError("request_id 不能为空")
            return 200, {"items": [item.__dict__ for item in loan_service.list_experiment_data(request_id)]}
        if method == "POST" and parsed.path == "/incidents":
            return 201, loan_service.report_incident(actor_id=actor_id, **body)
        if method == "GET" and parsed.path == "/incidents":
            items = [item.__dict__ for item in
                     loan_service.list_incidents(parameter("incident_type"))]
            return 200, {"items": items}
        if method == "GET" and parsed.path == "/slots":
            result = loan_service.explain_slot(
                equipment_id=parameter("equipment_id", ""), start_at=parameter("start_at", ""),
                end_at=parameter("end_at", ""), request_id=parameter("request_id"))
            return 200, result
        if method == "GET" and parsed.path == "/accountability":
            return 200, loan_service.accountability_report()
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _equipment_payload(item) -> dict[str, Any]:
    payload = item.__dict__
    payload["capabilities"] = sorted(payload["capabilities"])
    return payload


def _loan_payload(item) -> dict[str, Any]:
    payload = item.__dict__
    payload["components"] = list(payload["components"])
    payload["required_capabilities"] = sorted(payload["required_capabilities"])
    return payload


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    loan_service: LoanService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                loan_service=getattr(self, "loan_service", None))
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动极地科考站协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.loan_service = LoanService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .loan import LoanService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, loan: LoanService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def q(name: str, default: str = "") -> str:
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
            site_id = q("site_id")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = q("category") or None
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # --- 跨站借调平台 ---
        if loan is None:
            return 404, {"error": "route_not_found", "message": "接口不存在"}
        segments = [part for part in parsed.path.split("/") if part]
        if method == "POST" and parsed.path == "/loan/resources":
            result = loan.register_resource(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/calibrations":
            result = loan.register_calibration(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/crates":
            result = loan.register_crate(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/qualifications":
            result = loan.register_qualification(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/commitments":
            result = loan.register_commitment(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/requests":
            result = loan.submit_loan_request(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/loan/phases/confirm":
            return 200, loan.confirm_phase(actor_id=actor_id, loan_id=body["loan_id"],
                                           phase=body["phase"], evidence=body.get("evidence"))
        if method == "POST" and parsed.path == "/loan/cancel":
            return 200, loan.cancel_loan(actor_id=actor_id, loan_id=body["loan_id"],
                                        reason=body.get("reason", ""))
        if method == "POST" and parsed.path == "/loan/pack":
            return 200, loan.pack_for_shipment(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/ship":
            return 200, loan.ship(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/arrive":
            return 200, loan.arrive(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/accept-delivery":
            return 200, loan.accept_delivery(actor_id=actor_id, loan_id=body["loan_id"],
                                            observed_damage=body.get("observed_damage"))
        if method == "POST" and parsed.path == "/loan/runs/start":
            return 201, loan.start_experiment_run(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/runs/finish":
            return 200, loan.finish_experiment_run(actor_id=actor_id, run_id=body["run_id"])
        if method == "POST" and parsed.path == "/loan/return/begin":
            return 200, loan.begin_return(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/return/arrive":
            return 200, loan.arrive_return(actor_id=actor_id, loan_id=body["loan_id"])
        if method == "POST" and parsed.path == "/loan/return/accept":
            return 200, loan.accept_return(actor_id=actor_id, loan_id=body["loan_id"],
                                          conditions=body.get("conditions", {}))
        if method == "POST" and parsed.path == "/loan/events":
            return 200, loan.report_resource_event(actor_id=actor_id, resource_id=body["resource_id"],
                                                  kind=body["kind"], detail=body.get("detail"))
        if method == "POST" and parsed.path == "/loan/calibrations/revoke":
            return 200, loan.revoke_calibration(actor_id=actor_id,
                                               calibration_id=body["calibration_id"],
                                               reason=body.get("reason", ""))
        if method == "POST" and parsed.path == "/loan/detect-overdue":
            return 200, loan.detect_overdue(actor_id=actor_id)
        if method == "GET" and parsed.path == "/loan/resources":
            return 200, {"items": loan.list_resources(q("site_id") or None)}
        if method == "GET" and parsed.path == "/loan/waitlist":
            return 200, {"items": loan.list_waitlist(q("resource_id") or None)}
        if method == "GET" and parsed.path == "/loan/explain-slot":
            return 200, loan.explain_slot(resource_id=q("resource_id"), start=q("start"), end=q("end"))
        if method == "GET" and len(segments) == 4 and segments[:2] == ["loan", "loans"] \
                and segments[3] == "explanation":
            return 200, loan.explain_loan(segments[2])
        if method == "GET" and parsed.path == "/loan/issues":
            kind = q("kind")
            if not kind:
                raise ValidationError("kind 不能为空")
            return 200, {"items": loan.list_issues(kind)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError, KeyError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    loan: LoanService

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
                                loan=self.loan)
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
    Handler.loan = LoanService(database)
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

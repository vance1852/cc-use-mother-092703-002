"""无第三方依赖的联合研发项目 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from .errors import JointProjectError, ValidationFailed
from .service import JointProjectService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: JointProjectService) -> None:
        self.service = service
        # SQLite 连接跨工作线程共享；用串行锁保证事务隔离
        self._lock = RLock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _text(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"缺少字段 {key}")
        return value.strip()

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = target.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            service = self.service
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    service.create_user(payload["user_id"], payload["display_name"], payload["role"]),
                )
            actor = self._actor(normalized)

            if method == "POST" and path == "/parties":
                return Response(
                    201,
                    service.register_party(actor, self._text(payload, "party_id"), self._text(payload, "name"), self._text(payload, "kind")),
                )
            if method == "POST" and path == "/projects":
                return Response(
                    201,
                    service.create_project(actor, self._text(payload, "project_id"), self._text(payload, "name")),
                )
            if method == "POST" and path == "/agreements/versions":
                body_value = payload.get("body")
                if not isinstance(body_value, dict):
                    raise ValidationFailed("缺少协议 body 对象")
                return Response(
                    201,
                    service.create_agreement_version(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "title"),
                        body_value,
                        str(payload.get("change_summary", "")),
                    ),
                )
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "agreements"
                and parts[2] == "versions"
                and parts[4] == "sign"
            ):
                return Response(200, service.sign_agreement(actor, parts[1], int(parts[3])))
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "agreements"
                and parts[2] == "versions"
            ):
                return Response(200, service.agreement_version(actor, parts[1], int(parts[3])))
            if method == "POST" and path == "/commitments":
                detail = payload.get("detail", {})
                if not isinstance(detail, dict):
                    raise ValidationFailed("detail 必须是对象")
                return Response(
                    201,
                    service.add_commitment(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "commitment_id"),
                        self._text(payload, "party_id"),
                        self._text(payload, "kind"),
                        self._text(payload, "title"),
                        detail,
                        str(payload.get("quantity", "")),
                        str(payload.get("unit", "")),
                    ),
                )
            if method == "POST" and path == "/acceptance-rules":
                rule = payload.get("rule")
                if not isinstance(rule, dict):
                    raise ValidationFailed("rule 必须是对象")
                return Response(
                    201,
                    service.add_acceptance_rule(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "rule_id"),
                        self._text(payload, "milestone_code"),
                        self._text(payload, "name"),
                        rule,
                    ),
                )
            if method == "POST" and path == "/milestones":
                return Response(
                    201,
                    service.create_milestone(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "code"),
                        self._text(payload, "name"),
                        int(payload["sequence_no"]),
                        self._text(payload, "planned_date"),
                        payload.get("version_no"),
                        payload.get("milestone_id"),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "submit":
                evidences = payload.get("evidences")
                if not isinstance(evidences, list):
                    raise ValidationFailed("evidences 必须是数组")
                return Response(
                    201,
                    service.submit_milestone(
                        actor,
                        parts[1],
                        str(payload.get("note", "")),
                        evidences,
                        self._text(payload, "idempotency_key"),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "decide":
                decisions = payload.get("decisions")
                if not isinstance(decisions, list):
                    raise ValidationFailed("decisions 必须是数组")
                return Response(
                    200,
                    service.decide_acceptance(
                        actor, parts[1], self._text(payload, "submission_id"), decisions
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "resume":
                return Response(200, service.resume_milestone(actor, parts[1], str(payload.get("reason", ""))))
            if method == "GET" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "history":
                return Response(200, service.milestone_history(actor, parts[1]))
            if method == "POST" and path == "/disputes":
                return Response(
                    201,
                    service.open_dispute(
                        actor,
                        self._text(payload, "milestone_id"),
                        self._text(payload, "subject"),
                        str(payload.get("detail", "")),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, service.resolve_dispute(actor, parts[1], self._text(payload, "resolution")))
            if method == "POST" and path == "/funding/sources":
                return Response(
                    201,
                    service.register_funding_source(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "source_id"),
                        self._text(payload, "commitment_id"),
                        self._text(payload, "name"),
                        payload["total_amount_cny"],
                    ),
                )
            if method == "POST" and len(parts) == 4 and parts[0] == "funding" and parts[1] == "sources" and parts[3] == "receipts":
                return Response(
                    200,
                    service.record_funding_receipt(actor, parts[2], payload["amount_cny"]),
                )
            if method == "POST" and path == "/disbursements":
                return Response(
                    201,
                    service.propose_disbursement(
                        actor,
                        self._text(payload, "project_id"),
                        self._text(payload, "disbursement_id"),
                        self._text(payload, "milestone_id"),
                        self._text(payload, "source_id"),
                        payload["amount_cny"],
                        payload.get("submission_id"),
                        payload.get("idempotency_key"),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "disbursements" and parts[2] == "review":
                return Response(
                    200,
                    service.review_disbursement(actor, parts[1], self._text(payload, "decision"), str(payload.get("note", ""))),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "disbursements" and parts[2] == "pay":
                return Response(200, service.pay_disbursement(actor, parts[1]))
            lifecycle_actions = {"suspend", "restart", "close"}
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] in lifecycle_actions:
                reason = str(payload.get("reason", ""))
                if parts[2] == "close":
                    return Response(200, service.close_project(actor, parts[1], self._text(payload, "action"), reason))
                action = {"suspend": service.suspend_project, "restart": service.restart_project}[parts[2]]
                return Response(200, action(actor, parts[1], reason))
            if method == "GET" and len(parts) == 4 and parts[0] == "projects" and parts[2:4] == ["funding", "outstanding"]:
                return Response(200, service.outstanding_funding_decisions(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "dossier":
                return Response(200, service.project_dossier(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except JointProjectError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JointProject/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with application._lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动产学研联合研发项目管理服务")
    parser.add_argument("--database", type=Path, default=Path("joint_project.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(JointProjectService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

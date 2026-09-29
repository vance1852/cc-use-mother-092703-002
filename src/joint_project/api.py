"""无第三方依赖的 HTTP JSON 接口。

身份通过 ``X-Actor-Id`` 请求头传递（离线内网部署，鉴权由网关完成）。
首次部署且用户表为空时，不带该头的 ``POST /users`` 可引导创建管理员。
"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import ProjectService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: ProjectService) -> None:
        self.service = service
        # SQLite 连接不支持真正的跨线程并发；与写事务的串行化语义保持一致
        self._lock = threading.Lock()

    def _actor(self, headers: Mapping[str, str]) -> str | None:
        actor = headers.get("x-actor-id", "").strip()
        return actor or None

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

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        actor = self._actor(normalized)
        try:
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        try:
            with self._lock:
                return self._route(method, path, parts, parsed, actor, payload)
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})

    def _route(
        self, method: str, path: str, parts: list[str], parsed, actor: str | None,
        payload: dict[str, Any],
    ) -> Response:
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "joint-project"})

            if method == "POST" and path == "/users":
                has_users = self.service.connection.execute(
                    "SELECT count(*) FROM users"
                ).fetchone()[0]
                if actor is None and has_users == 0:
                    result = self.service.create_user(
                        None, payload["user_id"], payload["display_name"],
                        payload.get("role", "admin"), payload.get("org_name"),
                    )
                elif actor is None:
                    raise ValidationFailed("缺少 X-Actor-Id")
                else:
                    result = self.service.create_user(
                        actor, payload["user_id"], payload["display_name"], payload["role"],
                        payload.get("org_name"),
                    )
                return Response(201, result)

            if actor is None:
                raise ValidationFailed("缺少 X-Actor-Id")

            if method == "POST" and path == "/projects":
                result = self.service.register_project(actor, payload["project_id"], payload["title"])
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "projects":
                return Response(200, self.service.get_project(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "close":
                return Response(200, self.service.close_project(actor, parts[1]))

            if method == "POST" and path == "/agreements":
                return Response(201, self.service.sign_agreement(actor, payload))
            if method == "GET" and len(parts) == 4 and parts[0] == "projects" and parts[2] == "versions":
                return Response(200, self.service.get_version(parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "versions":
                return Response(200, {"versions": self.service.list_versions(actor, parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "report":
                return Response(200, self.service.project_report(actor, parts[1]))

            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "milestones":
                return Response(200, {
                    "milestones": self.service.list_milestones(actor, parts[1])
                })
            if method == "GET" and len(parts) == 4 and parts[0] == "projects" and parts[2] == "milestones":
                return Response(200, self.service.get_milestone(actor, parts[1], parts[3]))
            if method == "POST" and len(parts) == 5 and parts[0] == "projects" and parts[2] == "milestones":
                project_id, milestone_id, action = parts[1], parts[3], parts[4]
                if action == "evidence":
                    result = self.service.submit_evidence(
                        actor, project_id, milestone_id, payload["title"], payload["ref"],
                        payload.get("content_sha256"), payload.get("note", ""),
                    )
                    return Response(201, result)
                if action == "reviews":
                    result = self.service.review_acceptance(
                        actor, project_id, milestone_id, payload["decision"],
                        int(payload["achieved_ratio_pct"]), payload.get("note", ""),
                        payload.get("evidence_ids"),
                    )
                    return Response(201, result)
                if action == "suspend":
                    return Response(200, self.service.suspend_milestone(actor, project_id, milestone_id))
                if action == "resume":
                    return Response(200, self.service.resume_milestone(actor, project_id, milestone_id))
                if action == "disputes":
                    result = self.service.open_dispute(
                        actor, project_id, milestone_id, payload["reason"]
                    )
                    return Response(201, result)
                if action == "release-hold":
                    result = self.service.release_payment_hold(
                        actor, project_id, milestone_id, payload["note"]
                    )
                    return Response(200, result)
                if action == "payments":
                    result = self.service.decide_payment(
                        actor, project_id, milestone_id, payload["source_id"], payload["decision"],
                        payload.get("note", ""), payload.get("expected_amount_cents"),
                    )
                    return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                result = self.service.resolve_dispute(
                    actor, int(parts[1]), payload["resolution"],
                    keep_frozen=bool(payload.get("keep_frozen", False)),
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "payments" and parts[2] == "execute":
                return Response(200, self.service.execute_payment(actor, int(parts[1])))

            if method == "GET" and path == "/audit":
                query = parse_qs(parsed.query)
                result = self.service.audit_trail(
                    actor, query.get("entity_type", [None])[0], query.get("entity_id", [None])[0]
                )
                return Response(200, {"events": result})

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
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
    parser = argparse.ArgumentParser(description="启动产学研联合研发项目管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("joint_project.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(ProjectService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
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

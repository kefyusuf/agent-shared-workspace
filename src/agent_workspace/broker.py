"""Minimal M3.1 loopback JSON broker. NOT an MCP server or OS sandbox.

The server is deliberately narrow: no dynamic method access, database path,
source-code access, operator enrollment or bootstrap route is exposed.
"""
from __future__ import annotations

import argparse
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any

from .core import Workspace, WorkspaceError

_MAX_BODY = 32768

# Explicitly exported domain operations. A private method can never be selected
# by an HTTP client. The domain layer independently checks actor permissions.
_OPERATIONS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "session_info": {},
    "close_session": {},
    "list_tasks": {"limit": int},
    "get_task": {"task_id": str},
    "create_task": {"title": str, "acceptance_criteria": str, "request_key": str},
    "claim_task": {"task_id": str, "expected_version": int, "request_key": str, "lease_seconds": int},
    "inbox": {"after_seq": int, "limit": int, "include_acked": bool},
    "get_message": {"message_id": str},
    "send_message": {"recipient_agent_id": str, "body": str, "request_key": str, "reply_to": (str, type(None))},
    "knowledge_brief": {"limit": int},
    "get_decision": {"decision_id": str},
    "get_memory": {"memory_id": str},
    "search_memories": {"query": (str, type(None)), "kind": (str, type(None)), "before_seq": (int, type(None)), "limit": int},
    "get_context": {"snapshot_id": str},
    "list_task_contexts": {"task_id": str, "before_seq": (int, type(None)), "limit": int},
    "handoff_inbox": {"after_seq": int, "limit": int},
    "context_changes": {"after_event_id": int, "limit": int},
}


class BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def create_server(workspace: Workspace, *, host: str = "127.0.0.1", port: int = 8765) -> BrokerHTTPServer:
    """Bind a non-public IPv4 loopback listener only."""
    if host != "127.0.0.1":
        raise ValueError("broker requires explicit IPv4 loopback binding")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentWorkspaceBroker"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # Never log URLs, credentials, bearer tokens or response bodies.
            pass

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def _error(self, status: int, code: str) -> None:
            self._reply(status, {"ok": False, "error": {"code": code}})

        def _guard(self) -> bool:
            # Prevent DNS rebinding (Host spoofing) and browser-origin requests.
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get_all("Host") is None or len(self.headers.get_all("Host")) != 1 or self.headers["Host"] not in allowed:
                self._error(400, "INVALID_HOST")
                return False
            if self.headers.get("Origin") is not None:
                self._error(403, "ORIGIN_REJECTED")
                return False
            return True

        def _body(self) -> Any:
            if self.headers.get("Transfer-Encoding") is not None:
                raise _HTTPError(400, "INVALID_REQUEST")
            if self.headers.get("Content-Type") != "application/json":
                raise _HTTPError(415, "UNSUPPORTED_MEDIA_TYPE")
            lengths = self.headers.get_all("Content-Length")
            if lengths is None or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise _HTTPError(400, "INVALID_REQUEST")
            size = int(lengths[0])
            if size > _MAX_BODY:
                raise _HTTPError(413, "PAYLOAD_TOO_LARGE")
            if size <= 0:
                raise _HTTPError(400, "INVALID_REQUEST")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise _HTTPError(400, "INVALID_REQUEST")
            try:
                return json.loads(raw.decode("utf-8"),
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    object_pairs_hook=_strict_object)
            except (UnicodeError, ValueError):
                raise _HTTPError(400, "INVALID_JSON") from None

        def _bearer(self) -> str:
            headers = self.headers.get_all("Authorization")
            if headers is None or len(headers) != 1:
                raise _HTTPError(401, "UNAUTHENTICATED")
            value = headers[0]
            if not value.startswith("Bearer ") or not value[7:] or len(value) > 512 or " " in value[7:]:
                raise _HTTPError(401, "UNAUTHENTICATED")
            return value[7:]

        def do_GET(self) -> None:
            if not self._guard():
                return
            if self.path == "/v1/health":
                self._reply(200, {"ok": True, "status": "healthy"})
            else:
                self._error(404, "NOT_FOUND")

        def do_POST(self) -> None:
            if not self._guard():
                return
            if self.path not in ("/v1/session", "/v1/call"):
                self._error(404, "NOT_FOUND")
                return
            try:
                if self.path == "/v1/call":
                    token = self._bearer()  # Authenticate before reading request body.
                else:
                    token = None
                data = self._body()
                if not isinstance(data, dict):
                    raise _HTTPError(400, "INVALID_REQUEST")
                if self.path == "/v1/session":
                    if set(data) != {"credential"} or not isinstance(data["credential"], str):
                        raise _HTTPError(400, "INVALID_REQUEST")
                    result = workspace.open_session(data["credential"])
                else:
                    workspace.session_info(token)  # Validate bearer before dispatch.
                    if set(data) != {"operation", "arguments"} or not isinstance(data["operation"], str) or not isinstance(data["arguments"], dict):
                        raise _HTTPError(400, "INVALID_REQUEST")
                    operation = data["operation"]
                    if operation not in _OPERATIONS:
                        raise _HTTPError(400, "UNKNOWN_OPERATION")
                    arguments = data["arguments"]
                    required_types = _OPERATIONS[operation]
                    if any(k not in required_types or not _valid_type(v, required_types[k]) for k, v in arguments.items()):
                        raise _HTTPError(400, "INVALID_ARGUMENTS")
                    action = getattr(workspace, operation)
                    try:
                        inspect.signature(action).bind(token, **arguments)
                    except TypeError:
                        raise _HTTPError(400, "INVALID_ARGUMENTS") from None
                    result = action(token, **arguments)
                self._reply(200, {"ok": True, "result": result})
            except _HTTPError as error:
                self._error(error.status, error.code)
            except WorkspaceError as error:
                status = 401 if error.code == "UNAUTHENTICATED" else (403 if error.code == "FORBIDDEN" else (404 if error.code == "NOT_FOUND" else 409 if error.code in ("VERSION_CONFLICT", "LEASE_HELD", "STALE_FENCE", "IDEMPOTENCY_CONFLICT") else 400))
                self._error(status, error.code)

    return BrokerHTTPServer((host, port), Handler)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class _HTTPError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code


def _valid_type(value: Any, expected: type | tuple[type, ...]) -> bool:
    # `bool` is a subclass of `int`, so use exact types rather than isinstance.
    if isinstance(expected, tuple):
        return any(type(value) is t for t in expected)
    return type(value) is expected


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Workspace local broker (M3.1)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a local workspace (one-time)")
    init.add_argument("--db", type=Path, required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--root", type=Path, required=True)
    serve = commands.add_parser("serve", help="Run loopback-only broker")
    serve.add_argument("--db", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "serve" and not args.db.is_file():
        parser.error("database does not exist; run init first")
    workspace = Workspace(args.db)
    if args.command == "init":
        # Sensitive output printed once to the trusted local operator only.
        print(json.dumps(workspace.bootstrap(args.name, args.root), indent=2))
        return 0
    with sqlite3.connect(workspace.db_path) as db:
        if db.execute("SELECT count(*) FROM projects").fetchone()[0] != 1:
            parser.error("workspace is not initialized")
    server = create_server(workspace, port=args.port)
    print(f"Broker listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

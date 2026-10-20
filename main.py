#!/usr/bin/env python3
"""MockForge: a single-file mock HTTP API.

Stateful JSON routes, string templates, optional latency, and a random
HTTP 500 rate. Python standard library only.
"""

import argparse
import datetime
import http.server
import json
import random
import re
import socketserver
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# =====================================================================
# 1. SYNTHETIC DATA
# =====================================================================

class TemplateError(Exception):
    """A template macro could not be rendered."""


class DataForge:
    """Draws values from small fixed lists and fills ``{{...}}`` macros."""

    FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Elena", "Marcus", "Siddharth", "Mei", "Devon"]
    LAST_NAMES = ["Vance", "Chen", "Sterling", "Kowalski", "O'Connor", "Patel", "Nakamura", "Al-Mansoor", "Gomez"]
    COMPANIES = ["ApexScale", "OmniCloud", "HyperDrive AI", "Starlight Pay", "Quantix Labs", "VectorSec"]
    STATUSES = ["ACTIVE", "PENDING", "PROCESSING", "FLAGGED", "COMPLETED", "SUSPENDED"]
    DOMAINS = ["com", "io", "tech", "net", "org"]

    _UUID = re.compile(r"\{\{uuid\}\}")
    _NAME = re.compile(r"\{\{name\}\}")
    _EMAIL = re.compile(r"\{\{email\}\}")
    _COMPANY = re.compile(r"\{\{company\}\}")
    _STATUS = re.compile(r"\{\{status\}\}")
    _ISO_DATE = re.compile(r"\{\{iso_date\}\}")
    _INT = re.compile(r"\{\{int:(-?\d+):(-?\d+)\}\}")
    _CURRENCY = re.compile(r"\{\{currency\}\}")

    @classmethod
    def name(cls) -> str:
        return f"{random.choice(cls.FIRST_NAMES)} {random.choice(cls.LAST_NAMES)}"

    @classmethod
    def email(cls, name: Optional[str] = None) -> str:
        clean_name = re.sub(r"[^a-zA-Z]", "", name.lower()) if name else f"user{random.randint(100, 999)}"
        company = random.choice(cls.COMPANIES).lower().replace(" ", "")
        domain = random.choice(cls.DOMAINS)
        return f"{clean_name}@{company}.{domain}"

    @classmethod
    def company(cls) -> str:
        return random.choice(cls.COMPANIES)

    @classmethod
    def status(cls) -> str:
        return random.choice(cls.STATUSES)

    @classmethod
    def currency(cls, min_val: float = 10.0, max_val: float = 10000.0) -> float:
        return round(random.uniform(min_val, max_val), 2)

    @classmethod
    def _render_int(cls, min_v: int, max_v: int) -> int:
        if min_v > max_v:
            raise TemplateError(
                f"{{{{int:{min_v}:{max_v}}}}} is invalid because {min_v} is greater than {max_v}."
            )
        return random.randint(min_v, max_v)

    @classmethod
    def synthesize_template(cls, template: Any) -> Any:
        """Replace macros in strings. Each occurrence is rendered on its own.

        A string that is only ``{{int:min:max}}`` becomes an int. The same
        token embedded in other text stays text. ``min > max`` raises
        ``TemplateError``.
        """
        if isinstance(template, dict):
            return {k: cls.synthesize_template(v) for k, v in template.items()}
        if isinstance(template, list):
            return [cls.synthesize_template(item) for item in template]
        if not isinstance(template, str):
            return template

        whole_int = cls._INT.fullmatch(template)
        if whole_int:
            return cls._render_int(int(whole_int.group(1)), int(whole_int.group(2)))

        val = template
        val = cls._UUID.sub(lambda _m: str(uuid.uuid4()), val)
        val = cls._NAME.sub(lambda _m: cls.name(), val)
        val = cls._EMAIL.sub(lambda _m: cls.email(), val)
        val = cls._COMPANY.sub(lambda _m: cls.company(), val)
        val = cls._STATUS.sub(lambda _m: cls.status(), val)
        val = cls._ISO_DATE.sub(
            lambda _m: datetime.datetime.now(datetime.timezone.utc).isoformat(),
            val,
        )
        val = cls._INT.sub(
            lambda m: str(cls._render_int(int(m.group(1)), int(m.group(2)))),
            val,
        )
        val = cls._CURRENCY.sub(lambda _m: str(cls.currency()), val)
        return val


# =====================================================================
# 2. IN-MEMORY STORE
# =====================================================================

class StateStore:
    """Thread-safe in-memory store for stateful CRUD routes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._collections: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def get_collection(self, resource: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._collections.get(resource, {}).values())

    def get_item(self, resource: str, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._collections.get(resource, {}).get(str(item_id))

    def create_item(self, resource: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if resource not in self._collections:
                self._collections[resource] = {}
            item_id = str(payload.get("id") or uuid.uuid4())
            record = dict(payload)
            record["id"] = item_id
            record["_created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._collections[resource][item_id] = record
            return record

    def update_item(self, resource: str, item_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            coll = self._collections.get(resource, {})
            item_id = str(item_id)
            if item_id not in coll:
                return None
            record = coll[item_id]
            record.update(payload)
            record["id"] = item_id
            record["_updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return record

    def delete_item(self, resource: str, item_id: str) -> bool:
        with self._lock:
            item_id = str(item_id)
            if resource in self._collections and item_id in self._collections[resource]:
                del self._collections[resource][item_id]
                return True
            return False


# =====================================================================
# 3. ROUTES AND TELEMETRY
# =====================================================================

@dataclass
class RouteContract:
    path_regex: re.Pattern
    method: str
    path_template: str = ""
    response_template: Optional[Any] = None
    stateful_resource: Optional[str] = None
    latency_ms: Tuple[int, int] = (10, 60)
    failure_rate: float = 0.0
    is_stateful: bool = False
    action: str = "custom"  # list, get, create, update, delete, custom


@dataclass
class Telemetry:
    """Request counters. Latency includes any artificial sleep."""

    total_requests: int = 0
    total_failures: int = 0
    total_latency_ms: float = 0.0
    start_time: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, latency: float, failed: bool = False):
        with self._lock:
            self.total_requests += 1
            if failed:
                self.total_failures += 1
            self.total_latency_ms += latency

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            uptime_seconds = time.time() - self.start_time
            avg_latency = (self.total_latency_ms / self.total_requests) if self.total_requests > 0 else 0
            return {
                "uptime_seconds": round(uptime_seconds, 1),
                "total_requests": self.total_requests,
                "total_failures": self.total_failures,
                "avg_latency_ms": round(avg_latency, 2),
            }


# =====================================================================
# 4. HTTP HANDLER
# =====================================================================

class MockForgeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _app(self) -> "MockForge":
        return self.server.forge  # type: ignore[attr-defined]

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("X-Powered-By", "MockForge")

    def _read_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            raw = self.rfile.read(content_length).decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw": raw}
        return {}

    def _write_json(self, status: int, payload: Any):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _finish(self, app: "MockForge", method: str, url_path: str, start_time: float, status_code: int, payload: Any):
        elapsed = (time.time() - start_time) * 1000
        app.record_request(method, url_path, status_code, elapsed)
        if status_code == 204:
            self.send_response(204)
            self._send_cors_headers()
            self.end_headers()
        else:
            self._write_json(status_code, payload)

    def _execute(self, app: "MockForge", route: RouteContract, match_vars: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, Any]:
        if route.is_stateful and route.stateful_resource:
            res = route.stateful_resource
            action = route.action
            if action == "list":
                rows = app.store.get_collection(res)
                return 200, {"data": rows, "total": len(rows)}
            if action == "get":
                item_id = match_vars.get("id")
                record = app.store.get_item(res, item_id)
                if record:
                    return 200, record
                return 404, {"error": "NotFound", "message": f"{res} with id '{item_id}' not found."}
            if action == "create":
                return 201, app.store.create_item(res, body)
            if action == "update":
                item_id = match_vars.get("id")
                record = app.store.update_item(res, item_id, body)
                if record:
                    return 200, record
                return 404, {"error": "NotFound", "message": f"{res} with id '{item_id}' not found."}
            if action == "delete":
                item_id = match_vars.get("id")
                if app.store.delete_item(res, item_id):
                    return 204, ""
                return 404, {"error": "NotFound", "message": f"{res} with id '{item_id}' not found."}
        return 200, DataForge.synthesize_template(route.response_template)

    def _dispatch(self, method: str):
        app = self._app()
        start_time = time.time()
        url_path = self.path.split("?", 1)[0]

        if url_path == "/_mockforge/metrics":
            self._write_json(200, {"status": "ONLINE", "telemetry": app.telemetry.get_summary()})
            return

        if url_path == "/_mockforge/routes":
            active_routes = [
                {
                    "method": route.method,
                    "path": route.path_template,
                    "pattern": route.path_regex.pattern,
                    "action": route.action,
                    "stateful": route.is_stateful,
                }
                for route in app.routes
            ]
            self._write_json(200, {"routes": active_routes})
            return

        if url_path == "/_mockforge/logs":
            self._write_json(200, {"recent_logs": app.recent_logs()})
            return

        matched_route: Optional[RouteContract] = None
        match_vars: Dict[str, str] = {}
        for route in app.routes:
            if route.method == method:
                match = route.path_regex.match(url_path)
                if match:
                    matched_route = route
                    match_vars = match.groupdict()
                    break

        if matched_route is None:
            self._finish(app, method, url_path, start_time, 404, {
                "error": "Contract Unmatched",
                "message": f"No mock or state contract matches {method} {url_path}",
                "mockforge_hint": "Inspect registered endpoints at /_mockforge/routes",
            })
            return

        body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
        delay = random.uniform(matched_route.latency_ms[0], matched_route.latency_ms[1]) / 1000.0
        if delay > 0:
            time.sleep(delay)

        if matched_route.failure_rate > 0.0 and random.random() < matched_route.failure_rate:
            self._finish(app, method, url_path, start_time, 500, {
                "error": "InjectedChaosFailure",
                "message": "Chaos fault injection triggered by contract specification.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            return

        try:
            status_code, response_data = self._execute(app, matched_route, match_vars, body)
        except TemplateError as exc:
            self._finish(app, method, url_path, start_time, 400, {
                "error": "TemplateError",
                "message": str(exc),
            })
            return

        self._finish(app, method, url_path, start_time, status_code, response_data)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[MockForge:Access] {self.address_string()} - {args[0]} {args[1]} -> {args[2]}\n")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# =====================================================================
# 5. MOCKFORGE
# =====================================================================

class MockForge:
    """Programmatic mock server. Each instance has its own store and routes."""

    def __init__(self, port: int = 8080, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.routes: List[RouteContract] = []
        self.store = StateStore()
        self.telemetry = Telemetry()
        self.request_log: List[Dict[str, Any]] = []
        self.log_lock = threading.Lock()

    def mock(self, path: str, method: str = "GET", response: Any = None,
             latency_ms: Tuple[int, int] = (10, 50), failure_rate: float = 0.0):
        """Register a template route. ``{param}`` becomes a path segment."""
        regex_path = re.sub(r"\{([a-zA-Z0-9_]+)\}", r"(?P<\1>[^/]+)", path)
        pattern = re.compile(f"^{regex_path}$")
        self.routes.append(RouteContract(
            path_regex=pattern,
            method=method.upper(),
            path_template=path,
            response_template=response,
            latency_ms=latency_ms,
            failure_rate=failure_rate,
            is_stateful=False,
            action="custom",
        ))

    def resource(self, resource_name: str, base_path: Optional[str] = None):
        """Register list/create plus get/update/delete routes for one collection."""
        base = base_path or f"/api/v1/{resource_name}"
        list_pat = re.compile(f"^{base}/?$")
        item_pat = re.compile(f"^{base}/(?P<id>[^/]+)/?$")
        item_template = f"{base}/{{id}}"

        self.routes.append(RouteContract(list_pat, "GET", path_template=base, stateful_resource=resource_name, is_stateful=True, action="list"))
        self.routes.append(RouteContract(list_pat, "POST", path_template=base, stateful_resource=resource_name, is_stateful=True, action="create"))
        self.routes.append(RouteContract(item_pat, "GET", path_template=item_template, stateful_resource=resource_name, is_stateful=True, action="get"))
        self.routes.append(RouteContract(item_pat, "PUT", path_template=item_template, stateful_resource=resource_name, is_stateful=True, action="update"))
        self.routes.append(RouteContract(item_pat, "PATCH", path_template=item_template, stateful_resource=resource_name, is_stateful=True, action="update"))
        self.routes.append(RouteContract(item_pat, "DELETE", path_template=item_template, stateful_resource=resource_name, is_stateful=True, action="delete"))

    def seed(self, resource_name: str, count: int, generator: Callable[[], Dict[str, Any]]):
        """Insert ``count`` records from ``generator`` into this instance's store."""
        for _ in range(count):
            self.store.create_item(resource_name, generator())

    def record_request(self, method: str, path: str, status: int, latency_ms: float):
        self.telemetry.record(latency_ms, failed=status >= 400)
        entry = {
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": round(latency_ms, 2),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self.log_lock:
            self.request_log.append(entry)
            if len(self.request_log) > 200:
                self.request_log.pop(0)

    def recent_logs(self) -> List[Dict[str, Any]]:
        with self.log_lock:
            return list(self.request_log[-50:])

    def export_spec(self) -> Dict[str, Any]:
        """OpenAPI 3.0 path list for the routes on this instance.

        Built-in ``/_mockforge/`` inspection routes are included. Response
        schemas are not.
        """
        summaries = {
            "list": "List {name}",
            "create": "Create {name}",
            "get": "Get one {name} item",
            "update": "Update {name}",
            "delete": "Delete {name}",
        }
        paths: Dict[str, Dict[str, Any]] = {}
        for route in self.routes:
            path = route.path_template or route.path_regex.pattern
            operations = paths.setdefault(path, {})
            key = route.method.lower()
            if key in operations:
                continue
            if route.action == "custom":
                summary = f"{route.method} {path}"
            else:
                summary = summaries[route.action].format(name=route.stateful_resource or "resource")
            operation: Dict[str, Any] = {"summary": summary}
            if route.failure_rate > 0:
                operation["description"] = f"Returns HTTP 500 with probability {route.failure_rate}."
            operations[key] = operation

        for path, summary in (
            ("/_mockforge/metrics", "Uptime, request count, failures, and average latency"),
            ("/_mockforge/routes", "Routes registered on this server"),
            ("/_mockforge/logs", "Recent requests, including unmatched 404s and injected 500s"),
        ):
            paths.setdefault(path, {}).setdefault("get", {"summary": summary})

        return {
            "openapi": "3.0.0",
            "info": {
                "title": "MockForge",
                "version": "1.0.0",
                "description": (
                    "Generated from the routes registered on this MockForge instance, "
                    "plus the built-in /_mockforge/ inspection routes. "
                    "Response schemas are not included."
                ),
            },
            "paths": paths,
        }

    def build_server(self) -> ThreadedHTTPServer:
        server = ThreadedHTTPServer((self.host, self.port), MockForgeHandler)
        server.forge = self  # type: ignore[attr-defined]
        self.port = server.server_address[1]
        return server

    def serve(self):
        """Serve until Ctrl+C. ``shutdown()`` is not called on this thread."""
        server = self.build_server()
        print("=" * 70, flush=True)
        print(f"🚀 MOCKFORGE SERVER ACTIVE: http://{self.host}:{self.port}", flush=True)
        print(f"📡 Registered Contract Endpoints: {len(self.routes)}", flush=True)
        print(f"📊 Live Telemetry Dashboard   : http://{self.host}:{self.port}/_mockforge/metrics", flush=True)
        print(f"🔍 Route Manifest             : http://{self.host}:{self.port}/_mockforge/routes", flush=True)
        print("=" * 70, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down MockForge.", flush=True)
        finally:
            server.server_close()


# =====================================================================
# 6. DEMO AND CLI
# =====================================================================

def build_enterprise_demo() -> MockForge:
    """Demo server used by ``python3 main.py start`` and ``export-spec``."""
    forge = MockForge(port=8080)

    forge.resource("customers", base_path="/api/v1/customers")
    forge.seed("customers", 5, lambda: {
        "name": DataForge.name(),
        "email": DataForge.email(),
        "company": DataForge.company(),
        "account_tier": random.choice(["Starter", "Professional", "Enterprise"]),
        "status": "ACTIVE"
    })

    forge.resource("orders", base_path="/api/v1/orders")
    forge.seed("orders", 3, lambda: {
        "transaction_code": f"TXN-{random.randint(100000, 999999)}",
        "amount": DataForge.currency(50, 2500),
        "status": "COMPLETED"
    })

    forge.mock(
        path="/api/v1/analytics/realtime",
        method="GET",
        response={
            "session_id": "{{uuid}}",
            "server_time": "{{iso_date}}",
            "active_users": "{{int:150:850}}",
            "mrr": "{{currency}}",
            "status": "HEALTHY",
            "infrastructure": {
                "datacenter": "us-east-1",
                "load_factor": "{{int:10:95}}%"
            }
        },
        latency_ms=(15, 45)
    )

    forge.mock(
        path="/api/v1/gateway/charge",
        method="POST",
        response={
            "charge_id": "{{uuid}}",
            "status": "SETTLED",
            "verified": True
        },
        latency_ms=(150, 450),
        failure_rate=0.25
    )

    return forge


def main():
    parser = argparse.ArgumentParser(description="MockForge: mock HTTP API for local development")
    parser.add_argument("command", choices=["start", "export-spec"], help="Action to execute")
    parser.add_argument("--port", type=int, default=8080, help="Server port (Default: 8080)")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host interface (Default: 127.0.0.1). Use 0.0.0.0 to listen on all interfaces.",
    )

    args = parser.parse_args()

    if args.command == "start":
        forge = build_enterprise_demo()
        forge.port = args.port
        forge.host = args.host
        forge.serve()
    elif args.command == "export-spec":
        forge = build_enterprise_demo()
        print(json.dumps(forge.export_spec(), indent=2))


if __name__ == "__main__":
    main()

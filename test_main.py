#!/usr/bin/env python3
"""Standard-library tests for MockForge."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

import main
from main import DataForge, MockForge, TemplateError

ROOT = os.path.dirname(os.path.abspath(__file__))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._servers = []

    def serve(self, forge):
        server = forge.build_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return forge

    def tearDown(self):
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def request(self, forge, method, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {}
        if data is not None and raw is None:
            headers["Content-Type"] = "application/json"
        url = f"http://127.0.0.1:{forge.port}{path}"
        last_error = None
        for _ in range(20):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = resp.read()
                    return resp.status, json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                return exc.code, json.loads(payload) if payload else None
            except urllib.error.URLError as exc:
                last_error = exc
                time.sleep(0.05)
        raise AssertionError(f"request failed: {last_error}")


class TestMacros(unittest.TestCase):
    def test_whole_int_is_a_number_and_embedded_int_is_text(self):
        rendered = DataForge.synthesize_template({
            "active_users": "{{int:7:7}}",
            "label": "n={{int:7:7}}",
            "pair": "{{int:1:1}}-{{int:2:2}}",
            "ok": True,
        })
        self.assertEqual(rendered["active_users"], 7)
        self.assertIsInstance(rendered["active_users"], int)
        self.assertEqual(rendered["label"], "n=7")
        self.assertEqual(rendered["pair"], "1-2")
        self.assertIs(rendered["ok"], True)

    def test_repeated_macros_are_independent(self):
        rendered = DataForge.synthesize_template("{{uuid}} {{uuid}}")
        first, second = rendered.split()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 36)

    def test_min_greater_than_max_raises_template_error(self):
        with self.assertRaises(TemplateError) as caught:
            DataForge.synthesize_template("{{int:9:1}}")
        self.assertIn("greater than", str(caught.exception))

    def test_currency_stays_text(self):
        rendered = DataForge.synthesize_template("{{currency}}")
        self.assertIsInstance(rendered, str)

    def test_email_keeps_letters_only(self):
        self.assertTrue(DataForge.email("Casey O'Connor").startswith("caseyoconnor@"))


class TestInstanceState(ApiTest):
    def test_instances_do_not_share_store_or_routes(self):
        first = MockForge(port=0)
        second = MockForge(port=0)
        self.assertIsNot(first.store, second.store)
        first.resource("widgets")
        second.resource("widgets")
        first.seed("widgets", 1, lambda: {"id": "only-first", "name": "A"})
        first.mock("/only-first", response={"ok": True}, latency_ms=(0, 0))
        second.mock("/only-second", response={"ok": True}, latency_ms=(0, 0))
        self.serve(first)
        self.serve(second)

        status, body = self.request(first, "GET", "/api/v1/widgets/only-first")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "A")
        status, body = self.request(second, "GET", "/api/v1/widgets/only-first")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NotFound")
        self.assertEqual(self.request(second, "GET", "/only-first")[0], 404)
        self.assertEqual(self.request(first, "GET", "/only-second")[0], 404)
        self.assertEqual(self.request(second, "GET", "/only-second")[1]["ok"], True)


class TestHttp(ApiTest):
    def test_crud_merge_delete_and_explicit_id(self):
        forge = MockForge(port=0)
        forge.resource("widgets")
        self.serve(forge)

        status, body = self.request(forge, "GET", "/api/v1/widgets")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"data": [], "total": 0})

        status, created = self.request(forge, "POST", "/api/v1/widgets", {"name": "Ada", "sku": "W-9"})
        self.assertEqual(status, 201)
        item_id = created["id"]
        self.assertEqual(created["name"], "Ada")
        self.assertIn("_created_at", created)

        status, body = self.request(forge, "PUT", f"/api/v1/widgets/{item_id}", {"name": "Ada Lovelace"})
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Ada Lovelace")
        self.assertEqual(body["sku"], "W-9")
        self.assertIn("_updated_at", body)

        status, body = self.request(forge, "PATCH", f"/api/v1/widgets/{item_id}", {"sku": "W-10"})
        self.assertEqual(body["sku"], "W-10")
        self.assertEqual(body["name"], "Ada Lovelace")

        status, body = self.request(forge, "DELETE", f"/api/v1/widgets/{item_id}")
        self.assertEqual(status, 204)
        self.assertIsNone(body)
        status, missing = self.request(forge, "GET", f"/api/v1/widgets/{item_id}")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "NotFound")

        status, kept = self.request(forge, "POST", "/api/v1/widgets", {"id": "ada-1", "name": "Ada"})
        self.assertEqual(status, 201)
        self.assertEqual(kept["id"], "ada-1")

    def test_chaos_and_unmatched_paths_are_logged(self):
        forge = MockForge(port=0)
        forge.mock(
            "/flaky",
            method="POST",
            response={"ok": True},
            latency_ms=(0, 0),
            failure_rate=1.0,
        )
        self.serve(forge)

        status, body = self.request(forge, "POST", "/flaky", {"amount": 1})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "InjectedChaosFailure")

        status, missing = self.request(forge, "GET", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "Contract Unmatched")

        _status, logs = self.request(forge, "GET", "/_mockforge/logs")
        recorded = [(entry["method"], entry["path"], entry["status"]) for entry in logs["recent_logs"]]
        self.assertIn(("POST", "/flaky", 500), recorded)
        self.assertIn(("GET", "/missing", 404), recorded)

        _status, metrics = self.request(forge, "GET", "/_mockforge/metrics")
        telemetry = metrics["telemetry"]
        self.assertEqual(set(telemetry), {
            "uptime_seconds",
            "total_requests",
            "total_failures",
            "avg_latency_ms",
        })
        self.assertEqual(telemetry["total_requests"], 2)
        self.assertEqual(telemetry["total_failures"], 2)
        self.assertNotIn("velocity_roi_multiplier", telemetry)
        self.assertNotIn("economic_value_saved_usd", telemetry)

    def test_template_error_returns_json_and_server_stays_up(self):
        forge = MockForge(port=0)
        forge.mock("/bad", response={"n": "{{int:5:1}}"}, latency_ms=(0, 0))
        forge.mock("/ok", response={"n": "{{int:4:4}}", "ok": True}, latency_ms=(0, 0))
        self.serve(forge)

        status, body = self.request(forge, "GET", "/bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "TemplateError")
        self.assertIn("greater than", body["message"])

        status, body = self.request(forge, "GET", "/ok")
        self.assertEqual(status, 200)
        self.assertEqual(body["n"], 4)
        self.assertIsInstance(body["n"], int)
        self.assertIs(body["ok"], True)

    def test_zero_failure_rate_returns_template(self):
        forge = MockForge(port=0)
        forge.mock("/ok", method="POST", response={"verified": True}, latency_ms=(0, 0), failure_rate=0.0)
        self.serve(forge)
        status, body = self.request(forge, "POST", "/ok", {"amount": 19.99})
        self.assertEqual(status, 200)
        self.assertIs(body["verified"], True)


class TestExport(unittest.TestCase):
    def test_demo_spec_lists_live_routes(self):
        spec = main.build_enterprise_demo().export_spec()
        self.assertNotIn("auto-generated", spec["info"]["description"].lower())
        self.assertIn("registered", spec["info"]["description"].lower())
        paths = spec["paths"]
        self.assertEqual(set(paths["/api/v1/customers"]), {"get", "post"})
        self.assertEqual(set(paths["/api/v1/customers/{id}"]), {"get", "put", "patch", "delete"})
        self.assertEqual(set(paths["/api/v1/orders"]), {"get", "post"})
        self.assertEqual(set(paths["/api/v1/orders/{id}"]), {"get", "put", "patch", "delete"})
        self.assertIn("get", paths["/api/v1/analytics/realtime"])
        self.assertIn("post", paths["/api/v1/gateway/charge"])
        self.assertIn("0.25", paths["/api/v1/gateway/charge"]["post"]["description"])
        for path in ("/_mockforge/metrics", "/_mockforge/routes", "/_mockforge/logs"):
            self.assertIn(path, paths)
        self.assertNotIn("12.8x", json.dumps(spec))

    def test_spec_follows_routes_registered_in_code(self):
        forge = MockForge()
        forge.mock("/api/v1/health", method="GET", response={"ok": True})
        paths = forge.export_spec()["paths"]
        self.assertIn("/api/v1/health", paths)
        self.assertNotIn("/api/v1/customers", paths)

    def test_cli_export_spec_matches_the_demo(self):
        completed = subprocess.run(
            [sys.executable, "main.py", "export-spec", "--port", "1", "--host", "0.0.0.0"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        spec = json.loads(completed.stdout)
        self.assertIn("delete", spec["paths"]["/api/v1/orders/{id}"])
        self.assertIn("/_mockforge/metrics", spec["paths"])


class TestCli(unittest.TestCase):
    def test_default_host_is_loopback(self):
        self.assertEqual(MockForge().host, "127.0.0.1")
        completed = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Default: 127.0.0.1", completed.stdout)
        self.assertIn("0.0.0.0", completed.stdout)

    def test_sigint_shuts_the_server_down(self):
        proc = subprocess.Popen(
            [sys.executable, "main.py", "start", "--port", "0"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        banner = ""
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                line = proc.stdout.readline()
                if line == "" and proc.poll() is not None:
                    break
                banner += line
                if "MOCKFORGE SERVER ACTIVE" in line:
                    break
            else:
                self.fail("server did not start:\n" + banner)
            self.assertIn("MOCKFORGE SERVER ACTIVE", banner)
            port = int(banner.split("http://127.0.0.1:", 1)[1].split()[0])
            proc.send_signal(signal.SIGINT)
            code = proc.wait(timeout=3)
            self.assertEqual(code, 0, banner)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()

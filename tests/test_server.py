from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import http.cookiejar
import http.client
import urllib.error
import urllib.parse
import urllib.request

import server


class ValidationTests(unittest.TestCase):
    def test_demo_document_is_complete_and_editable(self) -> None:
        demo = server.demo_document()
        self.assertGreaterEqual(len(demo["nodes"]), 6)
        self.assertGreaterEqual(len(demo["links"]), 5)
        self.assertEqual(demo["settings"]["subnet"], "192.168.1.0/24")
        self.assertIn("name", demo["settings"])

    def test_node_normalizes_addresses_and_tags(self) -> None:
        node = server.validate_node(
            {
                "name": " Laptop ",
                "kind": "laptop",
                "ip": "2001:0db8::1",
                "mac": "AA-BB-CC-DD-EE-FF",
                "hostname": "Desk.Example.",
                "management_url": "HTTPS://Desk.Example:8443/admin",
                "winbox_enabled": True,
                "tags": ["Office", "office"],
            }
        )
        self.assertEqual(node["name"], "Laptop")
        self.assertEqual(node["ip"], "2001:db8::1")
        self.assertEqual(node["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(node["hostname"], "desk.example")
        self.assertEqual(node["management_url"], "https://Desk.Example:8443/admin")
        self.assertTrue(node["winbox_enabled"])
        self.assertEqual(node["tags"], ["Office"])

    def test_management_url_rejects_credentials_and_unsafe_schemes(self) -> None:
        for invalid in (
            "javascript:alert(1)",
            "ssh://192.168.1.1",
            "https://admin:secret@192.168.1.1",
            "https://example.test/bad path",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(server.APIError):
                server.validate_node({"name": "Router", "management_url": invalid})

    def test_discovery_network_is_strict_small_and_private(self) -> None:
        self.assertEqual(
            str(server.validate_discovery_network("10.20.30.0/24")),
            "10.20.30.0/24",
        )
        for invalid in (
            "8.8.8.0/24",
            "192.168.1.7/24",
            "192.168.0.0/16",
            "not-a-network",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(server.APIError):
                server.validate_discovery_network(invalid)

    def test_neighbor_discovery_filters_to_requested_cidr(self) -> None:
        output = "\n".join(
            (
                "192.168.50.1 dev eth0 lladdr AA:BB:CC:DD:EE:01 REACHABLE",
                "192.168.51.1 dev eth0 lladdr AA:BB:CC:DD:EE:02 STALE",
            )
        )

        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout=output, stderr="")

        result = server.discover_network(
            "192.168.50.0/24",
            runner=runner,
            which=lambda name: f"/usr/bin/{name}" if name == "ip" else None,
        )
        self.assertEqual(result["sources"], ["ip-neigh"])
        self.assertEqual([item["ip"] for item in result["devices"]], ["192.168.50.1"])
        self.assertEqual(result["devices"][0]["mac"], "aa:bb:cc:dd:ee:01")

    def test_ipv6_nmap_discovery_enables_ipv6_mode(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments, **kwargs):
            calls.append(arguments)
            output = "<nmaprun></nmaprun>" if arguments[0].endswith("nmap") else ""
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

        server.discover_network(
            "fd00::/120",
            use_nmap=True,
            runner=runner,
            which=lambda name: f"/usr/bin/{name}",
        )
        nmap_call = next(call for call in calls if call[0].endswith("nmap"))
        self.assertIn("-6", nmap_call)
        self.assertEqual(nmap_call[-1], "fd00::/120")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "networkmap.sqlite3"
        self.events: list[dict] = []
        self.store = server.StateStore(self.database, on_change=self.events.append)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_run_and_persistence(self) -> None:
        state = self.store.get_state()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(state["nodes"]), 6)
        self.store.create_node({"id": "kept", "name": "Kept node"})
        reopened = server.StateStore(self.database)
        state = reopened.get_state()
        self.assertEqual(state["revision"], 2)
        self.assertIn("kept", {item["id"] for item in state["nodes"]})

    def test_new_storage_is_private(self) -> None:
        private_database = Path(self.temporary.name) / "nested-data" / "state.sqlite3"
        server.StateStore(private_database)
        self.assertEqual(
            stat.S_IMODE(os.stat(private_database.parent).st_mode), 0o700
        )
        self.assertEqual(stat.S_IMODE(os.stat(private_database).st_mode), 0o600)

    def test_existing_data_directory_permissions_are_not_rewritten(self) -> None:
        shared_directory = Path(self.temporary.name) / "existing-data"
        shared_directory.mkdir()
        os.chmod(shared_directory, 0o755)

        database = shared_directory / "state.sqlite3"
        server.StateStore(database)

        self.assertEqual(stat.S_IMODE(os.stat(shared_directory).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(database).st_mode), 0o600)

    def test_crud_link_and_node_delete_cascade(self) -> None:
        state = self.store.create_node({"id": "a", "name": "A"})
        state = self.store.create_node({"id": "b", "name": "B"})
        state = self.store.create_link(
            {
                "id": "a-b",
                "source": "a",
                "target": "b",
                "kind": "fiber",
                "bandwidth_mbps": 10_000,
            }
        )
        self.assertIn("a-b", {item["id"] for item in state["links"]})
        state = self.store.update_link("a-b", {"status": "degraded"})
        link = next(item for item in state["links"] if item["id"] == "a-b")
        self.assertEqual(link["status"], "degraded")
        state = self.store.delete_node("a")
        self.assertNotIn("a-b", {item["id"] for item in state["links"]})

    def test_revision_conflict_is_atomic(self) -> None:
        revision = self.store.get_state()["revision"]
        self.store.update_settings({"name": "Changed"}, revision)
        with self.assertRaises(server.APIError) as raised:
            self.store.create_node({"name": "Too late"}, revision)
        self.assertEqual(raised.exception.code, "revision_conflict")
        state = self.store.get_state()
        self.assertEqual(state["revision"], revision + 1)
        self.assertNotIn("Too late", {item["name"] for item in state["nodes"]})

    def test_replace_with_empty_document_and_notification(self) -> None:
        previous = self.store.get_state()["revision"]
        state = self.store.replace_state(
            {
                "revision": 999,
                "updated_at": "old",
                "nodes": [],
                "links": [],
                "settings": {
                    "name": "Empty",
                    "description": "",
                    "subnet": "",
                    "refresh_interval": 0,
                    "show_link_labels": False,
                    "compact_labels": True,
                    "theme": "system",
                },
            }
        )
        self.assertEqual(state["revision"], previous + 1)
        self.assertEqual(state["nodes"], [])
        self.assertEqual(state["links"], [])
        self.assertEqual(self.events[-1]["revision"], previous + 1)


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.static = root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>NetworkMap test</h1>", encoding="utf-8")
        (self.static / "styles.css").write_text("body{}", encoding="utf-8")
        self.running = server.ServerThread(
            port=0,
            data_dir=root / "data",
            static_dir=self.static,
        ).start()

    def tearDown(self) -> None:
        self.running.stop()
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        value=None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, object]:
        body = None
        request_headers = dict(headers or {})
        if value is not None:
            body = json.dumps(value).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.running.url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, payload, response.headers

    def test_health_state_and_static_prefix(self) -> None:
        status, health, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        status, state, headers = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], f'"{state["revision"]}"')
        with urllib.request.urlopen(self.running.url + "/", timeout=3) as response:
            self.assertIn(b"NetworkMap test", response.read())
        with urllib.request.urlopen(
            self.running.url + "/static/styles.css", timeout=3
        ) as response:
            self.assertEqual(response.read(), b"body{}")
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_node_crud_validation_and_revision_guard(self) -> None:
        _, before, _ = self.request("/api/state")
        status, created, _ = self.request(
            "/api/nodes",
            method="POST",
            value={"id": "http-node", "name": "HTTP node", "kind": "camera"},
            headers={"If-Match": f'"{before["revision"]}"'},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["revision"], before["revision"] + 1)
        status, conflict, _ = self.request(
            "/api/nodes/http-node",
            method="PATCH",
            value={"status": "online"},
            headers={"If-Match": str(before["revision"])},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        status, invalid, _ = self.request(
            "/api/nodes/http-node",
            method="PATCH",
            value={"ip": "999.2.3.4"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "validation_error")

    def test_settings_export_and_import(self) -> None:
        status, state, _ = self.request(
            "/api/settings",
            method="PATCH",
            value={
                "name": "Office",
                "description": "Test map",
                "subnet": "10.10.5.0/24",
                "refresh_interval": 60,
                "show_link_labels": False,
                "compact_labels": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["settings"]["name"], "Office")
        status, exported, headers = self.request("/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        exported["nodes"] = []
        exported["links"] = []
        status, imported, _ = self.request(
            "/api/import", method="POST", value=exported
        )
        self.assertEqual(status, 200)
        self.assertEqual(imported["nodes"], [])

    def test_public_or_oversized_discovery_is_rejected(self) -> None:
        for cidr in ("8.8.8.0/24", "10.0.0.0/8"):
            status, payload, _ = self.request(
                "/api/discovery", method="POST", value={"cidr": cidr}
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "validation_error")

    def test_tokenless_loopback_rejects_dns_rebinding_headers(self) -> None:
        status, payload, _ = self.request(
            "/api/health",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 421)
        self.assertEqual(payload["error"]["code"], "invalid_host")

        status, payload, _ = self.request(
            "/api/discovery",
            method="POST",
            value={"cidr": "192.168.1.0/24"},
            headers={"Origin": "http://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden_origin")

    def test_sse_announces_committed_revision(self) -> None:
        parsed = urllib.parse.urlsplit(self.running.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.request("GET", "/api/events")
            events = connection.getresponse()
            self.assertEqual(events.status, 200)
            ready_lines = [events.readline().decode("utf-8") for _ in range(4)]
            self.assertTrue(any(line == "event: ready\n" for line in ready_lines))

            _, before, _ = self.request("/api/state")
            status, changed, _ = self.request(
                "/api/nodes",
                method="POST",
                value={"id": "sse-node", "name": "SSE node"},
                headers={"If-Match": f'"{before["revision"]}"'},
            )
            self.assertEqual(status, 201)
            change_lines = [events.readline().decode("utf-8") for _ in range(4)]
            self.assertTrue(any(line == "event: change\n" for line in change_lines))
            payload_line = next(line for line in change_lines if line.startswith("data: "))
            payload = json.loads(payload_line[len("data: ") :])
            self.assertEqual(payload["revision"], changed["revision"])
        finally:
            connection.close()


class AuthenticationTests(unittest.TestCase):
    def test_token_guards_api_and_browser_bootstrap_sets_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("ok", encoding="utf-8")
            with server.ServerThread(
                port=0,
                data_dir=root / "data",
                static_dir=static,
                token="correct horse",
            ) as running:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(running.url + "/api/state", timeout=3)
                self.assertEqual(raised.exception.code, 401)
                request = urllib.request.Request(
                    running.url + "/api/state",
                    headers={"Authorization": "Bearer correct horse"},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)

                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None

                opener = urllib.request.build_opener(NoRedirect)
                with self.assertRaises(urllib.error.HTTPError) as redirected:
                    opener.open(running.url + "/?token=correct%20horse", timeout=3)
                self.assertEqual(redirected.exception.code, 303)
                self.assertIn(
                    "HttpOnly", redirected.exception.headers.get("Set-Cookie", "")
                )
                self.assertNotIn("token", redirected.exception.headers["Location"])

                jar = http.cookiejar.CookieJar()
                cookie_opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(jar)
                )
                session_request = urllib.request.Request(
                    running.url + "/api/session",
                    data=b"",
                    method="POST",
                    headers={"Authorization": "Bearer correct horse"},
                )
                with cookie_opener.open(session_request, timeout=3) as response:
                    session = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(session["authenticated"])
                self.assertTrue(
                    any(cookie.name == "networkmap_token" for cookie in jar)
                )
                with cookie_opener.open(running.url + "/api/state", timeout=3) as response:
                    self.assertEqual(response.status, 200)

                self.assertIn("/#token=correct%20horse", running.browser_url)
                self.assertNotIn("?token=", running.browser_url)


class SecurityHelperTests(unittest.TestCase):
    def test_sensitive_query_values_are_redacted_from_logs(self) -> None:
        safe = server.NetworkMapRequestHandler._redact_request_target(
            "/api/events?token=super-secret&view=map"
        )
        self.assertNotIn("super-secret", safe)
        self.assertIn("view=map", safe)


if __name__ == "__main__":
    unittest.main()

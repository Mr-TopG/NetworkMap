from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
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

    def test_area_defaults_and_state_backward_compatibility(self) -> None:
        area = server.validate_area({"label": " Office ", "vlan_id": 20})
        self.assertEqual(area["label"], "Office")
        self.assertEqual(area["shape"], "rectangle")
        self.assertEqual(area["vlan_id"], 20)
        self.assertEqual(area["width"], 320)
        document = server.demo_document()
        document.pop("areas")
        self.assertEqual(server.validate_state_document(document)["areas"], [])
        for link in document["links"]:
            link.pop("duplex")
        self.assertTrue(all(
            link["duplex"] == "unknown"
            for link in server.validate_state_document(document)["links"]
        ))

    def test_area_rejects_invalid_geometry_labels_and_vlan(self) -> None:
        invalid_values = {
            "x": [True, "0", float("nan"), float("inf"), -1_000_001],
            "y": [False, 1_000_001],
            "width": [0, 39, 100_001, float("inf")],
            "height": [True, 39, 100_001],
            "label": ["", " " * 3, "x" * 121, None],
            "shape": ["triangle", None],
            "color": ["red", "url(https://example.test)", None],
            "vlan_id": [True, False, 0, 4095, 1.5, "20"],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(server.APIError):
                    server.validate_area({field: value})
        area = server.validate_area({"id": "area-a"})
        with self.assertRaises(server.APIError):
            server.validate_area({"id": "area-b"}, partial=True, current=area)
        with self.assertRaises(server.APIError):
            server.validate_area({"script": "not allowed"})
        for vlan_id in (None, 1, 4094):
            self.assertEqual(server.validate_area({"vlan_id": vlan_id})["vlan_id"], vlan_id)

    def test_state_rejects_invalid_or_duplicate_areas(self) -> None:
        for areas in (None, {}, "bad", [{"id": "same"}, {"id": "same"}], [{}] * 1_001):
            with self.subTest(areas_type=type(areas).__name__), self.assertRaises(server.APIError):
                server.validate_state_document({**server.demo_document(), "areas": areas})

    def test_link_duplex_defaults_validation_and_legacy_patch(self) -> None:
        link = server.validate_link({"source": "a", "target": "b"})
        self.assertEqual(link["duplex"], "unknown")
        link.pop("duplex")
        patched = server.validate_link({"notes": "Legacy"}, partial=True, current=link)
        self.assertEqual(patched["duplex"], "unknown")
        for duplex in ("full", "half", "auto", "unknown"):
            self.assertEqual(
                server.validate_link({"duplex": duplex}, partial=True, current=link)["duplex"],
                duplex,
            )
        for duplex in (None, True, "", "simplex"):
            with self.subTest(duplex=duplex), self.assertRaises(server.APIError):
                server.validate_link({"duplex": duplex}, partial=True, current=link)

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
        self.assertEqual(state["api_version"], 1)
        uuid.UUID(state["instance_id"])
        instance_id = state["instance_id"]
        self.assertEqual(len(state["nodes"]), 6)
        self.store.create_node({"id": "kept", "name": "Kept node"})
        reopened = server.StateStore(self.database)
        state = reopened.get_state()
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["instance_id"], instance_id)
        self.assertIn("kept", {item["id"] for item in state["nodes"]})

    def test_legacy_metadata_is_migrated_without_a_revision_change(self) -> None:
        legacy_database = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.executescript(
                """
                CREATE TABLE metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE nodes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE links (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    target TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    data TEXT NOT NULL
                );
                CREATE TABLE settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    data TEXT NOT NULL
                );
                INSERT INTO metadata VALUES(1, 7, '2025-01-02T03:04:05.000Z');
                """
            )
            connection.execute(
                "INSERT INTO settings(singleton, data) VALUES(1, ?)",
                (json.dumps(server.DEFAULT_SETTINGS),),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = server.StateStore(legacy_database)
        state = migrated.get_state()
        self.assertEqual(state["revision"], 7)
        self.assertEqual(state["updated_at"], "2025-01-02T03:04:05.000Z")
        self.assertEqual(state["api_version"], 1)
        uuid.UUID(state["instance_id"])
        self.assertEqual(
            server.StateStore(legacy_database).get_state()["instance_id"],
            state["instance_id"],
        )

    def test_import_metadata_never_replaces_database_identity(self) -> None:
        before = self.store.get_state()
        imported = deepcopy(before)
        imported["instance_id"] = str(uuid.uuid4())
        imported["api_version"] = 999
        imported["nodes"] = []
        imported["links"] = []

        result = self.store.replace_state(imported)

        self.assertEqual(result["instance_id"], before["instance_id"])
        self.assertEqual(result["api_version"], 1)
        self.assertEqual(result["nodes"], [])

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

    def test_area_crud_revisions_persistence_and_duplicate_rejection(self) -> None:
        revision = self.store.get_state()["revision"]
        state = self.store.create_area({"id": "office", "label": "Office", "vlan_id": 20}, revision)
        self.assertEqual(state["revision"], revision + 1)
        self.assertEqual(self.store.get_area("office")["vlan_id"], 20)
        self.assertEqual(server.StateStore(self.database).get_area("office")["label"], "Office")
        with self.assertRaises(server.APIError) as raised:
            self.store.create_area({"id": "office"})
        self.assertEqual(raised.exception.code, "already_exists")
        with self.assertRaises(server.APIError) as raised:
            self.store.update_area("office", {"x": 100}, revision)
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(self.store.get_area("office")["x"], 0)
        state = self.store.update_area("office", {"shape": "ellipse", "x": -40}, state["revision"])
        self.assertEqual(state["revision"], revision + 2)
        self.assertEqual(state["areas"][0]["shape"], "ellipse")
        self.assertEqual(state["areas"][0]["label"], "Office")
        self.assertEqual(self.events[-1]["revision"], state["revision"])
        with self.assertRaises(server.APIError):
            self.store.delete_area("office", revision)
        state = self.store.delete_area("office", state["revision"])
        self.assertEqual(state["areas"], [])
        self.assertEqual(state["revision"], revision + 3)
        for action in (
            lambda: self.store.get_area("office"),
            lambda: self.store.update_area("office", {"label": "Missing"}),
            lambda: self.store.delete_area("office"),
        ):
            with self.assertRaises(server.APIError) as raised:
                action()
            self.assertEqual(raised.exception.code, "not_found")

    def test_area_limit_is_enforced_on_create(self) -> None:
        document = self.store.get_state()
        document["areas"] = [{"id": f"zone-{index}"} for index in range(server.MAX_AREAS)]
        self.store.replace_state(document)
        before = self.store.get_state()
        with self.assertRaises(server.APIError):
            self.store.create_area({"id": "too-many"})
        self.assertEqual(self.store.get_state(), before)

    def test_areas_and_duplex_roundtrip_import_and_reset(self) -> None:
        state = self.store.create_area({"id": "vlan-30", "label": "Guest", "shape": "ellipse", "vlan_id": 30})
        link_id = state["links"][0]["id"]
        saved = self.store.update_link(link_id, {"duplex": "full", "bandwidth_mbps": 2500})
        self.store.reset_demo()
        self.assertEqual(self.store.get_state()["areas"], [])
        restored = self.store.replace_state(json.loads(json.dumps(saved)))
        self.assertEqual(restored["areas"], saved["areas"])
        self.assertEqual(restored["links"], saved["links"])
        legacy = deepcopy(saved)
        legacy.pop("areas")
        self.assertEqual(self.store.replace_state(legacy)["areas"], [])

    def test_invalid_area_import_is_atomic(self) -> None:
        before = self.store.get_state()
        document = deepcopy(before)
        document["areas"] = [{"width": -1}]
        with self.assertRaises(server.APIError):
            self.store.replace_state(document)
        self.assertEqual(self.store.get_state(), before)

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
        self.data_dir = root / "data"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>NetworkMap test</h1>", encoding="utf-8")
        (self.static / "styles.css").write_text("body{}", encoding="utf-8")
        self.running = server.ServerThread(
            port=0,
            data_dir=self.data_dir,
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

    def write_sync_status(self, **changes) -> dict:
        metadata = self.running.server.store.get_metadata()
        status = {
            "schema_version": 1,
            "session_id": str(uuid.uuid4()),
            "local_instance_id": metadata["instance_id"],
            "mode": "native-sync",
            "state": "synced",
            "remote_url": "https://networkmap.example.test",
            "message": "Up to date",
            "pending_changes": 0,
            "last_sync_at": "2026-09-06T12:00:00.000Z",
            "can_resolve": False,
            "heartbeat": time.time(),
            "local_revision": metadata["revision"],
            "remote_revision": metadata["revision"],
            "remote_instance_id": str(uuid.uuid4()),
            "token": "must-never-leave-the-status-file",
        }
        status.update(changes)
        path = self.data_dir / server.SYNC_STATUS_FILENAME
        path.write_text(json.dumps(status), encoding="utf-8")
        os.chmod(path, 0o600)
        return status

    def test_health_state_and_static_prefix(self) -> None:
        status, health, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["version"], "0.2.1")
        self.assertEqual(health["api_version"], 1)
        uuid.UUID(health["instance_id"])
        status, state, headers = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["instance_id"], health["instance_id"])
        self.assertEqual(state["api_version"], health["api_version"])
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

    def test_area_http_crud_and_revision_guard(self) -> None:
        status, before, _ = self.request("/api/areas")
        self.assertEqual(status, 200)
        self.assertEqual(before["areas"], [])
        status, created, _ = self.request(
            "/api/areas", method="POST",
            value={"id": "guest", "label": "Guest VLAN", "vlan_id": 40},
            headers={"If-Match": str(before["revision"])},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["revision"], before["revision"] + 1)
        status, item, _ = self.request("/api/areas/guest")
        self.assertEqual(status, 200)
        self.assertEqual(item["area"]["vlan_id"], 40)
        status, conflict, _ = self.request(
            "/api/areas/guest", method="PATCH", value={"x": 100},
            headers={"If-Match": str(before["revision"])},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        status, changed, _ = self.request(
            "/api/areas/guest", method="PATCH", value={"shape": "ellipse"},
            headers={"If-Match": str(created["revision"])},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["areas"][0]["shape"], "ellipse")
        status, _, _ = self.request("/api/areas/guest", method="PATCH", value={"width": 0})
        self.assertEqual(status, 400)
        status, deleted, _ = self.request(
            "/api/areas/guest", method="DELETE",
            headers={"If-Match": str(changed["revision"])},
        )
        self.assertEqual(status, 200)
        self.assertEqual(deleted["areas"], [])
        self.assertEqual(self.request("/api/areas/guest")[0], 404)

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

    def test_sync_status_uses_safe_default_for_missing_malformed_and_stale_files(self) -> None:
        status, payload, _ = self.request("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["mode"], "server")
        self.assertEqual(payload["state"], "hosted")

        status_path = self.data_dir / server.SYNC_STATUS_FILENAME
        status_path.write_text("{not json", encoding="utf-8")
        status, malformed, _ = self.request("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(malformed["available"])

        self.write_sync_status(heartbeat=time.time() - 61)
        status, stale, _ = self.request("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(stale["available"])

        self.write_sync_status(local_instance_id=str(uuid.uuid4()))
        status, wrong_database, _ = self.request("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(wrong_database["available"])

    def test_sync_status_returns_only_allowlisted_active_native_fields(self) -> None:
        decision_id = "a" * 64
        written = self.write_sync_status(
            state="conflict",
            pending_changes=3,
            can_resolve=True,
            decision_id=decision_id,
        )

        status, payload, _ = self.request("/api/sync/status")

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["mode"], "native-sync")
        self.assertEqual(payload["state"], "conflict")
        self.assertEqual(payload["pending_changes"], 3)
        self.assertTrue(payload["can_resolve"])
        self.assertEqual(payload["decision_id"], decision_id)
        self.assertEqual(payload["session_id"], written["session_id"])
        self.assertNotIn("token", payload)
        self.assertNotIn("must-never", json.dumps(payload))

    def test_sync_action_requires_active_native_session_and_writes_private_command(self) -> None:
        status, unavailable, _ = self.request(
            "/api/sync/actions", method="POST", value={"action": "sync-now"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(unavailable["error"]["code"], "sync_unavailable")

        decision_id = "b" * 64
        active = self.write_sync_status(
            state="conflict", can_resolve=True, decision_id=decision_id
        )
        status, accepted, _ = self.request(
            "/api/sync/actions",
            method="POST",
            value={"action": "use-local", "decision_id": decision_id},
        )
        self.assertEqual(status, 202)
        self.assertTrue(accepted["accepted"])
        uuid.UUID(accepted["command_id"])

        command_path = self.data_dir / server.SYNC_COMMAND_FILENAME
        command = json.loads(command_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(command_path.stat().st_mode), 0o600)
        self.assertEqual(
            set(command),
            {
                "schema_version",
                "command_id",
                "action",
                "session_id",
                "local_instance_id",
                "decision_id",
                "requested_at",
            },
        )
        self.assertEqual(command["action"], "use-local")
        self.assertEqual(command["session_id"], active["session_id"])
        self.assertEqual(command["local_instance_id"], active["local_instance_id"])
        self.assertEqual(command["decision_id"], decision_id)
        self.assertNotIn("token", command)

    def test_sync_now_writes_an_empty_decision_binding(self) -> None:
        self.write_sync_status(state="synced", can_resolve=False)

        status, accepted, _ = self.request(
            "/api/sync/actions", method="POST", value={"action": "sync-now"}
        )

        self.assertEqual(status, 202)
        self.assertTrue(accepted["accepted"])
        command_path = self.data_dir / server.SYNC_COMMAND_FILENAME
        command = json.loads(command_path.read_text(encoding="utf-8"))
        self.assertEqual(command["action"], "sync-now")
        self.assertEqual(command["decision_id"], "")

    def test_sync_resolution_requires_current_resolvable_decision(self) -> None:
        cases = (
            ({"state": "synced", "can_resolve": True, "decision_id": "c" * 64}, "c" * 64),
            ({"state": "conflict", "can_resolve": False, "decision_id": "d" * 64}, "d" * 64),
            ({"state": "conflict", "can_resolve": True}, "e" * 64),
            ({"state": "conflict", "can_resolve": True, "decision_id": "D" * 64}, "d" * 64),
            ({"state": "conflict", "can_resolve": True, "decision_id": "e" * 63}, "e" * 64),
        )
        for changes, requested_decision in cases:
            with self.subTest(changes=changes):
                self.write_sync_status(**changes)
                status, payload, _ = self.request(
                    "/api/sync/actions",
                    method="POST",
                    value={
                        "action": "use-hosted",
                        "decision_id": requested_decision,
                    },
                )
                self.assertEqual(status, 409)
                self.assertEqual(
                    payload["error"]["code"], "sync_resolution_unavailable"
                )

        self.write_sync_status(
            state="conflict", can_resolve=True, decision_id="not-safe"
        )
        status, payload, _ = self.request("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertNotIn("decision_id", payload)

        self.write_sync_status(
            state="conflict", can_resolve=True, decision_id="f" * 64
        )
        for requested in (None, "", "F" * 64, "f" * 63):
            value = {"action": "use-hosted"}
            if requested is not None:
                value["decision_id"] = requested
            with self.subTest(requested=requested):
                status, payload, _ = self.request(
                    "/api/sync/actions", method="POST", value=value
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "validation_error")

    def test_sync_resolution_rejects_a_conflict_newer_than_the_ui_choice(self) -> None:
        displayed_decision = "1" * 64
        current_decision = "2" * 64
        self.write_sync_status(
            state="conflict", can_resolve=True, decision_id=current_decision
        )

        status, payload, _ = self.request(
            "/api/sync/actions",
            method="POST",
            value={
                "action": "use-local",
                "decision_id": displayed_decision,
            },
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "sync_resolution_unavailable")
        self.assertFalse((self.data_dir / server.SYNC_COMMAND_FILENAME).exists())

    def test_sync_action_rejects_invalid_or_extra_fields(self) -> None:
        self.write_sync_status()
        for value in (
            {"action": "delete-everything"},
            {"action": []},
            {"action": "sync-now", "token": "not-allowed"},
            {"action": "sync-now", "decision_id": "a" * 64},
            {},
        ):
            with self.subTest(value=value):
                status, payload, _ = self.request(
                    "/api/sync/actions", method="POST", value=value
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "validation_error")

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

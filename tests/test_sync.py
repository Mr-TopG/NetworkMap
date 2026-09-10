from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

import networkmap_sync as sync
import server


def changed_name(running: server.ServerThread, name: str) -> dict:
    store = running.server.store
    before = store.get_state()
    return store.update_settings({"name": name}, before["revision"])


class TopologyDigestTests(unittest.TestCase):
    def test_legacy_checkpoint_digest_survives_empty_area_and_duplex_defaults(self) -> None:
        legacy = server.demo_document()
        legacy.pop("areas")
        for link in legacy["links"]:
            link.pop("duplex")
        legacy["nodes"].sort(key=lambda item: item["id"])
        legacy["links"].sort(key=lambda item: item["id"])
        old_digest = hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(sync.topology_digest(legacy), old_digest)
        upgraded = server.validate_state_document(legacy)
        self.assertEqual(upgraded["areas"], [])
        self.assertEqual(sync.topology_digest(upgraded), old_digest)
        upgraded["links"][0]["duplex"] = "full"
        self.assertNotEqual(sync.topology_digest(upgraded), old_digest)

    def test_area_changes_participate_and_area_order_is_stable(self) -> None:
        original = server.demo_document()
        changed = deepcopy(original)
        changed["areas"] = [
            server.validate_area({"id": "b", "label": "Guest", "vlan_id": 20}),
            server.validate_area({"id": "a", "label": "Office", "vlan_id": 10}),
        ]
        self.assertNotEqual(sync.topology_digest(original), sync.topology_digest(changed))
        reversed_areas = deepcopy(changed)
        reversed_areas["areas"].reverse()
        self.assertEqual(sync.topology_digest(changed), sync.topology_digest(reversed_areas))
        canonical = sync.canonical_topology(changed)
        self.assertEqual([item["id"] for item in canonical["areas"]], ["a", "b"])
        canonical["areas"][0]["label"] = "Detached"
        self.assertEqual(changed["areas"][1]["label"], "Office")
        for field, value in (("x", 10), ("label", "New"), ("vlan_id", 30), ("width", 400)):
            edited = deepcopy(changed)
            edited["areas"][0][field] = value
            self.assertNotEqual(sync.topology_digest(changed), sync.topology_digest(edited))
        duplicate = deepcopy(changed)
        duplicate["areas"].append(deepcopy(duplicate["areas"][0]))
        with self.assertRaises(sync.SyncProtocolError):
            sync.topology_digest(duplicate)
        with self.assertRaises(sync.SyncProtocolError):
            sync.topology_digest({**original, "areas": None})

    def test_digest_ignores_transport_metadata_and_entity_order(self) -> None:
        first = server.demo_document()
        first.update(
            {
                "revision": 17,
                "updated_at": "yesterday",
                "exported_at": "today",
                "instance_id": "one-instance",
            }
        )
        second = deepcopy(first)
        second["revision"] = 999
        second["updated_at"] = "tomorrow"
        second["instance_id"] = "another-instance"
        second["nodes"].reverse()
        second["links"].reverse()
        self.assertEqual(sync.topology_digest(first), sync.topology_digest(second))

        second["settings"]["name"] = "Actually changed"
        self.assertNotEqual(sync.topology_digest(first), sync.topology_digest(second))

    def test_canonical_document_is_detached_and_rejects_duplicate_ids(self) -> None:
        source = server.demo_document()
        canonical = sync.canonical_topology(source)
        canonical["nodes"][0]["name"] = "Detached"
        self.assertNotEqual(source["nodes"][0]["name"], "Detached")

        duplicate = deepcopy(source)
        duplicate["nodes"].append(deepcopy(duplicate["nodes"][0]))
        with self.assertRaises(sync.SyncProtocolError):
            sync.topology_digest(duplicate)


class ProtocolIdentityTests(unittest.TestCase):
    def client_with_health(self, health: dict) -> sync.NetworkMapHTTPClient:
        client = sync.NetworkMapHTTPClient("http://networkmap.example.test")
        client._request = mock.Mock(return_value=health)
        return client

    def test_health_requires_supported_api_and_stable_uuid(self) -> None:
        valid = {
            "status": "ok",
            "revision": 1,
            "api_version": 1,
            "instance_id": "11111111-1111-4111-8111-111111111111",
        }
        self.assertEqual(
            self.client_with_health(valid).get_health()["instance_id"],
            valid["instance_id"],
        )
        for changes in (
            {"api_version": 2},
            {"api_version": True},
            {"api_version": None},
            {"instance_id": None},
            {"instance_id": "legacy-url-derived-id"},
        ):
            health = {**valid, **changes}
            with self.subTest(changes=changes), self.assertRaises(
                sync.SyncProtocolError
            ):
                self.client_with_health(health).get_health()

    def test_snapshot_rejects_mismatched_health_and_state_identity(self) -> None:
        local_id = "11111111-1111-4111-8111-111111111111"
        other_id = "22222222-2222-4222-8222-222222222222"
        client = SimpleClient("http://networkmap.example.test")
        health = {
            "status": "ok",
            "revision": 1,
            "api_version": 1,
            "instance_id": local_id,
        }
        state = {
            "revision": 1,
            "api_version": 1,
            "instance_id": other_id,
            **server.demo_document(),
        }
        with self.assertRaises(sync.SyncProtocolError):
            sync._Snapshot(client, health, state)

        state["instance_id"] = local_id
        state["api_version"] = True
        with self.assertRaises(sync.SyncProtocolError):
            sync._Snapshot(client, health, state)


class HTTPProxyPolicyTests(unittest.TestCase):
    @staticmethod
    def proxy_handlers(client: sync.NetworkMapHTTPClient) -> list:
        return [
            handler
            for handler in client._opener.handlers
            if isinstance(handler, sync.urllib.request.ProxyHandler)
        ]

    def test_local_sync_disables_environment_proxy_but_hosted_sync_keeps_it(self) -> None:
        environment_proxy = "http://proxy.example.test:3128"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync.urllib.request,
            "getproxies",
            return_value={"http": environment_proxy},
        ):
            worker = sync.SyncWorker(
                "http://127.0.0.1:8765",
                "https://networkmap.example.test",
                temporary,
            )

            local_handlers = self.proxy_handlers(worker.local_client)
            hosted_handlers = self.proxy_handlers(worker.remote_client)
            # CPython does not retain an explicit empty ProxyHandler in the
            # opener's active-handler list; either way, no proxy route exists.
            self.assertFalse(any(handler.proxies for handler in local_handlers))
            self.assertEqual(len(hosted_handlers), 1)
            self.assertEqual(hosted_handlers[0].proxies["http"], environment_proxy)


class SimpleClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


class SyncFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.files = sync.SyncFileStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_checkpoint_is_private_atomic_complete_and_secret_free(self) -> None:
        document = server.demo_document()
        checkpoint = self.files.save_checkpoint(
            remote_url="HTTPS://host.example:8443/mapper/",
            baseline_document=document,
            local_instance_id="11111111-1111-4111-8111-111111111111",
            remote_instance_id="22222222-2222-4222-8222-222222222222",
            local_revision=7,
            remote_revision=9,
            last_sync_at="2026-09-06T12:00:00.000Z",
        )
        self.assertEqual(checkpoint["remote_url"], "https://host.example:8443/mapper")
        self.assertEqual(checkpoint["baseline_digest"], sync.topology_digest(document))
        self.assertEqual(stat.S_IMODE(self.files.checkpoint_path.stat().st_mode), 0o600)
        self.assertNotIn("correct horse", self.files.checkpoint_path.read_text())
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))
        self.assertEqual(self.files.load_checkpoint(), checkpoint)

    def test_corrupt_or_tampered_checkpoint_is_rejected(self) -> None:
        self.files.save_checkpoint(
            remote_url="http://host.example",
            baseline_document=server.demo_document(),
            local_instance_id="local",
            remote_instance_id="remote",
            local_revision=1,
            remote_revision=1,
            last_sync_at="now",
        )
        value = json.loads(self.files.checkpoint_path.read_text(encoding="utf-8"))
        value["baseline_document"]["settings"]["name"] = "tampered"
        self.files.checkpoint_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(sync.SyncProtocolError):
            self.files.load_checkpoint()

    def test_commands_are_private_and_only_matching_commands_are_consumed(self) -> None:
        decision_id = "a" * 64
        self.files.write_command(
            "use-local",
            session_id="old-session",
            local_instance_id="local-id",
            decision_id=decision_id,
        )
        self.assertEqual(stat.S_IMODE(self.files.command_path.stat().st_mode), 0o600)
        self.assertIsNone(
            self.files.take_command(
                session_id="new-session", local_instance_id="local-id"
            )
        )
        self.assertFalse(self.files.command_path.exists())
        self.files.write_command(
            "use-local",
            session_id="old-session",
            local_instance_id="local-id",
            decision_id=decision_id,
        )
        self.assertEqual(
            self.files.take_command(
                session_id="old-session", local_instance_id="local-id"
            ),
            {"action": "use-local", "decision_id": decision_id},
        )
        self.assertFalse(self.files.command_path.exists())

    def test_claimed_command_cannot_delete_a_newer_command(self) -> None:
        old_decision = "b" * 64
        new_decision = "c" * 64
        self.files.write_command(
            "use-local",
            session_id="session",
            local_instance_id="local",
            decision_id=old_decision,
        )
        original_reader = sync._read_json_file
        published_newer = False

        def interleaved_reader(path: Path, maximum: int):
            nonlocal published_newer
            if path.name.endswith(".claimed") and not published_newer:
                published_newer = True
                self.files.write_command(
                    "use-hosted",
                    session_id="session",
                    local_instance_id="local",
                    decision_id=new_decision,
                )
            return original_reader(path, maximum)

        with mock.patch.object(sync, "_read_json_file", side_effect=interleaved_reader):
            first = self.files.take_command(
                session_id="session", local_instance_id="local"
            )

        self.assertEqual(first, {"action": "use-local", "decision_id": old_decision})
        self.assertTrue(self.files.command_path.exists())
        self.assertEqual(
            self.files.take_command(session_id="session", local_instance_id="local"),
            {"action": "use-hosted", "decision_id": new_decision},
        )

    def test_backup_pair_has_one_timestamp_and_private_modes(self) -> None:
        local, hosted = self.files.backup_pair(
            {"revision": 1, **server.demo_document()},
            {"revision": 2, **server.demo_document()},
            reason="use-hosted",
        )
        self.assertEqual(local.name.rsplit("-", 2)[0], hosted.name.rsplit("-", 2)[0])
        self.assertTrue(local.name.endswith("-local.json"))
        self.assertTrue(hosted.name.endswith("-hosted.json"))
        self.assertEqual(stat.S_IMODE(self.files.backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(hosted.stat().st_mode), 0o600)


class AreaSyncTests(unittest.TestCase):
    """Exercise the real sync worker with local stores and no network sockets."""

    def test_area_push_pull_removal_conflict_and_duplex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = server.StateStore(root / "local.sqlite3")
            hosted = server.StateStore(root / "hosted.sqlite3")
            worker = sync.SyncWorker(
                "http://local.example.test", "http://hosted.example.test", root / "sync",
                demo_document=server.demo_document(),
            )

            def transport(store):
                def request(method, path, *, document=None, expected_revision=None):
                    if (method, path) == ("GET", "/api/health"):
                        return {"status": "ok", **store.get_metadata()}
                    if (method, path) == ("GET", "/api/state"):
                        return store.get_state()
                    if (method, path) == ("PUT", "/api/state"):
                        return store.replace_state(document, expected_revision)
                    raise AssertionError((method, path))
                return request

            with (
                mock.patch.object(worker.local_client, "_request", side_effect=transport(local)),
                mock.patch.object(worker.remote_client, "_request", side_effect=transport(hosted)),
            ):
                self.assertEqual(worker.sync_once()["state"], "synced")
                local.create_area({"id": "office", "label": "Office", "vlan_id": 10})
                self.assertEqual(worker.sync_once()["state"], "synced")
                self.assertEqual(hosted.get_area("office"), local.get_area("office"))
                hosted.update_area("office", {"shape": "ellipse", "x": 250})
                self.assertEqual(worker.sync_once()["state"], "synced")
                self.assertEqual(local.get_area("office")["x"], 250)
                link_id = local.get_state()["links"][0]["id"]
                local.update_link(link_id, {"duplex": "full", "bandwidth_mbps": 2500})
                self.assertEqual(worker.sync_once()["state"], "synced")
                self.assertEqual(hosted.get_link(link_id)["duplex"], "full")
                self.assertEqual(hosted.get_link(link_id)["bandwidth_mbps"], 2500)
                local.update_area("office", {"label": "Local label"})
                hosted.update_area("office", {"label": "Hosted label"})
                before_local, before_hosted = local.get_state(), hosted.get_state()
                self.assertEqual(worker.sync_once()["state"], "conflict")
                self.assertEqual(local.get_state(), before_local)
                self.assertEqual(hosted.get_state(), before_hosted)
                worker.write_command("use-local")
                self.assertEqual(worker.sync_once()["state"], "synced")
                self.assertEqual(hosted.get_area("office")["label"], "Local label")
                hosted.delete_area("office")
                self.assertEqual(worker.sync_once()["state"], "synced")
                self.assertEqual(local.get_state()["areas"], [])


class ServerPairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("ok", encoding="utf-8")
        self.local = server.ServerThread(
            port=0,
            data_dir=self.root / "local-data",
            static_dir=self.static,
        ).start()
        self.hosted = server.ServerThread(
            port=0,
            data_dir=self.root / "hosted-data",
            static_dir=self.static,
        ).start()

    def tearDown(self) -> None:
        self.local.stop()
        self.hosted.stop()
        self.temporary.cleanup()

    def worker(self, name: str = "sync-data", **values) -> sync.SyncWorker:
        return sync.SyncWorker(
            self.local.url,
            self.hosted.url,
            self.root / name,
            demo_document=server.demo_document(),
            timeout=2,
            **values,
        )

    def test_three_way_push_pull_conflict_and_command_resolution(self) -> None:
        worker = self.worker()

        # Equal first-run maps bind a baseline without changing either server.
        status_value = worker.sync_once()
        self.assertEqual(status_value["state"], "synced")
        self.assertEqual(self.local.server.store.get_state()["revision"], 1)
        self.assertEqual(self.hosted.server.store.get_state()["revision"], 1)

        # Only local changed: guarded push.
        changed_name(self.local, "Local edit")
        status_value = worker.sync_once()
        self.assertEqual(status_value["state"], "synced")
        self.assertEqual(
            self.hosted.server.store.get_state()["settings"]["name"], "Local edit"
        )

        # Only hosted changed after the new baseline: guarded pull.
        changed_name(self.hosted, "Hosted edit")
        status_value = worker.sync_once()
        self.assertEqual(status_value["state"], "synced")
        self.assertEqual(
            self.local.server.store.get_state()["settings"]["name"], "Hosted edit"
        )

        # Both changed: no writes and an explicit choice is required.
        local_before = changed_name(self.local, "Keep local")
        hosted_before = changed_name(self.hosted, "Discard hosted")
        status_value = worker.sync_once()
        self.assertEqual(status_value["state"], "conflict")
        self.assertEqual(status_value["can_resolve"], True)
        self.assertEqual(self.local.server.store.get_state(), local_before)
        self.assertEqual(self.hosted.server.store.get_state(), hosted_before)

        # The server/UI control file is accepted only for this worker session.
        worker.files.write_command(
            "use-local",
            session_id=worker.session_id,
            local_instance_id=worker.local_instance_id,
            decision_id=status_value["decision_id"],
        )
        status_value = worker.sync_once()
        self.assertEqual(status_value["state"], "synced")
        self.assertFalse(worker.files.command_path.exists())
        self.assertEqual(
            self.hosted.server.store.get_state()["settings"]["name"], "Keep local"
        )
        backups = sorted(worker.files.backup_dir.glob("*.json"))
        self.assertEqual(len(backups), 2)
        self.assertTrue(any(path.name.endswith("-local.json") for path in backups))
        self.assertTrue(any(path.name.endswith("-hosted.json") for path in backups))

        checkpoint = worker.files.load_checkpoint()
        self.assertEqual(checkpoint["local_revision"], self.local.server.store.get_state()["revision"])
        self.assertEqual(checkpoint["remote_revision"], self.hosted.server.store.get_state()["revision"])
        self.assertEqual(
            checkpoint["baseline_digest"],
            sync.topology_digest(self.local.server.store.get_state()),
        )

    def test_no_baseline_adopts_only_the_provably_untouched_demo(self) -> None:
        changed_name(self.hosted, "Established hosted map")
        local_before = self.local.server.store.get_state()
        status_value = self.worker("adopt-data").sync_once()
        self.assertEqual(status_value["state"], "synced")
        self.assertEqual(
            self.local.server.store.get_state()["settings"]["name"],
            "Established hosted map",
        )
        self.assertEqual(local_before["revision"], 1)
        self.assertEqual(
            len(list((self.root / "adopt-data" / sync.BACKUP_DIRECTORY).glob("*.json"))),
            2,
        )

    def test_resolution_is_rejected_if_either_map_changed_after_the_choice(self) -> None:
        worker = self.worker("bound-choice-data")
        worker.sync_once()
        changed_name(self.local, "First local choice")
        hosted_before = changed_name(self.hosted, "Keep hosted safe")
        conflict = worker.sync_once()
        self.assertEqual(conflict["state"], "conflict")

        worker.files.write_command(
            "use-hosted",
            session_id=worker.session_id,
            local_instance_id=worker.local_instance_id,
            decision_id=conflict["decision_id"],
        )
        local_after_click = changed_name(self.local, "Newer local edit")

        result = worker.sync_once()

        self.assertEqual(result["state"], "conflict")
        self.assertNotEqual(result["decision_id"], conflict["decision_id"])
        self.assertIn("choose again", result["message"].lower())
        self.assertEqual(self.local.server.store.get_state(), local_after_click)
        self.assertEqual(self.hosted.server.store.get_state(), hosted_before)
        self.assertFalse(worker.files.command_path.exists())

    def test_resolution_command_is_consumed_while_hosted_server_is_offline(self) -> None:
        worker = self.worker("offline-choice-data")
        worker.sync_once()
        changed_name(self.local, "Local conflict")
        changed_name(self.hosted, "Hosted conflict")
        conflict = worker.sync_once()
        worker.files.write_command(
            "use-hosted",
            session_id=worker.session_id,
            local_instance_id=worker.local_instance_id,
            decision_id=conflict["decision_id"],
        )
        port = self.hosted.server.server_address[1]
        hosted_data = self.root / "hosted-data"
        self.hosted.stop()

        offline = worker.sync_once()
        self.assertEqual(offline["state"], "offline")
        self.assertFalse(worker.files.command_path.exists())
        local_after_offline = changed_name(self.local, "Edited while offline")

        self.hosted = server.ServerThread(
            port=port,
            data_dir=hosted_data,
            static_dir=self.static,
        ).start()
        result = worker.sync_once()

        self.assertEqual(result["state"], "conflict")
        self.assertEqual(self.local.server.store.get_state(), local_after_offline)

    def test_no_baseline_with_two_non_demo_maps_is_read_only_conflict(self) -> None:
        local_before = changed_name(self.local, "Unrelated local")
        hosted_before = changed_name(self.hosted, "Unrelated hosted")
        status_value = self.worker("conflict-data").sync_once()
        self.assertEqual(status_value["state"], "conflict")
        self.assertEqual(status_value["pending_changes"], 1)
        self.assertEqual(self.local.server.store.get_state(), local_before)
        self.assertEqual(self.hosted.server.store.get_state(), hosted_before)
        self.assertFalse((self.root / "conflict-data" / sync.CHECKPOINT_FILENAME).exists())

    def test_status_contract_is_private_secret_free_and_callback_ready(self) -> None:
        observed: list[dict] = []
        worker = self.worker(status_callback=observed.append)
        result = worker.sync_once()
        status_path = self.root / "sync-data" / sync.STATUS_FILENAME
        stored = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(stored, result)
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["mode"], "native-sync")
        self.assertEqual(stored["state"], "synced")
        self.assertIsInstance(stored["heartbeat"], (int, float))
        self.assertIsInstance(stored["pending_changes"], int)
        self.assertTrue(stored["session_id"])
        self.assertTrue(stored["local_instance_id"])
        self.assertTrue(stored["last_sync_at"].endswith("Z"))
        self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)
        self.assertEqual(observed[-1], result)


class AuthenticatedHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("ok", encoding="utf-8")
        self.token = "correct horse battery staple"
        self.running = server.ServerThread(
            port=0,
            data_dir=self.root / "server-data",
            static_dir=static,
            token=self.token,
        ).start()

    def tearDown(self) -> None:
        self.running.stop()
        self.temporary.cleanup()

    def test_client_maps_auth_race_size_and_network_errors(self) -> None:
        wrong = sync.NetworkMapHTTPClient(self.running.url, "do-not-leak")
        with self.assertRaises(sync.SyncAuthError) as raised:
            wrong.get_state()
        self.assertNotIn("do-not-leak", str(raised.exception))

        client = sync.NetworkMapHTTPClient(self.running.url, self.token)
        state = client.get_state()
        changed_name(self.running, "Revision moved")
        with self.assertRaises(sync.SyncRaceError):
            client.put_state(state, expected_revision=state["revision"])

        tiny = sync.NetworkMapHTTPClient(
            self.running.url, self.token, max_response_bytes=64
        )
        with self.assertRaises(sync.SyncProtocolError):
            tiny.get_state()

        unreachable = sync.NetworkMapHTTPClient(
            "http://127.0.0.1:1", timeout=0.1
        )
        with self.assertRaises(sync.SyncNetworkError):
            unreachable.get_health()

    def test_worker_auth_status_never_serializes_the_token(self) -> None:
        local = server.ServerThread(
            port=0,
            data_dir=self.root / "local-data",
            static_dir=self.root / "static",
        ).start()
        try:
            data_dir = self.root / "sync-data"
            worker = sync.SyncWorker(
                local.url,
                self.running.url,
                data_dir,
                remote_token="definitely-wrong-secret",
                demo_document=server.demo_document(),
            )
            result = worker.sync_once()
            self.assertEqual(result["state"], "auth-required")
            serialized = "\n".join(
                path.read_text(encoding="utf-8")
                for path in data_dir.iterdir()
                if path.is_file()
            )
            self.assertNotIn("definitely-wrong-secret", serialized)
        finally:
            local.stop()


class WorkerLifecycleTests(unittest.TestCase):
    def test_start_wake_stop_and_linux_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("ok", encoding="utf-8")
            with server.ServerThread(
                port=0, data_dir=root / "local", static_dir=static
            ) as local:
                ready = threading.Event()

                def callback(value: dict) -> None:
                    if value["state"] == "local-only":
                        ready.set()

                worker = sync.SyncWorker(
                    local.url,
                    None,
                    root / "local",
                    status_callback=callback,
                    poll_interval=5,
                ).start()
                try:
                    self.assertTrue(ready.wait(2))
                    competitor = sync.SyncWorker(local.url, None, root / "local")
                    with self.assertRaises(sync.SyncLockError):
                        competitor.start()
                    before = worker.status()["heartbeat"]
                    worker.wake()
                    deadline = time.monotonic() + 2
                    while worker.status()["heartbeat"] <= before and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertGreater(worker.status()["heartbeat"], before)
                finally:
                    worker.stop()
                self.assertFalse((root / "local" / sync.STATUS_FILENAME).exists())
                self.assertFalse(
                    local.server.sync_bridge.get_status(
                        local.server.store.get_metadata()
                    )["available"]
                )
                competitor.start()
                competitor.stop()

    def test_run_loop_exception_releases_lock_and_clears_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("ok", encoding="utf-8")
            with server.ServerThread(
                port=0, data_dir=root / "local", static_dir=static
            ) as local:
                worker = sync.SyncWorker(local.url, None, root / "local")
                failed = threading.Event()

                def fail_pass() -> dict:
                    worker._publish("checking", "About to fail.")
                    failed.set()
                    raise RuntimeError("forced worker failure")

                with (
                    mock.patch.object(worker, "sync_once", side_effect=fail_pass),
                    mock.patch.object(threading, "excepthook"),
                ):
                    worker.start()
                    self.assertTrue(failed.wait(2))
                    worker._thread.join(2)

                self.assertFalse(worker._thread.is_alive())
                self.assertFalse((root / "local" / sync.STATUS_FILENAME).exists())
                competitor = sync.SyncWorker(local.url, None, root / "local").start()
                competitor.stop()

    def test_stop_timeout_keeps_ownership_until_active_pass_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("ok", encoding="utf-8")
            with server.ServerThread(
                port=0, data_dir=root / "local", static_dir=static
            ) as local:
                worker = sync.SyncWorker(local.url, None, root / "local")
                entered = threading.Event()
                release = threading.Event()

                def blocked_pass() -> dict:
                    worker._publish("checking", "Finishing an active request.")
                    entered.set()
                    release.wait(2)
                    return {"state": "local-only"}

                with mock.patch.object(worker, "sync_once", side_effect=blocked_pass):
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(sync.SyncError):
                        worker.stop(timeout=0.01)
                    competitor = sync.SyncWorker(local.url, None, root / "local")
                    with self.assertRaises(sync.SyncLockError):
                        competitor.start()
                    self.assertTrue((root / "local" / sync.STATUS_FILENAME).exists())
                    release.set()
                    worker.stop()

                self.assertFalse((root / "local" / sync.STATUS_FILENAME).exists())
                competitor.start()
                competitor.stop()

    def test_thread_start_failure_releases_worker_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = sync.SyncWorker(
                "http://127.0.0.1:1", None, root / "sync-data"
            )
            with mock.patch.object(
                sync.threading.Thread, "start", side_effect=RuntimeError("no thread")
            ):
                with self.assertRaises(RuntimeError):
                    worker.start()

            competitor = sync.SyncWorker(
                "http://127.0.0.1:1", None, root / "sync-data"
            )
            competitor._acquire_worker_lock()
            competitor._release_worker_lock()


if __name__ == "__main__":
    unittest.main()

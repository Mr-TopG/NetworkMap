from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse
from types import SimpleNamespace

import native
import server


class NativeURLTests(unittest.TestCase):
    def test_normalise_server_url(self) -> None:
        self.assertEqual(
            native._normalise_server_url("networkmap.example.net/"),
            "http://networkmap.example.net",
        )
        self.assertEqual(
            native._normalise_server_url("HTTPS://Example.NET:8443/map/"),
            "https://Example.NET:8443/map",
        )

        for invalid in (
            "ftp://example.net",
            "https://user:password@example.net",
            "https://example.net/?token=secret",
            "https://example.net/#token=secret",
            "http://example.net:not-a-port",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                native._normalise_server_url(invalid)

    def test_token_is_placed_in_fragment_only(self) -> None:
        url = native._tokenised_url("https://networkmap.example.net", "space / ?")
        parsed = urllib.parse.urlsplit(url)

        self.assertEqual(parsed.query, "")
        self.assertEqual(urllib.parse.parse_qs(parsed.fragment)["token"], ["space / ?"])

    def test_same_origin_normalises_default_ports(self) -> None:
        self.assertTrue(native._same_origin("https://EXAMPLE.net", "https://example.net:443/map"))
        self.assertFalse(native._same_origin("https://example.net", "http://example.net/map"))
        self.assertFalse(native._same_origin("https://example.net", "https://other.example/map"))

    def test_winbox_launch_uri_accepts_addresses_without_credentials(self) -> None:
        self.assertEqual(
            native._winbox_target_from_uri("winbox://connect/192.168.88.1"),
            "192.168.88.1",
        )
        self.assertEqual(
            native._winbox_target_from_uri("winbox://connect/AA%3ABB%3ACC%3ADD%3AEE%3AFF"),
            "AA:BB:CC:DD:EE:FF",
        )
        for invalid in (
            "https://192.168.88.1",
            "winbox://connect/--help",
            "winbox://connect/192.168.88.1?password=secret",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                native._winbox_target_from_uri(invalid)

    def test_winbox_executable_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "WinBox"
            executable.touch(mode=0o700)
            with mock.patch.dict(os.environ, {"NETWORKMAP_WINBOX": str(executable)}):
                self.assertEqual(native._find_winbox_executable(), str(executable))

    def test_ssh_launch_uri_accepts_only_a_bare_host(self) -> None:
        self.assertEqual(
            native._ssh_target_from_uri("networkmap-ssh://connect/server.example"),
            "server.example",
        )
        self.assertEqual(
            native._ssh_target_from_uri("networkmap-ssh://connect/2001%3Adb8%3A%3A1"),
            "2001:db8::1",
        )
        for invalid in (
            "https://server.example",
            "ssh://server.example",
            "networkmap-ssh://other/server.example",
            "networkmap-ssh://connect/user%40server.example",
            "networkmap-ssh://connect/server.example%3A2222",
            "networkmap-ssh://connect/server.example/command",
            "networkmap-ssh://connect/server.example?password=secret",
            "networkmap-ssh://connect/--help",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                native._ssh_target_from_uri(invalid)

    def test_ssh_launch_uses_argument_array_in_a_terminal(self) -> None:
        locations = {
            "ssh": "/usr/bin/ssh",
            "gnome-terminal": "/usr/bin/gnome-terminal",
        }
        with (
            mock.patch.object(native.shutil, "which", side_effect=locations.get),
            mock.patch.object(native.subprocess, "Popen") as popen,
        ):
            native._launch_ssh("192.168.1.50")

        arguments = popen.call_args.args[0]
        self.assertEqual(
            arguments,
            ["/usr/bin/gnome-terminal", "--", "/usr/bin/ssh", "192.168.1.50"],
        )
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_custom_ssh_uri_is_dispatched_without_starting_the_app(self) -> None:
        with mock.patch.object(native, "_launch_ssh") as launch:
            result = native.main(
                ["networkmap-ssh://connect/server.example"]
            )

        self.assertEqual(result, 0)
        launch.assert_called_once_with("server.example")


class NativeConfigTests(unittest.TestCase):
    def test_config_is_atomic_private_and_does_not_store_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": temporary}
        ):
            native._remember_server("https://networkmap.example.net")
            path = Path(temporary) / "networkmap" / "native.json"

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"server_url": "https://networkmap.example.net"},
            )
            self.assertNotIn("token", path.read_text(encoding="utf-8").lower())

            native._forget_saved_server()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})


class LocalServerReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def start_server(
        self, data_dir: Path, token: str | None = None
    ) -> server.ServerThread:
        running = server.ServerThread(
            port=0,
            data_dir=data_dir,
            token=token,
            static_dir=native.APP_DIR / "static",
        ).start()
        self.addCleanup(running.stop)
        return running

    def shell_for(
        self,
        running: server.ServerThread,
        data_dir: Path,
        token: str | None = None,
    ) -> native.LocalServer:
        return native.LocalServer(
            host="127.0.0.1",
            port=running.server.server_address[1],
            data_dir=str(data_dir),
            token=token,
            server_module=server,
        )

    def test_new_local_server_starts_with_the_requested_database(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        data_dir = self.root / "new-data"
        shell = native.LocalServer(
            host="127.0.0.1",
            port=port,
            data_dir=str(data_dir),
            token="local-secret",
            server_module=server,
        )
        self.addCleanup(shell.stop)

        shell.start()

        self.assertFalse(shell.reused)
        self.assertIsNotNone(shell.httpd)
        self.assertEqual(
            native._database_instance_id(data_dir / "networkmap.sqlite3"),
            shell.httpd.store.get_metadata()["instance_id"],
        )

    def test_reuses_only_the_same_database_with_an_authenticated_state_probe(self) -> None:
        data_dir = self.root / "same-data"
        running = self.start_server(data_dir, token="local-secret")
        shell = self.shell_for(running, data_dir, token="local-secret")

        shell.start()

        self.assertTrue(shell.reused)
        self.assertIsNone(shell.httpd)
        self.assertEqual(
            native._database_instance_id(data_dir / "networkmap.sqlite3"),
            running.server.store.get_metadata()["instance_id"],
        )

    def test_refuses_a_running_networkmap_with_a_different_database(self) -> None:
        served_data = self.root / "served-data"
        requested_data = self.root / "requested-data"
        running = self.start_server(served_data)
        server.StateStore(requested_data / "networkmap.sqlite3")
        shell = self.shell_for(running, requested_data)

        with self.assertRaisesRegex(
            RuntimeError, "different NetworkMap database.*refusing"
        ):
            shell.start()

        self.assertFalse(shell.reused)
        self.assertIsNone(shell.httpd)

    def test_refuses_a_running_networkmap_when_the_local_token_is_wrong(self) -> None:
        data_dir = self.root / "protected-data"
        running = self.start_server(data_dir, token="correct-secret")
        shell = self.shell_for(running, data_dir, token="wrong-secret")

        with self.assertRaisesRegex(RuntimeError, "access token does not match"):
            shell.start()

        self.assertFalse(shell.reused)
        self.assertIsNone(shell.httpd)

    def test_local_probe_does_not_follow_redirects(self) -> None:
        destination = self.start_server(self.root / "redirect-destination")

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self.send_response(302)
                self.send_header("Location", destination.url + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{redirect.server_address[1]}"
            with self.assertRaisesRegex(
                native._LocalProbeIncompatible, "redirects are not accepted"
            ):
                native._probe_networkmap_server(url, None)
        finally:
            redirect.shutdown()
            redirect.server_close()
            thread.join(timeout=2)

    def test_local_probe_requires_api_v1_and_matching_stable_identities(self) -> None:
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        with mock.patch.object(
            native,
            "_probe_json",
            return_value={
                "status": "ok",
                "api_version": 2,
                "instance_id": first_id,
            },
        ), self.assertRaisesRegex(native._LocalProbeIncompatible, "API v1"):
            native._probe_networkmap_server("http://127.0.0.1:8765", None)

        health = {
            "status": "ok",
            "api_version": 1,
            "instance_id": first_id,
        }
        mismatched_state = {
            "api_version": 1,
            "instance_id": second_id,
            "revision": 1,
        }
        with mock.patch.object(
            native, "_probe_json", side_effect=[health, mismatched_state]
        ), self.assertRaisesRegex(
            native._LocalProbeIncompatible, "identities do not agree"
        ):
            native._probe_networkmap_server("http://127.0.0.1:8765", None)


class NativeOfflineFirstTests(unittest.TestCase):
    def test_remote_configuration_still_opens_local_copy_and_starts_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server_module = SimpleNamespace(
                default_data_dir=lambda: Path(temporary),
                demo_document=lambda: {"nodes": [], "links": [], "settings": {}},
            )
            local_server = mock.Mock()
            sync_worker = mock.Mock()
            sync_worker_type = mock.Mock(return_value=sync_worker)
            gtk_modules = (mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())

            with (
                mock.patch.object(native, "_load_gtk", return_value=gtk_modules),
                mock.patch.object(native, "_load_server_module", return_value=server_module),
                mock.patch.object(native, "_resolve_token", return_value="private-token"),
                mock.patch.object(native, "LocalServer", return_value=local_server) as local_type,
                mock.patch.object(native, "SyncWorker", sync_worker_type),
                mock.patch.object(native, "_run_gui", return_value=0) as run_gui,
            ):
                result = native.main(
                    [
                        "--server-url",
                        "https://maps.example.test",
                        "--no-remember",
                    ]
                )

            self.assertEqual(result, 0)
            local_type.assert_called_once_with(
                host="127.0.0.1",
                port=8765,
                data_dir=temporary,
                token="private-token",
                server_module=server_module,
            )
            local_server.start.assert_called_once_with()
            sync_worker_type.assert_called_once_with(
                "http://127.0.0.1:8765",
                "https://maps.example.test",
                Path(temporary),
                local_token="private-token",
                remote_token="private-token",
                demo_document={"nodes": [], "links": [], "settings": {}},
            )
            sync_worker.start.assert_called_once_with()
            run_gui.assert_called_once_with(
                "http://127.0.0.1:8765",
                "http://127.0.0.1:8765/#token=private-token",
                False,
                gtk_modules,
                "https://maps.example.test",
            )
            sync_worker.stop.assert_called_once_with()
            local_server.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

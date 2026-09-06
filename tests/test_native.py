from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.parse

import native


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


if __name__ == "__main__":
    unittest.main()

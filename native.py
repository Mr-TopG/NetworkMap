#!/usr/bin/env python3
"""NetworkMap Linux desktop shell.

The shell embeds the web application with GTK 3 and WebKitGTK 4.1.  It always
starts (or reuses) the loopback NetworkMap server so the app works offline.  An
optional hosted instance is synchronized in the background; its address is
remembered without storing the authentication token.

Only Python's standard library is used here apart from the distro-provided
PyGObject/WebKitGTK bindings.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

from networkmap_sync import SyncWorker


APP_NAME = "NetworkMap"
APP_ID = "io.github.networkmap.NetworkMap"
APP_VERSION = "0.2.1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
APP_DIR = Path(__file__).resolve().parent
WINBOX_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%\-\[\]]{0,252}$")
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
LOCAL_API_VERSION = 1
MAX_LOCAL_PROBE_BYTES = 32 * 1024 * 1024


def _config_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "networkmap" / "native.json"


def _default_token_path() -> Path:
    return _config_path().with_name("token")


def _read_config() -> dict[str, Any]:
    path = _config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return {}
    return value if isinstance(value, dict) else {}


def _write_config(config: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".native-",
            suffix=".json",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(config, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _forget_saved_server() -> None:
    config = _read_config()
    if "server_url" not in config:
        return
    config.pop("server_url", None)
    _write_config(config)


def _remember_server(url: str) -> None:
    config = _read_config()
    config["server_url"] = url
    _write_config(config)


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _normalise_server_url(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("server URL must use http:// or https://")
    if not parsed.hostname:
        raise ValueError("server URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("put credentials in --token/--token-file, not in the URL")
    if parsed.query or parsed.fragment:
        raise ValueError("server URL cannot contain a query string or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("server URL contains an invalid port") from exc
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, path, "", "")
    )


def _loopback_url(host: str, port: int) -> str:
    display_host = host
    if host in {"0.0.0.0", ""}:
        display_host = "127.0.0.1"
    elif host in {"::", "[::]"}:
        display_host = "::1"
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _api_url(base_url: str, route: str) -> str:
    return base_url.rstrip("/") + "/" + route.lstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class _LocalProbeUnavailable(RuntimeError):
    """No service completed the local API probe."""


class _LocalProbeIncompatible(RuntimeError):
    """A service answered, but it is not the required local NetworkMap API."""


class _LocalProbeAuthenticationError(RuntimeError):
    """The local NetworkMap server rejected the supplied token."""


def _canonical_instance_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _LocalProbeIncompatible(
            "the NetworkMap API did not provide a stable instance ID"
        )
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise _LocalProbeIncompatible(
            "the NetworkMap API returned an invalid instance ID"
        ) from exc


def _probe_json(
    opener: Any,
    url: str,
    *,
    token: Optional[str],
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "NetworkMap-native",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code in {401, 403}:
                raise _LocalProbeAuthenticationError(
                    "the local NetworkMap access token was rejected"
                ) from None
            if 300 <= exc.code < 400:
                raise _LocalProbeIncompatible(
                    "redirects are not accepted for the local NetworkMap API"
                ) from None
            if 500 <= exc.code < 600:
                raise _LocalProbeUnavailable(
                    f"the local NetworkMap API returned HTTP {exc.code}"
                ) from None
            raise _LocalProbeIncompatible(
                f"the local service returned HTTP {exc.code}"
            ) from None
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _LocalProbeUnavailable("the local service could not be reached") from exc

    with response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise _LocalProbeIncompatible(
                f"the local service returned HTTP {int(status)}"
            )
        if response.geturl() != url:
            raise _LocalProbeIncompatible(
                "redirects are not accepted for the local NetworkMap API"
            )
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            raise _LocalProbeIncompatible(
                "the local NetworkMap API did not return JSON"
            )
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise _LocalProbeIncompatible(
                    "the local NetworkMap API returned an invalid response length"
                ) from exc
            if declared_size < 0 or declared_size > MAX_LOCAL_PROBE_BYTES:
                raise _LocalProbeIncompatible(
                    "the local NetworkMap API response is too large"
                )
        raw = response.read(MAX_LOCAL_PROBE_BYTES + 1)
    if len(raw) > MAX_LOCAL_PROBE_BYTES:
        raise _LocalProbeIncompatible("the local NetworkMap API response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _LocalProbeIncompatible(
            "the local NetworkMap API returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise _LocalProbeIncompatible(
            "the local NetworkMap API response is not a JSON object"
        )
    return payload


def _probe_networkmap_server(
    base_url: str, token: Optional[str], timeout: float = 0.5
) -> dict[str, Any]:
    """Verify a local server without following redirects or leaking via proxies."""

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    try:
        health = _probe_json(
            opener,
            _api_url(base_url, "/api/health"),
            token=None,
            timeout=timeout,
        )
    except _LocalProbeAuthenticationError as exc:
        raise _LocalProbeIncompatible(
            "the local service does not expose the public NetworkMap health API"
        ) from exc
    if (
        health.get("status") != "ok"
        or type(health.get("api_version")) is not int
        or health["api_version"] != LOCAL_API_VERSION
    ):
        raise _LocalProbeIncompatible(
            f"the local service is not NetworkMap API v{LOCAL_API_VERSION}"
        )
    instance_id = _canonical_instance_id(health.get("instance_id"))

    state = _probe_json(
        opener,
        _api_url(base_url, "/api/state"),
        token=token.strip() if token and token.strip() else None,
        timeout=timeout,
    )
    if (
        type(state.get("api_version")) is not int
        or state["api_version"] != LOCAL_API_VERSION
        or _canonical_instance_id(state.get("instance_id")) != instance_id
        or isinstance(state.get("revision"), bool)
        or not isinstance(state.get("revision"), int)
        or state["revision"] < 0
    ):
        raise _LocalProbeIncompatible(
            "the local NetworkMap health and state identities do not agree"
        )
    return {
        "api_version": LOCAL_API_VERSION,
        "instance_id": instance_id,
        "revision": state["revision"],
    }


def _database_instance_id(database_path: Path) -> Optional[str]:
    """Read the intended database identity without creating or changing the DB."""

    if not database_path.exists():
        return None
    if database_path.is_symlink() or not database_path.is_file():
        raise RuntimeError(
            f"the intended local database is not a regular file: {database_path}"
        )
    connection: Optional[sqlite3.Connection] = None
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        row = connection.execute(
            "SELECT instance_id FROM metadata WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"could not verify the intended local database identity: {database_path}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    if row is None:
        raise RuntimeError(
            f"the intended local database has no identity: {database_path}"
        )
    try:
        return str(uuid.UUID(row[0]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"the intended local database has an invalid identity: {database_path}"
        ) from exc


def _load_server_module() -> ModuleType:
    source = APP_DIR / "server.py"
    if not source.is_file():
        raise RuntimeError(f"server entry point not found: {source}")
    spec = importlib.util.spec_from_file_location("networkmap_embedded_server", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load server entry point: {source}")
    module = importlib.util.module_from_spec(spec)
    # Some standard-library helpers (including dataclasses) expect the module
    # to be registered while its source is evaluated.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalServer:
    """Own an in-process HTTP server and its serving thread."""

    def __init__(
        self,
        host: str,
        port: int,
        data_dir: Optional[str],
        token: Optional[str],
        server_module: Optional[ModuleType] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.token = token
        self.server_module = server_module
        self.url = _loopback_url(host, port)
        self.httpd: Any = None
        self.thread: Optional[threading.Thread] = None
        self.reused = False

    def _database_path(self, module: ModuleType) -> Path:
        if self.data_dir is not None:
            root = Path(self.data_dir).expanduser()
        else:
            default_data_dir = getattr(module, "default_data_dir", None)
            if not callable(default_data_dir):
                raise RuntimeError("server.py does not expose default_data_dir()")
            root = Path(default_data_dir()).expanduser()
        return root / "networkmap.sqlite3"

    def _accept_running_server(
        self,
        probe: dict[str, Any],
        database_path: Path,
    ) -> None:
        intended_id = _database_instance_id(database_path)
        if intended_id is None or probe["instance_id"] != intended_id:
            raise RuntimeError(
                f"a different NetworkMap database is already served at {self.url}; "
                f"refusing to open or synchronize the wrong local map. Stop that "
                f"server or choose another --port (requested database: {database_path})"
            )
        self.reused = True

    def _probe_existing(self, database_path: Path) -> bool:
        try:
            probe = _probe_networkmap_server(self.url, self.token)
        except _LocalProbeAuthenticationError as exc:
            raise RuntimeError(
                f"a NetworkMap server is already running at {self.url}, but its "
                "local access token does not match. Use the same token or choose "
                "another --port"
            ) from exc
        except (_LocalProbeUnavailable, _LocalProbeIncompatible):
            return False
        self._accept_running_server(probe, database_path)
        return True

    def start(self) -> None:
        module = self.server_module or _load_server_module()
        database_path = self._database_path(module)
        if self._probe_existing(database_path):
            return

        create_server = getattr(module, "create_server", None)
        if not callable(create_server):
            raise RuntimeError("server.py does not expose create_server()")

        try:
            self.httpd = create_server(
                host=self.host,
                port=self.port,
                data_dir=self.data_dir,
                token=self.token,
                static_dir=str(APP_DIR / "static"),
            )
        except OSError as exc:
            # A second launcher can win the race between our first health
            # probe and bind(). Give it a brief chance to become ready.
            for _ in range(20):
                if self._probe_existing(database_path):
                    return
                time.sleep(0.1)
            raise RuntimeError(
                f"could not bind the local server at {self.url}; the port is in "
                f"use and no compatible NetworkMap server for {database_path} "
                f"could be safely reused: {exc}"
            ) from exc

        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="networkmap-http",
            daemon=True,
        )
        self.thread.start()
        for _ in range(100):
            try:
                probe = _probe_networkmap_server(self.url, self.token)
            except _LocalProbeUnavailable:
                probe = None
            except (_LocalProbeAuthenticationError, _LocalProbeIncompatible) as exc:
                self.stop()
                raise RuntimeError(
                    f"the local server at {self.url} failed its identity and "
                    f"authentication check: {exc}"
                ) from exc
            if probe is not None:
                intended_id = _database_instance_id(database_path)
                if intended_id is None or probe["instance_id"] != intended_id:
                    self.stop()
                    raise RuntimeError(
                        f"the local server at {self.url} does not match the "
                        f"requested database: {database_path}"
                    )
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"local server did not become ready at {self.url}")

    def stop(self) -> None:
        if self.httpd is None:
            return
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=3)
            self.httpd = None
            self.thread = None


def _tokenised_url(base_url: str, token: Optional[str]) -> str:
    if not token:
        return base_url + "/"
    # Fragments never leave the browser. The frontend exchanges this value for
    # an HttpOnly session cookie and immediately removes it from the address.
    return base_url + "/#" + urllib.parse.urlencode({"token": token})


def _same_origin(first: str, second: str) -> bool:
    try:
        left = urllib.parse.urlsplit(first)
        right = urllib.parse.urlsplit(second)
        left_port = left.port or (443 if left.scheme == "https" else 80)
        right_port = right.port or (443 if right.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        left.scheme.lower(),
        (left.hostname or "").lower(),
        left_port,
    ) == (
        right.scheme.lower(),
        (right.hostname or "").lower(),
        right_port,
    )


def _winbox_target_from_uri(uri: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme.lower() != "winbox" or parsed.netloc != "connect":
        raise ValueError("invalid WinBox launch URL")
    if parsed.query or parsed.fragment:
        raise ValueError("WinBox launch URL cannot contain a query or fragment")
    target = urllib.parse.unquote(parsed.path.lstrip("/"), errors="strict").strip()
    if not WINBOX_TARGET_RE.fullmatch(target) or target.startswith("-"):
        raise ValueError("WinBox target must be an IP address, MAC address, or hostname")
    return target


def _find_winbox_executable() -> Optional[str]:
    configured = os.environ.get("NETWORKMAP_WINBOX", "").strip()
    if configured:
        candidate = shutil.which(configured)
        if candidate:
            return candidate
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for name in ("winbox", "WinBox", "winbox64", "WinBox64"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


def _launch_winbox(target: str) -> None:
    executable = _find_winbox_executable()
    if not executable:
        raise RuntimeError(
            "WinBox was not found. Install MikroTik WinBox for Linux or set "
            "NETWORKMAP_WINBOX to its executable path. The address has been "
            "copied by the web interface."
        )
    try:
        subprocess.Popen(
            [executable, target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start WinBox: {exc}") from exc


def _ssh_target_from_uri(uri: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme.lower() != "networkmap-ssh" or parsed.netloc != "connect":
        raise ValueError("invalid SSH launch URL")
    if parsed.query or parsed.fragment or not parsed.path.startswith("/"):
        raise ValueError("SSH launch URL cannot contain a query or fragment")
    encoded_target = parsed.path[1:]
    if not encoded_target or "/" in encoded_target:
        raise ValueError("SSH launch URL must contain one encoded target")
    target = urllib.parse.unquote(encoded_target, errors="strict").strip()
    if not target or len(target) > 253 or target.startswith("-"):
        raise ValueError("SSH target must be an IP address or hostname")
    try:
        ipaddress.ip_address(target)
    except ValueError:
        hostname = target.rstrip(".")
        if not hostname or any(
            not HOST_LABEL_RE.fullmatch(label) for label in hostname.split(".")
        ):
            raise ValueError("SSH target must be an IP address or hostname") from None
        target = hostname.lower()
    return target


def _ssh_terminal_command(target: str) -> list[str]:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("The OpenSSH client was not found. Install the 'ssh' command first.")
    candidates = (
        ("x-terminal-emulator", lambda terminal: [terminal, "-e", ssh, target]),
        ("gnome-terminal", lambda terminal: [terminal, "--", ssh, target]),
        ("kgx", lambda terminal: [terminal, "--", ssh, target]),
        ("konsole", lambda terminal: [terminal, "-e", ssh, target]),
        ("xfce4-terminal", lambda terminal: [terminal, "--execute", ssh, target]),
        ("xterm", lambda terminal: [terminal, "-e", ssh, target]),
        ("alacritty", lambda terminal: [terminal, "-e", ssh, target]),
        ("kitty", lambda terminal: [terminal, ssh, target]),
    )
    for name, command in candidates:
        terminal = shutil.which(name)
        if terminal:
            return command(terminal)
    raise RuntimeError("No supported terminal emulator was found for the SSH session.")


def _launch_ssh(target: str) -> None:
    try:
        subprocess.Popen(
            _ssh_terminal_command(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start SSH: {exc}") from exc


def _load_gtk() -> tuple[Any, Any, Any, Any]:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import Gio, GLib, Gtk, WebKit2
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            "GTK 3/WebKitGTK 4.1 Python bindings are required. "
            "On Debian, Ubuntu, or Linux Mint install python3-gi, "
            "gir1.2-gtk-3.0, and gir1.2-webkit2-4.1."
        ) from exc
    return Gio, GLib, Gtk, WebKit2


def _run_gui(
    base_url: str,
    initial_url: str,
    developer_tools: bool,
    gtk_modules: tuple[Any, Any, Any, Any],
    remote_url: Optional[str] = None,
) -> int:
    Gio, GLib, Gtk, WebKit2 = gtk_modules

    class NetworkMapApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id=APP_ID)
            self.window: Any = None
            self.webview: Any = None
            self.spinner: Any = None
            self._last_external_uri = ""
            self._last_external_at = 0.0

        def do_activate(self) -> None:
            if self.window is not None:
                self.window.present()
                return

            Gtk.Window.set_default_icon_name("networkmap")
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title(APP_NAME)
            self.window.set_default_size(1320, 840)
            self.window.set_size_request(720, 480)

            header = Gtk.HeaderBar()
            header.set_show_close_button(True)
            header.set_title(APP_NAME)
            if remote_url:
                remote_host = urllib.parse.urlsplit(remote_url).netloc
                header.set_subtitle(f"Local copy · sync with {remote_host}")
            else:
                header.set_subtitle("Local copy")
            self.window.set_titlebar(header)

            reload_button = Gtk.Button.new_from_icon_name(
                "view-refresh-symbolic", Gtk.IconSize.BUTTON
            )
            reload_button.set_tooltip_text("Reload (Ctrl+R)")
            reload_button.connect("clicked", lambda _button: self.webview.reload())
            header.pack_start(reload_button)

            browser_button = Gtk.Button.new_from_icon_name(
                "web-browser-symbolic", Gtk.IconSize.BUTTON
            )
            browser_button.set_tooltip_text("Open in the default browser")
            browser_button.connect("clicked", self._open_in_browser)
            header.pack_end(browser_button)

            self.spinner = Gtk.Spinner()
            header.pack_end(self.spinner)

            self.webview = WebKit2.WebView()
            settings = self.webview.get_settings()
            settings.set_enable_javascript(True)
            settings.set_enable_developer_extras(developer_tools)
            self.webview.connect("load-changed", self._on_load_changed)
            self.webview.connect("load-failed", self._on_load_failed)
            self.webview.connect("decide-policy", self._on_decide_policy)
            self.webview.connect("create", self._on_create_webview)
            self.webview.connect("notify::title", self._on_title_changed)
            self.window.add(self.webview)

            # Gtk's application accelerator API handles the platform's
            # primary-modifier mapping for us.
            self.set_accels_for_action("win.reload", ["<Primary>r", "F5"])

            reload_action = Gio.SimpleAction.new("reload", None)
            reload_action.connect("activate", lambda *_args: self.webview.reload())
            self.window.add_action(reload_action)

            self.window.show_all()
            self.webview.load_uri(initial_url)

        def _open_in_browser(self, _button: Any) -> None:
            try:
                Gio.AppInfo.launch_default_for_uri(base_url + "/", None)
            except GLib.Error as exc:
                self._show_error("Could not open the browser", str(exc))

        def _on_title_changed(self, view: Any, _spec: Any) -> None:
            title = view.get_title()
            self.window.set_title(f"{title} — {APP_NAME}" if title else APP_NAME)

        def _on_load_changed(self, _view: Any, event: Any) -> None:
            if event == WebKit2.LoadEvent.STARTED:
                self.spinner.start()
            elif event == WebKit2.LoadEvent.FINISHED:
                self.spinner.stop()

        def _on_load_failed(
            self, _view: Any, _event: Any, failing_uri: str, error: Any
        ) -> bool:
            self.spinner.stop()
            safe_uri = html.escape(failing_uri or base_url)
            safe_error = html.escape(getattr(error, "message", str(error)))
            page = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>NetworkMap unavailable</title>
<style>
body {{ font: 16px system-ui, sans-serif; color: #e6edf3; background: #0d1117;
       display: grid; min-height: 100vh; place-items: center; margin: 0; }}
main {{ width: min(34rem, calc(100% - 3rem)); background: #161b22;
        border: 1px solid #30363d; border-radius: 12px; padding: 2rem; }}
h1 {{ margin-top: 0; font-size: 1.5rem; }}
p {{ color: #b1bac4; line-height: 1.55; }}
a {{ display: inline-block; color: white; background: #238636; padding: .65rem 1rem;
     border-radius: 6px; text-decoration: none; }}
code {{ overflow-wrap: anywhere; }}
</style></head><body><main><h1>Cannot reach NetworkMap</h1>
<p><code>{safe_uri}</code></p><p>{safe_error}</p>
<a href=\"{html.escape(initial_url, quote=True)}\">Try again</a>
</main></body></html>"""
            self.webview.load_html(page, base_url + "/")
            return True

        def _show_error(self, title: str, detail: str) -> None:
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text=title,
            )
            dialog.format_secondary_text(detail)
            dialog.run()
            dialog.destroy()

        def _on_decide_policy(
            self, _view: Any, decision: Any, decision_type: Any
        ) -> bool:
            supported_types = {WebKit2.PolicyDecisionType.NAVIGATION_ACTION}
            new_window_type = getattr(
                WebKit2.PolicyDecisionType, "NEW_WINDOW_ACTION", None
            )
            if new_window_type is not None:
                supported_types.add(new_window_type)
            if decision_type not in supported_types:
                return False
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if not uri or uri.startswith(("about:", "data:", "blob:")):
                return False
            is_new_window = new_window_type is not None and decision_type == new_window_type
            if not is_new_window and _same_origin(base_url, uri):
                return False
            decision.ignore()
            self._open_external_uri(uri)
            return True

        def _on_create_webview(self, _view: Any, navigation_action: Any) -> None:
            uri = navigation_action.get_request().get_uri()
            if uri:
                self._open_external_uri(uri)
            return None

        def _open_external_uri(self, uri: str) -> None:
            now = time.monotonic()
            if uri == self._last_external_uri and now - self._last_external_at < 0.75:
                return
            self._last_external_uri = uri
            self._last_external_at = now
            scheme = urllib.parse.urlsplit(uri).scheme.lower()
            try:
                if scheme == "winbox":
                    _launch_winbox(_winbox_target_from_uri(uri))
                elif scheme == "networkmap-ssh":
                    _launch_ssh(_ssh_target_from_uri(uri))
                else:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
            except (GLib.Error, RuntimeError, ValueError, UnicodeError) as exc:
                label = "WinBox" if scheme == "winbox" else "SSH" if scheme == "networkmap-ssh" else "external link"
                self._show_error(f"Could not open {label}", str(exc))

    application = NetworkMapApplication()
    return int(application.run([sys.argv[0]]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="networkmap",
        description=(
            "Open NetworkMap as an offline-first Linux desktop app. A local "
            "server is always started or reused; --server-url synchronizes "
            "that local copy with shared hosting."
        )
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--server-url",
        metavar="URL",
        help="hosted NetworkMap sync URL; remembered for future launches",
    )
    target.add_argument(
        "--local",
        action="store_true",
        help="work locally for this launch without using the saved sync URL",
    )
    target.add_argument(
        "--forget-server-url",
        action="store_true",
        help="forget the saved hosted sync URL and keep working locally",
    )
    parser.add_argument(
        "--no-remember",
        action="store_true",
        help="do not remember the URL supplied with --server-url",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", default=DEFAULT_PORT, type=_port, help=argparse.SUPPRESS)
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="local server data directory (default: server/XDG default)",
    )
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument(
        "--token",
        help="server token (prefer NETWORKMAP_TOKEN or --token-file)",
    )
    credentials.add_argument(
        "--token-file",
        metavar="PATH",
        help=(
            "read the server token from a UTF-8 file "
            "(fallback: ~/.config/networkmap/token)"
        ),
    )
    parser.add_argument(
        "--developer-tools",
        action="store_true",
        help="enable WebKit's web inspector",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check desktop runtime dependencies and exit",
    )
    parser.add_argument("launch_uri", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser


def _resolve_token(args: argparse.Namespace) -> Optional[str]:
    token: Optional[str]
    if args.token_file:
        token_path = Path(args.token_file).expanduser()
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"could not read token file: {exc}") from exc
    elif args.token is not None:
        token = args.token
        token_path = None
    elif os.environ.get("NETWORKMAP_TOKEN") is not None:
        token = os.environ.get("NETWORKMAP_TOKEN")
        token_path = None
    else:
        token_path = _default_token_path()
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = None
        except OSError as exc:
            raise ValueError(f"could not read default token file: {exc}") from exc
    if token_path is not None and token is not None:
        try:
            if token_path.stat().st_mode & 0o077:
                print(
                    f"warning: token file should only be readable by you: {token_path}",
                    file=sys.stderr,
                )
        except OSError:
            pass
    if token is not None:
        token = token.strip()
    return token or None


def main(argv: Optional[list[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.launch_uri:
        try:
            scheme = urllib.parse.urlsplit(args.launch_uri).scheme.lower()
            if scheme == "winbox":
                _launch_winbox(_winbox_target_from_uri(args.launch_uri))
            elif scheme == "networkmap-ssh":
                _launch_ssh(_ssh_target_from_uri(args.launch_uri))
            else:
                raise ValueError("unsupported launch URL")
        except (RuntimeError, ValueError, UnicodeError) as exc:
            parser.exit(1, f"{parser.prog}: error: {exc}\n")
        return 0
    if args.no_remember and not args.server_url:
        parser.error("--no-remember can only be used with --server-url")

    try:
        gtk_modules = _load_gtk()
    except RuntimeError as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")

    if args.check:
        print("GTK 3, WebKitGTK 4.1, and PyGObject are available.")
        return 0

    try:
        token = _resolve_token(args)
    except ValueError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")

    if args.forget_server_url:
        try:
            _forget_saved_server()
        except OSError as exc:
            parser.exit(1, f"{parser.prog}: error: could not update config: {exc}\n")

    explicit_remote = args.server_url
    environment_remote = os.environ.get("NETWORKMAP_SERVER_URL")
    saved_remote = _read_config().get("server_url")
    requested_remote = None
    if not args.local and not args.forget_server_url:
        requested_remote = explicit_remote or environment_remote or saved_remote

    remote_url: Optional[str] = None
    if requested_remote:
        try:
            remote_url = _normalise_server_url(str(requested_remote))
        except ValueError as exc:
            parser.exit(2, f"{parser.prog}: error: {exc}\n")
        if explicit_remote and not args.no_remember:
            try:
                _remember_server(remote_url)
            except OSError as exc:
                parser.exit(1, f"{parser.prog}: error: could not save config: {exc}\n")

    base_url = _loopback_url(args.host, args.port)
    if remote_url and _same_origin(base_url, remote_url):
        parser.error("--server-url must identify a different hosted server")

    try:
        server_module = _load_server_module()
        default_data_dir = getattr(server_module, "default_data_dir")
        demo_factory = getattr(server_module, "demo_document")
        data_dir = Path(args.data_dir).expanduser() if args.data_dir else Path(default_data_dir())
    except Exception as exc:
        parser.exit(1, f"{parser.prog}: error: could not load the local server: {exc}\n")

    local_server = LocalServer(
        host=args.host,
        port=args.port,
        data_dir=str(data_dir),
        token=token,
        server_module=server_module,
    )
    try:
        local_server.start()
    except Exception as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")

    sync_worker: Optional[SyncWorker] = None
    try:
        sync_worker = SyncWorker(
            base_url,
            remote_url,
            data_dir,
            local_token=token,
            remote_token=token,
            demo_document=demo_factory(),
        )
        sync_worker.start()
    except Exception as exc:
        print(f"warning: hosted synchronization could not start: {exc}", file=sys.stderr)

    initial_url = _tokenised_url(base_url, token)
    try:
        return _run_gui(
            base_url,
            initial_url,
            args.developer_tools,
            gtk_modules,
            remote_url,
        )
    finally:
        try:
            if sync_worker is not None:
                # The default wait is deliberate: never stop the local API
                # underneath a sync pass that may still be verifying a PUT.
                sync_worker.stop()
        finally:
            local_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())

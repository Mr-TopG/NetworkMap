#!/usr/bin/env python3
"""NetworkMap Linux desktop shell.

The shell embeds the web application with GTK 3 and WebKitGTK 4.1.  With no
arguments it starts (or reuses) the loopback NetworkMap server.  It can also
connect to a hosted instance; that choice is remembered without storing the
authentication token.

Only Python's standard library is used here apart from the distro-provided
PyGObject/WebKitGTK bindings.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
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


APP_NAME = "NetworkMap"
APP_ID = "io.github.networkmap.NetworkMap"
APP_VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
APP_DIR = Path(__file__).resolve().parent
WINBOX_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%\-\[\]]{0,252}$")


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


def _is_networkmap_server(base_url: str, timeout: float = 0.5) -> bool:
    request = urllib.request.Request(
        _api_url(base_url, "/api/health"),
        headers={"Accept": "application/json", "User-Agent": "NetworkMap-native"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (
        OSError,
        UnicodeError,
        ValueError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and "version" in payload
    )


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
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.token = token
        self.url = _loopback_url(host, port)
        self.httpd: Any = None
        self.thread: Optional[threading.Thread] = None
        self.reused = False

    def start(self) -> None:
        if _is_networkmap_server(self.url):
            self.reused = True
            return

        module = _load_server_module()
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
                if _is_networkmap_server(self.url):
                    self.reused = True
                    return
                time.sleep(0.1)
            raise RuntimeError(
                f"could not bind the local server at {self.url}: {exc}"
            ) from exc

        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="networkmap-http",
            daemon=True,
        )
        self.thread.start()
        for _ in range(100):
            if _is_networkmap_server(self.url):
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
) -> int:
    Gio, GLib, Gtk, WebKit2 = gtk_modules

    class NetworkMapApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id=APP_ID)
            self.window: Any = None
            self.webview: Any = None
            self.spinner: Any = None

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
            header.set_subtitle(base_url)
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
            if urllib.parse.urlsplit(uri).scheme.lower() == "winbox":
                try:
                    _launch_winbox(_winbox_target_from_uri(uri))
                except (RuntimeError, ValueError, UnicodeError) as exc:
                    self._show_error("Could not open WinBox", str(exc))
                return True
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as exc:
                self._show_error("Could not open external link", str(exc))
            return True

    application = NetworkMapApplication()
    return int(application.run([sys.argv[0]]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="networkmap",
        description=(
            "Open NetworkMap as a Linux desktop app. By default a local server "
            "is started or reused; --server-url connects to shared hosting."
        )
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--server-url",
        metavar="URL",
        help="hosted NetworkMap URL; remembered for future launches",
    )
    target.add_argument(
        "--local",
        action="store_true",
        help="use the local server for this launch, ignoring a saved URL",
    )
    target.add_argument(
        "--forget-server-url",
        action="store_true",
        help="forget the saved hosted URL and use the local server",
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
            _launch_winbox(_winbox_target_from_uri(args.launch_uri))
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

    local_server: Optional[LocalServer] = None
    if requested_remote:
        try:
            base_url = _normalise_server_url(str(requested_remote))
        except ValueError as exc:
            parser.exit(2, f"{parser.prog}: error: {exc}\n")
        if explicit_remote and not args.no_remember:
            try:
                _remember_server(base_url)
            except OSError as exc:
                parser.exit(1, f"{parser.prog}: error: could not save config: {exc}\n")
    else:
        base_url = _loopback_url(args.host, args.port)
        local_server = LocalServer(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            token=token,
        )
        try:
            local_server.start()
        except Exception as exc:
            parser.exit(1, f"{parser.prog}: error: {exc}\n")

    initial_url = _tokenised_url(base_url, token)
    try:
        return _run_gui(base_url, initial_url, args.developer_tools, gtk_modules)
    finally:
        if local_server is not None:
            local_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exercise a real temporary NetworkMap server through its public HTTP API."""

from __future__ import annotations

import http.cookiejar
import json
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server  # noqa: E402  (project root is intentionally added first)


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, object, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers=request_headers,
    )
    client = opener or urllib.request.build_opener()
    try:
        response = client.open(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    content_type = response.headers.get("Content-Type", "")
    value: object = raw
    if "application/json" in content_type and raw:
        value = json.loads(raw.decode("utf-8"))
    return response.status, value, response.headers


def expect_status(actual: int, expected: int, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected HTTP {expected}, received {actual}")


def run_workflow() -> None:
    with tempfile.TemporaryDirectory(prefix="networkmap-smoke-") as temporary:
        with server.ServerThread(
            port=0,
            data_dir=Path(temporary) / "data",
            static_dir=PROJECT_ROOT / "static",
        ) as running:
            status, health, _ = request(running.url, "/api/health")
            expect_status(status, 200, "health")
            assert isinstance(health, dict) and health["status"] == "ok"

            status, state, _ = request(running.url, "/api/state")
            expect_status(status, 200, "initial state")
            assert isinstance(state, dict) and len(state["nodes"]) >= 6
            revision = state["revision"]

            status, state, _ = request(
                running.url,
                "/api/nodes",
                method="POST",
                payload={
                    "id": "smoke-client",
                    "name": "Smoke-test laptop",
                    "kind": "laptop",
                    "ip": "192.168.1.240",
                    "status": "online",
                    "x": 920,
                    "y": 480,
                },
                headers={"If-Match": f'"{revision}"'},
            )
            expect_status(status, 201, "create node")
            assert isinstance(state, dict)
            revision = state["revision"]

            status, state, _ = request(
                running.url,
                "/api/links",
                method="POST",
                payload={
                    "id": "smoke-link",
                    "source": "demo-switch",
                    "target": "smoke-client",
                    "name": "QA link",
                    "kind": "ethernet",
                    "bandwidth_mbps": 1000,
                },
                headers={"If-Match": f'"{revision}"'},
            )
            expect_status(status, 201, "create link")
            assert isinstance(state, dict)
            revision = state["revision"]

            status, state, _ = request(
                running.url,
                "/api/settings",
                method="PATCH",
                payload={"name": "Smoke-test network"},
                headers={"If-Match": f'"{revision}"'},
            )
            expect_status(status, 200, "update settings")
            assert isinstance(state, dict) and state["settings"]["name"] == "Smoke-test network"

            status, exported, headers = request(running.url, "/api/export")
            expect_status(status, 200, "export")
            assert isinstance(exported, dict) and headers.get("Content-Disposition")

            status, error, _ = request(
                running.url,
                "/api/discovery",
                method="POST",
                payload={"cidr": "8.8.8.0/24"},
            )
            expect_status(status, 400, "public discovery guard")
            assert isinstance(error, dict) and error["error"]["code"] == "validation_error"

            with urllib.request.urlopen(running.url + "/", timeout=5) as response:
                page = response.read().decode("utf-8")
            assert "NetworkMap" in page and "/static/app.js" in page


def run_auth_workflow() -> None:
    with tempfile.TemporaryDirectory(prefix="networkmap-auth-smoke-") as temporary:
        with server.ServerThread(
            port=0,
            data_dir=Path(temporary) / "data",
            static_dir=PROJECT_ROOT / "static",
            token="smoke-secret",
        ) as running:
            status, _, _ = request(running.url, "/api/state")
            expect_status(status, 401, "unauthenticated state")

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            status, session, _ = request(
                running.url,
                "/api/session",
                method="POST",
                headers={"Authorization": "Bearer smoke-secret"},
                opener=opener,
            )
            expect_status(status, 200, "session bootstrap")
            assert isinstance(session, dict) and session["authenticated"] is True
            assert any(cookie.name == "networkmap_token" for cookie in jar)

            status, state, _ = request(running.url, "/api/state", opener=opener)
            expect_status(status, 200, "cookie-authenticated state")
            assert isinstance(state, dict) and state["revision"] >= 1

            assert "#token=" in running.browser_url
            assert "?token=" not in running.browser_url


def main() -> int:
    run_workflow()
    run_auth_workflow()
    print("NetworkMap smoke test passed (web, CRUD, backup, guardrails, and auth).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""NetworkMap's dependency-free HTTP, SQLite, discovery, and sync server.

The module is intentionally importable by the Linux launcher and by tests.  Run
it directly for the web application::

    python3 server.py
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ET


VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_DISCOVERY_HOSTS = 256

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

NODE_KINDS = {
    "router",
    "switch",
    "server",
    "workstation",
    "laptop",
    "mobile",
    "printer",
    "camera",
    "storage",
    "nas",
    "iot",
    "firewall",
    "access-point",
    "cloud",
    "unknown",
    "other",
}
NODE_STATUSES = {"online", "offline", "degraded", "unknown"}
LINK_KINDS = {
    "ethernet",
    "fiber",
    "wifi",
    "wireless",
    "vpn",
    "wan",
    "logical",
    "other",
}
LINK_STATUSES = {"active", "inactive", "degraded", "unknown"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "name": "Home Lab Demo",
    "description": "A starter topology. Every demo device can be edited or deleted.",
    "subnet": "192.168.1.0/24",
    "refresh_interval": 30,
    "show_link_labels": True,
    "compact_labels": False,
    "theme": "dark",
    "grid_size": 24,
    "snap_to_grid": True,
    "show_labels": True,
}


def utc_now() -> str:
    """Return a compact, sortable UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class APIError(Exception):
    """An error safe to serialize to an API client."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = HTTPStatus.BAD_REQUEST,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)
        self.details = dict(details) if details else None

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def validation_error(message: str, field: str | None = None) -> APIError:
    details = {"field": field} if field else None
    return APIError("validation_error", message, HTTPStatus.BAD_REQUEST, details)


def _require_object(value: Any, field: str = "body") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise validation_error(f"{field} must be a JSON object", field)
    return value


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: set[str], field: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise validation_error(
            f"Unknown {field} field(s): {', '.join(unknown)}", field
        )


def _string(
    value: Any,
    field: str,
    *,
    maximum: int,
    allow_empty: bool = True,
    strip: bool = True,
) -> str:
    if not isinstance(value, str):
        raise validation_error(f"{field} must be a string", field)
    result = value.strip() if strip else value
    if not allow_empty and not result:
        raise validation_error(f"{field} cannot be empty", field)
    if len(result) > maximum:
        raise validation_error(
            f"{field} must be at most {maximum} characters", field
        )
    return result


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise validation_error(f"{field} must be true or false", field)
    return value


def _number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise validation_error(f"{field} must be a number", field)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise validation_error(
            f"{field} must be between {minimum:g} and {maximum:g}", field
        )
    return value


def _choice(value: Any, field: str, choices: set[str]) -> str:
    result = _string(value, field, maximum=40, allow_empty=False).lower()
    if result not in choices:
        raise validation_error(
            f"{field} must be one of: {', '.join(sorted(choices))}", field
        )
    return result


def _identifier(value: Any, field: str = "id") -> str:
    result = _string(value, field, maximum=64, allow_empty=False)
    if not ID_RE.fullmatch(result):
        raise validation_error(
            f"{field} may contain letters, numbers, '.', '_', ':', and '-'", field
        )
    return result


def _ip(value: Any, field: str = "ip") -> str:
    result = _string(value, field, maximum=45)
    if not result:
        return ""
    try:
        return str(ipaddress.ip_address(result))
    except ValueError as exc:
        raise validation_error(f"{field} is not a valid IP address", field) from exc


def _mac(value: Any, field: str = "mac") -> str:
    result = _string(value, field, maximum=17)
    if not result:
        return ""
    if not MAC_RE.fullmatch(result):
        raise validation_error(
            f"{field} must look like 00:11:22:33:44:55", field
        )
    return result.replace("-", ":").lower()


def _hostname(value: Any, field: str = "hostname") -> str:
    result = _string(value, field, maximum=253).rstrip(".")
    if not result:
        return ""
    if any(not HOST_LABEL_RE.fullmatch(label) for label in result.split(".")):
        raise validation_error(f"{field} is not a valid host name", field)
    return result.lower()


def _management_url(value: Any, field: str = "management_url") -> str:
    result = _string(value, field, maximum=2_048)
    if not result:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in result):
        raise validation_error(f"{field} cannot contain whitespace", field)
    try:
        parsed = urlsplit(result)
        parsed.port
    except ValueError as exc:
        raise validation_error(f"{field} is not a valid URL", field) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise validation_error(f"{field} must be an http:// or https:// URL", field)
    if parsed.username is not None or parsed.password is not None:
        raise validation_error(
            f"{field} must not contain embedded credentials", field
        )
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _json_object(value: Any, field: str) -> dict[str, Any]:
    obj = _require_object(value, field)

    def walk(item: Any, path: str, depth: int) -> None:
        if depth > 8:
            raise validation_error(f"{field} is nested too deeply", field)
        if item is None or isinstance(item, (str, bool)):
            if isinstance(item, str) and len(item) > 10_000:
                raise validation_error(f"String at {path} is too long", field)
            return
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if isinstance(item, float) and not math.isfinite(item):
                raise validation_error(f"Number at {path} must be finite", field)
            return
        if isinstance(item, list):
            if len(item) > 256:
                raise validation_error(f"Array at {path} is too large", field)
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]", depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 256:
                raise validation_error(f"Object at {path} is too large", field)
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 120:
                    raise validation_error(
                        f"Keys at {path} must be 1-120 character strings", field
                    )
                walk(child, f"{path}.{key}", depth + 1)
            return
        raise validation_error(f"Unsupported value at {path}", field)

    walk(obj, field, 0)
    return deepcopy(obj)


NODE_FIELDS = {
    "id",
    "name",
    "kind",
    "ip",
    "mac",
    "hostname",
    "vendor",
    "management_url",
    "winbox_enabled",
    "status",
    "x",
    "y",
    "notes",
    "tags",
    "config",
}


def validate_node(
    raw: Any,
    *,
    partial: bool = False,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    obj = _require_object(raw, "node")
    _reject_unknown_fields(obj, NODE_FIELDS, "node")
    defaults = {
        "id": uuid.uuid4().hex,
        "kind": "unknown",
        "ip": "",
        "mac": "",
        "hostname": "",
        "vendor": "",
        "management_url": "",
        "winbox_enabled": False,
        "status": "unknown",
        "x": 0,
        "y": 0,
        "notes": "",
        "tags": [],
        "config": {},
    }
    if partial:
        if current is None:
            raise RuntimeError("current node is required for a partial update")
        if "id" in obj and obj["id"] != current["id"]:
            raise validation_error("id cannot be changed", "id")
        values = defaults
        values.update(current)
        values.update(obj)
    else:
        values = defaults
        values.update(obj)
        if "name" not in obj:
            raise validation_error("name is required", "name")

    node: dict[str, Any] = {
        "id": _identifier(values["id"]),
        "name": _string(values["name"], "name", maximum=120, allow_empty=False),
        "kind": _choice(values["kind"], "kind", NODE_KINDS),
        "ip": _ip(values["ip"]),
        "mac": _mac(values["mac"]),
        "hostname": _hostname(values["hostname"]),
        "vendor": _string(values["vendor"], "vendor", maximum=120),
        "management_url": _management_url(values["management_url"]),
        "winbox_enabled": _boolean(values["winbox_enabled"], "winbox_enabled"),
        "status": _choice(values["status"], "status", NODE_STATUSES),
        "x": _number(values["x"], "x", minimum=-100_000, maximum=100_000),
        "y": _number(values["y"], "y", minimum=-100_000, maximum=100_000),
        "notes": _string(values["notes"], "notes", maximum=10_000, strip=False),
        "config": _json_object(values["config"], "config"),
    }
    tags = values["tags"]
    if not isinstance(tags, list) or len(tags) > 32:
        raise validation_error("tags must be an array with at most 32 items", "tags")
    normalized_tags: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(tags):
        clean = _string(
            tag, f"tags[{index}]", maximum=40, allow_empty=False
        )
        folded = clean.casefold()
        if folded not in seen:
            normalized_tags.append(clean)
            seen.add(folded)
    node["tags"] = normalized_tags
    return node


LINK_FIELDS = {
    "id",
    "source",
    "target",
    "label",
    "name",
    "kind",
    "status",
    "directed",
    "bandwidth_mbps",
    "notes",
    "config",
}


def validate_link(
    raw: Any,
    *,
    partial: bool = False,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    obj = dict(_require_object(raw, "link"))
    _reject_unknown_fields(obj, LINK_FIELDS, "link")
    if "label" in obj:
        if "name" in obj and obj["name"] != obj["label"]:
            raise validation_error("label and legacy name values disagree", "label")
        obj["name"] = obj.pop("label")
    if partial:
        if current is None:
            raise RuntimeError("current link is required for a partial update")
        current = dict(current)
        if "name" not in current and "label" in current:
            current["name"] = current.pop("label")
        if "id" in obj and obj["id"] != current["id"]:
            raise validation_error("id cannot be changed", "id")
        values = dict(current)
        values.update(obj)
    else:
        values = {
            "id": uuid.uuid4().hex,
            "name": "",
            "kind": "ethernet",
            "status": "active",
            "directed": False,
            "bandwidth_mbps": None,
            "notes": "",
            "config": {},
        }
        values.update(obj)
        for required in ("source", "target"):
            if required not in obj:
                raise validation_error(f"{required} is required", required)

    source = _identifier(values["source"], "source")
    target = _identifier(values["target"], "target")
    if source == target:
        raise validation_error("A link must connect two different nodes", "target")
    bandwidth = values["bandwidth_mbps"]
    if bandwidth is not None:
        bandwidth = _number(
            bandwidth,
            "bandwidth_mbps",
            minimum=0,
            maximum=1_000_000_000,
        )
    return {
        "id": _identifier(values["id"]),
        "source": source,
        "target": target,
        "name": _string(values["name"], "name", maximum=120),
        "kind": _choice(values["kind"], "kind", LINK_KINDS),
        "status": _choice(values["status"], "status", LINK_STATUSES),
        "directed": _boolean(values["directed"], "directed"),
        "bandwidth_mbps": bandwidth,
        "notes": _string(values["notes"], "notes", maximum=10_000, strip=False),
        "config": _json_object(values["config"], "config"),
    }


SETTINGS_FIELDS = set(DEFAULT_SETTINGS)


def validate_settings(raw: Any, *, partial: bool = False) -> dict[str, Any]:
    obj = _require_object(raw, "settings")
    _reject_unknown_fields(obj, SETTINGS_FIELDS, "settings")
    values = {} if partial else deepcopy(DEFAULT_SETTINGS)
    values.update(obj)
    result: dict[str, Any] = {}
    if "name" in values:
        result["name"] = _string(
            values["name"], "name", maximum=120, allow_empty=False
        )
    if "description" in values:
        result["description"] = _string(
            values["description"], "description", maximum=2_000, strip=False
        )
    if "subnet" in values:
        subnet = _string(values["subnet"], "subnet", maximum=64)
        if subnet:
            validate_discovery_network(subnet)
        result["subnet"] = subnet
    if "refresh_interval" in values:
        interval = values["refresh_interval"]
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 0 <= interval <= 86_400
        ):
            raise validation_error(
                "refresh_interval must be an integer from 0 to 86400 seconds",
                "refresh_interval",
            )
        result["refresh_interval"] = interval
    if "theme" in values:
        result["theme"] = _choice(
            values["theme"], "theme", {"dark", "light", "system"}
        )
    if "grid_size" in values:
        size = values["grid_size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 8 <= size <= 200:
            raise validation_error(
                "grid_size must be an integer between 8 and 200", "grid_size"
            )
        result["grid_size"] = size
    for key in (
        "show_link_labels",
        "compact_labels",
        "snap_to_grid",
        "show_labels",
    ):
        if key in values:
            result[key] = _boolean(values[key], key)
    return result


def demo_document() -> dict[str, Any]:
    """Return a fresh, fully editable sample topology for first launch/reset."""

    nodes = [
        {
            "id": "demo-internet",
            "name": "Internet",
            "kind": "cloud",
            "status": "online",
            "x": 100,
            "y": 280,
            "notes": "Public network",
        },
        {
            "id": "demo-gateway",
            "name": "Gateway",
            "kind": "router",
            "ip": "192.168.1.1",
            "hostname": "gateway.local",
            "vendor": "MikroTik",
            "management_url": "https://192.168.1.1",
            "winbox_enabled": True,
            "status": "online",
            "x": 330,
            "y": 280,
            "notes": "Edit or delete any demo device.",
        },
        {
            "id": "demo-switch",
            "name": "Core Switch",
            "kind": "switch",
            "ip": "192.168.1.2",
            "hostname": "switch.local",
            "status": "online",
            "x": 560,
            "y": 280,
        },
        {
            "id": "demo-ap",
            "name": "Wi-Fi AP",
            "kind": "access-point",
            "ip": "192.168.1.3",
            "hostname": "ap.local",
            "status": "online",
            "x": 760,
            "y": 130,
        },
        {
            "id": "demo-server",
            "name": "Home Server",
            "kind": "server",
            "ip": "192.168.1.10",
            "hostname": "server.local",
            "status": "online",
            "x": 790,
            "y": 300,
            "tags": ["demo", "infrastructure"],
            "config": {"services": ["files", "backups"]},
        },
        {
            "id": "demo-workstation",
            "name": "Workstation",
            "kind": "workstation",
            "ip": "192.168.1.101",
            "hostname": "workstation.local",
            "status": "online",
            "x": 760,
            "y": 470,
            "tags": ["demo"],
        },
    ]
    links = [
        {
            "id": "demo-link-wan",
            "source": "demo-internet",
            "target": "demo-gateway",
            "name": "WAN",
            "kind": "wan",
        },
        {
            "id": "demo-link-uplink",
            "source": "demo-gateway",
            "target": "demo-switch",
            "name": "1 GbE",
            "bandwidth_mbps": 1000,
        },
        {
            "id": "demo-link-ap",
            "source": "demo-switch",
            "target": "demo-ap",
            "name": "PoE",
        },
        {
            "id": "demo-link-server",
            "source": "demo-switch",
            "target": "demo-server",
            "name": "1 GbE",
            "bandwidth_mbps": 1000,
        },
        {
            "id": "demo-link-workstation",
            "source": "demo-switch",
            "target": "demo-workstation",
            "name": "LAN",
        },
    ]
    return {
        "nodes": [validate_node(node) for node in nodes],
        "links": [validate_link(link) for link in links],
        "settings": validate_settings(DEFAULT_SETTINGS),
    }


def validate_state_document(raw: Any) -> dict[str, Any]:
    obj = _require_object(raw, "state")
    allowed = {
        "nodes",
        "links",
        "settings",
        "revision",
        "updated_at",
        "exported_at",
        "format",
        "version",
    }
    _reject_unknown_fields(obj, allowed, "state")
    for required in ("nodes", "links", "settings"):
        if required not in obj:
            raise validation_error(f"{required} is required", required)
    if not isinstance(obj["nodes"], list) or len(obj["nodes"]) > 10_000:
        raise validation_error("nodes must be an array of at most 10,000 items", "nodes")
    if not isinstance(obj["links"], list) or len(obj["links"]) > 50_000:
        raise validation_error("links must be an array of at most 50,000 items", "links")
    nodes = [validate_node(item) for item in obj["nodes"]]
    node_ids = [item["id"] for item in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise validation_error("Node IDs must be unique", "nodes")
    links = [validate_link(item) for item in obj["links"]]
    link_ids = [item["id"] for item in links]
    if len(set(link_ids)) != len(link_ids):
        raise validation_error("Link IDs must be unique", "links")
    known_nodes = set(node_ids)
    for index, link in enumerate(links):
        for endpoint in ("source", "target"):
            if link[endpoint] not in known_nodes:
                raise validation_error(
                    f"links[{index}].{endpoint} does not identify a node",
                    f"links[{index}].{endpoint}",
                )
    return {
        "nodes": nodes,
        "links": links,
        "settings": validate_settings(obj["settings"]),
    }


class EventBroker:
    """A small in-process fan-out broker used by Server-Sent Events clients."""

    _CLOSED = object()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, queue.Queue[Any]] = {}
        self._next_id = 1
        self._closed = False

    def subscribe(self) -> tuple[int, queue.Queue[Any]]:
        with self._lock:
            if self._closed:
                raise RuntimeError("event broker is closed")
            subscription_id = self._next_id
            self._next_id += 1
            inbox: queue.Queue[Any] = queue.Queue(maxsize=16)
            self._subscribers[subscription_id] = inbox
            return subscription_id, inbox

    def unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def publish(self, event: Mapping[str, Any]) -> None:
        payload = dict(event)
        with self._lock:
            subscribers = list(self._subscribers.values())
        for inbox in subscribers:
            try:
                inbox.put_nowait(payload)
            except queue.Full:
                with suppress(queue.Empty):
                    inbox.get_nowait()
                with suppress(queue.Full):
                    inbox.put_nowait(payload)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for inbox in subscribers:
            with suppress(queue.Full):
                inbox.put_nowait(self._CLOSED)


class StateStore:
    """Atomic SQLite storage for a complete NetworkMap document."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        on_change: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._created_data_directory = False
        try:
            self.database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
            self._created_data_directory = True
        except FileExistsError:
            if not self.database_path.parent.is_dir():
                raise
        if self._created_data_directory:
            os.chmod(self.database_path.parent, 0o700)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.database_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._lock = threading.RLock()
        self._on_change = on_change
        self._initialize()

    def _secure_storage_permissions(self) -> None:
        # A caller may deliberately place the database in an existing shared
        # directory. Never rewrite that directory's permissions; only enforce
        # mode 0700 when NetworkMap created the dedicated directory itself.
        if self._created_data_directory:
            os.chmod(self.database_path.parent, 0o700)
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def _close(self, connection: sqlite3.Connection) -> None:
        # Sidecar files can be created after _connect(), so secure them while
        # the connection is still holding them open as well as the main DB.
        self._secure_storage_permissions()
        connection.close()
        self._secure_storage_permissions()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._secure_storage_permissions()
        return connection

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> Any:
        return json.loads(value)

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS nodes (
                        id TEXT PRIMARY KEY,
                        data TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS links (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        target TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        data TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        data TEXT NOT NULL
                    )
                    """
                )
                exists = connection.execute(
                    "SELECT 1 FROM metadata WHERE singleton = 1"
                ).fetchone()
                if exists is None:
                    sample = demo_document()
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO metadata(singleton, revision, updated_at) VALUES(1, 1, ?)",
                        (now,),
                    )
                    self._insert_document(connection, sample)
                else:
                    # Normalize settings from early development builds without
                    # making an ordinary upgrade look like a user edit.
                    settings_row = connection.execute(
                        "SELECT data FROM settings WHERE singleton = 1"
                    ).fetchone()
                    if settings_row is None:
                        connection.execute(
                            "INSERT INTO settings(singleton, data) VALUES(1, ?)",
                            (self._dump(validate_settings(DEFAULT_SETTINGS)),),
                        )
                    else:
                        stored_settings = self._load(settings_row["data"])
                        if not isinstance(stored_settings, dict):
                            raise RuntimeError("NetworkMap settings are corrupt")
                        if "name" not in stored_settings and "network_name" in stored_settings:
                            stored_settings["name"] = stored_settings["network_name"]
                        if "subnet" not in stored_settings and "discovery_cidr" in stored_settings:
                            stored_settings["subnet"] = stored_settings["discovery_cidr"]
                        migrated = deepcopy(DEFAULT_SETTINGS)
                        migrated.update(
                            {
                                key: value
                                for key, value in stored_settings.items()
                                if key in SETTINGS_FIELDS
                            }
                        )
                        migrated = validate_settings(migrated)
                        connection.execute(
                            "UPDATE settings SET data = ? WHERE singleton = 1",
                            (self._dump(migrated),),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._close(connection)

    def _insert_document(
        self, connection: sqlite3.Connection, document: Mapping[str, Any]
    ) -> None:
        for node in document["nodes"]:
            connection.execute(
                "INSERT INTO nodes(id, data) VALUES(?, ?)",
                (node["id"], self._dump(node)),
            )
        for link in document["links"]:
            connection.execute(
                "INSERT INTO links(id, source, target, data) VALUES(?, ?, ?, ?)",
                (link["id"], link["source"], link["target"], self._dump(link)),
            )
        connection.execute(
            "INSERT INTO settings(singleton, data) VALUES(1, ?)",
            (self._dump(document["settings"]),),
        )

    def _read_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        metadata = connection.execute(
            "SELECT revision, updated_at FROM metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RuntimeError("NetworkMap database metadata is missing")
        settings_row = connection.execute(
            "SELECT data FROM settings WHERE singleton = 1"
        ).fetchone()
        if settings_row is None:
            raise RuntimeError("NetworkMap database settings are missing")
        return {
            "revision": metadata["revision"],
            "updated_at": metadata["updated_at"],
            "nodes": [
                self._load(row["data"])
                for row in connection.execute("SELECT data FROM nodes ORDER BY rowid")
            ],
            "links": [
                self._load(row["data"])
                for row in connection.execute("SELECT data FROM links ORDER BY rowid")
            ],
            "settings": self._load(settings_row["data"]),
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                state = self._read_state(connection)
                connection.commit()
                return state
            except Exception:
                connection.rollback()
                raise
            finally:
                self._close(connection)

    def get_metadata(self) -> dict[str, Any]:
        """Read the lightweight revision record used by health and SSE."""

        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT revision, updated_at FROM metadata WHERE singleton = 1"
                ).fetchone()
            finally:
                self._close(connection)
        if row is None:
            raise RuntimeError("NetworkMap database metadata is missing")
        return {"revision": int(row["revision"]), "updated_at": row["updated_at"]}

    @staticmethod
    def _assert_revision(
        connection: sqlite3.Connection, expected_revision: int | None
    ) -> None:
        if expected_revision is None:
            return
        row = connection.execute(
            "SELECT revision FROM metadata WHERE singleton = 1"
        ).fetchone()
        current = int(row["revision"])
        if current != expected_revision:
            raise APIError(
                "revision_conflict",
                "The network changed after this client loaded it",
                HTTPStatus.CONFLICT,
                {"expected_revision": expected_revision, "current_revision": current},
            )

    def _mutate(
        self,
        operation: Callable[[sqlite3.Connection], None],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_revision(connection, expected_revision)
                operation(connection)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE metadata
                    SET revision = revision + 1, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (now,),
                )
                state = self._read_state(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._close(connection)
        if self._on_change:
            self._on_change(
                {"revision": state["revision"], "updated_at": state["updated_at"]}
            )
        return state

    @staticmethod
    def _missing(resource: str, resource_id: str) -> APIError:
        return APIError(
            "not_found",
            f"{resource.capitalize()} '{resource_id}' was not found",
            HTTPStatus.NOT_FOUND,
        )

    def get_node(self, node_id: str) -> dict[str, Any]:
        node_id = _identifier(node_id)
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT data FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
            finally:
                self._close(connection)
        if row is None:
            raise self._missing("node", node_id)
        return self._load(row["data"])

    def create_node(
        self, raw: Any, expected_revision: int | None = None
    ) -> dict[str, Any]:
        node = validate_node(raw)

        def operation(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO nodes(id, data) VALUES(?, ?)",
                    (node["id"], self._dump(node)),
                )
            except sqlite3.IntegrityError as exc:
                raise APIError(
                    "already_exists",
                    f"Node '{node['id']}' already exists",
                    HTTPStatus.CONFLICT,
                ) from exc

        return self._mutate(operation, expected_revision)

    def update_node(
        self,
        node_id: str,
        raw: Any,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        node_id = _identifier(node_id)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT data FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise self._missing("node", node_id)
            updated = validate_node(raw, partial=True, current=self._load(row["data"]))
            connection.execute(
                "UPDATE nodes SET data = ? WHERE id = ?",
                (self._dump(updated), node_id),
            )

        return self._mutate(operation, expected_revision)

    def delete_node(
        self, node_id: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        node_id = _identifier(node_id)

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            if cursor.rowcount == 0:
                raise self._missing("node", node_id)

        return self._mutate(operation, expected_revision)

    def get_link(self, link_id: str) -> dict[str, Any]:
        link_id = _identifier(link_id)
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT data FROM links WHERE id = ?", (link_id,)
                ).fetchone()
            finally:
                self._close(connection)
        if row is None:
            raise self._missing("link", link_id)
        return self._load(row["data"])

    @staticmethod
    def _assert_link_nodes(
        connection: sqlite3.Connection, link: Mapping[str, Any]
    ) -> None:
        for endpoint in ("source", "target"):
            exists = connection.execute(
                "SELECT 1 FROM nodes WHERE id = ?", (link[endpoint],)
            ).fetchone()
            if exists is None:
                raise validation_error(
                    f"{endpoint} does not identify an existing node", endpoint
                )

    def create_link(
        self, raw: Any, expected_revision: int | None = None
    ) -> dict[str, Any]:
        link = validate_link(raw)

        def operation(connection: sqlite3.Connection) -> None:
            self._assert_link_nodes(connection, link)
            try:
                connection.execute(
                    "INSERT INTO links(id, source, target, data) VALUES(?, ?, ?, ?)",
                    (link["id"], link["source"], link["target"], self._dump(link)),
                )
            except sqlite3.IntegrityError as exc:
                raise APIError(
                    "already_exists",
                    f"Link '{link['id']}' already exists",
                    HTTPStatus.CONFLICT,
                ) from exc

        return self._mutate(operation, expected_revision)

    def update_link(
        self,
        link_id: str,
        raw: Any,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        link_id = _identifier(link_id)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT data FROM links WHERE id = ?", (link_id,)
            ).fetchone()
            if row is None:
                raise self._missing("link", link_id)
            updated = validate_link(
                raw, partial=True, current=self._load(row["data"])
            )
            self._assert_link_nodes(connection, updated)
            connection.execute(
                """
                UPDATE links SET source = ?, target = ?, data = ? WHERE id = ?
                """,
                (
                    updated["source"],
                    updated["target"],
                    self._dump(updated),
                    link_id,
                ),
            )

        return self._mutate(operation, expected_revision)

    def delete_link(
        self, link_id: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        link_id = _identifier(link_id)

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute("DELETE FROM links WHERE id = ?", (link_id,))
            if cursor.rowcount == 0:
                raise self._missing("link", link_id)

        return self._mutate(operation, expected_revision)

    def update_settings(
        self, raw: Any, expected_revision: int | None = None
    ) -> dict[str, Any]:
        patch = validate_settings(raw, partial=True)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT data FROM settings WHERE singleton = 1"
            ).fetchone()
            current = self._load(row["data"])
            current.update(patch)
            complete = validate_settings(current)
            connection.execute(
                "UPDATE settings SET data = ? WHERE singleton = 1",
                (self._dump(complete),),
            )

        return self._mutate(operation, expected_revision)

    def replace_state(
        self, raw: Any, expected_revision: int | None = None
    ) -> dict[str, Any]:
        document = validate_state_document(raw)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM links")
            connection.execute("DELETE FROM nodes")
            connection.execute("DELETE FROM settings")
            self._insert_document(connection, document)

        return self._mutate(operation, expected_revision)

    def reset_demo(self, expected_revision: int | None = None) -> dict[str, Any]:
        return self.replace_state(demo_document(), expected_revision)


PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PRIVATE_IPV6_NETWORK = ipaddress.ip_network("fc00::/7")


def validate_discovery_network(value: Any) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Validate a small, explicitly private network safe for active discovery."""

    cidr = _string(value, "cidr", maximum=64, allow_empty=False)
    try:
        network = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        raise validation_error(
            "cidr must be a canonical network such as 192.168.1.0/24", "cidr"
        ) from exc
    if network.num_addresses > MAX_DISCOVERY_HOSTS:
        minimum_prefix = 24 if network.version == 4 else 120
        raise validation_error(
            f"Discovery is limited to {MAX_DISCOVERY_HOSTS} addresses; use /{minimum_prefix} or narrower",
            "cidr",
        )
    if network.version == 4:
        private = any(network.subnet_of(candidate) for candidate in PRIVATE_IPV4_NETWORKS)
    else:
        private = network.subnet_of(PRIVATE_IPV6_NETWORK)
    if not private:
        raise validation_error(
            "Discovery is restricted to RFC1918 IPv4 or unique-local IPv6 networks",
            "cidr",
        )
    return network


def _merge_discovered_device(
    devices: dict[str, dict[str, Any]],
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
    *,
    ip: str,
    mac: str = "",
    hostname: str = "",
    vendor: str = "",
    status: str = "online",
) -> None:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return
    if address not in network:
        return
    canonical_ip = str(address)
    item = devices.setdefault(
        canonical_ip,
        {
            "ip": canonical_ip,
            "mac": "",
            "hostname": "",
            "vendor": "",
            "status": "unknown",
        },
    )
    if mac and MAC_RE.fullmatch(mac):
        item["mac"] = mac.replace("-", ":").lower()
    if hostname:
        with suppress(APIError):
            item["hostname"] = _hostname(hostname)
    if vendor:
        item["vendor"] = vendor.strip()[:120]
    if status == "online" or item["status"] == "unknown":
        item["status"] = status


def _parse_ip_neigh(
    output: str,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
    devices: dict[str, dict[str, Any]],
) -> None:
    unavailable = {"FAILED", "INCOMPLETE", "NOARP"}
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        address = fields[0]
        mac = ""
        if "lladdr" in fields:
            index = fields.index("lladdr")
            if index + 1 < len(fields):
                mac = fields[index + 1]
        neighbor_state = fields[-1].upper()
        status = "unknown" if neighbor_state in unavailable else "online"
        _merge_discovered_device(
            devices, network, ip=address, mac=mac, status=status
        )


def _parse_nmap_xml(
    output: str,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
    devices: dict[str, dict[str, Any]],
) -> None:
    root = ET.fromstring(output)
    for host in root.findall("host"):
        status_node = host.find("status")
        if status_node is None or status_node.get("state") != "up":
            continue
        ip = ""
        mac = ""
        vendor = ""
        for address in host.findall("address"):
            kind = address.get("addrtype")
            if kind in {"ipv4", "ipv6"}:
                ip = address.get("addr", "")
            elif kind == "mac":
                mac = address.get("addr", "")
                vendor = address.get("vendor", "")
        hostname = ""
        hostname_node = host.find("hostnames/hostname")
        if hostname_node is not None:
            hostname = hostname_node.get("name", "")
        if ip:
            _merge_discovered_device(
                devices,
                network,
                ip=ip,
                mac=mac,
                hostname=hostname,
                vendor=vendor,
                status="online",
            )


def discover_network(
    cidr: Any,
    *,
    use_nmap: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Discover neighbors in a validated private CIDR without invoking a shell."""

    if type(use_nmap) is not bool:
        raise validation_error("use_nmap must be true or false", "use_nmap")
    network = validate_discovery_network(cidr)
    run = runner or subprocess.run
    find_executable = which or shutil.which
    devices: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    warnings: list[str] = []

    ip_binary = find_executable("ip")
    if ip_binary:
        try:
            result = run(
                [ip_binary, "neigh", "show"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                _parse_ip_neigh(result.stdout, network, devices)
                sources.append("ip-neigh")
            else:
                warnings.append("ip neigh did not complete successfully")
        except (OSError, subprocess.SubprocessError):
            warnings.append("ip neigh could not be run")
    else:
        warnings.append("The ip command is not installed")

    if use_nmap:
        nmap_binary = find_executable("nmap")
        if not nmap_binary:
            warnings.append("nmap was requested but is not installed")
        else:
            try:
                nmap_arguments = [nmap_binary]
                if network.version == 6:
                    nmap_arguments.append("-6")
                nmap_arguments.extend(["-sn", "-n", "-oX", "-", str(network)])
                result = run(
                    nmap_arguments,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode == 0:
                    _parse_nmap_xml(result.stdout, network, devices)
                    sources.append("nmap")
                else:
                    warnings.append("nmap did not complete successfully")
            except subprocess.TimeoutExpired:
                warnings.append("nmap timed out after 45 seconds")
            except (OSError, subprocess.SubprocessError, ET.ParseError):
                warnings.append("nmap output could not be read")

    ordered = sorted(
        devices.values(), key=lambda item: int(ipaddress.ip_address(item["ip"]))
    )
    return {
        "cidr": str(network),
        "scanned_at": utc_now(),
        "sources": sources,
        "devices": ordered,
        "warnings": warnings,
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_json_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise APIError(
            "invalid_json", "The request body is not valid JSON", HTTPStatus.BAD_REQUEST
        ) from exc


def _cookie_token(token: str) -> str:
    # The cookie is a session credential, not a copy of the bearer token.  A
    # domain separator prevents the digest being confused with an unrelated
    # plain SHA-256 use elsewhere.
    return hashlib.sha256(b"networkmap-session\0" + token.encode("utf-8")).hexdigest()


def _auth_cookie_header(token: str) -> str:
    return (
        f"networkmap_token={_cookie_token(token)}; "
        "Path=/; HttpOnly; SameSite=Strict"
    )


def _host_for_url(host: str) -> str:
    if host in {"0.0.0.0", ""}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _authority_is_loopback(authority: str) -> bool:
    if not authority or any(character.isspace() for character in authority):
        return False
    try:
        parsed = urlsplit(f"//{authority}")
        # Accessing .port also validates malformed/non-numeric ports.
        parsed.port
    except ValueError:
        return False
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return _is_loopback_host(parsed.hostname.rstrip("."))


def _origin_is_loopback(origin: str) -> bool:
    if origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    return _is_loopback_host(parsed.hostname.rstrip("."))


class NetworkMapHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        store: StateStore,
        broker: EventBroker,
        token: str | None,
        static_dir: Path,
        display_host: str,
    ) -> None:
        self.store = store
        self.broker = broker
        self.token = token.strip() if token and token.strip() else None
        self.static_dir = static_dir.resolve()
        self.display_host = display_host
        self._networkmap_closed = False
        super().__init__(server_address, handler_class)

    @property
    def url(self) -> str:
        return f"http://{_host_for_url(self.display_host)}:{self.server_address[1]}"

    @property
    def browser_url(self) -> str:
        if self.token:
            # URL fragments are handled by the frontend and are never sent in
            # an HTTP request, proxy access log, or Referer header.
            return f"{self.url}/#token={quote(self.token, safe='')}"
        return f"{self.url}/"

    def server_close(self) -> None:
        if not self._networkmap_closed:
            self._networkmap_closed = True
            self.broker.close()
        super().server_close()


class IPv6NetworkMapHTTPServer(NetworkMapHTTPServer):
    address_family = socket.AF_INET6


class NetworkMapRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"NetworkMap/{VERSION}"
    sys_version = ""

    @property
    def app_server(self) -> NetworkMapHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _handle_request(self) -> None:
        try:
            parsed = urlsplit(self.path)
            self._protect_tokenless_loopback()
            if self.command == "GET" and self._bootstrap_browser_token(parsed):
                return
            if parsed.path == "/api/health":
                if self.command not in {"GET", "HEAD"}:
                    raise self._method_not_allowed("GET, HEAD")
                state = self.app_server.store.get_metadata()
                self._send_json(
                    {
                        "status": "ok",
                        "version": VERSION,
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                    }
                )
                return
            if parsed.path.startswith("/api/") or parsed.path == "/api":
                self._authenticate(parsed)
                if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                    self._check_origin()
                self._route_api(parsed)
                return
            if self.command not in {"GET", "HEAD"}:
                raise self._method_not_allowed("GET, HEAD")
            self._serve_static(parsed.path)
        except APIError as exc:
            self._send_json(exc.payload(), status=exc.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.log_error("Unhandled server error: %r", exc)
            self._send_json(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "An unexpected server error occurred",
                    }
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _method_not_allowed(allowed: str) -> APIError:
        return APIError(
            "method_not_allowed",
            f"Method not allowed; use {allowed}",
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    def _bootstrap_browser_token(self, parsed: Any) -> bool:
        configured = self.app_server.token
        if not configured or parsed.path.startswith("/api/"):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "token" not in query:
            return False
        candidates = query["token"]
        if len(candidates) != 1 or not secrets.compare_digest(candidates[0], configured):
            raise APIError(
                "unauthorized", "The supplied access token is invalid", HTTPStatus.UNAUTHORIZED
            )
        remaining = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "token"
        ]
        location = urlunsplit(("", "", parsed.path or "/", urlencode(remaining), ""))
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header(
            "Set-Cookie",
            _auth_cookie_header(configured),
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _authenticate(self, parsed: Any) -> None:
        configured = self.app_server.token
        if not configured:
            return
        supplied: str | None = None
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if supplied is None:
            cookie_header = self.headers.get("Cookie", "")
            cookie = SimpleCookie()
            with suppress(Exception):
                cookie.load(cookie_header)
            morsel = cookie.get("networkmap_token")
            if morsel and secrets.compare_digest(morsel.value, _cookie_token(configured)):
                return
        if supplied is None and parsed.path == "/api/events":
            values = parse_qs(parsed.query, keep_blank_values=True).get("token", [])
            if len(values) == 1:
                supplied = values[0]
        if supplied is None or not secrets.compare_digest(supplied, configured):
            raise APIError(
                "unauthorized",
                "A valid bearer token is required",
                HTTPStatus.UNAUTHORIZED,
            )

    def _protect_tokenless_loopback(self) -> None:
        """Reject DNS-rebinding authorities on the convenient no-token mode."""

        if self.app_server.token or not _is_loopback_host(self.app_server.display_host):
            return
        authority = self.headers.get("Host", "")
        if not _authority_is_loopback(authority):
            raise APIError(
                "invalid_host",
                "The Host header is not valid for this loopback server",
                HTTPStatus.MISDIRECTED_REQUEST,
            )
        origin = self.headers.get("Origin")
        if origin and not _origin_is_loopback(origin):
            raise APIError(
                "forbidden_origin",
                "A non-loopback Origin cannot access this loopback server",
                HTTPStatus.FORBIDDEN,
            )

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        if origin == "null":
            raise APIError(
                "forbidden_origin",
                "Opaque-origin state changes are not allowed",
                HTTPStatus.FORBIDDEN,
            )
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != self.headers.get(
            "Host", ""
        ):
            raise APIError(
                "forbidden_origin",
                "Cross-origin state changes are not allowed",
                HTTPStatus.FORBIDDEN,
            )

    def _expected_revision(self) -> int | None:
        value = self.headers.get("If-Match")
        if value is None or value.strip() == "*":
            return None
        value = value.strip()
        if value.startswith("W/"):
            value = value[2:].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        try:
            revision = int(value)
        except ValueError as exc:
            raise APIError(
                "invalid_if_match",
                "If-Match must contain one numeric revision",
                HTTPStatus.BAD_REQUEST,
            ) from exc
        if revision < 0:
            raise APIError(
                "invalid_if_match",
                "If-Match revision cannot be negative",
                HTTPStatus.BAD_REQUEST,
            )
        return revision

    def _read_json(self) -> Any:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise APIError(
                "unsupported_media_type",
                "Content-Type must be application/json",
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise APIError(
                "length_required", "Content-Length is required", HTTPStatus.LENGTH_REQUIRED
            )
        try:
            length = int(length_header)
        except ValueError as exc:
            raise APIError(
                "invalid_content_length",
                "Content-Length must be an integer",
                HTTPStatus.BAD_REQUEST,
            ) from exc
        if length <= 0:
            self.close_connection = True
            raise APIError(
                "invalid_json", "A JSON request body is required", HTTPStatus.BAD_REQUEST
            )
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            raise APIError(
                "request_too_large",
                f"JSON bodies are limited to {MAX_REQUEST_BYTES} bytes",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        return decode_json(self.rfile.read(length))

    def _route_api(self, parsed: Any) -> None:
        path = parsed.path.rstrip("/") or "/"
        method = self.command
        store = self.app_server.store
        expected = self._expected_revision() if method in {"POST", "PUT", "PATCH", "DELETE"} else None

        if path == "/api/state":
            if method in {"GET", "HEAD"}:
                self._send_json(store.get_state())
            elif method == "PUT":
                self._send_json(store.replace_state(self._read_json(), expected))
            else:
                raise self._method_not_allowed("GET, HEAD, PUT")
            return
        if path == "/api/export":
            if method not in {"GET", "HEAD"}:
                raise self._method_not_allowed("GET, HEAD")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._send_json(
                store.get_state(),
                extra_headers={
                    "Content-Disposition": f'attachment; filename="networkmap-{timestamp}.json"'
                },
            )
            return
        if path == "/api/events":
            if method != "GET":
                raise self._method_not_allowed("GET")
            self._serve_events()
            return
        if path == "/api/session":
            if method != "POST":
                raise self._method_not_allowed("POST")
            headers = (
                {"Set-Cookie": _auth_cookie_header(self.app_server.token)}
                if self.app_server.token
                else None
            )
            self._send_json(
                {"status": "ok", "authenticated": True},
                extra_headers=headers,
            )
            return
        if path == "/api/demo":
            if method not in {"GET", "HEAD"}:
                raise self._method_not_allowed("GET, HEAD")
            document = demo_document()
            self._send_json({"revision": 0, "updated_at": "", **document})
            return
        if path == "/api/demo/reset":
            if method != "POST":
                raise self._method_not_allowed("POST")
            self._send_json(store.reset_demo(expected))
            return
        if path == "/api/import":
            if method != "POST":
                raise self._method_not_allowed("POST")
            self._send_json(store.replace_state(self._read_json(), expected))
            return
        if path == "/api/discovery":
            if method != "POST":
                raise self._method_not_allowed("POST")
            body = _require_object(self._read_json())
            _reject_unknown_fields(body, {"cidr", "use_nmap"}, "discovery")
            if "cidr" not in body:
                raise validation_error("cidr is required", "cidr")
            use_nmap = body.get("use_nmap", False)
            if type(use_nmap) is not bool:
                raise validation_error("use_nmap must be true or false", "use_nmap")
            self._send_json(discover_network(body["cidr"], use_nmap=use_nmap))
            return
        if path == "/api/nodes":
            if method in {"GET", "HEAD"}:
                state = store.get_state()
                self._send_json(
                    {
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                        "nodes": state["nodes"],
                    }
                )
            elif method == "POST":
                self._send_json(
                    store.create_node(self._read_json(), expected),
                    status=HTTPStatus.CREATED,
                )
            else:
                raise self._method_not_allowed("GET, HEAD, POST")
            return
        if path.startswith("/api/nodes/"):
            node_id = _identifier(unquote(path[len("/api/nodes/") :]))
            if method in {"GET", "HEAD"}:
                state = store.get_state()
                node = next(
                    (item for item in state["nodes"] if item["id"] == node_id),
                    None,
                )
                if node is None:
                    raise store._missing("node", node_id)
                self._send_json(
                    {
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                        "node": node,
                    }
                )
            elif method == "PATCH":
                self._send_json(store.update_node(node_id, self._read_json(), expected))
            elif method == "DELETE":
                self._send_json(store.delete_node(node_id, expected))
            else:
                raise self._method_not_allowed("GET, HEAD, PATCH, DELETE")
            return
        if path == "/api/links":
            if method in {"GET", "HEAD"}:
                state = store.get_state()
                self._send_json(
                    {
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                        "links": state["links"],
                    }
                )
            elif method == "POST":
                self._send_json(
                    store.create_link(self._read_json(), expected),
                    status=HTTPStatus.CREATED,
                )
            else:
                raise self._method_not_allowed("GET, HEAD, POST")
            return
        if path.startswith("/api/links/"):
            link_id = _identifier(unquote(path[len("/api/links/") :]))
            if method in {"GET", "HEAD"}:
                state = store.get_state()
                link = next(
                    (item for item in state["links"] if item["id"] == link_id),
                    None,
                )
                if link is None:
                    raise store._missing("link", link_id)
                self._send_json(
                    {
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                        "link": link,
                    }
                )
            elif method == "PATCH":
                self._send_json(store.update_link(link_id, self._read_json(), expected))
            elif method == "DELETE":
                self._send_json(store.delete_link(link_id, expected))
            else:
                raise self._method_not_allowed("GET, HEAD, PATCH, DELETE")
            return
        if path == "/api/settings":
            if method in {"GET", "HEAD"}:
                state = store.get_state()
                self._send_json(
                    {
                        "revision": state["revision"],
                        "updated_at": state["updated_at"],
                        "settings": state["settings"],
                    }
                )
            elif method == "PATCH":
                self._send_json(store.update_settings(self._read_json(), expected))
            else:
                raise self._method_not_allowed("GET, HEAD, PATCH")
            return
        raise APIError(
            "not_found", "The requested API endpoint was not found", HTTPStatus.NOT_FOUND
        )

    def _serve_events(self) -> None:
        subscription_id, inbox = self.app_server.broker.subscribe()
        try:
            state = self.app_server.store.get_metadata()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self._write_sse(
                "ready",
                {
                    "revision": state["revision"],
                    "updated_at": state["updated_at"],
                },
            )
            while True:
                try:
                    event = inbox.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event is EventBroker._CLOSED:
                    break
                self._write_sse("change", event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app_server.broker.unsubscribe(subscription_id)
            self.close_connection = True

    def _write_sse(self, event_name: str, event: Mapping[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        revision = event.get("revision")
        output = ""
        if revision is not None:
            output += f"id: {revision}\n"
        output += f"event: {event_name}\ndata: {data}\n\n"
        self.wfile.write(output.encode("utf-8"))
        self.wfile.flush()

    def _send_json(
        self,
        value: Any,
        *,
        status: int = HTTPStatus.OK,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        data = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if isinstance(value, dict) and isinstance(value.get("revision"), int):
            self.send_header("ETag", f'"{value["revision"]}"')
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="NetworkMap"')
        for key, header_value in (extra_headers or {}).items():
            self.send_header(key, header_value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_static(self, request_path: str) -> None:
        try:
            decoded = unquote(request_path, errors="strict")
        except UnicodeError as exc:
            raise APIError("invalid_path", "Invalid URL path", HTTPStatus.BAD_REQUEST) from exc
        relative = decoded.lstrip("/") or "index.html"
        # The project keeps its web root in ``static/`` while the HTML uses the
        # conventional /static/... URL prefix.
        if relative.startswith("static/"):
            relative = relative[len("static/") :]
        parts = Path(relative).parts
        if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
            raise APIError("not_found", "File not found", HTTPStatus.NOT_FOUND)
        static_root = self.app_server.static_dir
        candidate = (static_root / relative).resolve()
        try:
            inside_root = os.path.commonpath((static_root, candidate)) == str(static_root)
        except ValueError:
            inside_root = False
        if not inside_root:
            raise APIError("not_found", "File not found", HTTPStatus.NOT_FOUND)
        if candidate.is_dir():
            candidate = (candidate / "index.html").resolve()
        if not candidate.is_file() and "." not in Path(relative).name:
            candidate = (static_root / "index.html").resolve()
        if not candidate.is_file():
            raise APIError("not_found", "File not found", HTTPStatus.NOT_FOUND)
        stat = candidate.stat()
        content_type, encoding = mimetypes.guess_type(candidate.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Last-Modified", formatdate(stat.st_mtime, usegmt=True))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        if candidate.name in {
            "index.html",
            "sw.js",
            "app.js",
            "styles.css",
            "manifest.webmanifest",
        }:
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if self.command != "HEAD":
            with candidate.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=64 * 1024)

    @staticmethod
    def _redact_request_target(target: str) -> str:
        try:
            parsed = urlsplit(target)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            redacted = []
            for key, value in pairs:
                folded = key.casefold().replace("-", "_")
                sensitive = any(
                    marker in folded
                    for marker in ("token", "secret", "password", "credential", "api_key")
                ) or folded in {"key", "auth"}
                redacted.append((key, "[REDACTED]" if sensitive else value))
            return urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted), "")
            )
        except (TypeError, ValueError):
            return re.sub(
                r"(?i)((?:token|secret|password|credential|api[_-]?key|auth)=)[^&\s\"]+",
                r"\1[REDACTED]",
                target,
            )

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        safe_target = self._redact_request_target(getattr(self, "path", ""))
        self.log_message(
            '"%s %s %s" %s %s',
            getattr(self, "command", ""),
            safe_target,
            getattr(self, "request_version", ""),
            str(code),
            str(size),
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        message = re.sub(
            r"(?i)((?:token|secret|password|credential|api[_-]?key|auth)=)[^&\s\"]+",
            r"\1[REDACTED]",
            message,
        )
        print(f"[{self.log_date_time_string()}] {self.address_string()} {message}")


def default_data_dir() -> Path:
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    if configured:
        return Path(configured).expanduser() / "networkmap"
    return Path.home() / ".local" / "share" / "networkmap"


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    data_dir: str | os.PathLike[str] | None = None,
    token: str | None = None,
    static_dir: str | os.PathLike[str] | None = None,
) -> NetworkMapHTTPServer:
    """Create and bind an import-friendly server without starting its loop."""

    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    host = host.strip()
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("port must be an integer from 0 to 65535")
    storage = Path(data_dir).expanduser() if data_dir is not None else default_data_dir()
    assets = (
        Path(static_dir).expanduser()
        if static_dir is not None
        else Path(__file__).resolve().parent / "static"
    )
    normalized_token = token.strip() if token and token.strip() else None
    broker = EventBroker()
    store = StateStore(storage / "networkmap.sqlite3", on_change=broker.publish)
    try:
        address = ipaddress.ip_address(host)
        server_class = IPv6NetworkMapHTTPServer if address.version == 6 else NetworkMapHTTPServer
    except ValueError:
        server_class = NetworkMapHTTPServer
    try:
        return server_class(
            (host, port),
            NetworkMapRequestHandler,
            store=store,
            broker=broker,
            token=normalized_token,
            static_dir=assets,
            display_host=host,
        )
    except Exception:
        broker.close()
        raise


class ServerThread:
    """Start/stop helper for launchers, smoke tests, and embedded use."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        data_dir: str | os.PathLike[str] | None = None,
        token: str | None = None,
        static_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.server = create_server(host, port, data_dir, token, static_dir)
        self.thread: threading.Thread | None = None
        self._closed = False

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def browser_url(self) -> str:
        return self.server.browser_url

    def start(self) -> "ServerThread":
        if self._closed:
            raise RuntimeError("ServerThread cannot be restarted after stop()")
        if self.thread and self.thread.is_alive():
            return self
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="networkmap-http",
            daemon=True,
        )
        self.thread.start()
        return self

    def stop(self) -> None:
        if self._closed:
            return
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        self._closed = True

    def __enter__(self) -> "ServerThread":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Map and configure a network from a local web interface."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: %(default)s)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="TCP port (default: %(default)s)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="persistent data directory (default: %(default)s)",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "static",
        help="web frontend directory (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NETWORKMAP_TOKEN", ""),
        help="optional API/browser access token (or NETWORKMAP_TOKEN)",
    )
    parser.add_argument("--version", action="version", version=f"NetworkMap {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65_535:
        parser.error("--port must be from 0 to 65535")
    token = args.token.strip() if args.token and args.token.strip() else None
    if not _is_loopback_host(args.host) and not token:
        parser.error("--token is required when --host is not a loopback address")
    try:
        server = create_server(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            token=token,
            static_dir=args.static_dir,
        )
    except OSError as exc:
        parser.error(f"could not start server: {exc}")
    print(f"NetworkMap {VERSION} listening at {server.url}/")
    print(f"Data: {server.store.database_path}")
    if token:
        print("Authentication is enabled; enter the configured token in the sign-in dialog.")
    if not _is_loopback_host(args.host):
        print("Warning: use a TLS reverse proxy and firewall for non-loopback access.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping NetworkMap.")
    finally:
        server.server_close()
    return 0


__all__ = [
    "APIError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EventBroker",
    "ServerThread",
    "StateStore",
    "VERSION",
    "create_server",
    "demo_document",
    "discover_network",
    "main",
    "validate_discovery_network",
    "validate_link",
    "validate_node",
    "validate_settings",
    "validate_state_document",
]


if __name__ == "__main__":
    raise SystemExit(main())

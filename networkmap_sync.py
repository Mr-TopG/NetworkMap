#!/usr/bin/env python3
"""Safe, dependency-free snapshot synchronization for NetworkMap.

The native application owns a local NetworkMap HTTP server.  This module keeps
that server useful while offline and, when a hosted server is configured,
synchronizes complete topology snapshots using optimistic revision guards.

Authentication tokens deliberately live only in memory.  Checkpoints, status
files, control files, exceptions, and callbacks never contain them.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:  # Linux is the supported native platform; keep imports graceful elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX systems.
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
CHECKPOINT_FILENAME = ".native-sync-checkpoint.json"
STATUS_FILENAME = ".native-sync-status.json"
COMMAND_FILENAME = ".native-sync-command.json"
LOCK_FILENAME = ".native-sync.lock"
BACKUP_DIRECTORY = "sync-backups"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CONTROL_BYTES = 64 * 1024
VALID_ACTIONS = frozenset({"sync-now", "use-local", "use-hosted"})
RESOLUTION_ACTIONS = frozenset({"use-local", "use-hosted"})
VALID_STATES = frozenset(
    {
        "local-only",
        "checking",
        "synced",
        "pushing",
        "pulling",
        "offline",
        "auth-required",
        "conflict",
        "error",
    }
)


def _valid_decision_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class SyncError(Exception):
    """Base class for errors safe to show in native synchronization UI."""


class SyncAuthError(SyncError):
    """The endpoint rejected or requires authentication."""


class SyncNetworkError(SyncError):
    """The endpoint could not be reached before the request deadline."""


class SyncRaceError(SyncError):
    """An endpoint changed after it was read, so no overwrite was performed."""


class SyncProtocolError(SyncError):
    """An endpoint or local sync file did not follow the expected protocol."""


class SyncLockError(SyncError):
    """Another native synchronization worker owns the data directory."""


def normalize_server_url(value: str) -> str:
    """Return a safe HTTP(S) base URL with no credentials, query, or fragment."""

    if not isinstance(value, str):
        raise ValueError("server URL must be a string")
    candidate = value.strip()
    if not candidate:
        raise ValueError("server URL cannot be empty")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ValueError("server URL cannot contain whitespace")
    if "://" not in candidate:
        candidate = "http://" + candidate
    try:
        parsed = urllib.parse.urlsplit(candidate)
        parsed.port
    except ValueError as exc:
        raise ValueError("server URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("server URL must use http:// or https://")
    if not parsed.hostname:
        raise ValueError("server URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("server URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("server URL cannot contain a query string or fragment")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, path, "", "")
    )


def canonical_topology(document: Mapping[str, Any]) -> dict[str, Any]:
    """Extract canonical topology content, excluding all transport metadata.

    Only ``nodes``, ``links``, and ``settings`` participate.  Revisions,
    timestamps, export metadata, and instance identifiers therefore cannot make
    equivalent topologies appear different.  Node and link order is normalized
    by identifier; mapping key order is normalized when serialized for hashing.
    """

    if not isinstance(document, Mapping):
        raise SyncProtocolError("NetworkMap state must be a JSON object")
    nodes = document.get("nodes")
    links = document.get("links")
    settings = document.get("settings")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise SyncProtocolError("NetworkMap state must contain node and link arrays")
    if not isinstance(settings, Mapping):
        raise SyncProtocolError("NetworkMap state must contain settings")

    def entities(values: list[Any], name: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                raise SyncProtocolError(f"NetworkMap {name} must be JSON objects")
            identifier = value.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise SyncProtocolError(f"NetworkMap {name} must have string IDs")
            if identifier in identifiers:
                raise SyncProtocolError(f"NetworkMap {name} IDs must be unique")
            identifiers.add(identifier)
            result.append(deepcopy(dict(value)))
        result.sort(key=lambda item: item["id"])
        return result

    return {
        "nodes": entities(nodes, "nodes"),
        "links": entities(links, "links"),
        "settings": deepcopy(dict(settings)),
    }


def topology_digest(document: Mapping[str, Any]) -> str:
    """Return a SHA-256 digest for topology content, independent of metadata."""

    canonical = canonical_topology(document)
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SyncProtocolError("NetworkMap state contains invalid JSON values") from exc
    return hashlib.sha256(encoded).hexdigest()


def _ensure_data_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        if not path.is_dir():
            raise SyncProtocolError("The sync data path is not a directory")
    except OSError as exc:
        raise SyncProtocolError("The sync data directory is unavailable") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a private JSON file and durably flush its directory."""

    _ensure_data_directory(path.parent)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise SyncProtocolError(f"Could not write private sync file {path.name}") from exc
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                Path(temporary_name).unlink()


def _read_json_file(path: Path, maximum: int) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SyncProtocolError(f"Could not read private sync file {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SyncProtocolError(f"Private sync file {path.name} is not regular")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > maximum:
        raise SyncProtocolError(f"Private sync file {path.name} is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SyncProtocolError(f"Private sync file {path.name} is invalid") from exc


class SyncFileStore:
    """Durable checkpoint plus private status/control files in one data dir."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.data_dir = Path(data_dir).expanduser()
        _ensure_data_directory(self.data_dir)
        self.checkpoint_path = self.data_dir / CHECKPOINT_FILENAME
        self.status_path = self.data_dir / STATUS_FILENAME
        self.command_path = self.data_dir / COMMAND_FILENAME
        self.lock_path = self.data_dir / LOCK_FILENAME
        self.backup_dir = self.data_dir / BACKUP_DIRECTORY

    def load_checkpoint(self) -> dict[str, Any] | None:
        raw = _read_json_file(self.checkpoint_path, MAX_RESPONSE_BYTES)
        if raw is None:
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise SyncProtocolError("The native sync checkpoint has an unknown format")
        required = {
            "remote_url",
            "baseline_document",
            "baseline_digest",
            "local_instance_id",
            "remote_instance_id",
            "local_revision",
            "remote_revision",
            "last_sync_at",
        }
        if not required.issubset(raw):
            raise SyncProtocolError("The native sync checkpoint is incomplete")
        try:
            remote_url = normalize_server_url(raw["remote_url"])
        except (TypeError, ValueError) as exc:
            raise SyncProtocolError("The native sync checkpoint URL is invalid") from exc
        baseline = raw["baseline_document"]
        digest = raw["baseline_digest"]
        if not isinstance(digest, str) or topology_digest(baseline) != digest:
            raise SyncProtocolError("The native sync checkpoint digest is invalid")
        for key in ("local_instance_id", "remote_instance_id", "last_sync_at"):
            if not isinstance(raw[key], str):
                raise SyncProtocolError("The native sync checkpoint metadata is invalid")
        for key in ("local_revision", "remote_revision"):
            if isinstance(raw[key], bool) or not isinstance(raw[key], int) or raw[key] < 0:
                raise SyncProtocolError("The native sync checkpoint revision is invalid")
        result = dict(raw)
        result["remote_url"] = remote_url
        result["baseline_document"] = canonical_topology(baseline)
        return result

    def save_checkpoint(
        self,
        *,
        remote_url: str,
        baseline_document: Mapping[str, Any],
        local_instance_id: str,
        remote_instance_id: str,
        local_revision: int,
        remote_revision: int,
        last_sync_at: str,
    ) -> dict[str, Any]:
        baseline = canonical_topology(baseline_document)
        checkpoint = {
            "schema_version": SCHEMA_VERSION,
            "remote_url": normalize_server_url(remote_url),
            "baseline_document": baseline,
            "baseline_digest": topology_digest(baseline),
            "local_instance_id": str(local_instance_id),
            "remote_instance_id": str(remote_instance_id),
            "local_revision": int(local_revision),
            "remote_revision": int(remote_revision),
            "last_sync_at": str(last_sync_at),
        }
        _atomic_write_json(self.checkpoint_path, checkpoint)
        return deepcopy(checkpoint)

    def write_status(self, status_value: Mapping[str, Any]) -> None:
        _atomic_write_json(self.status_path, dict(status_value))

    def clear_status(self, *, session_id: str | None = None) -> bool:
        """Remove one status atomically, optionally only for its owning session.

        Renaming before reading avoids unlinking a newer status that appeared at
        the well-known path.  Normal callers hold the worker lock, so restoring
        a non-matching status cannot race another legitimate worker.
        """

        claimed = self.status_path.with_name(
            f".{self.status_path.name}.{uuid.uuid4().hex}.claimed"
        )
        try:
            os.replace(self.status_path, claimed)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SyncProtocolError("Could not claim the native sync status") from exc
        _fsync_directory(self.data_dir)
        remove_claim = True
        try:
            if session_id is not None:
                raw = _read_json_file(claimed, MAX_CONTROL_BYTES)
                if not isinstance(raw, dict) or raw.get("session_id") != session_id:
                    try:
                        os.replace(claimed, self.status_path)
                    except OSError as exc:
                        raise SyncProtocolError(
                            "Could not restore a native sync status"
                        ) from exc
                    remove_claim = False
                    _fsync_directory(self.data_dir)
                    return False
            return True
        finally:
            if remove_claim:
                with suppress(FileNotFoundError):
                    claimed.unlink()
                _fsync_directory(self.data_dir)

    def write_command(
        self,
        action: str,
        *,
        session_id: str,
        local_instance_id: str,
        decision_id: str = "",
    ) -> None:
        if action not in VALID_ACTIONS:
            raise ValueError(f"unsupported sync action: {action}")
        if action in RESOLUTION_ACTIONS and not _valid_decision_id(decision_id):
            raise ValueError("resolution commands require a valid decision ID")
        _atomic_write_json(
            self.command_path,
            {
                "schema_version": SCHEMA_VERSION,
                "action": action,
                "session_id": session_id,
                "local_instance_id": local_instance_id,
                "decision_id": decision_id if action in RESOLUTION_ACTIONS else "",
            },
        )

    def take_command(
        self, *, session_id: str, local_instance_id: str
    ) -> dict[str, str] | None:
        """Atomically claim and consume a command for the current worker.

        A producer may publish another command immediately after the rename;
        that newer file remains at ``command_path`` for the next pass.
        """

        claimed = self.command_path.with_name(
            f".{self.command_path.name}.{uuid.uuid4().hex}.claimed"
        )
        try:
            os.replace(self.command_path, claimed)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SyncProtocolError("Could not claim the native sync command") from exc
        _fsync_directory(self.data_dir)
        try:
            raw = _read_json_file(claimed, MAX_CONTROL_BYTES)
            if not isinstance(raw, dict):
                return None
            action = raw.get("action")
            decision_id = raw.get("decision_id", "")
            if (
                raw.get("schema_version") != SCHEMA_VERSION
                or raw.get("session_id") != session_id
                or raw.get("local_instance_id") != local_instance_id
                or action not in VALID_ACTIONS
                or not isinstance(decision_id, str)
                or (action in RESOLUTION_ACTIONS and not _valid_decision_id(decision_id))
            ):
                return None
            return {"action": str(action), "decision_id": decision_id}
        finally:
            with suppress(FileNotFoundError):
                claimed.unlink()
            _fsync_directory(self.data_dir)

    def backup_pair(
        self,
        local_document: Mapping[str, Any],
        hosted_document: Mapping[str, Any],
        *,
        reason: str,
    ) -> tuple[Path, Path]:
        """Write same-timestamp local/hosted snapshots before an overwrite."""

        try:
            self.backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.backup_dir, 0o700)
        except OSError as exc:
            raise SyncProtocolError("Could not create the private sync backup directory") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_reason = "".join(
            character if character.isalnum() or character == "-" else "-"
            for character in reason
        ).strip("-") or "sync"
        local_path = self.backup_dir / f"{stamp}-{safe_reason}-local.json"
        hosted_path = self.backup_dir / f"{stamp}-{safe_reason}-hosted.json"
        _atomic_write_json(local_path, dict(local_document))
        try:
            _atomic_write_json(hosted_path, dict(hosted_document))
        except Exception:
            with suppress(OSError):
                local_path.unlink()
            raise
        return local_path, hosted_path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, file_pointer: Any, code: int, message: str,
                         headers: Any, new_url: str) -> None:
        # Refusing redirects guarantees a bearer token is never forwarded to a
        # different origin due to server or proxy configuration.
        return None


class NetworkMapHTTPClient:
    """Small bounded HTTP JSON client for one NetworkMap endpoint."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 3.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        use_environment_proxies: bool = True,
    ) -> None:
        self.base_url = normalize_server_url(base_url)
        self._token = token if token else None
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if type(use_environment_proxies) is not bool:
            raise ValueError("use_environment_proxies must be true or false")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        handlers: list[Any] = []
        if not use_environment_proxies:
            # Loopback API traffic must never inherit HTTP_PROXY/HTTPS_PROXY:
            # it can contain the local bearer token and complete topology.
            handlers.append(urllib.request.ProxyHandler({}))
        handlers.append(_NoRedirect())
        self._opener = urllib.request.build_opener(*handlers)

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        document: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "NetworkMap-native-sync/1",
        }
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        body: bytes | None = None
        if document is not None:
            try:
                body = json.dumps(
                    document,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise SyncProtocolError("NetworkMap state cannot be encoded as JSON") from exc
            headers["Content-Type"] = "application/json"
        if expected_revision is not None:
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise ValueError("expected revision must be a non-negative integer")
            headers["If-Match"] = f'"{expected_revision}"'
        request = urllib.request.Request(
            self._url(path), data=body, headers=headers, method=method
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            with suppress(Exception):
                exc.read(min(self.max_response_bytes, MAX_CONTROL_BYTES) + 1)
            if exc.code in {401, 403}:
                raise SyncAuthError("A valid hosted NetworkMap token is required") from None
            if exc.code in {409, 412}:
                raise SyncRaceError("The NetworkMap changed while it was synchronizing") from None
            if 300 <= exc.code < 400:
                raise SyncProtocolError("NetworkMap API redirects are not accepted") from None
            raise SyncProtocolError(
                f"The NetworkMap API returned HTTP {int(exc.code)}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SyncNetworkError("The NetworkMap server could not be reached") from None

        with response:
            status_code = int(getattr(response, "status", response.getcode()))
            if not 200 <= status_code < 300:
                raise SyncProtocolError(
                    f"The NetworkMap API returned HTTP {status_code}"
                )
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            media_type = media_type.strip().lower()
            if media_type != "application/json" and not media_type.endswith("+json"):
                raise SyncProtocolError("The NetworkMap API did not return JSON")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise SyncProtocolError(
                        "The NetworkMap API returned an invalid response length"
                    ) from exc
                if declared_size < 0 or declared_size > self.max_response_bytes:
                    raise SyncProtocolError("The NetworkMap API response is too large")
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise SyncProtocolError("The NetworkMap API response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SyncProtocolError("The NetworkMap API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SyncProtocolError("The NetworkMap API response must be a JSON object")
        return payload

    def get_health(self) -> dict[str, Any]:
        health = self._request("GET", "/api/health")
        revision = health.get("revision")
        instance_id = health.get("instance_id")
        if (
            health.get("status") != "ok"
            or type(health.get("api_version")) is not int
            or health["api_version"] != SCHEMA_VERSION
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise SyncProtocolError("The server is not a compatible NetworkMap endpoint")
        try:
            normalized_instance_id = str(uuid.UUID(instance_id))
        except (AttributeError, TypeError, ValueError):
            raise SyncProtocolError(
                "NetworkMap synchronization requires a stable server instance ID"
            ) from None
        if instance_id != normalized_instance_id:
            raise SyncProtocolError("NetworkMap returned an invalid instance identifier")
        return health

    def get_state(self) -> dict[str, Any]:
        state = self._request("GET", "/api/state")
        revision = state.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise SyncProtocolError("NetworkMap state has no valid revision")
        canonical_topology(state)
        return state

    def put_state(
        self, document: Mapping[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        canonical_topology(document)
        result = self._request(
            "PUT",
            "/api/state",
            document=canonical_topology(document),
            expected_revision=expected_revision,
        )
        revision = result.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise SyncProtocolError("NetworkMap update returned no valid revision")
        canonical_topology(result)
        return result


def _instance_id(health: Mapping[str, Any], base_url: str) -> str:
    del base_url
    supplied = health.get("instance_id")
    try:
        normalized = str(uuid.UUID(supplied))
    except (AttributeError, TypeError, ValueError):
        raise SyncProtocolError(
            "NetworkMap synchronization requires a stable server instance ID"
        ) from None
    if supplied != normalized:
        raise SyncProtocolError("NetworkMap returned an invalid instance identifier")
    return normalized


class _Snapshot:
    def __init__(
        self,
        client: NetworkMapHTTPClient,
        health: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> None:
        self.client = client
        self.health = dict(health)
        self.state = dict(state)
        self.revision = int(self.state["revision"])
        self.digest = topology_digest(self.state)
        self.instance_id = _instance_id(self.health, client.base_url)
        if (
            type(self.state.get("api_version")) is not int
            or self.state["api_version"] != SCHEMA_VERSION
            or self.state.get("instance_id") != self.instance_id
        ):
            raise SyncProtocolError(
                "NetworkMap health and state identities do not match"
            )


def _read_snapshot(client: NetworkMapHTTPClient) -> _Snapshot:
    health = client.get_health()
    state = client.get_state()
    return _Snapshot(client, health, state)


class SyncWorker:
    """Synchronize a local NetworkMap server with an optional hosted server.

    ``sync_once()`` is deterministic and convenient for tests.  ``start()``
    runs it in a daemon thread every 5-10 seconds, and ``wake()`` requests an
    immediate pass.  The status callback receives the same secret-free mapping
    written to :data:`STATUS_FILENAME`.
    """

    def __init__(
        self,
        local_url: str,
        remote_url: str | None,
        data_dir: str | os.PathLike[str],
        *,
        remote_token: str | None = None,
        local_token: str | None = None,
        demo_document: Mapping[str, Any] | None = None,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
        poll_interval: float = 7.0,
        timeout: float = 3.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.local_url = normalize_server_url(local_url)
        self.remote_url = (
            normalize_server_url(remote_url) if remote_url is not None else ""
        )
        self.files = SyncFileStore(data_dir)
        self.local_client = NetworkMapHTTPClient(
            self.local_url,
            local_token,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            use_environment_proxies=False,
        )
        self.remote_client = (
            NetworkMapHTTPClient(
                self.remote_url,
                remote_token,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
            if self.remote_url
            else None
        )
        self.demo_digest = (
            topology_digest(demo_document) if demo_document is not None else None
        )
        self.status_callback = status_callback
        self.poll_interval = min(10.0, max(5.0, float(poll_interval)))
        self.session_id = uuid.uuid4().hex
        self.local_instance_id = ""
        self._last_sync_at: str | None = None
        try:
            previous_checkpoint = self.files.load_checkpoint()
        except SyncProtocolError:
            # The guarded pass will surface a corrupt checkpoint as an error;
            # construction itself must not prevent the local app from opening.
            previous_checkpoint = None
        if previous_checkpoint is not None and (
            not self.remote_url
            or previous_checkpoint["remote_url"] == self.remote_url
        ):
            self._last_sync_at = previous_checkpoint["last_sync_at"]
        self._status_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_stream: Any = None
        self._has_started = False
        self._status: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mode": "native-sync",
            "state": "checking" if self.remote_url else "local-only",
            "remote_url": self.remote_url,
            "message": (
                "Waiting to check the hosted map."
                if self.remote_url
                else "Changes are saved locally. Connect a hosted server to sync."
            ),
            "pending_changes": 0,
            "last_sync_at": self._last_sync_at or "",
            "can_resolve": False,
            "local_instance_id": "",
            "heartbeat": time.time(),
            "session_id": self.session_id,
        }

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return deepcopy(self._status)

    def _publish(
        self,
        state: str,
        message: str,
        *,
        pending_changes: bool = False,
        can_resolve: bool = False,
        local: _Snapshot | None = None,
        remote: _Snapshot | None = None,
        decision_id: str = "",
    ) -> dict[str, Any]:
        if state not in VALID_STATES:
            raise ValueError(f"invalid sync state: {state}")
        value = {
            "schema_version": SCHEMA_VERSION,
            "mode": "native-sync",
            "state": state,
            "remote_url": self.remote_url,
            "message": message,
            "pending_changes": int(bool(pending_changes)),
            "last_sync_at": self._last_sync_at or "",
            "can_resolve": bool(can_resolve),
            "local_instance_id": self.local_instance_id,
            "heartbeat": time.time(),
            "session_id": self.session_id,
        }
        if local is not None:
            value["local_revision"] = local.revision
        if remote is not None:
            value["remote_revision"] = remote.revision
            value["remote_instance_id"] = remote.instance_id
        if decision_id:
            if state != "conflict" or not _valid_decision_id(decision_id):
                raise ValueError("decision IDs are valid only for sync conflicts")
            value["decision_id"] = decision_id
        with self._status_lock:
            self._status = value
            self.files.write_status(value)
        if self.status_callback is not None:
            try:
                self.status_callback(deepcopy(value))
            except Exception:
                # A presentation callback must never interrupt the data guard.
                pass
        return deepcopy(value)

    def _acquire_worker_lock(self) -> None:
        if self._lock_stream is not None:
            return
        try:
            descriptor = os.open(
                self.files.lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise SyncProtocolError("Could not open the native sync lock") from exc
        try:
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SyncProtocolError("The native sync lock is not a regular file")
        except Exception:
            os.close(descriptor)
            raise
        stream = os.fdopen(descriptor, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise SyncLockError(
                    "Another NetworkMap sync worker is already running"
                ) from exc
        self._lock_stream = stream

    def _release_worker_lock(self) -> None:
        stream = self._lock_stream
        self._lock_stream = None
        if stream is None:
            return
        if fcntl is not None:
            with suppress(OSError):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()

    @contextmanager
    def _single_pass_lock(self) -> Iterator[None]:
        owned = self._lock_stream is None
        if owned:
            self._acquire_worker_lock()
        try:
            yield
        finally:
            if owned:
                self._release_worker_lock()

    def start(self) -> "SyncWorker":
        if self._thread is not None and self._thread.is_alive():
            return self
        if self._has_started:
            self.session_id = uuid.uuid4().hex
            self.local_instance_id = ""
        self._has_started = True
        self._stop_event.clear()
        self._wake_event.clear()
        self._acquire_worker_lock()
        try:
            self.files.clear_status()
            self._thread = threading.Thread(
                target=self._run,
                name="networkmap-sync",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self._thread = None
            self._release_worker_lock()
            raise
        return self

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            raise SyncError(
                "The synchronization worker is still finishing an active request"
            )
        self._thread = None

    def wake(self) -> None:
        self._wake_event.set()

    def write_command(self, action: str) -> None:
        """Write a session-bound control request and wake this worker."""

        if not self.local_instance_id:
            raise SyncProtocolError("The local NetworkMap instance is not ready")
        self.files.write_command(
            action,
            session_id=self.session_id,
            local_instance_id=self.local_instance_id,
            decision_id=(
                str(self.status().get("decision_id", ""))
                if action in RESOLUTION_ACTIONS
                else ""
            ),
        )
        self.wake()

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                result = self.sync_once()
                delay = (
                    10.0
                    if result["state"] in {"offline", "auth-required", "error"}
                    else self.poll_interval
                )
                self._wake_event.wait(delay)
                self._wake_event.clear()
        finally:
            with suppress(Exception):
                self.files.clear_status(session_id=self.session_id)
            self._release_worker_lock()

    def sync_once(self, action: str | None = None) -> dict[str, Any]:
        """Perform one guarded synchronization pass and return public status."""

        if action is not None and action not in VALID_ACTIONS:
            raise ValueError(f"unsupported sync action: {action}")
        with self._sync_lock:
            try:
                with self._single_pass_lock():
                    return self._sync_once_locked(action)
            except SyncAuthError:
                return self._publish(
                    "auth-required",
                    "The hosted server requires a valid access token.",
                    pending_changes=True,
                )
            except SyncNetworkError:
                return self._publish(
                    "offline",
                    "The hosted server is unavailable; local changes remain safe.",
                    pending_changes=True,
                )
            except SyncRaceError:
                return self._publish(
                    "checking",
                    "A map changed during synchronization; it will be checked again.",
                    pending_changes=True,
                )
            except SyncLockError:
                raise
            except SyncProtocolError as exc:
                return self._publish(
                    "error",
                    str(exc),
                    pending_changes=True,
                )
            except Exception:
                return self._publish(
                    "error",
                    "An unexpected synchronization error occurred.",
                    pending_changes=True,
                )

    def _sync_once_locked(self, action: str | None) -> dict[str, Any]:
        self._publish("checking", "Checking the local map.")
        local = _read_snapshot(self.local_client)
        self.local_instance_id = local.instance_id
        queued_command = (
            None
            if action is not None
            else self.files.take_command(
                session_id=self.session_id,
                local_instance_id=self.local_instance_id,
            )
        )

        if self.remote_client is None:
            checkpoint = self.files.load_checkpoint()
            pending = bool(
                checkpoint is not None
                and local.digest != checkpoint["baseline_digest"]
            )
            return self._publish(
                "local-only",
                "Changes are saved locally. Connect a hosted server to sync.",
                pending_changes=pending,
            )

        self._publish("checking", "Comparing local and hosted maps.")
        remote = _read_snapshot(self.remote_client)
        chosen_action = action or (queued_command or {}).get("action")
        if chosen_action in {None, "sync-now"}:
            return self._automatic_sync(local, remote)
        if action is None and (
            queued_command is None
            or queued_command.get("decision_id") != self._decision_id(local, remote)
        ):
            return self._conflict(
                local,
                remote,
                "A map changed after the sync choice was made. Review both current copies and choose again.",
            )
        return self._explicit_resolution(chosen_action, local, remote)

    def _decision_id(self, local: _Snapshot, remote: _Snapshot) -> str:
        material = "\0".join(
            (
                self.session_id,
                local.instance_id,
                str(local.revision),
                local.digest,
                remote.instance_id,
                str(remote.revision),
                remote.digest,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _conflict(
        self, local: _Snapshot, remote: _Snapshot, message: str
    ) -> dict[str, Any]:
        return self._publish(
            "conflict",
            message,
            pending_changes=True,
            can_resolve=True,
            local=local,
            remote=remote,
            decision_id=self._decision_id(local, remote),
        )

    def _applicable_checkpoint(
        self, local: _Snapshot, remote: _Snapshot
    ) -> dict[str, Any] | None:
        checkpoint = self.files.load_checkpoint()
        if checkpoint is None:
            return None
        if checkpoint["remote_url"] != self.remote_url:
            return None
        if checkpoint["local_instance_id"] != local.instance_id:
            return None
        if checkpoint["remote_instance_id"] != remote.instance_id:
            return None
        return checkpoint

    def _automatic_sync(self, local: _Snapshot, remote: _Snapshot) -> dict[str, Any]:
        checkpoint = self._applicable_checkpoint(local, remote)
        if local.digest == remote.digest:
            return self._checkpoint_synced(local, remote, "Local and hosted maps match.")

        if checkpoint is not None:
            baseline = checkpoint["baseline_digest"]
            local_changed = local.digest != baseline
            remote_changed = remote.digest != baseline
            if local_changed and not remote_changed:
                return self._transfer(
                    local,
                    remote,
                    direction="pushing",
                    message="Uploading local changes to the hosted map.",
                )
            if remote_changed and not local_changed:
                return self._transfer(
                    remote,
                    local,
                    direction="pulling",
                    message="Downloading hosted changes to the local map.",
                )
            # Both changed, or neither matches a now-inconsistent checkpoint.
            return self._conflict(
                local,
                remote,
                "Local and hosted maps both changed. Choose which version to keep.",
            )

        local_is_demo = (
            self.demo_digest is not None
            and local.revision == 1
            and local.digest == self.demo_digest
        )
        remote_is_demo = (
            self.demo_digest is not None
            and remote.revision == 1
            and remote.digest == self.demo_digest
        )
        if local_is_demo and not remote_is_demo:
            return self._transfer(
                remote,
                local,
                direction="pulling",
                message="Adopting the existing hosted map locally.",
                backup_reason="initial-hosted-adoption",
            )
        if remote_is_demo and not local_is_demo:
            return self._transfer(
                local,
                remote,
                direction="pushing",
                message="Publishing the existing local map to the hosted server.",
                backup_reason="initial-local-adoption",
            )
        return self._conflict(
            local,
            remote,
            "Local and hosted maps differ without shared sync history. Choose which version to keep.",
        )

    def _explicit_resolution(
        self, action: str, local: _Snapshot, remote: _Snapshot
    ) -> dict[str, Any]:
        if local.digest == remote.digest:
            return self._checkpoint_synced(local, remote, "Local and hosted maps match.")
        if action == "use-local":
            return self._transfer(
                local,
                remote,
                direction="pushing",
                message="Replacing the hosted map with the selected local version.",
                backup_reason="use-local",
            )
        if action == "use-hosted":
            return self._transfer(
                remote,
                local,
                direction="pulling",
                message="Replacing the local map with the selected hosted version.",
                backup_reason="use-hosted",
            )
        raise SyncProtocolError("The native sync command is invalid")

    def _transfer(
        self,
        source: _Snapshot,
        target: _Snapshot,
        *,
        direction: str,
        message: str,
        backup_reason: str | None = None,
    ) -> dict[str, Any]:
        # Confirm both inputs once more after the direction decision.  If
        # either changed, defer to the next pass instead of acting on a stale
        # snapshot.  The target's If-Match remains the final atomic guard.
        confirmed_source = _read_snapshot(source.client)
        confirmed_target = _read_snapshot(target.client)
        if (
            confirmed_source.instance_id != source.instance_id
            or confirmed_source.revision != source.revision
            or confirmed_source.digest != source.digest
            or confirmed_target.instance_id != target.instance_id
            or confirmed_target.revision != target.revision
            or confirmed_target.digest != target.digest
        ):
            raise SyncRaceError("A NetworkMap changed before synchronization")
        source = confirmed_source
        target = confirmed_target
        if backup_reason is not None:
            local_state = source.state if direction == "pushing" else target.state
            hosted_state = target.state if direction == "pushing" else source.state
            self.files.backup_pair(local_state, hosted_state, reason=backup_reason)
        local_snapshot = source if direction == "pushing" else target
        remote_snapshot = target if direction == "pushing" else source
        self._publish(
            direction,
            message,
            pending_changes=True,
            local=local_snapshot,
            remote=remote_snapshot,
        )
        target.client.put_state(
            canonical_topology(source.state), expected_revision=target.revision
        )

        # Never checkpoint a PUT response alone.  Fresh reads of both endpoints
        # prove that the resulting snapshots still agree and retain identity.
        fresh_local = _read_snapshot(self.local_client)
        assert self.remote_client is not None
        fresh_remote = _read_snapshot(self.remote_client)
        if (
            fresh_local.instance_id != self.local_instance_id
            or fresh_remote.instance_id != (
                source.instance_id if direction == "pulling" else target.instance_id
            )
            or fresh_local.digest != fresh_remote.digest
        ):
            raise SyncRaceError("A NetworkMap changed while verifying synchronization")
        return self._checkpoint_synced(
            fresh_local, fresh_remote, "Local and hosted maps are synchronized."
        )

    def _checkpoint_synced(
        self, local: _Snapshot, remote: _Snapshot, message: str
    ) -> dict[str, Any]:
        if local.digest != remote.digest:
            raise SyncRaceError("Different maps cannot become a common checkpoint")
        completed_at = _utc_now()
        self.files.save_checkpoint(
            remote_url=self.remote_url,
            baseline_document=local.state,
            local_instance_id=local.instance_id,
            remote_instance_id=remote.instance_id,
            local_revision=local.revision,
            remote_revision=remote.revision,
            last_sync_at=completed_at,
        )
        self._last_sync_at = completed_at
        return self._publish("synced", message, local=local, remote=remote)


# Short aliases make exception handling pleasant for callers while preserving
# descriptive public names in tracebacks and documentation.
AuthError = SyncAuthError
NetworkError = SyncNetworkError
RaceError = SyncRaceError
ProtocolError = SyncProtocolError


__all__ = [
    "AuthError",
    "BACKUP_DIRECTORY",
    "CHECKPOINT_FILENAME",
    "COMMAND_FILENAME",
    "MAX_RESPONSE_BYTES",
    "NetworkError",
    "NetworkMapHTTPClient",
    "ProtocolError",
    "RaceError",
    "SCHEMA_VERSION",
    "STATUS_FILENAME",
    "SyncAuthError",
    "SyncError",
    "SyncFileStore",
    "SyncLockError",
    "SyncNetworkError",
    "SyncProtocolError",
    "SyncRaceError",
    "SyncWorker",
    "canonical_topology",
    "normalize_server_url",
    "topology_digest",
]

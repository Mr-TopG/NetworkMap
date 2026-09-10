# NetworkMap backend API

`server.py` is a Python-standard-library server. It stores the map in SQLite,
serves the frontend, and notifies other browser/native clients after every
committed change.

## Run and embed

```console
python3 server.py --host 127.0.0.1 --port 8765 \
  --data-dir ~/.local/share/networkmap --static-dir ./static
```

The default bind is `127.0.0.1:8765`. `--token` (or `NETWORKMAP_TOKEN`) enables
authentication; a non-loopback CLI bind requires it. An empty token means auth
is disabled. The default database is
`~/.local/share/networkmap/networkmap.sqlite3` (respecting `XDG_DATA_HOME`).

Launchers can use:

```python
from server import ServerThread, create_server

with ServerThread(port=0, data_dir="/tmp/example") as running:
    print(running.url)

httpd = create_server(port=8765)  # bound, but not started
# httpd.serve_forever(); httpd.shutdown(); httpd.server_close()
```

## State shape

```json
{
  "api_version": 1,
  "instance_id": "621ff6cc-6374-4af9-9da7-fcd71ea83390",
  "revision": 3,
  "updated_at": "2026-09-05T17:30:00.000Z",
  "nodes": [],
  "links": [],
  "areas": [],
  "settings": {}
}
```

A node has `id`, `name`, `kind`, `ip`, `mac`, `hostname`, `vendor`,
`management_url`, `winbox_enabled`, `status`, `x`, `y`, `notes`, `tags`, and
`config`. `management_url` is empty or an HTTP(S) URL without embedded
credentials. `winbox_enabled` remains accepted for backup compatibility, but
the current UI detects WinBox support from `MikroTik` in the vendor field. A
link has `id`, `source`, `target`, `name`, `kind`, `status`,
`directed`, `bandwidth_mbps`, `duplex`, `notes`, and `config`.
The `source` and `target` values are node IDs. The backend accepts `label` as a
legacy input alias for link `name`, but always emits `name`.
`duplex` is `unknown` (the default for older links), `full`, `half`, or `auto`.
Speed and duplex are stored configuration, not live negotiated measurements.

An area is a labeled map annotation with `id`, `label`, `shape` (`rectangle` or
`ellipse`), `x`, `y`, `width`, `height`, `color`, and `vlan_id`. Coordinates give
the upper-left corner in map units, with each coordinate from -1,000,000 to
1,000,000 and width/height from 40 to 100,000. Equal dimensions make a square or
circle. Labels are nonempty and at most 120 characters; color is `blue`,
`green`, `amber`, `violet`, or `gray`. `vlan_id` is null or an integer from 1 to
4094. Areas are visual annotations only and do not change device configuration.
Up to 1,000 areas can be stored. Older imports may omit `areas`; this means an
empty array. Areas persist through exports, imports, and native synchronization.

Settings use `name`, `description`, `subnet`, `refresh_interval`,
`show_link_labels`, `compact_labels`, and `theme`; `grid_size`, `snap_to_grid`,
and `show_labels` are also persisted display options.

Every mutation is atomic, increments `revision`, and returns the complete new
state. Clients may send the ETag received from `GET /api/state` as `If-Match`.
A stale revision returns HTTP 409 without changing anything.

`instance_id` permanently identifies the database, while `api_version` gates
safe native synchronization. Importing a backup never replaces either value.
The native worker compares topology content against a durable common baseline;
it does not treat unrelated revision numbers or timestamps as ordering. A
conflict choice is bound to the exact local and hosted snapshots shown to the
user, so a later edit invalidates the choice instead of being overwritten.
Entity order does not affect sync comparison. Empty areas and unknown duplex
have the same digest as their omitted legacy equivalents, preserving existing
sync checkpoints on upgrade. Upgrade both clients and the hosted server to use
nonempty areas or explicit duplex; older servers reject these new fields.

## Routes

| Method | Route | Result |
| --- | --- | --- |
| GET | `/api/health` | Public readiness, version, and revision |
| GET | `/api/state` | Complete current state |
| PUT | `/api/state` | Validate and replace nodes, links, areas, and settings |
| GET | `/api/sync/status` | Secret-free native synchronization status, or hosted-only status |
| POST | `/api/sync/actions` | Ask the active native worker to sync or resolve its current conflict |
| GET, POST | `/api/nodes` | List or create nodes |
| GET, PATCH, DELETE | `/api/nodes/{id}` | Read, edit, or delete a node; deletion cascades its links |
| GET, POST | `/api/links` | List links or create one between existing nodes |
| GET, PATCH, DELETE | `/api/links/{id}` | Read, edit, or delete a link |
| GET, POST | `/api/areas` | List or create labeled topology regions |
| GET, PATCH, DELETE | `/api/areas/{id}` | Read, edit, or delete an area |
| GET, PATCH | `/api/settings` | Read settings or update one or more |
| GET | `/api/export` | Download complete state JSON |
| POST | `/api/import` | Validate and atomically restore exported JSON |
| GET | `/api/events` | Server-Sent Events (`ready`, then `change`) |
| POST | `/api/session` | Exchange a bearer token for an HttpOnly browser cookie |
| POST | `/api/discovery` | Return reviewable local discovery candidates |
| GET | `/api/demo` | Return the built-in sample without changing state |
| POST | `/api/demo/reset` | Replace state with the editable sample topology |

Request an immediate comparison with `{"action":"sync-now"}`. Conflict
resolutions use `{"action":"use-local","decision_id":"..."}` or
`{"action":"use-hosted","decision_id":"..."}`, where `decision_id` is the
current value returned by `/api/sync/status`. A missing or stale decision ID is
rejected without changing either topology.

Discovery accepts `{"cidr":"192.168.1.0/24","use_nmap":false}`. It runs
`ip neigh` and, only when requested and installed, `nmap -sn` (plus `-6` for
IPv6). The CIDR must be
a canonical RFC1918 IPv4 or unique-local IPv6 network of at most 256 addresses.
Results are never inserted automatically.

Errors use this stable envelope:

```json
{"error":{"code":"validation_error","message":"...","details":{"field":"ip"}}}
```

When a token is configured, API clients send `Authorization: Bearer TOKEN`.
Browsers and the native shell open `/#token=TOKEN`; fragments never reach HTTP
logs. The frontend sends that token once to `POST /api/session`, receives an
HttpOnly SameSite cookie, clears its temporary copy, and opens SSE with the
cookie. The legacy `/?token=TOKEN` redirect remains for compatibility and its
value is redacted from built-in access logs. The backend does not enable
cross-origin access or trust proxy headers. Use a TLS reverse proxy and firewall
rather than exposing the process directly to the Internet.

The offline-first native worker uses the same bearer token for hosted API calls,
but never writes it to its checkpoint, status, command, backup, or remembered-URL
files.

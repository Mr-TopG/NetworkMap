# NetworkMap

NetworkMap is a self-hosted network topology workspace with a browser UI and a
native Linux window. It lets you inspect and open a real device by clicking it,
unlock topology editing only when needed, and maintain devices, connections,
workspace settings, and backups from a dedicated **Configuration** view.

The server, database layer, API, discovery helper, and web UI have no third-party
Python or JavaScript dependencies. The Linux window uses the GTK 3 and WebKitGTK
4.1 bindings supplied by the operating system.

## What is included

- Locked-by-default topology with pan, zoom, fit-to-view, search, and an explicit
  edit mode for dragging nodes or changing links
- Click-through device inspector with HTTP/HTTPS management launch actions
- MikroTik-aware WinBox launch from the Linux app, with a copy-address fallback
- Separate configuration tabs for inventory, connections, workspace, and data
- SQLite persistence with validated, atomic updates and revision conflict checks
- Live synchronization between open browser and desktop clients using
  Server-Sent Events (SSE)
- JSON export/import for portable backups
- Conservative local discovery using the host's neighbor table, with optional
  `nmap` discovery
- Optional shared-token authentication for hosted use
- GTK/WebKit Linux shell that starts a local server or connects to a hosted one
- User-local desktop, icon, launcher, and systemd service packaging
- Multi-architecture container image and PC/RouterOS deployment examples

This version can open a device's web interface or hand its address to WinBox,
but it does not submit login forms, store device passwords, or push vendor
configuration commands. Passwords are deliberately excluded until NetworkMap
has an encrypted OS-keyring-backed credential design. The per-device JSON area
is for non-secret metadata only.

## Architecture and synchronization

```text
 Browser on any machine ---------\
                                  HTTP API + SSE       SQLite
 Linux GTK/WebKit client -------->  server.py  --------> state
                                      ^
 Local browser/native client --------/
```

There is one source of truth: the server selected by a client. To see the same
map in the web UI and Linux app, point both at the same hosted URL. Updates are
written to that server and broadcast to every connected client. This is shared
server synchronization, not file synchronization between separate local
databases.

The default native launch uses a local database. A saved hosted URL takes
precedence on later launches; `--local` temporarily selects the local database,
and `--forget-server-url` returns the default to local.

## Quick start

Requirements for the web app are Python 3.10 or newer and a modern browser.

```bash
cd NetworkMap
python3 server.py
```

Open <http://127.0.0.1:8765>. On first start, NetworkMap creates a small demo
topology; it can be edited or removed. See every server option with:

```bash
python3 server.py --help
```

Run the Linux desktop window directly from the checkout:

```bash
./networkmap
```

On Debian, Ubuntu, and Linux Mint, the native runtime packages are:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
./native.py --check
```

The desktop process embeds the server in a background thread and stops that
server when the app exits. If a healthy NetworkMap server is already listening
on `127.0.0.1:8765`—for example, the optional user service—the app reuses it.

## Open a real device

Click a device on the Overview map. **Open HTTP** or **Open HTTPS** launches its
management page in a separate browser tab. NetworkMap uses the explicit **Web
management URL** from the device editor; when that field is blank, it falls back
to `http://` plus the device hostname or IP address. Edit the URL to switch a
device to HTTPS, use a non-default port, or add a WebFig path.

For a device marked **Open with MikroTik WinBox** (or identified as MikroTik),
the inspector also shows **WinBox**. In the NetworkMap Linux app this launches an
installed `winbox`/`WinBox` executable with the device address. Set
`NETWORKMAP_WINBOX=/path/to/WinBox` if it is not on `PATH`. A regular browser
can hand the `winbox://` link to an installed desktop handler; NetworkMap also
tries to copy the address when clipboard permission is available. No username
or password is placed in either URL.

## Install the Linux app

The installer writes only to user-owned locations and does not use `sudo`:

```bash
./install.sh
~/.local/bin/networkmap
```

It installs a desktop menu entry and icon, the application under
`~/.local/lib/networkmap`, and an inactive user-service unit. Log out and back in
if a desktop environment does not immediately discover a newly-created
`~/.local/bin`.

Uninstallation is recoverable and deliberately preserves maps, tokens, and
configuration:

```bash
./uninstall.sh
```

Application files are moved under
`~/.local/state/networkmap/uninstall-backups/` rather than deleted. The script
prints the exact recovery directory.

## Connect the native app to hosted NetworkMap

The following remembers only the server URL:

```bash
networkmap --server-url https://networkmap.example.net
```

For a token-protected server, store the token separately with private
permissions. This default file is read automatically on future desktop/menu
launches:

```bash
install -d -m 700 ~/.config/networkmap
printf '%s\n' 'your-long-random-token' > ~/.config/networkmap/token
chmod 600 ~/.config/networkmap/token
networkmap --server-url https://networkmap.example.net
```

Alternatively use `NETWORKMAP_TOKEN`, `--token-file PATH`, or (least preferred,
because process listings can expose it) `--token TOKEN`. The token is included
only in the initial URL fragment, which is never sent to the HTTP server. The
frontend exchanges it for an HttpOnly session cookie and immediately removes
the fragment. NetworkMap never stores it in `native.json` or prints it.

Useful controls:

```bash
networkmap --local                 # ignore a remembered URL once
networkmap --forget-server-url     # remove it from native.json
networkmap --server-url URL --no-remember
networkmap --developer-tools       # enable the WebKit inspector
```

`NETWORKMAP_SERVER_URL` can select a hosted server without changing saved
configuration. Explicit command-line options take precedence over environment
and saved settings.

## Optional always-on user service

`install.sh` installs, but does not enable, a hardened user service. A running
local service makes the map available to both the browser and native window and
keeps it running after the window closes.

```bash
systemctl --user enable --now networkmap.service
systemctl --user status networkmap.service
journalctl --user -u networkmap.service -f
```

To protect the service with a token, create matching service and desktop token
files. URL-safe random tokens do not need shell quoting in `server.env`:

```bash
install -d -m 700 ~/.config/networkmap
token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf 'NETWORKMAP_TOKEN=%s\n' "$token" > ~/.config/networkmap/server.env
printf '%s\n' "$token" > ~/.config/networkmap/token
chmod 600 ~/.config/networkmap/server.env ~/.config/networkmap/token
unset token
systemctl --user restart networkmap.service
```

The checked-in examples are
[`packaging/systemd/networkmap.service`](packaging/systemd/networkmap.service)
and
[`packaging/systemd/server.env.example`](packaging/systemd/server.env.example).
Use `systemctl --user edit networkmap` for local unit overrides so reinstalling
does not overwrite them.

## Host with a container on a PC

The included image runs as an unprivileged user and is built for both AMD64 PCs
and ARM64 routers. Create a private Compose environment file, then start it:

```bash
umask 077
printf 'NETWORKMAP_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
docker compose up -d --build
```

Compose publishes NetworkMap only on `127.0.0.1:8765`; put the supplied nginx
HTTPS configuration in front of it. Every browser and Linux client pointed at
that one URL shares the same SQLite-backed topology. The GitHub workflow builds
and publishes `ghcr.io/mr-topg/networkmap` for `linux/amd64` and `linux/arm64`
after `main` is pushed.

## Host on a MikroTik RB5009

The RB5009 is ARM64 and RouterOS supports Linux containers on ARM64 when the
optional Container package and container device mode are enabled. MikroTik
strongly recommends external storage for container data. A ready-to-adapt veth,
mount, token, image, and startup walkthrough is in
[`packaging/routeros/README.md`](packaging/routeros/README.md).

The walkthrough uses the ARM64 image published by this repository and places
the container directly on a trusted management LAN so web/native clients can
sync with it. Review the security warning and change every example address. See
MikroTik's [Container manual](https://help.mikrotik.com/docs/spaces/ROS/pages/84901929/Container)
and [RB5009 specifications](https://mikrotik.com/product/rb5009ug_s_in) before
enabling the feature.

## Hosting safely

The built-in HTTP server is intentionally small. For anything beyond a trusted
loopback workflow:

1. Keep NetworkMap bound to `127.0.0.1` and put a maintained reverse proxy in
   front of it.
2. Terminate HTTPS at the proxy, enable HSTS, and have the proxy append the
   `Secure` attribute to NetworkMap's authentication cookie. The backend does
   not trust `X-Forwarded-*` headers and therefore cannot infer external HTTPS.
3. Set a long random `--token`; do not put it in source control, service unit
   files, screenshots, bookmarks, or shared shell history.
4. Restrict the firewall so only the reverse proxy can reach NetworkMap's HTTP
   port. Do not expose an unauthenticated `--host 0.0.0.0` listener to the
   Internet.
5. Configure the proxy not to buffer `/api/events` and give that SSE connection
   a long read timeout. Preserve cookies and the `Origin` and `Host` headers.

A ready-to-adapt TLS reverse-proxy example is included at
[`packaging/nginx/networkmap.conf.example`](packaging/nginx/networkmap.conf.example).
It uses nginx's `proxy_cookie_flags` directive to add `Secure` to the session
cookie and disables buffering for live events. Replace its hostname and
certificate paths, then validate the installed nginx configuration before
reloading it.

The server emits no permissive CORS headers. Cookie-authenticated mutations
check same-host origins when browsers send an `Origin` header. The session
cookie is `HttpOnly`, `SameSite=Strict`, and path-scoped, but it is not marked
`Secure` by the loopback HTTP server—that must be handled at the TLS proxy.

The health endpoint is public and reports only service status, version,
revision, and update time. A token protects all state and mutation endpoints.
NetworkMap currently provides a shared bearer token rather than individual user
accounts or authorization roles.

Run a single NetworkMap server process for each database. SQLite coordinates
data writes, but live SSE notifications are intentionally in-process and are
not a multi-worker message bus.

## Discovery boundaries

Discovery runs on the machine hosting `server.py`. A local desktop launch scans
the desktop's local network; a desktop connected to a remote server asks that
remote host to scan *its* network. It does not install or control a discovery
agent on the desktop.

NetworkMap validates that discovery targets are private networks and caps the
scan size. The basic scan reads the Linux neighbor table and is non-invasive.
The optional `nmap` mode runs a ping scan if `nmap` is installed; only enable it
on networks you own or are authorized to assess.

## Backups and restore

Use **Configuration → Data & backup → Export** for a consistent JSON snapshot.
The matching Import action validates the entire document and atomically replaces
devices, links, and settings. Export before a large edit or import.

The same operation is available to automation:

```bash
curl -fsS -H "Authorization: Bearer $NETWORKMAP_TOKEN" \
  -o "networkmap-$(date +%F).json" \
  http://127.0.0.1:8765/api/export

curl -fsS -X POST \
  -H "Authorization: Bearer $NETWORKMAP_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @networkmap-2026-09-05.json \
  http://127.0.0.1:8765/api/import
```

Omit the authorization header for a local server with no token. Import ignores
the backup's old revision metadata and creates a new local revision. Because the
database uses SQLite WAL mode, copying only the `.db` file while the server is
running is not a reliable backup; prefer JSON export. If a raw filesystem backup
is required, stop every server process and copy the complete data directory.

## Data locations

| Purpose | Default location |
| --- | --- |
| Server database | `~/.local/share/networkmap/networkmap.sqlite3` (or `$XDG_DATA_HOME/networkmap/networkmap.sqlite3`) |
| Remembered native URL | `${XDG_CONFIG_HOME:-~/.config}/networkmap/native.json` |
| Optional native token | `${XDG_CONFIG_HOME:-~/.config}/networkmap/token` |
| Installed application | `~/.local/lib/networkmap` |
| User service | `${XDG_DATA_HOME:-~/.local/share}/systemd/user/networkmap.service` |
| Previous app versions after upgrade | `${XDG_STATE_HOME:-~/.local/state}/networkmap/install-backups/` |
| Recoverable uninstall files | `${XDG_STATE_HOME:-~/.local/state}/networkmap/uninstall-backups/` |

Use `server.py --data-dir PATH` to choose an explicit, preferably dedicated
server data directory. NetworkMap creates a missing directory with mode `0700`
and database files with mode `0600`; it does not rewrite the mode of a directory
that already exists. Neither installer nor uninstaller removes an existing
database.

## API overview

The UI uses the same JSON API available to scripts:

| Method and route | Purpose |
| --- | --- |
| `GET /api/health` | Public readiness and version check |
| `GET /api/state` | Complete topology and current revision |
| `GET /api/events` | Live SSE update stream |
| `POST /api/session` | Exchange a bearer token for an HttpOnly browser session |
| `POST /api/nodes` | Create a device |
| `PATCH /api/nodes/:id` / `DELETE /api/nodes/:id` | Update or remove a device |
| `POST /api/links` | Create a connection |
| `PATCH /api/links/:id` / `DELETE /api/links/:id` | Update or remove a connection |
| `PATCH /api/settings` | Update workspace settings |
| `POST /api/discovery` | Discover devices from the server host |
| `GET /api/export` / `POST /api/import` | Backup and restore complete state |

State-changing requests accept an expected revision so concurrent editors do
not silently overwrite one another. API errors use a stable JSON shape with an
error code and human-readable message.

## Development checks

```bash
make check
make test
make smoke
```

`make check` runs both `check-web` and `check-native`; the latter confirms the
GTK/WebKit desktop bindings. `make test` needs only Python and runs the
standard-library server and native-helper suites, so it is suitable for a
headless host. `make smoke` starts a temporary live server and exercises its
main HTTP workflow.

## License

MIT — see [`LICENSE`](LICENSE).

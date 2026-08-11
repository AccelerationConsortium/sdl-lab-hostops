# sdl-lab-hostops

Whitelisted **host-operations MCP server** for SDL2 lab machines. It answers
"is the service up, what did it log, did the USB adapter come back, is the
local device API answering" — the class of incident (boot USB-enumeration
races, corrupt-venv locks, silent partial syncs) that a network client can
see but not diagnose.

**Deliberate non-goals:** no shell tool, no arbitrary commands, and no path
to any device `/control/*` endpoint. Hardware control belongs to the
`lab-skills` SDK and its MCP server (`ac-organic-lab` — AGENT_RULES §1.1).
The tool surface below is the complete surface; every target must be
whitelisted in `config.toml`, so a service or port not listed cannot be
touched at all.

## Tools

| Tool | Mutates? | Notes |
|---|---|---|
| `host_info()` | no | hostname, OS, uptime, disk free, loadavg, backend |
| `service_status(service)` | no | systemd unit / Windows service state |
| `tail_service_log(service, lines)` | no | journald (Linux) or NSSM `.out/.err` logs (Windows); 10–400 lines |
| `restart_service(service)` | **yes** | only services in the smaller `restartable` list; Windows path runs `nssm restart` + the mandatory `sc continue` quirk; audited (see below) |
| `list_serial_ports()` | no | pyserial (`[serial]` extra) or `/dev` glob fallback |
| `probe_local_status(port)` | no | `GET http://127.0.0.1:<port>/status` for whitelisted ports |

## Backends

Selected automatically by platform:

- **systemd** (Linux, Raspberry Pi): `systemctl` / `journalctl`. Restarts can
  be prefixed with `sudo -n` (`use_sudo = true`) — grant only the exact
  units, e.g. `/etc/sudoers.d/sdl-lab-hostops`:

  ```
  sdl2 ALL=(root) NOPASSWD: /usr/bin/systemctl restart plateloc.service
  ```

- **nssm** (Windows device PCs): `sc query`, `nssm restart` + `sc continue`
  (DEVICE_PC_SETUP §7 quirk), log tails from `C:\SDL_Logs\<svc>.{out,err}.log`.

## Transports & auth

- **`stdio`** (default): the MCP client spawns the process per connection.
  No daemon, no token. This is also the **daemonless "lite" mode** — see the
  Pi section.
- **`http`** (streamable-http): long-lived service. A non-loopback bind
  **refuses to start** unless `HOSTOPS_TOKEN` is set; clients send
  `Authorization: Bearer <token>`. Network reachability is additionally
  gated by Tailscale ACLs, per the lab's v1 auth posture (STATUS_SPEC §11).
  Exception: `GET /status` is unauthenticated — it serves a read-only
  STATUS_SPEC v1.2 envelope (read-only conformance clause, §9) so the
  dashboard can register the instance in `equipment.yaml` and monitor the
  watchers. Primary operation: none (ops service) — `activity` is
  permanently `idle`; claims/`allowed_actions` are N/A because there is no
  control surface.

## Environment

| Var | Meaning |
|---|---|
| `HOSTOPS_CONFIG` | path to `config.toml` (or pass `--config`) |
| `HOSTOPS_TOKEN` | bearer token, required for non-loopback http binds |
| `HOSTOPS_INGEST_URL` | e.g. `http://sdl2-server:8001` — enables audit rows |

**Audit:** every `restart_service` posts a `hostops_action` event to the
dashboard's `POST /api/ingest/events` (best-effort, never blocks the
restart). Reads are not audited. Register the `hostops_action` event type in
`ac-organic-lab/docs/LAB_MONITORING.md` §4 when the first instance ships.

## Install — Windows device PC (NSSM)

Follow `ac-organic-lab/docs/DEVICE_PC_SETUP.md` §3 with:

```powershell
cd C:\Users\sdl2\Projects
git clone <this-repo> sdl-lab-hostops
cd sdl-lab-hostops
Copy-Item config.example.toml config.toml   # edit whitelists for this PC
C:\SDL_Tools\uv.exe sync --extra serial
nssm install sdl-lab-hostops C:\SDL_Tools\uv.exe run --project C:\Users\sdl2\Projects\sdl-lab-hostops lab-hostops-serve --transport http
nssm set sdl-lab-hostops AppDirectory C:\Users\sdl2\Projects\sdl-lab-hostops
nssm set sdl-lab-hostops AppEnvironmentExtra HOSTOPS_TOKEN=<token> HOSTOPS_INGEST_URL=http://sdl2-server-gaia.tail6a1dd7.ts.net:8001
# ... stdout/stderr logs, auto-start, run-as user: exactly as DEVICE_PC_SETUP §3
nssm start sdl-lab-hostops
sc continue sdl-lab-hostops
```

Note the service account needs rights to restart the *other* services
(`sc sdset` per service, or run sdl-lab-hostops under an account that has them).
A permission failure is reported truthfully in the tool result.

## Install — Linux / Raspberry Pi (systemd)

```bash
git clone <this-repo> /opt/sdl-lab-hostops && cd /opt/sdl-lab-hostops
cp config.example.toml config.toml   # edit
uv sync                              # base deps only — no extras needed
sudo cp deploy/sdl-lab-hostops.service /etc/systemd/system/
sudo systemctl enable --now sdl-lab-hostops
```

### Pi Zero 2W "lite" mode

The base install is small (mcp + httpx/starlette/uvicorn, all pure-Python or
armv8 wheels) and idles at a few tens of MB — the same footprint class as the
`sense-every-zone` gateway already running on `sdl2-pi0-environ-01`. Two ways
to run it lighter:

1. **http, minimal**: skip the `[serial]` extra, keep the whitelist tiny.
2. **daemonless stdio-over-SSH** — zero resident footprint; the central
   Hermes spawns it on demand and SSH is the auth (no token, no open port):

   ```yaml
   # Hermes mcp_servers entry on the central server
   hostops-pi0-environ:
     command: ssh
     args: [sdl2@sdl2-pi0-environ-01, /opt/sdl-lab-hostops/.venv/bin/lab-hostops-serve, --transport, stdio, --config, /opt/sdl-lab-hostops/config.toml]
   ```

   Requires key-based SSH from the central server. Prefer this on the Pis;
   prefer the http service on the Windows PCs (no SSH daemon there).

## Client registration

Register each instance in the lab's reviewed MCP registry
(`ac-organic-lab/mcp/servers.yaml`) with the full 6-tool `include` list, and
mirror that allowlist in the client config (e.g. the Hermes `lab-ops`
profile's `mcp_servers.<name>.tools.include`). A new tool added here should
only reach an agent after both lists are updated — that's the point.

## Tests

```bash
uv run pytest -q
```

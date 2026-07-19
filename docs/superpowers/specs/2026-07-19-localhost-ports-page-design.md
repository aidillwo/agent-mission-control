# Localhost ports page — design

_2026-07-19._

A second dashboard page (separate from the agent view) that lists every
localhost server listening on this Mac, enriched with what app/project it is,
so the user can see at a glance what's running and (later) free a stuck port.

## Verified feasibility (measured on this machine)

- `psutil.net_connections(kind='inet')` → **AccessDenied without root** on
  macOS. Not usable.
- `lsof -nP -iTCP -sTCP:LISTEN` as the user (no sudo) → **works, ~33ms**, lists
  the user's own listeners. Enriching each PID via psutil (name, cmdline, cwd,
  memory, create_time) was **sub-millisecond**.
- The working directory (`proc.cwd()`) of a dev server is its project root —
  e.g. our own `python app.py` on 7777 resolves to `agents-mission-control`. App
  bundles (Spotify, ControlCenter) sit at `/`, so they read as "app, not
  project" for free.

**Scope note (accepted):** without sudo, `lsof` only sees the *user's own*
processes. That is exactly the desired scope (your dev servers); root-owned
listeners simply won't appear. We do **not** escalate to sudo.

## Decisions

1. **Separate page, sibling file.** `GET /ports` serves a new
   `static/ports.html` — its own single vanilla-JS file, mirroring
   `index.html`'s design tokens/header. A header nav link switches between
   **Agents** (`/`) and **Ports** (`/ports`) on both pages.
2. **Data source: lsof + psutil by PID.** `scan_ports()` runs lsof for the
   pid→ports map, then enriches each PID with psutil. lsof truncates the command
   column, so *all display data comes from psutil by PID*; lsof is used only for
   the port mapping. Every psutil call is individually try/except'd (a PID can
   die mid-scan or deny access).
3. **On-demand, not a background loop.** `GET /api/ports` is a **sync** path
   operation, so FastAPI runs it in a threadpool (lsof blocks). No always-on
   scanner: the page polls every 4s *only while its tab is visible*
   (`visibilitychange` pauses it) plus a manual refresh. Zero cost when nobody's
   looking — this is what keeps it not-heavy.
4. **Kill port — shipped (follow-up).** `POST /api/ports/{pid}/kill`, SIGTERM
   then escalate to SIGKILL after 1.5s if it doesn't exit. See "Kill scope"
   below. Frontend confirms (native `confirm()` showing app + PID + ports)
   before firing, then toasts the result and rescans.
5. **Open-in-browser per port.** Each listening port renders as a clickable
   chip/button that opens `http://localhost:<port>` in a new tab — the "what is
   this actually" affordance, and the headline interaction for this pass.
6. **Group by process.** One card per PID with its port(s) as chips (Vite's
   server+HMR, Spotify's several). Mark our own server (`pid == os.getpid()`)
   with a "this dashboard" badge.

## `/api/ports` response

```
{ "scanned_at": <epoch>,
  "servers": [
    { "pid": 60664, "app": "Python", "cmd": "…app.py",
      "cwd": "/Users/…/agents-mission-control",
      "project": "agents-mission-control",   // cwd basename, or null
      "project_like": true,                   // real project vs app/system
      "framework": "Uvicorn",                 // best-effort guess, or null
      "mem_mb": 56, "uptime_s": 1234,
      "ports": [7777], "is_self": true } ],
  "counts": { "servers": N, "projects": M, "ports": P } }
```

`framework` is a best-effort substring guess over the cmdline (vite / next /
react-scripts / webpack / uvicorn / flask / gunicorn / rails / astro / nuxt /
http.server / …) — a label only, never load-bearing.

## Kill scope (added 2026-07-19)

**Project ports only** — the kill button renders only on `project_like` cards,
and the endpoint enforces it server-side (three guards, defense-in-depth against
PID reuse and crafted requests):

1. `pid == os.getpid()` → 400 `self` (never kill the dashboard's own server).
2. pid not a current listener in a fresh `scan_ports()` → 404 `not_listening`
   (re-validates at kill time, shrinking the PID-reuse window; also means only
   things actually shown on the page can be targeted).
3. target not `project_like` → 403 `system` (never a macOS/GUI daemon).

Rationale: the feature's job is freeing a stuck *dev* port. Non-project
listeners are system daemons (ControlCenter/AirPlay, rapportd) and GUI apps —
killing them from a dashboard is useless-to-harmful and they respawn. The rare
non-project process you truly need to kill belongs in Activity Monitor. This
keeps the same `project_like` notion behind both the default filter and the kill
button. The default view is **Projects only** (`hideSystem = true` on load).

## Frontend (`static/ports.html`)

- Header: logo + title, **Agents | Ports** nav, connection/last-scanned
  indicator, a refresh button, and a "Hide system apps" toggle (filters on
  `project_like`; default shows all).
- Insights strip: N servers · M projects · P ports (from `counts`).
- One card per server: app name + framework label + project badge (or
  "app/system"), PID, memory, uptime, and the port chips (each opens
  `localhost:<port>` in a new tab). Our own server gets a distinct badge.
- Empty state when nothing is listening.
- Poll every 4s while visible; pause when hidden; manual refresh.

## Fail-open / safety

Orthogonal to agents — this page never touches sessions, gating, or the DB, so
it cannot affect any agent. Read-only: the worst case is a stale or empty list.
(The future kill button is the only state-changing surface and lands separately,
gated by a confirm.)

## Tests (`tests/test_app.py`)

- `/ports` route returns 200 and serves HTML.
- `parse_lsof_listeners(sample_text)` (pure parser, factored out) extracts the
  right `{pid: {ports}}` from representative lsof output incl. IPv6 `[::1]:port`
  and `*:port` forms.
- `framework_guess(cmdline)` maps known signatures and returns None otherwise.
- `GET /api/ports` returns the documented shape with `servers`/`counts` keys
  (structure only — real listeners are environment-dependent).
- Kill: refuses `os.getpid()` (400 `self`); refuses a non-project pid via a
  monkeypatched scan (403 `system`); 404 when the pid isn't a current listener;
  and a real happy-path — spawn a throwaway `sleep` process, monkeypatch scan to
  claim it as a project listener, POST kill, assert the process actually exits.

Out of scope: asserting specific live ports (machine-dependent).

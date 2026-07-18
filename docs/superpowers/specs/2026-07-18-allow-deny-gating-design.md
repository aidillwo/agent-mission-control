# Allow/Deny Gating — Design

Date: 2026-07-18
Status: Approved (opt-in gating chosen over always-on; Codex limitation accepted)

## Purpose

Let the user approve or reject a blocked agent's pending action directly from
the Agent Mission Control dashboard, instead of switching to the agent's own
terminal/app. Turns the dashboard from watch-only into watch-and-act.

## Goals

- Per-session, opt-in "Gate" toggle on session cards (default off).
- When gated, pre-action hooks hold until the user clicks Allow or Deny on the
  dashboard, with a hard timeout.
- Zero added latency and unchanged behavior for ungated sessions.
- Never brick an agent: any failure (dashboard down, timeout, malformed
  response) falls back to the agent's native permission flow.

## Non-goals (YAGNI)

- No auto-allow rules or policy engine.
- No decision analytics/history views beyond the existing event timeline.
- No Codex remote decisions (technically impossible today, see below).
- No auth/multi-user; localhost-only like the rest of the app.

## Agent support matrix

| Agent | Remote allow/deny | Mechanism |
|---|---|---|
| Claude Code | Yes | `PreToolUse` hook runs synchronously and accepts `permissionDecision: allow/deny` output. |
| Cursor | Yes (beta) | Hook scripts return `{"permission": "allow"/"deny"/"ask"}` synchronously. |
| Codex | No | `notify` is one-way fire-and-forget; Codex never waits for a reply and OpenAI exposes no synchronous hook. Codex keeps its current waiting-input card + notification only. Revisit when OpenAI ships a blocking hook. |
| Kiro | Deferred | Adapter is beta and untested against a live install; wire gating only after event shapes are validated. |

## Architecture

Three pieces: gate hooks (agent side), decision store + long-poll endpoint
(server side), Allow/Deny UI (dashboard side).

### 1. Hook side

- New `hooks/cc_gate.py` for Claude Code, wired by `install_hooks.py` as a
  `PreToolUse` hook (replacing the fire-and-forget curl for that event only;
  all other events keep the curl one-liner). Reads the hook JSON from stdin,
  POSTs it to `/gate/claude-code`, waits up to the gate timeout + 10s slack
  (`curl`-equivalent via urllib with timeout).
  - Response `{gate:false}` or any error/timeout → exit 0 with no output
    (Claude Code proceeds with its native flow: allowlisted tools run,
    others prompt in the terminal).
  - Response `{decision:"allow"}` → print
    `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Approved from Mission Control"}}`.
  - Response `{decision:"deny"}` → same shape with `deny` and reason
    "Denied from Mission Control".
- `hooks/cursor_hook.sh` gains a gate path for `beforeShellExecution` /
  `beforeMCPExecution`: POST to `/gate/cursor` with `--max-time` slightly
  above the gate timeout, then translate the server's `decision` field:
  `allow` → echo `{"permission":"allow"}`, `deny` → echo
  `{"permission":"deny"}`, anything else (`timeout`, `gate:false`, curl
  error, unparseable body) → echo `{"permission":"allow"}` (today's
  behavior). Non-gating events (`beforeSubmitPrompt`, `afterFileEdit`,
  `stop`) keep today's background fire-and-forget.

### 2. Server side (`app.py`)

- Schema additions:
  - `sessions.gated INTEGER DEFAULT 0` (ALTER TABLE guarded by try/except for
    existing DBs).
  - New table `decisions(id INTEGER PK, session_id TEXT, tool TEXT,
    summary TEXT, detail TEXT, status TEXT DEFAULT 'pending',
    created_at REAL, decided_at REAL)`. Status values: `pending`, `allowed`,
    `denied`, `expired`.
- In-memory `pending: dict[int, asyncio.Event]` maps decision id → event used
  to wake the long-poll when the UI decides.
- `GATE_TIMEOUT_S = 120` constant at top of file with the other thresholds.
- Endpoints:
  - `POST /gate/{agent}` — body is the raw hook payload. Resolve session id
    the same way the agent's normalizer does. If session not gated →
    `{gate:false}` immediately. If gated → insert `pending` decision row,
    `add_event(kind="waiting_input", decision_id=...)`, set session status
    `waiting_input`, broadcast, then `await event.wait()` with
    `GATE_TIMEOUT_S` timeout. On decision → `{gate:true, decision:"allow"|"deny"}`.
    On timeout → mark row `expired`, add timeline event, set session back to
    `working`, broadcast, return `{gate:true, decision:"timeout"}`.
  - `POST /api/decision/{id}` — body `{"action":"allow"|"deny"}`. 404 if the
    id is unknown; `{ok:false, reason:"not_pending"}` if already resolved.
    Otherwise update row, add timeline event (`kind="status_change"`,
    summary "Allowed from dashboard: <tool>" / "Denied ..."), set session
    status back to `working`, fire the asyncio event, broadcast.
  - `POST /api/session/{sid}/gate` — body `{"on": true|false}`. Flips
    `sessions.gated`, adds a timeline event, broadcasts.
- `state_payload()` includes each session's `gated` flag and any pending
  decisions (id, session_id, tool, summary, age) so the UI can render buttons.

### 3. UI side (`static/index.html`)

- Session cards for `claude-code` and `cursor` agents get a small "Gate"
  toggle chip (aria-pressed styling like the filter chips). Clicking POSTs
  `/api/session/{sid}/gate`. Gated cards show a shield tint/icon.
- When a pending decision exists for a session, its card shows the action
  ("Bash: rm -rf build") with two buttons: Allow (lime) and Deny (coral),
  wired to `POST /api/decision/{id}`. Buttons stop event propagation so they
  don't open the drawer.
- Pending decisions reuse the existing waiting_input orange pulse +
  browser notification path (no new notification code).
- Card click-through to the timeline shows decision outcomes via the events
  already written server-side.

## Failure behavior (fail-open, always)

| Failure | Result |
|---|---|
| Dashboard not running | Hook's HTTP call errors fast → no output → agent native flow. |
| Timeout (no click in 120s) | Decision marked `expired`; hook emits nothing (CC) / echoes `allow` (Cursor, matching today's behavior) → native flow. |
| Malformed server response | Hook treats as error → native flow. |
| UI decides after expiry | `/api/decision` returns `not_pending`; UI re-renders from state. |

Deny is the only path that actively stops an action, and it returns a clean
refusal with the reason string so the agent can continue conversationally.

## Testing

Extend `tests/test_app.py`:

1. `/gate/claude-code` with ungated session → `{gate:false}` immediately.
2. Gated allow: toggle gate on, fire `/gate` in a task, resolve via
   `/api/decision` → hook receives `allow`, events recorded, session back to
   `working`.
3. Gated deny: same, receives `deny`.
4. Timeout: monkeypatch `GATE_TIMEOUT_S` small → `{decision:"timeout"}`,
   row `expired`.
5. Gate toggle endpoint flips the flag and records an event.
6. `/api/decision/999` → 404; double-decide → `not_pending`.
7. Cursor gate path parity test.

## Rollout

1. Server schema + endpoints + tests.
2. Hook scripts + installer wiring (installer change only touches the
   `PreToolUse` entry; idempotent as before).
3. UI toggle + buttons.
4. Live verification: gate this session, watch a Bash call surface on the
   dashboard, click Allow; then a Deny round; then a timeout round.
5. README: new "Allow/deny" section + update the "Read-only by design" note.

# progress.md — Agent Deck

Living status doc. Update it as work lands. For instructions see `AGENTS.md`;
for the why behind decisions see `context.md`.

_Last updated: 2026-07-19_

## Done (shipped to main)

- **Core dashboard** — FastAPI + SQLite, live WebSocket push (with polling
  fallback), single-file frontend. Session cards, activity feed, hourly +
  7-day charts.
- **Intake**: Claude Code hooks + JSONL tailer, Cursor hooks, Codex notify +
  tailer, Kiro (beta, blind), generic webhook, psutil process scan.
- **Bug fixes**: correct local-midnight "today" stats; `Stop`→idle +
  `SessionEnd`→ended; sticky `waiting_input` (2h); WAL + busy timeout; tailer
  rotation recovery; **tailer now broadcasts live** (was only updating DB);
  `wss` scheme; `Cache-Control: no-cache` on `/`.
- **Icons/branding**: inline Lucide SVGs + SVG favicon; renamed to "Agent Deck".
- **Kiro adapter** (beta, untested against real Kiro).
- **Allow/deny gating v1**: opt-in gate, PreToolUse long-poll, Allow/Deny UI,
  fail-open, 120s timeout. Verified end-to-end (server + hook script + UI).
- **Filters**: two-row multi-select agent/status chips; "Active" filter
  pre-selected on load; 3-day active window; Kiro sorts last.
- **Session history + cost + retention**: `daily_usage`/`daily_rollup` tables,
  `PRICES` estimates, "Tokens today" tile, History drawer (`/api/history`),
  daily rollup+prune, notification click focuses window.
- **Terminal Approval Parity** (spec `2026-07-18-terminal-approval-parity-
  design.md`, commit `45b3fd5`) — SHIPPED:
  - Part 1: `upsert_session(..., soft=True)` — tailers (`claude_line`,
    `codex_line`) never clear a sticky `waiting_input`; hook events still do.
    So real terminal prompts now reliably show orange on the dashboard.
  - Part 2: 3-state `gate_mode` (auto/all/off) with schema migration from the
    old `gated` column; `effective_gate_mode`, `would_prompt` +
    `allow_rules_for`/`_read_allow_rules` (global + project settings, mtime
    cache, bare/exact/`prefix:*` matchers, permission-mode aware);
    `do_gate` holds only would-prompt calls in auto and only when a WS client
    is connected; `do_set_gate(sid, mode)`.
  - Part 3: `do_decide` accepts `always` → appends a conservative allow rule
    to the project's `.claude/settings.local.json`, then allows.
  - UI: 3-state gate chip (`gm-auto`/`gm-all`/`gm-off`), Allow·Always·Deny
    buttons. README gating section rewritten around the modes.
- **Git**: repo initialized, pushed to origin main. `.gitignore` covers
  `.venv`, `amc.db*`, caches, `.claude/settings.local.json`.
- **Launchd auto-start** (spec `2026-07-19-launchd-autostart-design.md`) —
  opt-in `python3 install_hooks.py --launchd` generates a real LaunchAgent at
  `~/Library/LaunchAgents/com.aidill.amc.plist` using the **venv** interpreter
  (the old template pointed at dep-less `/usr/bin/python3`) + absolute repo
  paths, then loads it via `launchctl bootout`/`bootstrap`/`enable` (idempotent).
  `--uninstall-launchd` reverses it. Default install run is unchanged + prints a
  hint. `render_launchd_plist()` is a pure, unit-tested builder; generated plist
  passes `plutil -lint`. Reference `scripts/com.aidill.amc.plist` marked
  REFERENCE-ONLY. **Live load is the user's step** (must free port 7777 first).
- **Tests**: 38 passing (`.venv/bin/pytest -q`) — +4 in `tests/test_launchd.py`.
- **Specs** committed under `docs/superpowers/specs/`.

## To confirm (feature shipped, real-world check pending)

- [ ] **Live-verify terminal parity with a real Claude Code session** (only
      the user can do this end-to-end): in a gated=auto session, run an
      un-allowlisted command → the card should turn orange while the terminal
      stays silent → clicking **Allow** on the dashboard runs it with no
      terminal prompt. Timeout path: no click within 120s → the terminal
      prompt appears and the card stays orange. The server/hook/UI paths are
      unit-tested and committed; this is the human-in-the-loop confirmation.

## Backlog / next pipeline (not started)

1. ~~**Wire the launchd plist**~~ — DONE (opt-in `--launchd`). Remaining: the
   user runs `python3 install_hooks.py --launchd` live (frees 7777 first) to
   confirm auto-start end-to-end.
2. **Quiet the process-scan cards** — `*-proc` "no log data yet" cards are
   noise; hide or only show when no real session exists for that agent.
3. **Validate the Kiro adapter** against a real Kiro install (event/field
   names are guesses).
4. **Bash `prefix:*` "Always" rules** — v1 "Always" writes exact Bash commands
   only; consider safe prefix generation later.
5. **Notch / menu-bar native app** — deferred; SwiftBar plugin covers ~80%.
   This is the surface that could eventually answer already-shown terminal
   prompts (keystroke injection) — explicitly out of scope for now.

## Gotchas for the next assistant

- The preview pane caches `index.html` hard — always cache-bust with `/?v=N`
  when re-verifying frontend changes.
- Port 7777 conflicts: a stray `app.py` may hold it — `lsof -ti :7777 | xargs
  kill` then restart.
- `amc.db` is gitignored and lives on this machine with real history — the
  gate_mode migration must handle the existing `gated` column gracefully.
- Run `git diff` first: the terminal-approval-parity work was mid-implementation
  when the last session ended. Reconcile what's applied vs. the checklist above.

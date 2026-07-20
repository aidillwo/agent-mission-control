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
- **Quiet process-scan cards** (spec `2026-07-19-quiet-proc-cards-design.md`) —
  `{agent}-proc` safety-net cards no longer flip-flop or linger. New
  `reconcile_proc_card(agent)`: retires the `-proc` card when a real
  (non-proc) session is live, else keeps it fresh (no more create→reap→recreate
  every 10 min); `process_scan` broadcasts on change. Frontend guard hides a
  `*-proc` card when a real card for the same agent is already shown (closes the
  ≤15s window). Verified live: guard hides `cursor-proc` when a real cursor
  session is present, keeps a lone `codex-proc`.
- **Safe Bash `prefix:*` "Always" rules** (spec
  `2026-07-19-safe-bash-prefix-rules-design.md`) — the Always button now writes
  a curated safe prefix for Bash (e.g. `git status --short` →
  `Bash(git status:*)`, `ls -la` → `Bash(ls:*)`, `npm test` →
  `Bash(npm test:*)`) via `safe_bash_prefix()` + `SAFE_BASH_HEADS`/
  `SAFE_BASH_SUBCMDS` allowlists (read-only-biased + build/test; destructive
  ops like `git push`, `rm`, `docker run` fall back to exact). `would_prompt`
  hardened: a `prefix:*` rule is **not** trusted for a compound Bash command, so
  `git status; rm -rf ~` can't ride in on `Bash(git status:*)`.
- **Notification sound** (spec `2026-07-19-notification-sound-design.md`) — the
  waiting/error alert now plays a synthesized Web Audio chime (no asset, no CDN):
  rising two-note for `waiting_input`, falling two-note for `error`. Gated by the
  existing bell/`muted` toggle (one control for popup + sound); `AudioContext`
  unlocked on bell click + first pointerdown (autoplay policy). Chime fires even
  when browser-notification permission is denied. Feature-detected + try/catch —
  purely additive, can't break the dashboard. Verified live (chime fires via
  `notify()` on transition, mute suppresses, no console errors).
- **Localhost ports page** (spec `2026-07-19-localhost-ports-page-design.md`) —
  a second page at `/ports` (new `static/ports.html`, header nav links Agents ↔
  Ports on both) listing every localhost TCP listener the user owns, enriched per
  PID with app name, project (from `cwd`), framework guess, memory, uptime.
  `scan_ports()` uses `lsof -nP -iTCP -sTCP:LISTEN` (works without sudo;
  `psutil.net_connections` needs root on macOS) + psutil; `GET /api/ports` is a
  sync path op (threadpool), scanned on demand — the page polls every 4s only
  while visible. Each port renders as a link that opens `localhost:<port>` in a
  new tab; own server badged "this dashboard". Page **defaults to "Projects
  only"** on load (`hideSystem = true`), hiding macOS/GUI apps.
- **Kill port** (same spec, "Kill scope") — `POST /api/ports/{pid}/kill`
  (SIGTERM→SIGKILL after 1.5s). **Project ports only**, enforced in the UI (kill
  button on `project_like`, non-self cards) *and* server-side (3 guards: 400
  `self` for our own server, 404 `not_listening` re-validated via fresh scan,
  403 `system` for non-project pids). Confirm dialog → toast → rescan. Verified
  live end-to-end: spawned a throwaway `http.server`, killed it from the page
  (process actually died, port freed), and confirmed all three guards reject
  (self 400 / ControlCenter 403 / bogus 404) while the dashboard stayed up.
- **Token history by agent/provider** (spec
  `2026-07-19-token-history-grouping-design.md`) — `/api/history` gained
  `by_agent` and `by_provider` (totals over the window, sorted by tokens desc),
  additive to the unchanged `days` array. `token_breakdown()` groups the
  existing `daily_usage.agent` column, and a new `PROVIDERS`/`provider_of()`
  (separate from `PRICES` — vendor identification vs. pricing are different
  concerns) maps model → Anthropic/OpenAI/Other. History drawer now shows
  "Tokens by agent" and "Tokens by provider" tables above the per-day one.
  Verified live with seeded usage (Anthropic/OpenAI split rendered correctly,
  no console errors); test data removed from the live DB afterward.
- **README quick start now uses a venv** (`python3 -m venv .venv` +
  `.venv/bin/pip install`) instead of a bare `pip install` — verified via a
  simulated fresh clone that the old bare-`pip` instructions risk
  `externally-managed-environment` on Homebrew Python, while a venv always
  works. `install_hooks.py`/`fake_agent.py` stay on plain `python3` (genuinely
  stdlib-only, confirmed by reading their imports).
- **Claude yes/no prompt resurfacing** — Claude Code `AskUserQuestion` tool
  calls now mark the session `waiting_input` and notify, both through the
  PreToolUse gate path and the JSONL tailer. These prompts still resolve in the
  CLI; Agent Deck surfaces them so the user knows to go choose there.
- **Port 7777 self-kill** — Ports page now shows a Kill button for Agent Deck's
  own fixed `7777` listener, guarded by an in-page No / Yes modal and a
  server-side `confirm:true` requirement. The API responds first, then
  terminates the server after a short delay. Added `scripts/kill_port.sh` as a
  CLI fallback for later manual port cleanup.
- **Tests**: 56 passing (`.venv/bin/pytest -q`) — +4 launchd, +3 proc-card,
  +3 bash-prefix (and `test_always_appends_rule` updated), +3 ports, +4 kill,
  +2 token-breakdown (and `test_history_endpoint` updated), +2 Claude question
  prompt, +1 confirmed self-kill.
  Notification sound is frontend-only (no server surface) — verified live.
- **Specs** committed under `docs/superpowers/specs/`.

## To confirm (feature shipped, real-world check pending)

- [ ] **Live-verify terminal parity with a real Claude Code session** (only
      the user can do this end-to-end): in a gated=auto session, run an
      un-allowlisted command → the card should turn orange while the terminal
      stays silent → clicking **Allow** on the dashboard runs it with no
      terminal prompt. Timeout path: no click within 120s → the terminal
      prompt appears and the card stays orange. The server/hook/UI paths are
      unit-tested and committed; this is the human-in-the-loop confirmation.
- [ ] **Live-verify launchd auto-start**: free port 7777, then
      `python3 install_hooks.py --launchd` → server comes up and survives a
      logout/login and a `kill`. Reverse with `--uninstall-launchd`.
- [ ] **Live-click "Always"** on a real gated Bash command (e.g. `git status`)
      → confirm `Bash(git status:*)` lands in the project's
      `.claude/settings.local.json` and the next variant isn't held.

## Backlog / next pipeline (not started)

1. ~~**Wire the launchd plist**~~ — DONE (opt-in `--launchd`). Remaining: the
   user runs `python3 install_hooks.py --launchd` live (frees 7777 first) to
   confirm auto-start end-to-end.
2. ~~**Quiet the process-scan cards**~~ — DONE (`reconcile_proc_card` + frontend
   guard).
3. **Validate the Kiro adapter** against a real Kiro install (event/field
   names are guesses).
4. ~~**Bash `prefix:*` "Always" rules**~~ — DONE (`safe_bash_prefix` +
   compound-command gate hardening). Extend the allowlists in `app.py` as needed.
5. ~~**Kill-port action on the Ports page**~~ — DONE (project ports only,
   `POST /api/ports/{pid}/kill` with self/system/not-listening guards + confirm).
6. **Notch / menu-bar native app** — deferred; SwiftBar plugin covers ~80%.
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

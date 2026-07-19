# AGENTS.md — Agent Deck

Canonical instructions for any AI assistant working on this repo.
**Read this first**, then `context.md` (why things are the way they are) and
`progress.md` (what's done, what's next).

## What this is

**Agent Deck**: a local FastAPI + SQLite dashboard at `http://localhost:7777`
that monitors every AI coding agent session on this Mac (Claude Code, Cursor,
Codex, Kiro beta, custom bots) and lets the user approve/deny agent actions
from the dashboard. Local-only by design: no cloud, no auth, no accounts —
never add external dependencies, CDNs, or network calls beyond localhost.

## Layout

| Path | What |
|---|---|
| `app.py` | The entire server: DB, intake normalizers, gating, tailers, endpoints, WS hub. Single file on purpose. |
| `static/index.html` | The entire frontend: single file, vanilla JS, inline Lucide SVGs, no build step. |
| `hooks/cc_gate.py` | Claude Code PreToolUse hook — reports tool use AND gates (fail-open). |
| `hooks/cursor_hook.sh`, `hooks/codex_notify.py`, `hooks/kiro_hook.sh` | Other agent forwarders. |
| `install_hooks.py` | Idempotent installer; writes user-level agent configs, backs up to `*.amc.bak`. Also generates + loads the launchd LaunchAgent (`--launchd` / `--uninstall-launchd`). |
| `tests/test_app.py` | Full pytest suite (44 tests). Keep it green. |
| `tests/test_launchd.py` | Pure/plist-level tests for the launchd wiring (no launchctl, no real `~/Library`). |
| `docs/superpowers/specs/` | Design specs. **Write a spec here before any non-trivial feature.** |
| `scripts/` | SwiftBar menu-bar plugin, reference launchd plist (installer generates the real one), fake_agent demo. |

## Commands

```bash
.venv/bin/pytest -q                          # run tests (must pass before commit)
.venv/bin/python app.py                      # run server (port 7777, fixed — hooks post there)
.venv/bin/python install_hooks.py --dry-run
.venv/bin/python install_hooks.py --launchd  # generate + load LaunchAgent (auto-start on login)
.venv/bin/python scripts/fake_agent.py       # demo data
```

Dev server via Claude Code preview: launch config `amc` in `.claude/launch.json`.

## Hard rules

1. **Fail-open is sacred.** No hook, gate, or dashboard failure may ever block
   or break an agent. Dashboard down ⇒ agents behave exactly as if Agent Deck
   didn't exist. Only an explicit user Deny stops an action.
2. **Port 7777 is fixed** — installed hooks on the user's machine POST to it.
   Never make it dynamic.
3. **Don't rename/move the repo folder** and don't change
   `MARK = "agent-mission-control"` in `install_hooks.py` — installed hooks
   hold absolute paths and the MARK is how the installer finds/uninstalls its
   own entries. (Product name is "Agent Deck"; these identifiers stay legacy.)
4. **No build step, no frameworks.** Frontend is one HTML file; icons are
   inline Lucide SVGs (ISC). Server is one Python file + FastAPI/uvicorn/psutil.
5. **Tests + live verification before "done".** Every feature: pytest suite
   green AND verified in the running browser (the preview pane caches
   `index.html` aggressively — cache-bust with `/?v=N` when re-verifying).
6. **Spec first** for non-trivial features → `docs/superpowers/specs/
   YYYY-MM-DD-<topic>-design.md`, commit it, then implement.
7. Commits: imperative summary + body explaining why; end with
   `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. Push to
   `origin main` (github.com/aidillwo/agent-mission-control) after tests pass.

## Architecture cheat sheet

- **Intake paths** (all feed the same SQLite): Claude Code hooks
  (`/ingest/claude-code/{event}`), Cursor hooks (`/ingest/cursor/{event}`),
  Kiro (`/ingest/kiro/{event}`, beta/untested), Codex notify
  (`/ingest/codex-notify`), generic webhook (`/ingest`), JSONL tailers
  (`~/.claude/projects/`, `~/.codex/sessions/`), psutil process scan
  (`reconcile_proc_card()`: the `{agent}-proc` safety-net card exists only
  while no real session is live for that agent — retired once one appears,
  kept fresh otherwise so it doesn't flip-flop).
- **Status model**: working / idle / waiting_input / error / ended.
  `waiting_input` is sticky (TTL 2h). **Tailers use soft upserts** — they must
  never clear `waiting_input` (see the visibility-stomp fix in
  `docs/superpowers/specs/2026-07-18-terminal-approval-parity-design.md`).
- **Gating**: per-session `gate_mode` off/auto/all. `auto` (claude-code
  default) holds only calls Claude Code would prompt for — `would_prompt()`
  approximates its permission rules (bare tool / exact / `prefix:*`, global +
  project settings, permission modes) — and only while a dashboard WS client
  is connected. Decisions: Allow / Always (writes a rule to the project's
  `.claude/settings.local.json` — for Bash, a curated safe `prefix:*` via
  `safe_bash_prefix()` when the command qualifies, else the exact command;
  compound commands never get a prefix, and a prefix rule is never trusted to
  match a compound command) / Deny. 120s timeout ⇒ native terminal flow.
- **Notifications**: browser `Notification` + a synthesized Web Audio chime
  (rising tone for waiting, falling for error) on the waiting_input/error
  transition, both gated by the header bell/`muted` toggle. No audio asset —
  generated in `static/index.html`.
- **Auto-start**: `install_hooks.py --launchd` generates and loads a
  LaunchAgent (`~/Library/LaunchAgents/com.aidill.amc.plist`) using the venv
  interpreter; `--uninstall-launchd` reverses it. Opt-in, not part of the
  default install.
- **Usage/cost**: `daily_usage` table fed by tailers + webhook; `PRICES`
  table in app.py (estimates only). History drawer reads `/api/history`.
- **Retention**: daily rollup+prune — events >14d aggregated into
  `daily_rollup` then deleted; decisions/ended-sessions >30d deleted.

## Known boundaries (do not "fix" without reading context.md)

- Codex cannot be gated (its notify is one-way). Chat apps (Claude Desktop,
  ChatGPT) cannot be tracked. Claude Code prompts already shown in the
  terminal cannot be answered remotely — the dashboard pre-empts only.
- Multi-machine aggregation was considered and **rejected** — each Mac runs
  its own independent Deck.

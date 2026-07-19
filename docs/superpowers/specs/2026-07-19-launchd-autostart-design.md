# Launchd auto-start — design

_2026-07-19. Backlog item #1: run the Agent Deck server on login and restart it
on crash._

## Problem

The server (`app.py`, port 7777) is started by hand today (`python3 app.py` or
the `amc` preview launch config). If the Mac reboots, or the process dies, the
dashboard is silently gone and no agent activity is recorded until the user
notices and restarts it. Agents themselves are unaffected (fail-open), but the
whole point of Agent Deck — *seeing* what's happening — is lost.

`scripts/com.aidill.amc.plist` exists as a template but is unusable as-is:

- `ProgramArguments` points at `/usr/bin/python3`, which has **no deps**
  (fastapi/uvicorn/psutil live only in `.venv`). The service would crash-loop.
- The app path is a literal `/ABSOLUTE/PATH/TO/...` placeholder.

## Decisions

1. **LaunchAgent, not LaunchDaemon.** Agent Deck monitors *this user's* agents
   and reads `~/.claude`, `~/.codex`, per-project `.claude/settings*`. It must
   run as the user in their GUI session, so it goes in
   `~/Library/LaunchAgents/com.aidill.amc.plist` and loads at login. A system
   LaunchDaemon (root, pre-login) would have the wrong identity and no reason to
   exist for a personal dashboard.

2. **Use the venv interpreter, absolute.** `ProgramArguments` =
   `[<repo>/.venv/bin/python3, <repo>/app.py]`. `WorkingDirectory` = repo root
   (belt-and-suspenders; `app.py` already resolves `DB_PATH`/`static` off
   `__file__`, but project-relative settings reads use session `cwd` so a sane
   cwd doesn't hurt). If `.venv/bin/python3` is missing we refuse to install and
   tell the user to create the venv — better than writing a crash-looping plist.

3. **Opt-in, not part of the default install.** `install_hooks.py` with no args
   keeps doing exactly what it does (wire hooks only). Auto-start changes login
   behavior and starts a long-lived process, so it's a separate, explicit,
   reversible action:
   - `python3 install_hooks.py --launchd` — generate + load the LaunchAgent.
   - `python3 install_hooks.py --uninstall-launchd` — unload + remove it.
   The default run prints a one-line hint pointing at `--launchd`.
   `--dry-run` is honored (prints the plist path + `launchctl` commands, writes
   nothing, calls nothing).

4. **KeepAlive + RunAtLoad true.** Always-running dashboard: start at load,
   restart on any exit. launchd's ~10s throttle on rapid respawns is acceptable
   (a genuinely broken server should back off, not spin). Logs stay at
   `/tmp/amc.log` / `/tmp/amc.err` as the template had them.

5. **Idempotent load.** Before loading, `launchctl bootout gui/$UID <plist>`
   (ignoring "not loaded" errors), then
   `launchctl bootstrap gui/$UID <plist>` and `enable`. Re-running `--launchd`
   after an `app.py` edit picks up the new file cleanly. Uses the modern
   `bootstrap`/`bootout` verbs (macOS 11+); the user is on Darwin 25.

6. **Port-conflict note, not enforcement.** If a hand-started `app.py` already
   holds 7777, the launchd copy fails to bind and throttle-loops. We don't try
   to kill the user's process; `--launchd` prints a reminder to stop any manual
   instance first. (`lsof -ti :7777` is the existing escape hatch in
   `progress.md`.)

## Fail-open note

Auto-start is orthogonal to agent gating. Whether the service is loaded, dead,
or never installed, the PreToolUse gate still fails open — agents never block on
the dashboard's availability. This feature only affects whether the *dashboard*
is up, never whether *agents* run.

## Implementation

- `render_launchd_plist(python, app_py, workdir) -> str` — pure function
  building the XML. No placeholders, no I/O. Unit-testable.
- `LAUNCHD_LABEL = "com.aidill.amc"`, target
  `~/Library/LaunchAgents/com.aidill.amc.plist`.
- `install_launchd()` / `uninstall_launchd()` — write/remove the file and run
  the `launchctl` verbs (skipped under `--dry-run`). `venv_python()` locates
  `.venv/bin/python3` and errors clearly if absent.
- Wire the two flags into `__main__`; keep the default path hooks-only + hint.
- Keep `scripts/com.aidill.amc.plist` as a documented reference, updated to note
  the installer generates the real one.

## Tests

`tests/test_launchd.py` (pure, no launchctl, no real `~/Library`):

- rendered plist contains the venv python path and the app.py path, and **no**
  `/ABSOLUTE/PATH` / `/usr/bin/python3` placeholder.
- rendered plist has `RunAtLoad`, `KeepAlive`, the right `Label`, and parses as
  valid plist XML (`plistlib.loads`).
- `venv_python()` raises/returns clearly when `.venv` is absent (temp dir).

Out of scope: actually loading via launchctl in CI (side-effectful, machine
state) — that's the user's live check, same pattern as the hooks installer.

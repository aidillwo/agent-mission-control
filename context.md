# context.md — Agent Deck

Background and decision record. For instructions see `AGENTS.md`; for status
and next steps see `progress.md`.

## The product in one paragraph

The user (Aidill, github.com/aidillwo) runs several AI coding agents at once
(Claude Code — launched from the desktop app, Cursor, Codex desktop) on two
MacBooks and kept missing the moment an agent was **blocked waiting for their
approval** in some terminal. Agent Deck is a local dashboard (localhost:7777)
that shows every agent session live, screams (orange pulse + browser
notification) when one is waiting on the user, and — the headline feature —
lets the user **approve/deny the agent's action from the dashboard** so they
don't have to find the right terminal. Secondary features: session history,
token/cost estimates, daily digests.

## Environment facts

- Repo: `~/Documents/github_projects/agents-projects/agents-mission-control`,
  remote `https://github.com/aidillwo/agent-mission-control.git`, branch main.
- Python 3.14 venv at `.venv` (system python has no deps). fastapi, uvicorn,
  psutil, websockets + pytest, httpx.
- Hooks are **installed live on this machine**: `~/.claude/settings.json` has
  AMC entries for SessionStart/UserPromptSubmit/PreToolUse (→ `cc_gate.py`)/
  PostToolUse/Notification/Stop/SessionEnd; `~/.cursor/hooks.json` wired.
  Codex `notify` slot was already taken by Codex Computer Use — left alone
  (Codex is covered by the log tailer).
- The user runs the server themselves (`python3 app.py` or the preview launch
  config `amc`), or via the opt-in LaunchAgent (`install_hooks.py --launchd`,
  see decision 12) — the user hasn't loaded the live one yet.
- The user's second MacBook: copy folder, `pip install -r requirements.txt`,
  `python3 install_hooks.py` — each machine fully independent (by decision).

## Decision record (chronological, with the why)

1. **Rename**: "Agent Mission Control" → **"Agent Deck"** (user's pick).
   Visible branding only; folder name, `MARK`, `amc.db`, plist names kept —
   renaming them would break the live install.
2. **Claude Code `Stop` ≠ session end** — it fires per turn. Mapped to idle;
   `SessionEnd` is the real end. `waiting_input` sticky for 2h (a blocked
   session is silent by definition).
3. **Icons**: inline Lucide SVGs + SVG favicon, no CDN — preserves the
   "everything local" promise. lucide-react rejected (needs build step).
4. **Chat apps (Claude Desktop, ChatGPT) are untrackable** by design:
   conversations are server-side; local data is opaque Chromium storage; no
   hooks, no lifecycle. Documented in README "Notes and limits". Only viable
   integration would be a custom MCP server posting to `/ingest`.
5. **Kiro adapter wired blind** (user doesn't have Kiro yet, "KIV"):
   `/ingest/kiro/{event}` + `hooks/kiro_hook.sh` + installer detection, with
   defensive field matching. Event/field names are best-guess — must be
   validated when Kiro is actually installed. Kiro sorts last in UI (least
   used). Gating for Kiro deferred.
6. **Codex cannot be gated** — `notify` is one-way; no synchronous hook
   exists. Watch-only, approve in Codex itself. User accepted this.
7. **Multi-machine aggregation rejected** by the user — each Mac independent.
8. **Gating v1** (spec `2026-07-18-allow-deny-gating-design.md`): opt-in
   per-session boolean gate, PreToolUse long-polls `/gate/{agent}`, fail-open
   everywhere, 120s timeout. User chose opt-in over always-on.
9. **History + cost + retention** (spec
   `2026-07-18-history-cost-retention-design.md`): `daily_usage` (tokens by
   day/agent/model, fed by Claude Code JSONL usage w/ message-id dedupe,
   Codex token_count deltas (blind, beta), webhook `tokens_in/out`),
   `PRICES` static table (opus 5/25, sonnet 3/15, haiku 1/5, fable 10/50,
   gpt-5 1.25/10 USD/MTok — estimates, edit in app.py), history drawer,
   "Tokens today" tile, daily rollup+prune (events 14d, sessions/decisions
   30d). Costs labeled "est" — cache discounts not modeled.
10. **Terminal approval parity** (spec
    `2026-07-18-terminal-approval-parity-design.md`) — the big correction.
    User found that (a) real terminal approvals never showed on the app (the
    tailer stomped `waiting_input` within 3s) and (b) the gate held noise
    (ls, skills) instead of real approvals. Fix: soft tailer upserts; 3-state
    `gate_mode` (auto/all/off) with **auto** the claude-code default —
    `would_prompt()` approximates Claude Code's own permission rules and
    holds only would-prompt calls, only while a dashboard client is
    connected; **Always** button writes an allow rule to the project's
    `.claude/settings.local.json` (same file Claude Code's own "always
    allow" uses). Boundary accepted: prompts already rendered in the
    terminal can't be answered remotely — the app pre-empts only.
11. **UI decisions**: bottom panels `min-width:0` fix (grid blowout), agent
    chips and status chips on two rows, both **multi-select**, "Active"
    (= not ended) status filter **pre-selected on load**, cards limited to
    sessions active in the last 3 days, `Cache-Control: no-cache` on `/`.
12. **Launchd auto-start** (spec `2026-07-19-launchd-autostart-design.md`) —
    opt-in `install_hooks.py --launchd` (LaunchAgent, not a system daemon —
    needs the user's GUI-session identity to read `~/.claude` etc.). Fixed the
    old plist template, which pointed at dep-less `/usr/bin/python3` and a
    placeholder path; the installer now generates it from the venv interpreter.
    Kept opt-in rather than folded into the default install because it changes
    login behavior and starts a long-lived process.
13. **Quiet process-scan cards** (spec `2026-07-19-quiet-proc-cards-design.md`)
    — the `{agent}-proc` psutil safety net was flip-flopping (create → reaped
    after ~10min idle → recreate, forever) and lingering next to real sessions.
    Fixed by making the card's existence conditional on there being no real
    session for the agent, rather than a one-shot creation.
14. **Safe Bash `prefix:*` "Always" rules** (spec
    `2026-07-19-safe-bash-prefix-rules-design.md`) — v1 Always wrote the exact
    Bash command only, so near-variants re-prompted. Added a curated
    read-only/build-test allowlist for safe prefixes; destructive commands
    (`git push`, `rm`, `docker run`, ...) are deliberately excluded and still
    get exact rules. Also hardened `would_prompt`: a prefix rule is never
    trusted for a *compound* Bash command, so a prefix can't be ridden in on by
    an appended destructive command.
15. **Notification sound** — synthesized Web Audio chime (rising tone for
    waiting_input, falling for error) added alongside the existing browser
    `Notification`, gated by the same bell/mute control. Chosen over a shipped
    audio file to keep the "everything local, no external assets" rule; fires
    even when Notification permission is denied, since a missed popup was the
    original complaint.
16. **Localhost ports page** (spec `2026-07-19-localhost-ports-page-design.md`)
    — a second page (`/ports`) listing localhost servers with what app/project
    each is, so the user can see what's running and open any port. Verified the
    hinge before building: `psutil.net_connections` is AccessDenied without root
    on macOS, but `lsof` as the user works (~33ms) and `proc.cwd()` gives the
    project — so it's feasible and cheap. Kept a **separate page** (user's
    instinct) because it's really a generic port manager, not agent-specific;
    scanned on demand (sync endpoint → threadpool, page polls only while
    visible) so it adds zero background cost. Shipped **read-only** at the
    user's request; kill-port deferred to a later pass with a confirm dialog.
    Uses `lsof` not sudo, so only the user's own processes appear (the right
    scope for their dev servers). **Kill port** followed shortly after, scoped
    to **project ports only** (user asked "project or all?" → project: the
    non-project listeners are macOS daemons/GUI apps where a kill is
    useless-to-harmful and they respawn). Enforced in the UI (button on project,
    non-self cards) and server-side (self/system/not-listening guards, re-scan
    at kill time vs PID reuse). Page defaults to a Projects-only view.
17. **README quick start now creates a `.venv`** instead of a bare
    `pip install`. Verified (not assumed) by simulating a fresh clone twice:
    once confirming the whole quick-start pipeline actually works end-to-end
    (db auto-creates, tables auto-migrate, install_hooks/fake_agent both run
    clean), and once confirming the venv change itself installs cleanly. The
    bare-`pip install` form risks `externally-managed-environment` on Homebrew
    Python (a well-known PEP 668 failure mode) even though it happened to work
    on this machine's Framework Python. `install_hooks.py`/`fake_agent.py`
    stay on plain `python3` — read their imports, genuinely stdlib-only, no
    reason to imply they need the venv.
18. **Token history by agent/provider** (spec
    `2026-07-19-token-history-grouping-design.md`) — `/api/history` gained
    `by_agent`/`by_provider` breakdowns, additive to the existing `days` array.
    Provider is derived from the **model** string, not the agent field —
    deliberately a separate axis, since Cursor/custom sessions can run either
    vendor's model. `PROVIDERS` kept as its own list rather than reusing
    `PRICES`, since vendor identification and pricing are different concerns
    (a model can be identifiable without a known price).

## Style / working preferences observed

- User approves broadly ("proceed", "i approve everything") and prefers
  momentum over questions — but wants specs written before features.
- Be honest about limitations (Codex, chat apps, blind Kiro wiring) — the
  user explicitly values that over overpromising.
- The dashboard is self-hosting: this very repo's Claude Code session shows
  up on it — convenient for live verification.

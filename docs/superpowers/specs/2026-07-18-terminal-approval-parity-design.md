# Terminal Approval Parity — Design

Date: 2026-07-18
Status: Draft — awaiting user approval

## Problem

The approvals Claude Code raises in the terminal are the ones the user wants
on the dashboard, and they mostly don't appear there:

1. **Visibility bug.** The `Notification` hook does set the session to
   `waiting_input` when Claude Code asks for permission — but the JSONL
   tailer (3s poll) reads the transcript line for that same tool call moments
   later and stomps the status back to `working`. The dashboard shows a calm
   "working" card while the terminal sits blocked, so the user never knows to
   go approve.
2. **Wrong gate population.** The gate holds *every* tool call on a gated
   session (noise: `ls`, skill loads, reads that Claude Code would auto-allow
   silently), while on ungated sessions the calls Claude Code *would* prompt
   for pass straight to the terminal, un-catchable from the app.

Goal: the dashboard surfaces (and can approve) the same approvals Claude Code
would raise in the terminal — not more, not less. Tool-level "gate
everything" stays available but stops being the main mode.

## Honest boundary (stated up front)

Claude Code has no API to answer a permission prompt **already displayed** in
the terminal. The only remote-decision mechanism is the `PreToolUse` hook
returning `permissionDecision` *before* the prompt renders. So the dashboard
**pre-empts** prompts; it cannot answer one that already fell through to the
terminal. Fallback behavior: if the user doesn't decide on the dashboard
within the hold window, the terminal prompt appears as it does today — and,
with the visibility fix, the card now correctly shows "Waiting for you" so
the user knows to switch to the terminal.

## Part 1 — Fix the visibility stomp

- `upsert_session` gains a `soft=False` parameter. Soft updates set
  `status='working'` **only if** the current status is not `waiting_input`.
- The Claude Code and Codex **tailers** use soft updates (they are passive
  observers of the transcript; a transcript line is not evidence the block
  cleared).
- **Hook-driven** events keep hard updates: `PostToolUse` (the tool actually
  ran ⇒ the user approved in the terminal), `UserPromptSubmit`,
  `SessionStart`, gate pass-throughs. These clear stale waiting states.
- `Notification` handler: parse the tool name out of the message where
  possible ("Claude needs your permission to use Bash") so the card says
  what is being asked.

Result: any terminal prompt reliably flips the card orange + fires the
browser notification, even with no gating at all.

## Part 2 — "Auto" gate: hold only what Claude Code would ask

New per-session gate mode replacing the boolean:

| Mode | Behavior |
|---|---|
| `off` | Never hold. Report tool_use events only. |
| `auto` (**default for claude-code sessions**) | Hold **only calls Claude Code would prompt for**, and only while at least one dashboard client is connected (`hub.clients` non-empty). No client ⇒ zero added latency, straight to native flow. |
| `all` | Current behavior — hold everything (kept per user request; the strict remote-control mode). |

`sessions.gated` column becomes `gate_mode TEXT` (`off`/`auto`/`all`);
migration maps 0→`auto` (new default for claude-code; `off` for cursor), 1→`all`.

### Would-prompt evaluation (server-side, approximate by design)

`would_prompt(tool_name, tool_input, cwd, permission_mode)` in `app.py`:

1. `permission_mode` from the hook payload: `bypassPermissions` ⇒ never
   hold; `plan` ⇒ never hold; `acceptEdits` ⇒ don't hold Edit/Write/
   NotebookEdit; else continue.
2. **Safe set** never held: Read, Glob, Grep, LS, TodoWrite, NotebookRead,
   Task, Skill, and other read-only tools.
3. **Prompt-y set**: Bash, Write, Edit, MultiEdit, NotebookEdit, WebFetch,
   and `mcp__*` tools — held **unless** matched by an allow rule.
4. Allow rules read from `~/.claude/settings.json` plus
   `<cwd>/.claude/settings.json` and `<cwd>/.claude/settings.local.json`
   (`permissions.allow`), cached by file mtime. Matcher supports the common
   forms: bare `ToolName`, `ToolName(exact)`, and Bash prefix rules
   `Bash(prefix:*)`. Rules we cannot confidently parse are ignored (erring
   toward holding, i.e. toward surfacing an approval).
5. Deny/ask rules are **not** replicated — Claude Code enforces them
   natively after our pass-through.

This is an approximation of Claude Code's matcher and is documented as such.
Failure modes are graceful both ways: wrongly held ⇒ times out into the
native flow; wrongly passed ⇒ terminal prompt appears and (Part 1) is now
visible on the dashboard.

### Decision flow (auto mode)

`/gate/claude-code` → would_prompt? no ⇒ `{gate:false}` (report event only).
Yes and a client is connected ⇒ create pending decision, broadcast + browser
notification (existing waiting_input path), hold up to `GATE_TIMEOUT_S`.
Allow ⇒ `permissionDecision: allow` (terminal never prompts — approving in
the app *is* the approval). Deny ⇒ clean refusal. Timeout ⇒ no output ⇒
native terminal prompt (now visible per Part 1).

## Part 3 — "Always allow" from the dashboard

The decision UI gains a third button: **Always** — appends a conservative
allow rule to `<cwd>/.claude/settings.local.json` (the same file Claude
Code's own "always allow" writes), then allows:

- non-Bash tool ⇒ `ToolName(exact-specifier)` where applicable, else bare
  `ToolName`.
- Bash ⇒ exact `Bash(<command>)` only. No wildcard generation in v1 (too
  easy to over-grant); note as future work.

File edited with the same backup + valid-JSON guard the installer uses.
`would_prompt` cache invalidates on mtime so the rule takes effect on the
next call.

## UI changes

- Gate chip becomes a 3-state cycle: `Auto` (shield, default) → `All` →
  `Off` → `Auto`. Label shows the mode; tooltip explains it.
- Decision block buttons: **Allow · Always · Deny**.
- Card text for held decisions and Notification-driven waits shows the tool
  + target (already mostly in place).

## Cursor

Unchanged semantics (`off`/`all` only — we cannot evaluate Cursor's
permission rules). `auto` is claude-code-only for now.

## Testing

- soft vs hard upsert: tailer line does not clear `waiting_input`;
  PostToolUse does.
- `would_prompt`: safe set passes; Bash held; allow-rule exact + `prefix:*`
  matches suppress hold; `bypassPermissions`/`acceptEdits`/`plan` modes;
  unparseable rules ignored.
- auto mode end-to-end: no client ⇒ `{gate:false}`; with client ⇒ held,
  allow/deny/timeout paths.
- migration 0/1 → `auto`/`all`; default modes per agent.
- Always: rule appended to settings.local.json (tmp dir), JSON stays valid,
  next `would_prompt` passes.

## Rollout

1. Part 1 (visibility) — smallest, highest value, ships even if the rest
   slips.
2. Part 2 server + tests, then UI 3-state chip.
3. Part 3 Always button.
4. Live verification with a real Claude Code session: un-allowlisted command
   → card turns orange while terminal is silent → Allow from app → command
   runs with no terminal prompt. Timeout path → terminal prompt appears and
   card stays orange.
5. README rewrite of the gating section around the new modes.

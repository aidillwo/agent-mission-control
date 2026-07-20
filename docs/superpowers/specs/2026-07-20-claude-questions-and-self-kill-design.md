# Claude Questions and Self Kill — Design

Date: 2026-07-20
Status: Approved

## Problem

Claude Code can ask a direct yes/no-style question in the terminal using the
`AskUserQuestion` tool. Agent Deck currently records that as ordinary tool
activity, so the card does not enter `waiting_input` and no notification fires.
Separately, the Ports page can kill project servers, but hides and rejects the
dashboard's own port 7777 process. The user wants a visible way to stop it from
the dashboard, guarded by a yes/no confirmation UI.

## Design

1. Treat Claude Code `AskUserQuestion` tool calls as user-attention events.
   They are not remote-answered by Agent Deck; they only mark the session
   `waiting_input`, add a `waiting_input` event, and let Claude Code continue to
   its native terminal prompt.
2. Detect the prompt both from the PreToolUse gate path and the JSONL transcript
   tailer. Either path may arrive first; both should keep the card waiting.
3. Keep the existing permission gate semantics for real approve/deny tool
   decisions. `AskUserQuestion` is always pass-through because Agent Deck has no
   yes/no answer channel for it yet.
4. Add a special self-kill API path for port 7777. It requires an explicit
   confirmation payload, returns success first, then terminates the current
   process after a short delay so the browser receives the response.
5. Replace the Ports page native `confirm()` with an in-page modal styled like
   the existing UI. The modal has `No` and `Yes, kill` buttons. The self card
   shows a kill button only when it is actually listening on port 7777.

## Safety

- Fail-open remains intact: prompt notification failures must not block Claude
  Code.
- System/non-project ports stay protected.
- Self-kill requires both the UI confirmation and server-side
  `{"confirm": true}`.
- The existing script `scripts/kill_port.sh` remains the CLI fallback.

## Testing

- Unit test: `AskUserQuestion` through `do_gate()` marks the session
  `waiting_input` and returns `gate:false`.
- Unit test: `AskUserQuestion` in `claude_line()` marks the session
  `waiting_input`.
- Unit test: self-kill rejects missing confirmation and schedules termination
  when confirmed, without killing the pytest process.
- Existing kill-port tests remain green.

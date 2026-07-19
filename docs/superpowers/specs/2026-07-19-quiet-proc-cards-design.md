# Quiet the process-scan cards — design

_2026-07-19. Backlog item #2._

## Problem

`process_scan()` (app.py) is a psutil safety net: every 15s it looks for known
agent processes (claude, codex, cursor, kiro) and, when it finds one **and no
currently-live session exists** for that agent, creates a placeholder card
`{agent}-proc` with status `idle` and task "Process detected (no log data yet)".

Two noise sources:

1. **Flip-flop.** Once created, the `-proc` card is itself an `idle` session, so
   the "is a session live?" guard is satisfied and the card is never refreshed.
   ~10 min later the reaper ends it (IDLE_S), the next scan sees nothing live,
   and recreates it. So it blinks in and out roughly every 10 minutes forever.
2. **Redundancy / lingering.** When a real hook/tailer-driven session *does*
   appear for the agent, the stale `-proc` card isn't cleaned up — it lingers as
   an idle card next to the real one until the reaper eventually ends it.

The card only has value when it's the *sole* signal for an agent (a process we
have no hook/tailer coverage for). Alongside a real session it's pure clutter.

## Fix

Make the `-proc` card obey a single rule: **it exists only while there is no
real session for that agent.**

**Backend** — extract `reconcile_proc_card(agent)` (pure DB, unit-testable),
called once per detected agent by `process_scan`:

- If a **real** (non-`-proc`) session for the agent is live
  (working/idle/waiting_input) → retire the `-proc` card (`status='ended'`) so
  it stops cluttering. Return "changed" if it was retired.
- Else (the process is our only signal) → `upsert_session` the `-proc` card,
  which refreshes `last_seen_at` so it stays alive instead of flip-flopping.
  Return "changed" only when the card was newly created or revived from
  `ended` (so we don't broadcast on every idle refresh).

`process_scan` `await hub.broadcast()` once per pass if any reconcile reported a
change (it didn't before — it relied on the reaper's next tick).

**Frontend** — belt-and-suspenders for the ≤15s window between a real session
appearing and the next scan retiring the `-proc` card: after status filtering,
drop any `*-proc` card whose agent already has a non-`-proc` card in the shown
set.

## Trade-off (accepted)

A genuinely uncovered agent process (no hooks/tailer) now keeps a *live*
`-proc` card refreshed every 15s — it reads `working` rather than blinking
idle. That's more honest ("this agent is running, we just have no session
data") and, crucially, stops the every-10-min flip-flop. The task text keeps the
"no log data yet" caveat so it's never mistaken for real activity.

## Tests

`tests/test_app.py`:

- real live claude-code session + a `claude-code-proc` card → `reconcile_proc_
  card('claude-code')` retires the proc card (status ended) and returns True.
- no session at all → creates the `-proc` card, returns True; a second call with
  the card already idle returns False (no redundant broadcast) and leaves it
  idle (no flip-flop / not ended).
- proc card previously `ended`, still no real session → revived to idle, returns
  True.

# Session History, Cost Tracking & Retention — Design

Date: 2026-07-18
Status: Approved scope (features #1 session history + #4 cost/token tracking;
retention folded in as their prerequisite; notification click-through as a
minor add-on; multi-machine explicitly out — each Mac runs its own Deck).

## Purpose

Turn Agent Deck from a live monitor into a record: "what did my agents do
today/this week, and roughly what did it cost" — while stopping the database
from growing forever.

## Non-goals

- Multi-machine aggregation (each Mac independent, per user decision).
- Native notification action buttons (needs the notch/native app — deferred;
  v1 ships notification *click focuses the dashboard* only).
- Exact billing. Costs are **estimates** from a static price table; cache
  discounts and price changes are not modeled. UI labels them "est".

## Data model

Two small aggregate tables that survive pruning:

- `daily_usage(day TEXT, agent TEXT, model TEXT, tokens_in INT, tokens_out INT,
  PRIMARY KEY(day, agent, model))` — written live by `add_usage()` whenever a
  usage sample arrives. Powers the "Tokens today" tile and history cost lines.
- `daily_rollup(day TEXT, agent TEXT, events INT, tool_calls INT, completed INT,
  PRIMARY KEY(day, agent))` — written by the pruner just before old events are
  deleted, so history counts survive retention.

## Usage intake

- **Claude Code (tailer):** assistant JSONL lines carry `message.usage`
  (`input_tokens`, `output_tokens`). Streaming writes several lines per
  message with repeated usage, so dedupe by message id (skip if same id as
  the previous usage-bearing line for that file).
- **Codex (tailer):** defensive parse of `token_count` info events
  (`payload.info.total_token_usage`); beta until validated against real logs.
  Counts are cumulative per session, so store the delta from the last seen
  totals per file.
- **Custom bots (webhook):** optional `tokens_in` / `tokens_out` integer
  fields on `/ingest`, added to that day's bucket.

## Cost estimation

Static `PRICES` table in `app.py`, substring-matched against the model name,
USD per MTok (input, output): opus → (5, 25), sonnet → (3, 15),
haiku → (1, 5), fable → (10, 50), gpt-5 → (1.25, 10). Unknown model → tokens
shown, cost omitted. Comment directs users to edit `PRICES` as prices change.

## Retention

Constants: `EVENTS_RETAIN_DAYS = 14`, `SESSIONS_RETAIN_DAYS = 30`.
A daily task (piggybacked on the reaper loop, guarded by a last-run-day check):

1. Aggregate events older than 14 days into `daily_rollup` (grouped by local
   day + the session's agent_type), then delete them.
2. Delete decisions older than 30 days.
3. Delete ended sessions not seen for 30 days.

## Endpoints

- `GET /api/history?days=30` — per-day rows merging live counts (events table,
  recent days) with `daily_rollup` (older days), joined with `daily_usage`
  tokens and estimated cost. Shape:
  `{days: [{day, agents: {...}, events, tool_calls, completed, tokens_in,
  tokens_out, est_cost}]}` (est_cost null when unknown-model tokens only).
- `state_payload()` gains `usage_today: {tokens_in, tokens_out, est_cost}`.

## UI

- Fifth hero tile **"Tokens today"**: compact count (e.g. `132k`), subtitle
  `≈ $0.42 est` (or "no cost data" when unknown).
- **History** button in the header opens an overlay (drawer pattern) listing
  the last 30 days: day, events, tool calls, completed, tokens, est cost.
- Browser notification `onclick` focuses the dashboard window.

## Testing

- usage accumulation + Claude Code dedupe-by-message-id
- Codex cumulative-delta handling
- webhook token fields
- `PRICES` matching incl. unknown model → no cost
- rollup+prune: old events aggregated then deleted, recent kept, decisions
  and stale sessions pruned
- `/api/history` merges live + rolled-up days; `usage_today` in state

## Rollout

Server + tests → UI → live verification (seed usage, check tile/history in
browser) → README → commit/push.

# Token history grouped by agent / provider — design

_2026-07-19._

## Problem

The History drawer's per-day table shows one lumped `tokens_in+tokens_out`
number per day. `daily_usage` is already keyed by `(day, agent, model)`, so the
data to break tokens down by agent (Claude Code / Codex / Cursor / custom) or
by service provider (Anthropic / OpenAI / other) already exists — it's just
never surfaced. The user wants to see which agent or which provider the tokens
(and estimated cost) came from.

## Decisions

1. **Additive to `/api/history`, not a replacement.** The existing `days` array
   (one row per day) stays exactly as-is — nothing consuming it today breaks.
   Add two new top-level keys, `by_agent` and `by_provider`: totals over the
   same `days` window, one row per agent/provider, sorted by total tokens
   descending.
2. **Provider is derived from `model`, not `agent`.** "Which coding tool" and
   "which model vendor" are different axes — Cursor or a custom bot could run
   an Anthropic or an OpenAI model. A small `PROVIDERS` substring list (same
   style as the existing `PRICES` table) maps a model string to `Anthropic` /
   `OpenAI` / `Other`. Kept separate from `PRICES` (pricing and vendor
   identification are different concerns — a model could be identifiable
   without a known price, or vice versa).
3. **`by_agent` groups the existing `daily_usage.agent` column** (already
   populated: `claude-code`, `codex`, or whatever a custom `/ingest` caller
   sends). No new column, no migration.
4. **Frontend: two compact tables in the History drawer**, "Tokens by agent"
   and "Tokens by provider", reusing the existing `.hist` table style, placed
   above the per-day table. Read-only, no interaction — just another view onto
   data the drawer already fetches.

## Implementation

- `PROVIDERS = [(substr, name), ...]` + `provider_of(model)` next to
  `PRICES`/`est_cost`.
- `token_breakdown(days)` — one query (`SELECT agent, model, SUM(tokens_in),
  SUM(tokens_out) FROM daily_usage WHERE day>=cutoff GROUP BY agent, model`),
  aggregated in Python into per-agent and per-provider totals with
  `est_cost()` summed per bucket (`None` cost rows excluded from the cost sum,
  same convention as `history_days`).
- `api_history` calls it once and merges into the existing response.
- `openHistory()` (frontend) renders the two new tables from the same fetch.

## Tests

- `provider_of()`: known substrings map correctly, unknown model → `"Other"`.
- `token_breakdown()`: usage across two agents/models aggregates correctly per
  agent and per provider, sorted descending, cost summed only over priced rows.
- `/api/history` response includes `by_agent`/`by_provider` alongside the
  unchanged `days` array.

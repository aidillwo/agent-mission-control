# Token Usage Drilldown — Design

Date: 2026-07-20
Status: Approved

## Problem

The dashboard's "Tokens today" card shows a useful headline number, but the
details live in the History drawer and are fixed to a 30-day agent/provider/day
view. The user wants clicking the token card to open a focused usage view with
cost calculations and flexible grouping by date, model, agent, provider, and
similar dimensions.

## Scope

V1 uses the existing `daily_usage(day, agent, model, tokens_in, tokens_out)`
table. It supports dimensions that are already reliable historically:

- day/date
- model
- agent
- provider, derived from model

Project/session grouping is intentionally out of v1 because `daily_usage` does
not currently store project or session ids. Adding that later requires a schema
migration and capture changes.

## Backend

Add `GET /api/usage?days=30&group_by=model`.

Parameters:

- `days`: `1`, `7`, `30`, `365`, or `0` for all available usage.
- `group_by`: `day`, `model`, `agent`, or `provider`.

Response:

- `range`: normalized range label.
- `group_by`: normalized grouping.
- `totals`: input, output, total tokens, estimated cost, and unknown-cost flag.
- `rows`: grouped rows sorted by total tokens descending except `day`, which is
  sorted newest first.
- `daily`: day-level rows for the selected range, used by the frontend chart.

Cost uses existing `PRICES` and `est_cost()`. Unknown models still show token
counts but contribute no cost; totals expose `has_unknown_cost`.

## Frontend

Clicking the "Tokens today" tile opens a new Usage drawer, separate from the
existing History drawer.

Controls:

- Range: Today, 7d, 30d, All
- Group: Date, Model, Agent, Provider

Drawer content:

- Total tokens, input, output, estimated cost summary
- Compact bar chart from `daily`
- Grouped table with group, input, output, total, estimated cost

The UI stays single-file vanilla JS and uses the existing dashboard visual
language. No external libraries or build step.

## Testing

- Unit test the aggregation helper for model grouping and cost totals.
- Unit test provider grouping and unknown-cost signaling.
- Endpoint test validates parameter normalization and response shape.
- Parse-check `static/index.html` after frontend changes.

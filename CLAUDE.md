# CLAUDE.md

**Always read and follow [AGENTS.md](AGENTS.md) — it is the canonical
instruction file for this repo.** Everything there applies to Claude Code.

Then read, in order:

1. [context.md](context.md) — what this project is and the decisions behind it
2. [progress.md](progress.md) — what's done, current state, and the next pipeline

Quick reminders (details in AGENTS.md):

- Tests: `.venv/bin/pytest -q` — must pass before any commit.
- Fail-open is sacred: nothing here may ever block or break a coding agent.
- Spec before feature: `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`.
- Verify in the live browser too, not just tests (cache-bust with `/?v=N`).
- Port 7777 is fixed; don't rename the folder or the `MARK` identifier.

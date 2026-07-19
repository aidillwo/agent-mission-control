# Safe Bash `prefix:*` "Always" rules — design

_2026-07-19. Backlog item #4._

## Problem

The **Always** button (`add_always_rule`) writes, for Bash, the *exact* command
only: click Always on `git status --short` and you get `Bash(git status --short)`
— the very next `git status -s` prompts again. Claude Code's own "always allow"
offers a prefix ("don't ask again for `git status` commands" →
`Bash(git status:*)`). We want that convenience, **without** broadening into
anything dangerous.

The gating side already *matches* `prefix:*` rules (`rule_matches` →
`specifier.startswith(...)`). The gap is generating them safely, plus a matching
hazard: pure string-prefix matching means a rule `Bash(git status:*)` would
match `git status; rm -rf ~` — the appended command rides in on the prefix.

## Two changes

### 1. Generate a prefix only for a curated, safe set

`safe_bash_prefix(cmd) -> str | None` in `add_always_rule`:

- Return `None` (→ fall back to today's exact-command rule) if the command is
  **compound** — contains any of `; & | < > ` `$(` `` ` `` newline. A prefix off
  a compound command is never safe.
- Otherwise `shlex.split` it (parse failure → `None`) and:
  - first token in `SAFE_BASH_HEADS` (read-only / routinely-whitelisted
    single-token commands: `ls cat grep rg find head tail wc pytest make tsc
    ruff mypy …`) → prefix = that token;
  - else first two tokens in `SAFE_BASH_SUBCMDS` (curated read-only two-token
    ops: `git status`, `git log`, `git diff`, `npm test`, `npm run`,
    `cargo test`, `go build`, …) → prefix = those two tokens;
  - else `None`.
- Rule written = `Bash(<prefix>:*)`, else `Bash(<exact command>)`.

The allowlists are **read-only-biased** plus the build/test commands developers
overwhelmingly want to blanket-approve. Destructive or state-changing
subcommands (`git push`, `git reset`, `rm`, `docker run`, `pip install`,
`brew install`, `kubectl delete`) are deliberately **absent** → they still get
an exact rule, so Always never silently widens their blast radius. The sets live
next to `PRICES` in `app.py` as the documented extension point.

### 2. Harden the gate against prefix-riding on compound commands

In `would_prompt`, for `tool == "Bash"`: a `prefix:*` rule only counts as a
match when the command is **not** compound. Exact rules still match exactly.
So `Bash(git status:*)` suppresses the prompt for `git status -s` but **not**
for `git status; rm -rf ~` — the compound command stays gated.

This also makes `would_prompt` a closer mirror of Claude Code (which parses
compound commands and requires every segment to be allowed). Trade-off: a
compound command whose segments are each individually allow-listed (e.g.
`cat f | grep x` with both `cat:*` and `grep:*`) is now *held* rather than
passed. That's the safe direction — holding only surfaces it on the dashboard
for approval and still falls through to the terminal after the 120s timeout
(fail-open intact). Accepted.

## Fail-open / safety notes

- Nothing here can *block* an agent: worse case is an extra dashboard hold that
  times out to the normal terminal flow.
- Rules are written only to the **project** `.claude/settings.local.json` (the
  same file Claude Code uses), never global, and never when the file is present
  but unparseable (existing guard kept).
- Conservative by construction: unknown/dangerous commands fall back to exact
  rules — identical to today's v1 behavior.

## Tests (`tests/test_app.py`)

- `safe_bash_prefix`: `git status --short`→`git status`; `ls -la`→`ls`;
  `pytest tests/x.py`→`pytest`; `npm run build`→`npm run`;
  `git push origin`→`None`; `rm -rf /`→`None`; `git status; rm -rf ~`→`None`
  (compound); `cat a | grep b`→`None`.
- `add_always_rule`/`do_decide` on `git status --short` writes
  `Bash(git status:*)` and it then suppresses `git status -s`.
- Update `test_always_appends_rule_and_allows`: `npm test` → `Bash(npm test:*)`,
  and `npm test --coverage` is now also allowed.
- `would_prompt` hardening: with `Bash(git status:*)`, `git status -s`→False,
  `git status; rm -rf ~`→True (compound, still prompts).

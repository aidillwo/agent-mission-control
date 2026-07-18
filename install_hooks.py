#!/usr/bin/env python3
"""Wire Claude Code, Cursor, and Codex to Agent Mission Control.

Idempotent: safe to run twice. Backs up every file it touches to <file>.amc.bak
the first time. Run on each MacBook after cloning:

    python3 install_hooks.py            # install all
    python3 install_hooks.py --dry-run  # show what would change
"""
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
DRY = "--dry-run" in sys.argv
MARK = "agent-mission-control"

CC_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
             "Notification", "Stop", "SessionEnd"]
CURSOR_EVENTS = ["beforeSubmitPrompt", "beforeShellExecution", "afterFileEdit", "stop"]
KIRO_EVENTS = ["prompt", "shell", "fileEdit", "approval", "stop"]


def backup(path: Path):
    bak = path.with_suffix(path.suffix + ".amc.bak")
    if path.exists() and not bak.exists() and not DRY:
        shutil.copy2(path, bak)


def save(path: Path, text: str, label: str):
    if DRY:
        print(f"[dry-run] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"[ok] {label}: {path}")


def cc_command(event: str) -> str:
    if event == "PreToolUse":
        # PreToolUse runs the gate: it reports the tool_use AND, when the session
        # has gating enabled, blocks for an allow/deny decision from the dashboard.
        script = HERE / "hooks" / "cc_gate.py"
        return f'python3 "{script}"  # {MARK}'
    return ("curl -s -m 2 -X POST "
            f"http://localhost:7777/ingest/claude-code/{event} "
            "-H 'Content-Type: application/json' --data-binary @- "
            ">/dev/null 2>&1 || true  # " + MARK)


def install_claude_code():
    path = HOME / ".claude" / "settings.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except Exception:
            print(f"[warn] {path} is not valid JSON, skipping Claude Code. "
                  "Fix the file and rerun.")
            return
    backup(path)
    hooks = settings.setdefault("hooks", {})
    for event in CC_EVENTS:
        entries = hooks.setdefault(event, [])
        desired = cc_command(event)
        if any(desired in json.dumps(e) for e in entries):
            continue  # exact command already present
        # drop any stale AMC entry (e.g. the pre-gate PreToolUse curl) then add
        entries[:] = [e for e in entries if MARK not in json.dumps(e)]
        entries.append({"matcher": "*",
                        "hooks": [{"type": "command",
                                   "command": desired}]})
    save(path, json.dumps(settings, indent=2), "Claude Code hooks")


def install_cursor():
    path = HOME / ".cursor" / "hooks.json"
    cfg = {"version": 1, "hooks": {}}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except Exception:
            print(f"[warn] {path} is not valid JSON, skipping Cursor.")
            return
    backup(path)
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    script = HERE / "hooks" / "cursor_hook.sh"
    for event in CURSOR_EVENTS:
        entries = hooks.setdefault(event, [])
        cmd = f"{script} {event}"
        if not any(str(script) in json.dumps(e) for e in entries):
            entries.append({"command": cmd})
    save(path, json.dumps(cfg, indent=2), "Cursor hooks (beta)")
    print("     note: Cursor hooks are beta. If cards never appear from Cursor,"
          " check current event names in Cursor docs and edit hooks.json.")


def install_codex():
    path = HOME / ".codex" / "config.toml"
    script = HERE / "hooks" / "codex_notify.py"
    line = f'notify = ["python3", "{script}"]  # {MARK}'
    text = path.read_text() if path.exists() else ""
    if MARK in text:
        print(f"[ok] Codex notify already wired: {path}")
        return
    if "notify" in text and MARK not in text:
        print(f"[warn] {path} already defines notify. Merge manually:\n     {line}")
        return
    backup(path)
    save(path, text + ("\n" if text and not text.endswith("\n") else "") + line + "\n",
         "Codex notify")


def install_kiro():
    # Kiro (AWS agentic IDE, Code OSS based) configures Agent Hooks per-workspace
    # through its own UI (stored as .kiro/hooks/*.kiro.hook), so there is no single
    # global file to edit the way Cursor and Claude Code have. We detect Kiro and
    # print how to point a hook at the forwarder; the /ingest/kiro endpoint is
    # always live regardless.
    script = HERE / "hooks" / "kiro_hook.sh"
    present = (HOME / ".kiro").exists() or Path("/Applications/Kiro.app").exists()
    if present:
        print("[ok] Kiro detected (beta). Add an Agent Hook in Kiro that runs, "
              "for each agent event:")
    else:
        print("[skip] Kiro not detected. When installed, add an Agent Hook that "
              "runs, for each agent event:")
    print(f"       {script} <eventName>")
    print(f"     Suggested event names: {', '.join(KIRO_EVENTS)}. Or POST "
          "directly to http://localhost:7777/ingest/kiro/<event>.")


if __name__ == "__main__":
    print(f"Agent Mission Control installer (dry-run={DRY})\n")
    install_claude_code()
    install_cursor()
    install_codex()
    install_kiro()
    print("\nDone. Custom Python bots need no install: POST to "
          "http://localhost:7777/ingest (see README).")
    print("Uninstall: restore the .amc.bak files or delete entries containing "
          f"'{MARK}'.")

#!/usr/bin/env python3
"""Claude Code PreToolUse gate for Agent Deck.

Installed as the PreToolUse hook. Reads the hook JSON from stdin and asks the
dashboard whether this session is gated. If it is, the dashboard blocks until
you click Allow or Deny and returns the decision here.

Fail-open by design: for an ungated session, or if the dashboard is down, times
out, or answers unexpectedly, this prints nothing and exits 0 so Claude Code
follows its own normal permission flow. The only thing that ever stops a tool is
an explicit Deny click.
"""
import json
import sys
import urllib.request

# a little above the server's GATE_TIMEOUT_S, so the server is the one that
# decides "timed out" rather than the socket dying underneath it
TIMEOUT = 135


def emit(decision):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason":
            "Approved from Agent Deck" if decision == "allow"
            else "Denied from Agent Deck"}}))


def main():
    raw = sys.stdin.read()
    try:
        json.loads(raw)  # validate; we forward the original bytes as-is
    except Exception:
        return
    try:
        req = urllib.request.Request(
            "http://localhost:7777/gate/claude-code",
            data=raw.encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode())
    except Exception:
        return  # dashboard down / timeout -> native flow
    if resp.get("gate") and resp.get("decision") in ("allow", "deny"):
        emit(resp["decision"])
    # gate:false or decision "timeout" -> print nothing -> native flow


if __name__ == "__main__":
    main()

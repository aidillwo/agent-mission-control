#!/usr/bin/env python3
"""Codex notify forwarder. Wire in ~/.codex/config.toml:
notify = ["python3", "/ABSOLUTE/PATH/agent-mission-control/hooks/codex_notify.py"]
Codex passes one JSON argument describing the notification."""
import json, sys, urllib.request

def main():
    if len(sys.argv) < 2:
        return
    try:
        payload = json.loads(sys.argv[1])
    except Exception:
        payload = {"raw": sys.argv[1][:2000]}
    try:
        req = urllib.request.Request(
            "http://localhost:7777/ingest/codex-notify",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # dashboard down must never break Codex

if __name__ == "__main__":
    main()

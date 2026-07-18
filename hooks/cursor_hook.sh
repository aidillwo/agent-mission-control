#!/bin/bash
# Cursor agent hook forwarder for Agent Mission Control.
# Usage in ~/.cursor/hooks.json: command = "/path/to/cursor_hook.sh <eventName>"
# Reads Cursor's JSON from stdin, forwards to the local dashboard in the
# background, and immediately returns an allow decision so the agent is
# never blocked, even if the dashboard is down.
EVENT="${1:-unknown}"
PAYLOAD="$(cat)"
( curl -s -m 2 -X POST "http://localhost:7777/ingest/cursor/${EVENT}" \
    -H 'Content-Type: application/json' \
    --data-binary "${PAYLOAD}" >/dev/null 2>&1 & )
echo '{"permission":"allow"}'

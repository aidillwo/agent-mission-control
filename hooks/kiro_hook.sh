#!/bin/bash
# Kiro agent-hook forwarder for Agent Deck (beta).
# Point a Kiro Agent Hook at this script, passing the event name as $1:
#   /path/to/kiro_hook.sh <eventName>
# Reads Kiro's JSON from stdin, forwards it to the local dashboard in the
# background, and returns an allow decision so the agent is never blocked,
# even if the dashboard is down.
EVENT="${1:-unknown}"
PAYLOAD="$(cat)"
( curl -s -m 2 -X POST "http://localhost:7777/ingest/kiro/${EVENT}" \
    -H 'Content-Type: application/json' \
    --data-binary "${PAYLOAD}" >/dev/null 2>&1 & )
echo '{"permission":"allow"}'

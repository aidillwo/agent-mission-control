#!/bin/bash
# Cursor agent hook forwarder for Agent Deck.
# Usage in ~/.cursor/hooks.json: command = "/path/to/cursor_hook.sh <eventName>"
#
# For shell/MCP executions it asks the dashboard for an allow/deny decision
# (only actually blocks when that session has gating turned on); for every other
# event it forwards in the background and returns allow immediately. Fail-open:
# if the dashboard is down or the response is unexpected, the agent is allowed
# to proceed so it is never blocked by this hook.
EVENT="${1:-unknown}"
PAYLOAD="$(cat)"

case "$EVENT" in
  beforeShellExecution|beforeMCPExecution)
    RESP=$(curl -s -m 140 -X POST "http://localhost:7777/gate/cursor" \
      -H 'Content-Type: application/json' --data-binary "$PAYLOAD" 2>/dev/null)
    case "$RESP" in
      *'"decision":"deny"'*)  echo '{"permission":"deny"}';;
      *)                      echo '{"permission":"allow"}';;
    esac
    ;;
  *)
    ( curl -s -m 2 -X POST "http://localhost:7777/ingest/cursor/${EVENT}" \
        -H 'Content-Type: application/json' \
        --data-binary "$PAYLOAD" >/dev/null 2>&1 & )
    echo '{"permission":"allow"}'
    ;;
esac

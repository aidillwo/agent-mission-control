#!/usr/bin/env bash
set -euo pipefail

port="${1:-7777}"

if ! [[ "$port" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 [port]" >&2
  exit 2
fi

pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN || true)"
if [[ -z "$pids" ]]; then
  echo "No listener on port $port"
  exit 0
fi

echo "Stopping listener(s) on port $port: $pids"
kill $pids

sleep 1
remaining="$(lsof -tiTCP:"$port" -sTCP:LISTEN || true)"
if [[ -n "$remaining" ]]; then
  echo "Still running; forcing: $remaining"
  kill -9 $remaining
fi

echo "Port $port is free"

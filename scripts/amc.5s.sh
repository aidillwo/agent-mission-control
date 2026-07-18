#!/bin/bash
# SwiftBar / xbar plugin. Drop into your SwiftBar plugins folder.
# Shows "AMC 3●" in the menu bar (live count, ! prefix when something waits).
DATA=$(curl -s -m 2 http://localhost:7777/summary)
if [ -z "$DATA" ]; then echo "AMC ⏻"; echo "---"; echo "Dashboard offline"; exit 0; fi
python3 - "$DATA" << 'PY'
import json, sys
d = json.loads(sys.argv[1])
flag = "!" if d["waiting"] else ""
print(f"AMC {flag}{d['live']}\u25CF")
print("---")
for w in d["waiting"]:
    print(f"✋ {w['project'] or w['session_id']}: {(w['current_task'] or '')[:40]} | color=orange")
for s in d["sessions"]:
    print(f"{s['status']:>13}  {s['agent_type']}  {s['project'] or s['session_id']}")
print("Open dashboard | href=http://localhost:7777")
PY

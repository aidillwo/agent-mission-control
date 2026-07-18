#!/usr/bin/env python3
"""Demo agent: simulates two sessions so you can see the dashboard move.
Run while app.py is running:  python3 scripts/fake_agent.py"""
import json, random, time, urllib.request

def post(payload):
    req = urllib.request.Request("http://localhost:7777/ingest",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=3)

sid1 = f"demo-etl-{int(time.time())}"
sid2 = f"demo-report-{int(time.time())}"

post({"agent":"custom","session_id":sid1,"status":"working",
      "task":"Nightly gold price ETL to BigQuery","model":"claude-sonnet-4-6",
      "project":"gold-dashboard"})
post({"agent":"custom","session_id":sid2,"status":"working",
      "task":"Rendering StockFlow pitch report","model":"gpt-5-codex",
      "project":"stockflow"})
print("Two demo sessions started. Watch http://localhost:7777")

steps = ["query bq: gold_prices_daily","transform: dedupe + fx convert",
         "write: marts.gold_daily","validate: row counts","upload chart assets"]
for i in range(12):
    sid = random.choice([sid1, sid2])
    post({"agent":"custom","session_id":sid,"kind":"tool_use",
          "summary":random.choice(steps)})
    time.sleep(2)

post({"agent":"custom","session_id":sid2,"status":"waiting_input",
      "kind":"waiting_input","summary":"Approve write to production dataset?"})
print("demo-report is now WAITING FOR INPUT (orange card + notification)")
time.sleep(8)
post({"agent":"custom","session_id":sid2,"status":"completed",
      "summary":"Report rendered and saved"})
post({"agent":"custom","session_id":sid1,"status":"completed",
      "summary":"ETL finished, 1,204 rows loaded"})
print("Both demo sessions completed.")

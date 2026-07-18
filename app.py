"""Agent Deck - local AI agent session monitor.

Run: python app.py   (serves http://localhost:7777)

Intake paths:
  POST /ingest                      generic webhook for custom bots
  POST /ingest/claude-code/{event}  Claude Code hooks (raw stdin JSON forwarded)
  POST /ingest/cursor/{event}       Cursor agent hooks (raw stdin JSON forwarded)
  POST /ingest/codex-notify         Codex notify command payload
  (background) JSONL tailers for Claude Code and Codex session logs
  (background) psutil process scan safety net (if psutil installed)
"""

import asyncio
import datetime
import glob
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

PORT = 7777
HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "amc.db"
HOME = Path.home()

WORKING_S = 30          # activity within 30s = working
IDLE_S = 600            # 30s..10min = idle, beyond = ended
WAITING_TTL_S = 7200    # waiting_input stays visible this long before ended
GATE_TIMEOUT_S = 120    # how long a gated hook waits for an allow/deny click
EVENT_FEED_LIMIT = 300
EVENTS_RETAIN_DAYS = 14   # raw events older than this are rolled up then pruned
SESSIONS_RETAIN_DAYS = 30 # ended sessions / decisions older than this are pruned

# USD per MTok (input, output), substring-matched against the model name.
# Estimates only (cache discounts not modeled). Edit as prices change.
PRICES = [
    ("fable", (10, 50)),
    ("opus", (5, 25)),
    ("sonnet", (3, 15)),
    ("haiku", (1, 5)),
    ("gpt-5", (1.25, 10)),
]

def est_cost(model, tin, tout):
    """Estimated USD cost for a token count, or None for unknown models."""
    m = (model or "").lower()
    for key, (pin, pout) in PRICES:
        if key in m:
            return tin / 1e6 * pin + tout / 1e6 * pout
    return None

def local_day(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")

# ---------------------------------------------------------------- db

def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          session_id TEXT PRIMARY KEY,
          agent_type TEXT, model TEXT, project TEXT, cwd TEXT,
          status TEXT DEFAULT 'working',
          current_task TEXT,
          started_at REAL, last_seen_at REAL,
          gated INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT, ts REAL, kind TEXT, summary TEXT,
          detail TEXT, awaiting_decision_id TEXT
        );
        CREATE TABLE IF NOT EXISTS decisions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT, tool TEXT, summary TEXT, detail TEXT,
          status TEXT DEFAULT 'pending',
          created_at REAL, decided_at REAL
        );
        CREATE TABLE IF NOT EXISTS daily_usage(
          day TEXT, agent TEXT, model TEXT,
          tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
          PRIMARY KEY(day, agent, model)
        );
        CREATE TABLE IF NOT EXISTS daily_rollup(
          day TEXT, agent TEXT,
          events INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0,
          completed INTEGER DEFAULT 0,
          PRIMARY KEY(day, agent)
        );
        CREATE INDEX IF NOT EXISTS ev_sid ON events(session_id, ts);
        CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS dec_status ON decisions(status);
        """)
        # older DBs predate the gated column; add it if missing
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        if "gated" not in cols:
            c.execute("ALTER TABLE sessions ADD COLUMN gated INTEGER DEFAULT 0")

def upsert_session(sid, agent_type, *, model=None, project=None, cwd=None,
                   status=None, task=None, ts=None):
    ts = ts or time.time()
    if cwd and not project:
        project = Path(cwd).name or cwd
    with db() as c:
        row = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO sessions(session_id,agent_type,model,project,cwd,"
                "status,current_task,started_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, agent_type, model, project, cwd, status or "working",
                 task, ts, ts))
        else:
            c.execute(
                "UPDATE sessions SET "
                "model=COALESCE(?,model), project=COALESCE(?,project),"
                "cwd=COALESCE(?,cwd), status=COALESCE(?,status),"
                "current_task=COALESCE(?,current_task),"
                "last_seen_at=MAX(last_seen_at,?) WHERE session_id=?",
                (model, project, cwd, status, task, ts, sid))

def add_event(sid, kind, summary, detail=None, ts=None, decision_id=None):
    ts = ts or time.time()
    with db() as c:
        c.execute(
            "INSERT INTO events(session_id,ts,kind,summary,detail,awaiting_decision_id)"
            " VALUES(?,?,?,?,?,?)",
            (sid, ts, kind, (summary or "")[:300],
             json.dumps(detail)[:4000] if detail else None, decision_id))

def add_usage(agent, model, tin, tout):
    if not (tin or tout):
        return
    with db() as c:
        c.execute(
            "INSERT INTO daily_usage(day,agent,model,tokens_in,tokens_out) "
            "VALUES(?,?,?,?,?) ON CONFLICT(day,agent,model) DO UPDATE SET "
            "tokens_in=tokens_in+excluded.tokens_in, "
            "tokens_out=tokens_out+excluded.tokens_out",
            (local_day(), agent, model or "unknown", int(tin or 0), int(tout or 0)))

def derive_statuses():
    """Recompute working/idle/ended from last_seen. waiting_input and error are
    sticky until new activity arrives; ended is terminal for the session id.
    A silent waiting_input session is still blocked on the user, so it only
    expires after WAITING_TTL_S rather than the normal IDLE_S window."""
    now = time.time()
    changed = False
    with db() as c:
        for r in c.execute("SELECT session_id,status,last_seen_at FROM sessions "
                           "WHERE status != 'ended'").fetchall():
            age = now - r["last_seen_at"]
            new = r["status"]
            if r["status"] in ("waiting_input", "error"):
                if age > WAITING_TTL_S:
                    new = "ended"
            elif age > IDLE_S:
                new = "ended"
            elif age > WORKING_S:
                new = "idle"
            else:
                new = "working"
            if new != r["status"]:
                c.execute("UPDATE sessions SET status=? WHERE session_id=?",
                          (new, r["session_id"]))
                changed = True
    return changed

def state_payload():
    with db() as c:
        sessions = [dict(r) for r in c.execute(
            "SELECT * FROM sessions ORDER BY last_seen_at DESC LIMIT 200")]
        events = [dict(r) for r in c.execute(
            "SELECT id,session_id,ts,kind,summary FROM events "
            "ORDER BY ts DESC LIMIT ?", (EVENT_FEED_LIMIT,))]
        decisions = [dict(r) for r in c.execute(
            "SELECT id,session_id,tool,summary,created_at FROM decisions "
            "WHERE status='pending' ORDER BY created_at")]
        day_start = datetime.datetime.combine(
            datetime.date.today(), datetime.time.min).timestamp()
        today = c.execute(
            "SELECT COUNT(*) n, SUM(kind='tool_use') tools, "
            "SUM(kind='completed') done FROM events WHERE ts>=?",
            (day_start,)).fetchone()
        usage_rows = c.execute(
            "SELECT model, SUM(tokens_in) i, SUM(tokens_out) o FROM daily_usage "
            "WHERE day=? GROUP BY model", (local_day(),)).fetchall()
    tin = sum(r["i"] or 0 for r in usage_rows)
    tout = sum(r["o"] or 0 for r in usage_rows)
    costs = [est_cost(r["model"], r["i"] or 0, r["o"] or 0) for r in usage_rows]
    known = [x for x in costs if x is not None]
    return {"type": "state", "now": time.time(),
            "sessions": sessions, "events": events, "decisions": decisions,
            "today": {"events": today["n"] or 0,
                      "tool_calls": today["tools"] or 0,
                      "completed": today["done"] or 0},
            "usage_today": {"tokens_in": tin, "tokens_out": tout,
                            "est_cost": round(sum(known), 4) if known else None}}

# ---------------------------------------------------------------- ws hub

class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
    async def join(self, ws):
        await ws.accept()
        self.clients.add(ws)
    def leave(self, ws):
        self.clients.discard(ws)
    async def broadcast(self):
        if not self.clients:
            return
        msg = json.dumps(state_payload())
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(ws)

hub = Hub()

# decision id -> asyncio.Event, used to wake a gated hook when the user decides
pending: dict[int, "asyncio.Event"] = {}

def txt(x, limit=200):
    if x is None:
        return ""
    if isinstance(x, str):
        return x[:limit]
    return json.dumps(x)[:limit]

# ---------------------------------------------------------------- normalizers

async def handle_generic(p):
    sid = str(p.get("session_id") or f"{p.get('agent','bot')}-{int(time.time())}")
    agent = p.get("agent") or p.get("agent_type") or "custom"
    status = p.get("status")
    if status not in (None, "working", "waiting_input", "idle", "ended",
                     "error", "completed"):
        status = "working"
    kind = p.get("kind") or ("status_change" if status else "ping")
    if status == "completed":
        status, kind = "ended", "completed"
    upsert_session(sid, agent, model=p.get("model"), project=p.get("project"),
                   cwd=p.get("cwd"), status=status, task=p.get("task"))
    add_event(sid, kind, p.get("summary") or p.get("task") or status or "ping",
              detail=p, decision_id=p.get("awaiting_decision_id"))
    add_usage(agent, p.get("model"), p.get("tokens_in"), p.get("tokens_out"))
    await hub.broadcast()

async def handle_claude_code(event, p):
    sid = str(p.get("session_id") or "claude-code")
    cwd = p.get("cwd")
    common = dict(cwd=cwd)
    if event == "SessionStart":
        upsert_session(sid, "claude-code", status="working", **common)
        add_event(sid, "status_change", "Session started", p)
    elif event == "UserPromptSubmit":
        task = txt(p.get("prompt"), 160)
        upsert_session(sid, "claude-code", status="working", task=task, **common)
        add_event(sid, "prompt", task, p)
    elif event == "PreToolUse":
        tool = p.get("tool_name", "tool")
        ti = p.get("tool_input") or {}
        target = ti.get("file_path") or ti.get("command") or ti.get("pattern") or ""
        upsert_session(sid, "claude-code", status="working", **common)
        add_event(sid, "tool_use", f"{tool} {txt(target,120)}".strip(), p)
    elif event == "PostToolUse":
        upsert_session(sid, "claude-code", status="working", **common)
    elif event == "Notification":
        msg = txt(p.get("message"), 160) or "Needs your attention"
        upsert_session(sid, "claude-code", status="waiting_input", **common)
        add_event(sid, "waiting_input", msg, p)
    elif event == "Stop":
        # Stop fires at the end of every turn, not when the session closes.
        upsert_session(sid, "claude-code", status="idle", **common)
        add_event(sid, "completed", "Turn finished", p)
    elif event == "SessionEnd":
        upsert_session(sid, "claude-code", status="ended", **common)
        add_event(sid, "completed", "Session ended", p)
    else:
        upsert_session(sid, "claude-code", **common)
        add_event(sid, "status_change", event, p)
    await hub.broadcast()

async def handle_cursor(event, p):
    sid = str(p.get("conversation_id") or p.get("session_id")
              or p.get("generation_id") or "cursor")
    roots = p.get("workspace_roots") or []
    cwd = p.get("cwd") or (roots[0] if roots else None)
    if event in ("beforeSubmitPrompt",):
        task = txt(p.get("prompt") or p.get("text"), 160)
        upsert_session(sid, "cursor", cwd=cwd, status="working",
                       task=task or None)
        add_event(sid, "prompt", task or "Prompt submitted", p)
    elif event in ("beforeShellExecution", "beforeMCPExecution"):
        cmd = txt(p.get("command") or p.get("tool_name"), 140)
        upsert_session(sid, "cursor", cwd=cwd, status="working")
        add_event(sid, "tool_use", cmd or event, p)
    elif event == "afterFileEdit":
        f = txt(p.get("file_path"), 140)
        upsert_session(sid, "cursor", cwd=cwd, status="working")
        add_event(sid, "tool_use", f"edit {f}", p)
    elif event == "stop":
        upsert_session(sid, "cursor", cwd=cwd, status="ended")
        add_event(sid, "completed", "Agent finished", p)
    else:
        upsert_session(sid, "cursor", cwd=cwd, status="working")
        add_event(sid, "status_change", event, p)
    await hub.broadcast()

async def handle_kiro(event, p):
    """Kiro (AWS agentic IDE, Code OSS based). Wired blind against Kiro's
    documented agent-hook shape, so field names are matched defensively and
    this adapter is beta until validated against a live Kiro install."""
    sid = str(p.get("session_id") or p.get("conversation_id")
              or p.get("chat_id") or "kiro")
    roots = p.get("workspace_roots") or p.get("workspaceFolders") or []
    cwd = p.get("cwd") or p.get("workspace") or (roots[0] if roots else None)
    ev = event.lower()
    if "approval" in ev or "permission" in ev or "confirm" in ev:
        msg = txt(p.get("message") or p.get("command"), 160)
        upsert_session(sid, "kiro", cwd=cwd, status="waiting_input")
        add_event(sid, "waiting_input", msg or "Kiro needs your approval", p)
    elif "prompt" in ev or "submit" in ev or "userinput" in ev:
        task = txt(p.get("prompt") or p.get("text") or p.get("message"), 160)
        upsert_session(sid, "kiro", cwd=cwd, status="working", task=task or None)
        add_event(sid, "prompt", task or "Prompt submitted", p)
    elif "edit" in ev or "file" in ev or "write" in ev:
        f = txt(p.get("file_path") or p.get("path"), 140)
        upsert_session(sid, "kiro", cwd=cwd, status="working")
        add_event(sid, "tool_use", f"edit {f}".strip(), p)
    elif ("shell" in ev or "command" in ev or "exec" in ev or "tool" in ev
          or "mcp" in ev):
        cmd = txt(p.get("command") or p.get("tool_name") or p.get("name"), 140)
        upsert_session(sid, "kiro", cwd=cwd, status="working")
        add_event(sid, "tool_use", cmd or event, p)
    elif "stop" in ev or "complete" in ev or "finish" in ev or "end" in ev:
        # treat as turn end, not session end (reaper ages it out if truly idle)
        upsert_session(sid, "kiro", cwd=cwd, status="idle")
        add_event(sid, "completed", "Agent finished", p)
    else:
        upsert_session(sid, "kiro", cwd=cwd, status="working")
        add_event(sid, "status_change", event, p)
    await hub.broadcast()

async def handle_codex_notify(p):
    ntype = p.get("type", "")
    # attach to the most recent codex session if the payload has no id
    sid = str(p.get("turn-id") or p.get("session_id") or "")
    if not sid:
        with db() as c:
            r = c.execute("SELECT session_id FROM sessions WHERE agent_type='codex' "
                          "ORDER BY last_seen_at DESC LIMIT 1").fetchone()
        sid = r["session_id"] if r else "codex"
    if "approval" in ntype or "permission" in ntype:
        upsert_session(sid, "codex", status="waiting_input")
        add_event(sid, "waiting_input", txt(p.get("message"), 160)
                  or "Codex is asking for approval", p)
    elif "complete" in ntype:
        upsert_session(sid, "codex", status="idle")
        add_event(sid, "completed",
                  txt(p.get("last-assistant-message"), 160) or "Turn complete", p)
    else:
        upsert_session(sid, "codex")
        add_event(sid, "status_change", ntype or "notify", p)
    await hub.broadcast()

# ---------------------------------------------------------------- history

def history_days(days=30):
    """Per-day digest merging live event counts (recent) with daily_rollup
    (pruned days) and daily_usage tokens/cost."""
    out = {}
    with db() as c:
        for r in c.execute(
                "SELECT date(e.ts,'unixepoch','localtime') d, s.agent_type agent, "
                "COUNT(*) n, SUM(e.kind='tool_use') tools, "
                "SUM(e.kind='completed') done FROM events e "
                "LEFT JOIN sessions s ON s.session_id=e.session_id "
                "GROUP BY d, agent"):
            day = out.setdefault(r["d"], {"events": 0, "tool_calls": 0,
                                          "completed": 0, "agents": set()})
            day["events"] += r["n"] or 0
            day["tool_calls"] += r["tools"] or 0
            day["completed"] += r["done"] or 0
            if r["agent"]:
                day["agents"].add(r["agent"])
        for r in c.execute("SELECT * FROM daily_rollup"):
            day = out.setdefault(r["day"], {"events": 0, "tool_calls": 0,
                                            "completed": 0, "agents": set()})
            day["events"] += r["events"] or 0
            day["tool_calls"] += r["tool_calls"] or 0
            day["completed"] += r["completed"] or 0
            day["agents"].add(r["agent"])
        usage = {}
        for r in c.execute("SELECT day, model, SUM(tokens_in) i, "
                           "SUM(tokens_out) o FROM daily_usage GROUP BY day, model"):
            u = usage.setdefault(r["day"], {"in": 0, "out": 0, "costs": []})
            u["in"] += r["i"] or 0
            u["out"] += r["o"] or 0
            cost = est_cost(r["model"], r["i"] or 0, r["o"] or 0)
            if cost is not None:
                u["costs"].append(cost)
    cutoff = local_day(time.time() - days * 86400)
    result = []
    for day in sorted(set(out) | set(usage), reverse=True):
        if day < cutoff:
            continue
        d = out.get(day, {"events": 0, "tool_calls": 0, "completed": 0,
                          "agents": set()})
        u = usage.get(day, {"in": 0, "out": 0, "costs": []})
        result.append({
            "day": day, "events": d["events"], "tool_calls": d["tool_calls"],
            "completed": d["completed"], "agents": sorted(d["agents"]),
            "tokens_in": u["in"], "tokens_out": u["out"],
            "est_cost": round(sum(u["costs"]), 4) if u["costs"] else None})
    return result

def rollup_and_prune():
    """Aggregate events older than EVENTS_RETAIN_DAYS into daily_rollup, then
    delete them; prune old decisions and long-ended sessions."""
    ev_cutoff = time.time() - EVENTS_RETAIN_DAYS * 86400
    sess_cutoff = time.time() - SESSIONS_RETAIN_DAYS * 86400
    with db() as c:
        for r in c.execute(
                "SELECT date(e.ts,'unixepoch','localtime') d, "
                "COALESCE(s.agent_type,'unknown') agent, COUNT(*) n, "
                "SUM(e.kind='tool_use') tools, SUM(e.kind='completed') done "
                "FROM events e LEFT JOIN sessions s ON s.session_id=e.session_id "
                "WHERE e.ts < ? GROUP BY d, agent", (ev_cutoff,)).fetchall():
            c.execute(
                "INSERT INTO daily_rollup(day,agent,events,tool_calls,completed) "
                "VALUES(?,?,?,?,?) ON CONFLICT(day,agent) DO UPDATE SET "
                "events=events+excluded.events, "
                "tool_calls=tool_calls+excluded.tool_calls, "
                "completed=completed+excluded.completed",
                (r["d"], r["agent"], r["n"] or 0, r["tools"] or 0, r["done"] or 0))
        c.execute("DELETE FROM events WHERE ts < ?", (ev_cutoff,))
        c.execute("DELETE FROM decisions WHERE created_at < ?", (sess_cutoff,))
        c.execute("DELETE FROM sessions WHERE status='ended' AND "
                  "last_seen_at < ?", (sess_cutoff,))

# ---------------------------------------------------------------- gating

def gate_sid(agent, p):
    if agent == "cursor":
        return str(p.get("conversation_id") or p.get("session_id")
                   or p.get("generation_id") or "cursor")
    return str(p.get("session_id") or agent)

def describe_action(agent, p):
    """Human summary of the action a hook is asking permission for."""
    if agent == "claude-code":
        tool = p.get("tool_name", "tool")
        ti = p.get("tool_input") or {}
        target = ti.get("file_path") or ti.get("command") or ti.get("pattern") or ""
        return tool, f"{tool} {txt(target, 160)}".strip()
    if agent == "cursor":
        cmd = p.get("command") or p.get("tool_name") or ""
        return "command", txt(cmd, 160) or "Cursor action"
    return "action", "Pending action"

def is_gated(sid):
    with db() as c:
        row = c.execute("SELECT gated FROM sessions WHERE session_id=?",
                        (sid,)).fetchone()
    return bool(row and row["gated"])

async def do_gate(agent, p):
    """Block a gated hook until the user clicks Allow/Deny, or time out.
    Returns one of: {gate:False} | {gate:True, decision:allow|deny|timeout}.
    Always records the action as a tool_use event so the feed stays live even
    for ungated sessions (this hook replaces the plain PreToolUse reporter)."""
    sid = gate_sid(agent, p)
    tool, summary = describe_action(agent, p)
    cwd = p.get("cwd") or (p.get("workspace_roots") or [None])[0]
    if not is_gated(sid):
        upsert_session(sid, agent, cwd=cwd, status="working")
        add_event(sid, "tool_use", summary)
        await hub.broadcast()
        return {"gate": False}
    with db() as c:
        cur = c.execute(
            "INSERT INTO decisions(session_id,tool,summary,detail,status,created_at)"
            " VALUES(?,?,?,?, 'pending', ?)",
            (sid, tool, summary, json.dumps(p)[:4000], time.time()))
        did = cur.lastrowid
    upsert_session(sid, agent, cwd=cwd, status="waiting_input")
    add_event(sid, "waiting_input", summary or "Awaiting your decision",
              decision_id=str(did))
    ev = asyncio.Event()
    pending[did] = ev
    await hub.broadcast()
    try:
        await asyncio.wait_for(ev.wait(), GATE_TIMEOUT_S)
    except asyncio.TimeoutError:
        with db() as c:
            c.execute("UPDATE decisions SET status='expired', decided_at=? "
                      "WHERE id=? AND status='pending'", (time.time(), did))
        pending.pop(did, None)
        with db() as c:
            c.execute("UPDATE sessions SET status='working' WHERE session_id=?",
                      (sid,))
        add_event(sid, "status_change", "Decision timed out, agent proceeded")
        await hub.broadcast()
        return {"gate": True, "decision": "timeout"}
    pending.pop(did, None)
    with db() as c:
        r = c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()
    return {"gate": True,
            "decision": "allow" if r and r["status"] == "allowed" else "deny"}

async def do_decide(did, action):
    """Resolve a pending decision. Returns (body, http_status)."""
    if action not in ("allow", "deny"):
        return {"ok": False, "reason": "bad_action"}, 400
    with db() as c:
        row = c.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}, 404
        if row["status"] != "pending":
            return {"ok": False, "reason": "not_pending"}, 200
        new_status = "allowed" if action == "allow" else "denied"
        c.execute("UPDATE decisions SET status=?, decided_at=? WHERE id=?",
                  (new_status, time.time(), did))
    sid = row["session_id"]
    verb = "Allowed" if action == "allow" else "Denied"
    add_event(sid, "status_change",
              f"{verb} from dashboard: {row['summary'] or row['tool']}")
    with db() as c:
        c.execute("UPDATE sessions SET status='working' WHERE session_id=?", (sid,))
    ev = pending.get(did)
    if ev:
        ev.set()
    await hub.broadcast()
    return {"ok": True, "decision": new_status}, 200

async def do_set_gate(sid, on):
    on = 1 if on else 0
    with db() as c:
        c.execute("UPDATE sessions SET gated=? WHERE session_id=?", (on, sid))
    add_event(sid, "status_change", f"Gating {'enabled' if on else 'disabled'}")
    await hub.broadcast()
    return {"ok": True, "gated": bool(on)}

# ---------------------------------------------------------------- tailers

class Tailer:
    """Polls a glob pattern, follows appended JSONL lines per file."""
    def __init__(self, pattern, on_line, max_age_days=2):
        self.pattern = pattern
        self.on_line = on_line
        self.offsets = {}
        self.max_age = max_age_days * 86400
        self.first_pass = True
        self.dirty = False   # set when scan() ingests new lines

    async def run(self):
        while True:
            self.dirty = False
            try:
                self.scan()
            except Exception:
                pass
            self.first_pass = False
            if self.dirty:
                # tailer-sourced sessions (real Claude Code / Codex) must push
                # live too, not just the webhook/hook paths.
                await hub.broadcast()
            await asyncio.sleep(3)

    def scan(self):
        cutoff = time.time() - self.max_age
        for path in glob.glob(self.pattern, recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            pos = self.offsets.get(path)
            if pos is None:
                # do not replay history on startup, start from the end
                self.offsets[path] = st.st_size if self.first_pass else 0
                if self.first_pass:
                    continue
                pos = 0
            if st.st_size < pos:
                pos = self.offsets[path] = 0  # file truncated/rotated, restart
            if st.st_size <= pos:
                continue
            with open(path, "r", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.on_line(path, json.loads(line))
                        self.dirty = True
                    except Exception:
                        continue
                self.offsets[path] = f.tell()

def claude_line(path, obj):
    sid = obj.get("sessionId") or Path(path).stem
    cwd = obj.get("cwd")
    msg = obj.get("message") or {}
    t = obj.get("type")
    if t == "user":
        content = msg.get("content")
        text = content if isinstance(content, str) else next(
            (b.get("text") for b in content or [] if isinstance(b, dict)
             and b.get("type") == "text"), None)
        if text and not str(text).startswith("<"):
            upsert_session(sid, "claude-code", cwd=cwd, status="working",
                           task=txt(text, 160))
            add_event(sid, "prompt", txt(text, 200))
    elif t == "assistant":
        model = msg.get("model")
        upsert_session(sid, "claude-code", cwd=cwd, model=model, status="working")
        # usage: streaming writes several lines per message with the same
        # usage repeated — count each message id once
        usage = msg.get("usage") or {}
        mid = msg.get("id")
        if usage and mid and claude_line.last_usage_id.get(path) != mid:
            claude_line.last_usage_id[path] = mid
            add_usage("claude-code", model,
                      usage.get("input_tokens"), usage.get("output_tokens"))
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                ti = b.get("input") or {}
                target = ti.get("file_path") or ti.get("command") or ""
                add_event(sid, "tool_use",
                          f"{b.get('name','tool')} {txt(target,120)}".strip())
claude_line.last_usage_id = {}

def codex_line(path, obj):
    payload = obj.get("payload") or {}
    t = obj.get("type")
    sid = None
    if t == "session_meta":
        sid = payload.get("id") or Path(path).stem
        upsert_session(sid, "codex", cwd=payload.get("cwd"), status="working")
        add_event(sid, "status_change", "Codex session started")
        codex_line.last_sid[path] = sid
        return
    sid = codex_line.last_sid.get(path) or Path(path).stem
    if t == "turn_context":
        upsert_session(sid, "codex", model=payload.get("model"),
                       cwd=payload.get("cwd"), status="working")
    elif t == "response_item":
        pt = payload.get("type")
        if pt == "message" and payload.get("role") == "user":
            content = payload.get("content") or []
            text = next((b.get("text") for b in content if isinstance(b, dict)
                         and "text" in b), None)
            if text:
                upsert_session(sid, "codex", status="working", task=txt(text, 160))
                add_event(sid, "prompt", txt(text, 200))
        elif pt in ("function_call", "local_shell_call", "custom_tool_call"):
            name = payload.get("name") or pt
            upsert_session(sid, "codex", status="working")
            add_event(sid, "tool_use", txt(name, 140))
    elif t == "event_msg" and (payload.get("type") == "token_count"):
        # cumulative totals per session; store the delta since last seen (beta,
        # wired defensively against Codex's documented rollout shape)
        info = payload.get("info") or {}
        tot = info.get("total_token_usage") or info
        tin, tout = tot.get("input_tokens"), tot.get("output_tokens")
        if isinstance(tin, (int, float)) or isinstance(tout, (int, float)):
            prev = codex_line.last_usage.get(path, (0, 0))
            d_in = max(0, int(tin or 0) - prev[0])
            d_out = max(0, int(tout or 0) - prev[1])
            codex_line.last_usage[path] = (int(tin or 0), int(tout or 0))
            add_usage("codex", info.get("model") or "codex", d_in, d_out)
codex_line.last_sid = {}
codex_line.last_usage = {}

async def process_scan():
    try:
        import psutil
    except ImportError:
        return
    KNOWN = {"claude": "claude-code", "codex": "codex",
             "cursor": "cursor", "Cursor": "cursor",
             "kiro": "kiro", "Kiro": "kiro"}
    while True:
        try:
            seen = set()
            for proc in psutil.process_iter(["name", "cmdline"]):
                name = (proc.info["name"] or "")
                for key, agent in KNOWN.items():
                    if key in name and agent not in seen:
                        seen.add(agent)
                        with db() as c:
                            live = c.execute(
                                "SELECT 1 FROM sessions WHERE agent_type=? AND "
                                "status IN ('working','idle','waiting_input') "
                                "LIMIT 1", (agent,)).fetchone()
                        if not live:
                            upsert_session(f"{agent}-proc", agent, status="idle",
                                           task="Process detected (no log data yet)")
        except Exception:
            pass
        await asyncio.sleep(15)

# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app):
    init_db()
    tasks = [
        asyncio.create_task(Tailer(
            str(HOME / ".claude/projects/*/*.jsonl"), claude_line).run()),
        asyncio.create_task(Tailer(
            str(HOME / ".codex/sessions/**/rollout-*.jsonl"), codex_line).run()),
        asyncio.create_task(process_scan()),
        asyncio.create_task(reaper()),
    ]
    yield
    for t in tasks:
        t.cancel()

async def reaper():
    last_prune_day = None
    while True:
        if derive_statuses():
            await hub.broadcast()
        today = local_day()
        if today != last_prune_day:
            last_prune_day = today
            try:
                rollup_and_prune()
            except Exception:
                pass
        await asyncio.sleep(5)

app = FastAPI(lifespan=lifespan)

@app.get("/")
def index():
    # no-cache so a plain reload always picks up dashboard upgrades
    return FileResponse(HERE / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})

@app.post("/ingest")
async def ingest(req: Request):
    await handle_generic(await safe_json(req))
    return {"ok": True}

@app.post("/ingest/claude-code/{event}")
async def ingest_cc(event: str, req: Request):
    await handle_claude_code(event, await safe_json(req))
    return {"ok": True}

@app.post("/ingest/cursor/{event}")
async def ingest_cur(event: str, req: Request):
    await handle_cursor(event, await safe_json(req))
    return {"ok": True}

@app.post("/ingest/kiro/{event}")
async def ingest_kiro(event: str, req: Request):
    await handle_kiro(event, await safe_json(req))
    return {"ok": True}

@app.post("/ingest/codex-notify")
async def ingest_codex(req: Request):
    await handle_codex_notify(await safe_json(req))
    return {"ok": True}

@app.post("/gate/{agent}")
async def gate(agent: str, req: Request):
    return await do_gate(agent, await safe_json(req))

@app.post("/api/decision/{did}")
async def decision(did: int, req: Request):
    p = await safe_json(req)
    body, code = await do_decide(did, p.get("action"))
    return JSONResponse(body, status_code=code)

@app.post("/api/session/{sid}/gate")
async def session_gate(sid: str, req: Request):
    p = await safe_json(req)
    return await do_set_gate(sid, p.get("on"))

async def safe_json(req):
    try:
        return await req.json()
    except Exception:
        body = (await req.body()).decode(errors="replace")
        return {"raw": body[:2000]}

@app.get("/api/state")
def api_state():
    derive_statuses()
    return state_payload()

@app.get("/api/session/{sid}/events")
def api_session_events(sid: str):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ts,kind,summary,detail FROM events WHERE session_id=? "
            "ORDER BY ts DESC LIMIT 200", (sid,))]
    return {"session_id": sid, "events": rows}

@app.get("/api/history")
def api_history(days: int = 30):
    return {"days": history_days(min(max(days, 1), 365))}

@app.get("/api/daily")
def api_daily():
    since = time.time() - 7 * 86400
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT date(ts,'unixepoch','localtime') d, "
            "s.agent_type agent, COUNT(*) n "
            "FROM events e JOIN sessions s ON s.session_id=e.session_id "
            "WHERE ts>=? GROUP BY d, agent ORDER BY d", (since,))]
    return {"days": rows}

@app.get("/summary")
def summary():
    derive_statuses()
    with db() as c:
        live = c.execute("SELECT COUNT(*) n FROM sessions WHERE status IN "
                         "('working','waiting_input')").fetchone()["n"]
        waiting = [dict(r) for r in c.execute(
            "SELECT session_id,agent_type,project,current_task FROM sessions "
            "WHERE status='waiting_input'")]
        top = [dict(r) for r in c.execute(
            "SELECT session_id,agent_type,project,status,current_task "
            "FROM sessions WHERE status!='ended' "
            "ORDER BY last_seen_at DESC LIMIT 5")]
    return {"live": live, "waiting": waiting, "sessions": top}

@app.websocket("/ws")
async def ws(sock: WebSocket):
    await hub.join(sock)
    try:
        await sock.send_text(json.dumps(state_payload()))
        while True:
            await sock.receive_text()
    except WebSocketDisconnect:
        hub.leave(sock)
    except Exception:
        hub.leave(sock)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

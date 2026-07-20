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
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
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

# Model -> vendor, substring-matched. Separate from PRICES: identifying who
# made a model is a different concern from what it costs (a model can be
# identifiable without a known price, or vice versa).
PROVIDERS = [
    ("fable", "Anthropic"), ("opus", "Anthropic"), ("sonnet", "Anthropic"),
    ("haiku", "Anthropic"), ("claude", "Anthropic"),
    ("gpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"), ("codex", "OpenAI"),
]

def provider_of(model):
    m = (model or "").lower()
    for key, name in PROVIDERS:
        if key in m:
            return name
    return "Other"

def token_breakdown(days=30):
    """Total tokens/cost over the window, grouped by agent and by provider
    (derived from model). Each returned list is sorted by total tokens desc."""
    cutoff = local_day(time.time() - days * 86400)
    by_agent, by_provider = {}, {}
    with db() as c:
        for r in c.execute(
                "SELECT agent, model, SUM(tokens_in) i, SUM(tokens_out) o "
                "FROM daily_usage WHERE day>=? GROUP BY agent, model", (cutoff,)):
            tin, tout = r["i"] or 0, r["o"] or 0
            cost = est_cost(r["model"], tin, tout)
            a = by_agent.setdefault(r["agent"] or "unknown",
                                    {"tokens_in": 0, "tokens_out": 0, "costs": []})
            a["tokens_in"] += tin; a["tokens_out"] += tout
            if cost is not None:
                a["costs"].append(cost)
            p = by_provider.setdefault(provider_of(r["model"]),
                                       {"tokens_in": 0, "tokens_out": 0, "costs": []})
            p["tokens_in"] += tin; p["tokens_out"] += tout
            if cost is not None:
                p["costs"].append(cost)
    def finalize(grouped, key_name):
        rows = [{key_name: k, "tokens_in": v["tokens_in"], "tokens_out": v["tokens_out"],
                 "est_cost": round(sum(v["costs"]), 4) if v["costs"] else None}
                for k, v in grouped.items()]
        rows.sort(key=lambda r: r["tokens_in"] + r["tokens_out"], reverse=True)
        return rows
    return finalize(by_agent, "agent"), finalize(by_provider, "provider")

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
          gated INTEGER DEFAULT 0,
          gate_mode TEXT DEFAULT ''
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
        # older DBs predate these columns; add and migrate as needed
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        if "gated" not in cols:
            c.execute("ALTER TABLE sessions ADD COLUMN gated INTEGER DEFAULT 0")
        if "gate_mode" not in cols:
            c.execute("ALTER TABLE sessions ADD COLUMN gate_mode TEXT DEFAULT ''")
            # sessions gated under the old boolean keep strict gating
            c.execute("UPDATE sessions SET gate_mode='all' WHERE gated=1")

def upsert_session(sid, agent_type, *, model=None, project=None, cwd=None,
                   status=None, task=None, ts=None, soft=False):
    """soft=True marks a passive observation (log tailers): it must never
    clear waiting_input, because a transcript line is not evidence that the
    block was answered. Hook-driven events (PostToolUse etc.) stay hard."""
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
            hard_status = None if (soft and row["status"] == "waiting_input") else status
            c.execute(
                "UPDATE sessions SET "
                "model=COALESCE(?,model), project=COALESCE(?,project),"
                "cwd=COALESCE(?,cwd), status=COALESCE(?,status),"
                "current_task=COALESCE(?,current_task),"
                "last_seen_at=MAX(last_seen_at,?) WHERE session_id=?",
                (model, project, cwd, hard_status, task, ts, sid))

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
    for s in sessions:  # resolve per-agent default so the UI shows the real mode
        if s.get("gate_mode") not in GATE_MODES:
            s["gate_mode"] = ("auto" if s["agent_type"] == "claude-code" else "off")
    with db() as c:
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
        m = re.search(r"permission to use (\S+)", msg)
        task = f"Needs approval: {m.group(1)}" if m else None
        upsert_session(sid, "claude-code", status="waiting_input", task=task,
                       **common)
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
        if is_claude_question_tool(tool):
            return tool, claude_question_summary(ti)
        target = ti.get("file_path") or ti.get("command") or ti.get("pattern") or ""
        return tool, f"{tool} {txt(target, 160)}".strip()
    if agent == "cursor":
        cmd = p.get("command") or p.get("tool_name") or ""
        return "command", txt(cmd, 160) or "Cursor action"
    return "action", "Pending action"

GATE_MODES = ("off", "auto", "all")

CLAUDE_QUESTION_TOOLS = {"AskUserQuestion"}

def is_claude_question_tool(tool):
    return tool in CLAUDE_QUESTION_TOOLS

def claude_question_summary(tool_input):
    ti = tool_input or {}
    q = (ti.get("question") or ti.get("prompt") or ti.get("message")
         or ti.get("description") or "")
    if isinstance(q, (list, dict)):
        q = txt(q, 140)
    q = txt(str(q).strip(), 140)
    return f"Claude asks: {q}" if q else "Claude needs your yes/no decision"

def effective_gate_mode(sid, agent):
    """auto is the default for claude-code (mirror its own approvals); other
    agents default to off because we can't evaluate their permission rules."""
    with db() as c:
        row = c.execute("SELECT gate_mode FROM sessions WHERE session_id=?",
                        (sid,)).fetchone()
    mode = row["gate_mode"] if row else None
    if mode in GATE_MODES:
        return mode
    return "auto" if agent == "claude-code" else "off"

# ---- would-prompt evaluation (approximate mirror of Claude Code's rules) ----

SAFE_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "NotebookRead",
              "Task", "Skill", "TaskCreate", "TaskUpdate", "TaskList"}
PROMPTY_TOOLS = {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
                 "WebFetch"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_rules_cache: dict[str, tuple[float, list]] = {}

def _read_allow_rules(path):
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _rules_cache.get(path)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    try:
        rules = (json.loads(Path(path).read_text())
                 .get("permissions", {}).get("allow", []) or [])
        rules = [r for r in rules if isinstance(r, str)]
    except Exception:
        rules = []
    _rules_cache[path] = (st.st_mtime, rules)
    return rules

def allow_rules_for(cwd):
    paths = [str(HOME / ".claude" / "settings.json")]
    if cwd:
        paths += [str(Path(cwd) / ".claude" / "settings.json"),
                  str(Path(cwd) / ".claude" / "settings.local.json")]
    out = []
    for p in paths:
        out += _read_allow_rules(p)
    return out

def rule_matches(rule, tool, specifier):
    """Supports the common rule forms: bare ToolName, ToolName(exact),
    and prefix rules ToolName(prefix:*). Anything else is ignored."""
    if rule == tool:
        return True
    if not (rule.startswith(tool + "(") and rule.endswith(")")):
        return False
    body = rule[len(tool) + 1:-1]
    if body.endswith(":*"):
        return specifier.startswith(body[:-2])
    return specifier == body

# ---- safe Bash prefix generation (for the "Always" button) ----
# Read-only-biased, plus the build/test commands developers routinely blanket-
# approve. Destructive/state-changing ops (git push/reset, rm, pip/brew install,
# docker run, kubectl delete, ...) are deliberately ABSENT so "Always" writes an
# exact rule for them, never a prefix. Extend these lists to taste.
SAFE_BASH_HEADS = {
    "ls", "pwd", "echo", "cat", "head", "tail", "wc", "grep", "rg", "find",
    "which", "whoami", "date", "tree", "hostname", "uname", "id", "sort",
    "uniq", "cut", "column", "basename", "dirname", "realpath", "stat", "file",
    "df", "du", "printenv", "env", "jq",
    "pytest", "tox", "make", "tsc", "ruff", "mypy", "eslint", "prettier",
    "jest", "vitest", "black", "flake8",
}
SAFE_BASH_SUBCMDS = {
    "git status", "git log", "git diff", "git show", "git describe",
    "git rev-parse", "git blame", "git shortlog", "git ls-files",
    "git cat-file", "git reflog",
    "npm test", "npm run", "npm ci", "pnpm test", "pnpm run", "yarn test",
    "cargo check", "cargo test", "cargo build", "cargo clippy",
    "go build", "go test", "go vet", "poetry run", "python -m",
}
_BASH_META = re.compile(r"[;&|<>`]|\$\(|\n")

def bash_is_compound(cmd):
    """True if the command chains/substitutes/redirects — a prefix rule can't be
    trusted for it (e.g. `git status; rm -rf ~` rides in on `git status:*`)."""
    return bool(_BASH_META.search(cmd or ""))

def safe_bash_prefix(cmd):
    """Conservative prefix (WITHOUT the trailing `:*`) for a Bash command that is
    safe to blanket-allow, or None to fall back to an exact rule. Only simple
    (non-compound) commands whose leading token(s) name a curated read-only or
    routinely-whitelisted build/test op qualify."""
    if bash_is_compound(cmd):
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if not toks:
        return None
    if toks[0] in SAFE_BASH_HEADS:
        return toks[0]
    if len(toks) >= 2 and f"{toks[0]} {toks[1]}" in SAFE_BASH_SUBCMDS:
        return f"{toks[0]} {toks[1]}"
    return None

def would_prompt(tool, tool_input, cwd, permission_mode):
    """Approximate: would Claude Code show a terminal permission prompt for
    this call? Errs graceful both ways (see spec)."""
    if permission_mode in ("bypassPermissions", "plan"):
        return False
    if permission_mode == "acceptEdits" and tool in EDIT_TOOLS:
        return False
    if not (tool in PROMPTY_TOOLS or tool.startswith("mcp__")):
        return False
    ti = tool_input or {}
    if tool == "Bash":
        specifier = (ti.get("command") or "").strip()
    else:
        specifier = ti.get("file_path") or ti.get("url") or ""
    bash_compound = tool == "Bash" and bash_is_compound(specifier)
    for rule in allow_rules_for(cwd):
        try:
            if rule_matches(rule, tool, specifier):
                # A prefix rule can't be trusted for a compound Bash command:
                # `git status; rm -rf ~` must not ride in on `Bash(git status:*)`.
                if bash_compound and rule.endswith(":*)"):
                    continue
                return False
        except Exception:
            continue
    return True

async def do_gate(agent, p):
    """Block a gated hook until the user clicks Allow/Deny, or time out.
    Returns one of: {gate:False} | {gate:True, decision:allow|deny|timeout}.
    Always records the action as a tool_use event so the feed stays live even
    for unheld sessions (this hook replaces the plain PreToolUse reporter).

    Modes: off = never hold. all = hold everything (strict remote control).
    auto (claude-code default) = hold only calls Claude Code itself would
    prompt for, and only while a dashboard client is watching — approving on
    the dashboard pre-empts the terminal prompt entirely."""
    sid = gate_sid(agent, p)
    tool, summary = describe_action(agent, p)
    cwd = p.get("cwd") or (p.get("workspace_roots") or [None])[0]
    if agent == "claude-code" and is_claude_question_tool(tool):
        upsert_session(sid, agent, cwd=cwd, status="waiting_input", task=summary)
        add_event(sid, "waiting_input", summary)
        await hub.broadcast()
        return {"gate": False}
    mode = effective_gate_mode(sid, agent)
    if mode == "all":
        hold = True
    elif mode == "auto" and agent == "claude-code":
        hold = bool(hub.clients) and would_prompt(
            p.get("tool_name") or "", p.get("tool_input") or {},
            cwd, p.get("permission_mode"))
    else:
        hold = False
    if not hold:
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
        # No hook decision means Claude Code falls back to its native terminal
        # prompt. Keep the card waiting so the user is notified to choose there.
        upsert_session(sid, agent, cwd=cwd, status="waiting_input")
        add_event(sid, "waiting_input",
                  "Decision timed out, choose in the terminal")
        await hub.broadcast()
        return {"gate": True, "decision": "timeout"}
    pending.pop(did, None)
    with db() as c:
        r = c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()
    return {"gate": True,
            "decision": "allow" if r and r["status"] == "allowed" else "deny"}

def add_always_rule(row):
    """Append a conservative allow rule to the project's settings.local.json —
    the same file Claude Code's own 'always allow' writes. Conservative on
    purpose: Bash gets a curated safe `prefix:*` when the command qualifies
    (else the exact command), WebFetch the domain, other tools the bare name."""
    try:
        detail = json.loads(row["detail"] or "{}")
    except Exception:
        detail = {}
    cwd = detail.get("cwd")
    tool = detail.get("tool_name") or row["tool"]
    if not cwd or not tool:
        return None
    ti = detail.get("tool_input") or {}
    if tool == "Bash":
        cmd = (ti.get("command") or "").strip()
        if not cmd:
            return None
        prefix = safe_bash_prefix(cmd)
        rule = f"Bash({prefix}:*)" if prefix else f"Bash({cmd})"
    elif tool == "WebFetch" and ti.get("url"):
        host = re.sub(r"^\w+://", "", ti["url"]).split("/")[0]
        rule = f"WebFetch(domain:{host})"
    else:
        rule = tool
    path = Path(cwd) / ".claude" / "settings.local.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except Exception:
            return None  # never clobber a file we can't parse
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if rule not in allow:
        allow.append(rule)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2))
    return rule

async def do_decide(did, action):
    """Resolve a pending decision. Returns (body, http_status).
    'always' additionally persists an allow rule, then allows."""
    if action not in ("allow", "deny", "always"):
        return {"ok": False, "reason": "bad_action"}, 400
    with db() as c:
        row = c.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}, 404
        if row["status"] != "pending":
            return {"ok": False, "reason": "not_pending"}, 200
        new_status = "denied" if action == "deny" else "allowed"
        c.execute("UPDATE decisions SET status=?, decided_at=? WHERE id=?",
                  (new_status, time.time(), did))
    sid = row["session_id"]
    rule = None
    if action == "always":
        try:
            rule = add_always_rule(row)
        except Exception:
            rule = None
    verb = {"allow": "Allowed", "deny": "Denied",
            "always": "Always-allowed"}[action]
    extra = f" (rule: {rule})" if rule else ""
    add_event(sid, "status_change",
              f"{verb} from dashboard: {row['summary'] or row['tool']}{extra}")
    with db() as c:
        c.execute("UPDATE sessions SET status='working' WHERE session_id=?", (sid,))
    ev = pending.get(did)
    if ev:
        ev.set()
    await hub.broadcast()
    return {"ok": True, "decision": new_status, "rule": rule}, 200

async def do_set_gate(sid, mode):
    if mode not in GATE_MODES:
        return {"ok": False, "reason": "bad_mode"}
    with db() as c:
        c.execute("UPDATE sessions SET gate_mode=? WHERE session_id=?", (mode, sid))
    add_event(sid, "status_change", f"Gate mode set to {mode}")
    await hub.broadcast()
    return {"ok": True, "gate_mode": mode}

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
        # soft: this line may be the tool_use written right before a terminal
        # permission prompt — it must not clear waiting_input (the stomp bug)
        upsert_session(sid, "claude-code", cwd=cwd, model=model, status="working",
                       soft=True)
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
                if is_claude_question_tool(b.get("name")):
                    summary = claude_question_summary(ti)
                    upsert_session(sid, "claude-code", cwd=cwd, model=model,
                                   status="waiting_input", task=summary)
                    add_event(sid, "waiting_input", summary)
                    continue
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
                       cwd=payload.get("cwd"), status="working", soft=True)
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
            upsert_session(sid, "codex", status="working", soft=True)
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

def reconcile_proc_card(agent):
    """Process-scan safety net for one agent. Returns True if a UI-visible
    change was made (card retired, created, or revived), else False.

    The `{agent}-proc` card only has value when we have NO real session for the
    agent. When a real (hook/tailer-driven) session is live it is redundant, so
    retire it; otherwise keep it fresh so it stops flip-flopping (create ->
    reaped after IDLE_S -> recreate)."""
    proc_sid = f"{agent}-proc"
    with db() as c:
        real = c.execute(
            "SELECT 1 FROM sessions WHERE agent_type=? AND session_id!=? AND "
            "status IN ('working','idle','waiting_input') LIMIT 1",
            (agent, proc_sid)).fetchone()
        existing = c.execute("SELECT status FROM sessions WHERE session_id=?",
                             (proc_sid,)).fetchone()
    if real:
        if existing and existing["status"] != "ended":
            with db() as c:
                c.execute("UPDATE sessions SET status='ended' WHERE session_id=?",
                          (proc_sid,))
            return True
        return False
    # This process is our only signal for the agent: keep the card alive.
    upsert_session(proc_sid, agent, status="idle",
                   task="Process detected (no log data yet)")
    return existing is None or existing["status"] == "ended"


async def process_scan():
    try:
        import psutil
    except ImportError:
        return
    KNOWN = {"claude": "claude-code", "codex": "codex",
             "cursor": "cursor", "Cursor": "cursor",
             "kiro": "kiro", "Kiro": "kiro"}
    while True:
        changed = False
        try:
            seen = set()
            for proc in psutil.process_iter(["name"]):
                name = (proc.info["name"] or "")
                for key, agent in KNOWN.items():
                    if key in name and agent not in seen:
                        seen.add(agent)
                        if reconcile_proc_card(agent):
                            changed = True
        except Exception:
            pass
        if changed:
            await hub.broadcast()
        await asyncio.sleep(15)

# ---------------------------------------------------------------- localhost ports

_LISTEN_RE = re.compile(r":(\d+)\s*\(LISTEN\)\s*$")

def parse_lsof_listeners(text):
    """Map {pid: {ports}} from `lsof -nP -iTCP -sTCP:LISTEN` output. Handles the
    IPv4 `127.0.0.1:PORT`, wildcard `*:PORT`, and IPv6 `[::1]:PORT` name forms."""
    out = {}
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        m = _LISTEN_RE.search(line)
        if not m:
            continue
        out.setdefault(int(parts[1]), set()).add(int(m.group(1)))
    return out

_FRAMEWORKS = [
    ("vite", "Vite"), ("next", "Next.js"), ("react-scripts", "CRA"),
    ("webpack", "Webpack"), ("nuxt", "Nuxt"), ("astro", "Astro"),
    ("ng serve", "Angular"), ("@angular", "Angular"), ("svelte", "Svelte"),
    ("uvicorn", "Uvicorn"), ("gunicorn", "Gunicorn"), ("hypercorn", "Hypercorn"),
    ("fastapi", "FastAPI"), ("flask", "Flask"), ("django", "Django"),
    ("http.server", "http.server"), ("rails", "Rails"), ("puma", "Puma"),
    ("jekyll", "Jekyll"), ("hugo", "Hugo"), ("php", "PHP"),
    ("nodemon", "nodemon"), ("vercel", "Vercel"), ("storybook", "Storybook"),
]

def framework_guess(cmdline):
    low = (cmdline or "").lower()
    for needle, label in _FRAMEWORKS:
        if needle in low:
            return label
    return None

def scan_ports():
    """List localhost TCP listeners owned by this user, enriched per PID. Uses
    lsof (works without sudo; psutil.net_connections needs root on macOS) for the
    pid->ports map, then psutil for display data. Blocking — call off the loop."""
    empty = {"scanned_at": time.time(), "servers": [],
             "counts": {"servers": 0, "projects": 0, "ports": 0}}
    try:
        import psutil
    except ImportError:
        return empty
    try:
        text = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return empty
    self_pid = os.getpid()
    servers, projects, nports = [], set(), 0
    for pid, ports in parse_lsof_listeners(text).items():
        try:
            p = psutil.Process(pid)
        except Exception:
            continue
        def safe(fn, default=None):
            try:
                return fn()
            except Exception:
                return default
        cmd = " ".join(safe(p.cmdline, []) or [])
        cwd = safe(p.cwd)
        mem = safe(lambda: p.memory_info().rss // 1024 // 1024)
        created = safe(p.create_time)
        # cwd is the reliable signal: GUI/system apps run from "/" (or deny cwd),
        # a dev server runs from its project dir. Don't use the cmdline — the
        # framework Python interpreter lives under Python.app/Contents/.
        project_like = bool(cwd and cwd not in ("/", str(HOME)))
        project = Path(cwd).name if project_like else None
        if project:
            projects.add(project)
        nports += len(ports)
        servers.append({
            "pid": pid, "app": safe(p.name) or "?", "cmd": cmd[:200],
            "cwd": cwd, "project": project, "project_like": project_like,
            "framework": framework_guess(cmd),
            "mem_mb": mem, "uptime_s": (time.time() - created) if created else None,
            "ports": sorted(ports), "is_self": pid == self_pid})
    servers.sort(key=lambda s: (not s["is_self"], not s["project_like"],
                                min(s["ports"])))
    return {"scanned_at": time.time(), "servers": servers,
            "counts": {"servers": len(servers), "projects": len(projects),
                       "ports": nports}}

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

@app.get("/ports")
def ports_page():
    return FileResponse(HERE / "static" / "ports.html",
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
    mode = p.get("mode")
    if mode is None and "on" in p:  # back-compat with the old boolean API
        mode = "all" if p.get("on") else "off"
    return await do_set_gate(sid, mode)

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
    n = min(max(days, 1), 365)
    by_agent, by_provider = token_breakdown(n)
    return {"days": history_days(n), "by_agent": by_agent, "by_provider": by_provider}

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

@app.get("/api/ports")
def api_ports():
    # sync def => FastAPI runs it in a threadpool, so the blocking lsof scan
    # never stalls the event loop. Scanned on demand (page polls while visible).
    return scan_ports()

def schedule_self_terminate(pid):
    def stop():
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    threading.Timer(0.25, stop).start()

@app.post("/api/ports/{pid}/kill")
def api_kill_port(pid: int, payload: dict = Body(default=None)):
    """Kill a localhost server. Guarded by live listener re-scan and project
    ownership. The dashboard's own PID is allowed only with explicit
    confirmation and is terminated after the response is sent."""
    if pid == os.getpid():
        if not (payload or {}).get("confirm"):
            return JSONResponse({"ok": False, "reason": "confirm_required"},
                                status_code=400)
        schedule_self_terminate(pid)
        return {"ok": True, "how": "scheduled", "pid": pid,
                "app": "Agent Deck", "project": HERE.name, "ports": [PORT]}
    target = next((s for s in scan_ports()["servers"] if s["pid"] == pid), None)
    if not target:
        return JSONResponse({"ok": False, "reason": "not_listening"}, status_code=404)
    if not target["project_like"]:
        return JSONResponse({"ok": False, "reason": "system"}, status_code=403)
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=1.5)
            how = "terminated"
        except psutil.TimeoutExpired:
            p.kill()
            how = "killed"
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    return {"ok": True, "how": how, "pid": pid, "app": target["app"],
            "project": target["project"], "ports": target["ports"]}

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

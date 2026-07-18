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
    return {"type": "state", "now": time.time(),
            "sessions": sessions, "events": events, "decisions": decisions,
            "today": {"events": today["n"] or 0,
                      "tool_calls": today["tools"] or 0,
                      "completed": today["done"] or 0}}

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
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                ti = b.get("input") or {}
                target = ti.get("file_path") or ti.get("command") or ""
                add_event(sid, "tool_use",
                          f"{b.get('name','tool')} {txt(target,120)}".strip())

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
codex_line.last_sid = {}

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
    while True:
        if derive_statuses():
            await hub.broadcast()
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

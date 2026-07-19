"""Tests for Agent Deck. Run: .venv/bin/pytest -q"""
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as amc


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point the app at a throwaway database."""
    monkeypatch.setattr(amc, "DB_PATH", tmp_path / "test.db")
    amc.init_db()
    return amc


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- statuses

def test_derive_working_idle_ended(fresh_db):
    now = time.time()
    amc.upsert_session("s1", "custom", status="working", ts=now)
    amc.upsert_session("s2", "custom", status="working", ts=now - amc.WORKING_S - 5)
    amc.upsert_session("s3", "custom", status="working", ts=now - amc.IDLE_S - 5)
    amc.derive_statuses()
    with amc.db() as c:
        got = {r["session_id"]: r["status"]
               for r in c.execute("SELECT session_id,status FROM sessions")}
    assert got == {"s1": "working", "s2": "idle", "s3": "ended"}


def test_waiting_input_survives_idle_window(fresh_db):
    """A blocked session is silent by definition; it must not expire on the
    normal idle timeout, only after WAITING_TTL_S."""
    now = time.time()
    amc.upsert_session("w1", "custom", status="waiting_input",
                       ts=now - amc.IDLE_S - 60)
    amc.upsert_session("w2", "custom", status="waiting_input",
                       ts=now - amc.WAITING_TTL_S - 60)
    amc.derive_statuses()
    with amc.db() as c:
        got = {r["session_id"]: r["status"]
               for r in c.execute("SELECT session_id,status FROM sessions")}
    assert got == {"w1": "waiting_input", "w2": "ended"}


def test_today_counts_use_local_midnight(fresh_db):
    import datetime
    midnight = datetime.datetime.combine(
        datetime.date.today(), datetime.time.min).timestamp()
    amc.upsert_session("s", "custom", status="working")
    amc.add_event("s", "tool_use", "yesterday", ts=midnight - 60)
    amc.add_event("s", "tool_use", "today", ts=midnight + 60)
    payload = amc.state_payload()
    assert payload["today"]["tool_calls"] == 1


# ---------------------------------------------------------------- handlers

def test_claude_stop_is_turn_end_not_session_end(fresh_db):
    run(amc.handle_claude_code("SessionStart", {"session_id": "cc1", "cwd": "/x/p"}))
    run(amc.handle_claude_code("Stop", {"session_id": "cc1"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='cc1'").fetchone()
    assert row["status"] == "idle"

    run(amc.handle_claude_code("SessionEnd", {"session_id": "cc1"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='cc1'").fetchone()
    assert row["status"] == "ended"


def test_claude_notification_sets_waiting(fresh_db):
    run(amc.handle_claude_code("Notification",
                               {"session_id": "cc2", "message": "Approve Bash?"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='cc2'").fetchone()
    assert row["status"] == "waiting_input"


def test_generic_completed_maps_to_ended(fresh_db):
    run(amc.handle_generic({"agent": "custom", "session_id": "g1",
                            "status": "working", "task": "do stuff"}))
    run(amc.handle_generic({"agent": "custom", "session_id": "g1",
                            "status": "completed", "summary": "done"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='g1'").fetchone()
        kinds = [r["kind"] for r in c.execute(
            "SELECT kind FROM events WHERE session_id='g1' ORDER BY id")]
    assert row["status"] == "ended"
    assert kinds[-1] == "completed"


def test_generic_rejects_unknown_status(fresh_db):
    run(amc.handle_generic({"agent": "custom", "session_id": "g2",
                            "status": "exploded"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='g2'").fetchone()
    assert row["status"] == "working"


# ---------------------------------------------------------------- kiro

def test_kiro_prompt_and_approval(fresh_db):
    run(amc.handle_kiro("prompt", {"session_id": "k1", "prompt": "refactor auth",
                                   "cwd": "/x/proj"}))
    with amc.db() as c:
        row = c.execute("SELECT status,current_task,agent_type,project "
                        "FROM sessions WHERE session_id='k1'").fetchone()
    assert row["status"] == "working"
    assert row["current_task"] == "refactor auth"
    assert row["agent_type"] == "kiro"
    assert row["project"] == "proj"

    run(amc.handle_kiro("approval", {"session_id": "k1",
                                     "message": "Run rm -rf build?"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='k1'").fetchone()
    assert row["status"] == "waiting_input"


def test_kiro_tool_and_stop(fresh_db):
    run(amc.handle_kiro("shell", {"session_id": "k2", "command": "npm test"}))
    run(amc.handle_kiro("fileEdit", {"session_id": "k2", "file_path": "a.py"}))
    run(amc.handle_kiro("stop", {"session_id": "k2"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='k2'").fetchone()
        kinds = [r["kind"] for r in c.execute(
            "SELECT kind FROM events WHERE session_id='k2' ORDER BY id")]
    assert row["status"] == "idle"  # turn end, not session end
    assert kinds == ["tool_use", "tool_use", "completed"]


def test_kiro_endpoint(fresh_db):
    client = TestClient(amc.app)
    r = client.post("/ingest/kiro/prompt",
                    json={"session_id": "k3", "prompt": "hi"})
    assert r.json() == {"ok": True}
    with amc.db() as c:
        row = c.execute("SELECT agent_type FROM sessions WHERE session_id='k3'").fetchone()
    assert row["agent_type"] == "kiro"


# ---------------------------------------------------------------- tailer

def test_tailer_marks_dirty_on_new_lines(fresh_db, tmp_path):
    """New tailer lines must flag dirty so run() pushes a live WS broadcast."""
    log = tmp_path / "a.jsonl"
    t = amc.Tailer(str(tmp_path / "*.jsonl"), lambda p, o: None)
    t.first_pass = False
    log.write_text('{"n": 1}\n')
    t.dirty = False
    t.scan()
    assert t.dirty is True
    t.dirty = False
    t.scan()  # nothing new
    assert t.dirty is False


def test_tailer_recovers_from_truncation(fresh_db, tmp_path):
    log = tmp_path / "a.jsonl"
    seen = []
    t = amc.Tailer(str(tmp_path / "*.jsonl"), lambda p, o: seen.append(o))
    t.first_pass = False

    log.write_text('{"n": 1}\n{"n": 2}\n')
    t.scan()
    assert [o["n"] for o in seen] == [1, 2]

    log.write_text('{"n": 3}\n')  # rotated: shorter than old offset
    t.scan()
    assert [o["n"] for o in seen] == [1, 2, 3]


# ---------------------------------------------------------------- gating

def _pending_id(sid="g1"):
    with amc.db() as c:
        r = c.execute("SELECT id FROM decisions WHERE session_id=? AND "
                      "status='pending' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    return r["id"] if r else None


def test_gate_passthrough_when_not_gated(fresh_db):
    # auto mode + no connected dashboard client => never hold
    amc.hub.clients.clear()
    amc.upsert_session("u1", "claude-code", status="working")
    res = run(amc.do_gate("claude-code", {"session_id": "u1",
                                              "tool_name": "Bash",
                                              "tool_input": {"command": "ls"}}))
    assert res == {"gate": False}
    with amc.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    assert n == 0  # no decision row created for unheld sessions


def test_gate_modes_and_defaults(fresh_db):
    amc.upsert_session("g0", "claude-code", status="working")
    amc.upsert_session("c0", "cursor", status="working")
    assert amc.effective_gate_mode("g0", "claude-code") == "auto"  # cc default
    assert amc.effective_gate_mode("c0", "cursor") == "off"       # others off
    run(amc.do_set_gate("g0", "all"))
    assert amc.effective_gate_mode("g0", "claude-code") == "all"
    run(amc.do_set_gate("g0", "off"))
    assert amc.effective_gate_mode("g0", "claude-code") == "off"
    body = run(amc.do_set_gate("g0", "bogus"))
    assert body["ok"] is False


def _gate_roundtrip(action):
    async def scenario():
        amc.upsert_session("g1", "claude-code", status="working")
        await amc.do_set_gate("g1", "all")
        task = asyncio.create_task(amc.do_gate(
            "claude-code", {"session_id": "g1", "tool_name": "Bash",
                            "tool_input": {"command": "rm -rf build"}}))
        did = None
        for _ in range(200):
            await asyncio.sleep(0.005)
            did = _pending_id()
            if did:
                break
        assert did, "gate never created a pending decision"
        body, code = await amc.do_decide(did, action)
        gate_res = await task
        return did, body, code, gate_res
    return asyncio.run(scenario())


def test_gate_allow_roundtrip(fresh_db):
    did, body, code, gate_res = _gate_roundtrip("allow")
    assert code == 200 and body["ok"] is True
    assert gate_res == {"gate": True, "decision": "allow"}
    with amc.db() as c:
        st = c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()["status"]
        sess = c.execute("SELECT status FROM sessions WHERE session_id='g1'").fetchone()["status"]
    assert st == "allowed"
    assert sess == "working"


def test_gate_deny_roundtrip(fresh_db):
    did, body, code, gate_res = _gate_roundtrip("deny")
    assert gate_res == {"gate": True, "decision": "deny"}
    with amc.db() as c:
        st = c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()["status"]
    assert st == "denied"


def test_gate_timeout(fresh_db, monkeypatch):
    monkeypatch.setattr(amc, "GATE_TIMEOUT_S", 0.05)

    async def scenario():
        amc.upsert_session("g2", "claude-code", status="working")
        await amc.do_set_gate("g2", "all")
        return await amc.do_gate("claude-code",
                                 {"session_id": "g2", "tool_name": "Bash",
                                  "tool_input": {"command": "ls"}})
    res = asyncio.run(scenario())
    assert res == {"gate": True, "decision": "timeout"}
    with amc.db() as c:
        st = c.execute("SELECT status FROM decisions WHERE session_id='g2'").fetchone()["status"]
        sess = c.execute("SELECT status FROM sessions WHERE session_id='g2'").fetchone()["status"]
    assert st == "expired"
    assert sess == "working"


def test_decision_not_found_and_double_decide(fresh_db):
    body, code = run(amc.do_decide(999, "allow"))
    assert code == 404 and body["reason"] == "not_found"

    with amc.db() as c:
        c.execute("INSERT INTO decisions(session_id,tool,summary,status,created_at)"
                  " VALUES('s','Bash','x','pending',?)", (time.time(),))
        did = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    amc.upsert_session("s", "claude-code", status="waiting_input")
    body, code = run(amc.do_decide(did, "allow"))
    assert body["ok"] is True
    body, code = run(amc.do_decide(did, "deny"))  # already resolved
    assert body == {"ok": False, "reason": "not_pending"}


def test_cursor_gate_sid_and_summary(fresh_db):
    # cursor identifies sessions by conversation_id, not session_id
    assert amc.gate_sid("cursor", {"conversation_id": "conv-9"}) == "conv-9"
    tool, summary = amc.describe_action("cursor", {"command": "npm run deploy"})
    assert tool == "command"
    assert "npm run deploy" in summary


# ---------------------------------------------------------------- approval parity

def test_tailer_does_not_stomp_waiting_input(fresh_db):
    """The visibility bug: a transcript line arriving after a permission
    Notification must not flip the card back to working."""
    run(amc.handle_claude_code("Notification",
                               {"session_id": "v1",
                                "message": "Claude needs your permission to use Bash"}))
    # tailer reads the assistant tool_use line moments later (soft)
    amc.claude_line("/fake/v.jsonl",
                    {"sessionId": "v1", "type": "assistant",
                     "message": {"id": "m1", "model": "claude-opus-4-8",
                                 "content": [{"type": "tool_use", "name": "Bash",
                                              "input": {"command": "ls"}}]}})
    with amc.db() as c:
        row = c.execute("SELECT status,current_task FROM sessions "
                        "WHERE session_id='v1'").fetchone()
    assert row["status"] == "waiting_input"          # NOT stomped
    assert row["current_task"] == "Needs approval: Bash"

    # the tool actually running (PostToolUse hook) is real evidence => clears
    run(amc.handle_claude_code("PostToolUse", {"session_id": "v1"}))
    with amc.db() as c:
        row = c.execute("SELECT status FROM sessions WHERE session_id='v1'").fetchone()
    assert row["status"] == "working"


def test_would_prompt_rules(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(amc, "HOME", tmp_path / "nohome")  # no global settings
    amc._rules_cache.clear()
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": [
            "Bash(ls)", "Bash(npm run test:*)", "WebFetch",
            "weird-rule-(((", 42]}}))
    cwd = str(proj)

    wp = amc.would_prompt
    assert wp("Read", {"file_path": "x"}, cwd, None) is False       # safe tool
    assert wp("Bash", {"command": "ls"}, cwd, None) is False        # exact rule
    assert wp("Bash", {"command": "npm run test --watch"}, cwd, None) is False  # prefix
    assert wp("Bash", {"command": "rm -rf /"}, cwd, None) is True   # no rule
    assert wp("WebFetch", {"url": "https://x.dev"}, cwd, None) is False  # bare rule
    assert wp("Edit", {"file_path": "a.py"}, cwd, None) is True
    assert wp("Edit", {"file_path": "a.py"}, cwd, "acceptEdits") is False
    assert wp("Bash", {"command": "rm -rf /"}, cwd, "bypassPermissions") is False
    assert wp("Bash", {"command": "rm -rf /"}, cwd, "plan") is False
    assert wp("mcp__github__create_pr", {}, cwd, None) is True      # mcp held
    assert wp("UnknownTool", {}, cwd, None) is False                # unknown passes


def test_auto_mode_holds_only_would_prompt(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(amc, "HOME", tmp_path / "nohome")
    monkeypatch.setattr(amc, "GATE_TIMEOUT_S", 0.05)
    amc._rules_cache.clear()
    class FakeWS:  # survives hub.broadcast, unlike a bare object()
        async def send_text(self, msg): pass
    amc.hub.clients.add(FakeWS())  # a dashboard client is watching
    try:
        amc.upsert_session("a1", "claude-code", status="working")
        # safe tool: passes straight through even in auto with a client
        res = run(amc.do_gate("claude-code", {"session_id": "a1",
                                              "tool_name": "Read",
                                              "tool_input": {"file_path": "x"}}))
        assert res == {"gate": False}
        # would-prompt tool: held (times out with nobody clicking)
        res = run(amc.do_gate("claude-code", {"session_id": "a1",
                                              "tool_name": "Bash",
                                              "tool_input": {"command": "rm -rf build"},
                                              "cwd": str(tmp_path)}))
        assert res == {"gate": True, "decision": "timeout"}
    finally:
        amc.hub.clients.clear()


def test_always_appends_rule_and_allows(fresh_db, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with amc.db() as c:
        c.execute("INSERT INTO decisions(session_id,tool,summary,detail,status,"
                  "created_at) VALUES('s1','Bash','Bash npm test','"
                  + json.dumps({"cwd": str(proj), "tool_name": "Bash",
                                "tool_input": {"command": "npm test"}})
                  + "','pending',1)")
        did = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    amc.upsert_session("s1", "claude-code", status="waiting_input")

    body, code = run(amc.do_decide(did, "always"))
    assert code == 200 and body["ok"] is True
    # "npm test" is a curated safe subcommand -> a prefix rule, not exact
    assert body["rule"] == "Bash(npm test:*)"
    settings = json.loads((proj / ".claude" / "settings.local.json").read_text())
    assert "Bash(npm test:*)" in settings["permissions"]["allow"]
    # the prefix rule now suppresses future holds for variants of the command
    amc._rules_cache.clear()
    assert amc.would_prompt("Bash", {"command": "npm test"}, str(proj), None) is False
    assert amc.would_prompt("Bash", {"command": "npm test --coverage"},
                            str(proj), None) is False


def test_safe_bash_prefix():
    sp = amc.safe_bash_prefix
    # curated safe heads / subcommands -> prefix
    assert sp("git status --short") == "git status"
    assert sp("ls -la /tmp") == "ls"
    assert sp("pytest tests/x.py -q") == "pytest"
    assert sp("npm run build") == "npm run"
    # not on the allowlist -> exact rule (None)
    assert sp("git push origin main") is None
    assert sp("rm -rf /") is None
    assert sp("docker run x") is None
    # compound commands never get a prefix
    assert sp("git status; rm -rf ~") is None
    assert sp("cat a | grep b") is None
    assert sp("echo hi && rm x") is None
    assert sp("") is None


def test_always_rule_falls_back_to_exact_for_unsafe(fresh_db, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with amc.db() as c:
        c.execute("INSERT INTO decisions(session_id,tool,summary,detail,status,"
                  "created_at) VALUES('s1','Bash','x','"
                  + json.dumps({"cwd": str(proj), "tool_name": "Bash",
                                "tool_input": {"command": "git push origin main"}})
                  + "','pending',1)")
        did = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    body, _ = run(amc.do_decide(did, "always"))
    assert body["rule"] == "Bash(git push origin main)"  # exact, no prefix


# ---------------------------------------------------------------- localhost ports

def test_parse_lsof_listeners():
    sample = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
              "node 111 me 25u IPv6 0x1 0t0 TCP *:11470 (LISTEN)\n"
              "node 111 me 26u IPv6 0x2 0t0 TCP *:12470 (LISTEN)\n"
              "Python 222 me 9u IPv4 0x3 0t0 TCP 127.0.0.1:7777 (LISTEN)\n"
              "vite 222 me 9u IPv6 0x4 0t0 TCP [::1]:5173 (LISTEN)\n"
              "junk line without a port\n")
    got = {pid: sorted(ports) for pid, ports in amc.parse_lsof_listeners(sample).items()}
    assert got == {111: [11470, 12470], 222: [5173, 7777]}


def test_framework_guess():
    assert amc.framework_guess("/x/node_modules/.bin/vite dev") == "Vite"
    assert amc.framework_guess("uvicorn main:app --reload") == "Uvicorn"
    assert amc.framework_guess("next dev -p 3000") == "Next.js"
    assert amc.framework_guess("python app.py") is None


def test_ports_page_and_api(client):
    r = client.get("/ports")
    assert r.status_code == 200 and "<html" in r.text.lower()
    data = client.get("/api/ports").json()
    assert set(data) >= {"scanned_at", "servers", "counts"}
    assert set(data["counts"]) == {"servers", "projects", "ports"}
    assert isinstance(data["servers"], list)


def test_prefix_rule_not_trusted_for_compound(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(amc, "HOME", tmp_path / "nohome")
    amc._rules_cache.clear()
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(git status:*)"]}}))
    cwd = str(proj)
    # a variant of the allowed prefix passes
    assert amc.would_prompt("Bash", {"command": "git status -s"}, cwd, None) is False
    # a compound command must NOT ride in on the prefix rule -> still prompts
    assert amc.would_prompt("Bash", {"command": "git status; rm -rf ~"},
                            cwd, None) is True


# ---------------------------------------------------------------- usage & history

def test_est_cost_matching():
    assert amc.est_cost("claude-opus-4-8", 1_000_000, 0) == 5
    assert amc.est_cost("claude-sonnet-5", 0, 1_000_000) == 15
    assert amc.est_cost("gpt-5-codex", 1_000_000, 0) == 1.25
    assert amc.est_cost("mystery-model-9", 500, 500) is None


def test_add_usage_accumulates(fresh_db):
    amc.add_usage("claude-code", "claude-opus-4-8", 100, 50)
    amc.add_usage("claude-code", "claude-opus-4-8", 10, 5)
    payload = amc.state_payload()
    assert payload["usage_today"]["tokens_in"] == 110
    assert payload["usage_today"]["tokens_out"] == 55
    assert payload["usage_today"]["est_cost"] is not None


def test_claude_line_usage_dedupes_by_message_id(fresh_db):
    amc.claude_line.last_usage_id.clear()
    line = {"sessionId": "u1", "type": "assistant",
            "message": {"id": "msg_1", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                        "content": []}}
    amc.claude_line("/fake/a.jsonl", line)
    amc.claude_line("/fake/a.jsonl", line)  # streaming repeat, same id
    line2 = dict(line, message=dict(line["message"], id="msg_2"))
    amc.claude_line("/fake/a.jsonl", line2)
    p = amc.state_payload()
    assert p["usage_today"]["tokens_in"] == 200  # msg_1 once + msg_2 once


def test_codex_usage_stores_delta(fresh_db):
    amc.codex_line.last_usage.clear()
    amc.codex_line.last_sid.clear()
    def tc(tin, tout):
        return {"type": "event_msg",
                "payload": {"type": "token_count",
                            "info": {"total_token_usage":
                                     {"input_tokens": tin, "output_tokens": tout}}}}
    amc.codex_line("/fake/r.jsonl", tc(1000, 100))
    amc.codex_line("/fake/r.jsonl", tc(1500, 180))  # cumulative totals
    p = amc.state_payload()
    assert p["usage_today"]["tokens_in"] == 1500
    assert p["usage_today"]["tokens_out"] == 180


def test_generic_webhook_tokens(fresh_db):
    run(amc.handle_generic({"agent": "custom", "session_id": "t1",
                            "status": "working", "model": "claude-haiku-4-5",
                            "tokens_in": 5000, "tokens_out": 800}))
    p = amc.state_payload()
    assert p["usage_today"]["tokens_in"] == 5000


def test_rollup_and_prune(fresh_db):
    now = time.time()
    old = now - (amc.EVENTS_RETAIN_DAYS + 2) * 86400
    very_old = now - (amc.SESSIONS_RETAIN_DAYS + 2) * 86400
    amc.upsert_session("live", "claude-code", status="working", ts=now)
    amc.upsert_session("dead", "claude-code", status="working", ts=very_old)
    with amc.db() as c:
        c.execute("UPDATE sessions SET status='ended' WHERE session_id='dead'")
    amc.add_event("live", "tool_use", "recent", ts=now)
    amc.add_event("dead", "tool_use", "ancient1", ts=old)
    amc.add_event("dead", "completed", "ancient2", ts=old)
    with amc.db() as c:
        c.execute("INSERT INTO decisions(session_id,tool,summary,status,created_at)"
                  " VALUES('dead','Bash','x','expired',?)", (very_old,))

    amc.rollup_and_prune()

    with amc.db() as c:
        ev = [r["summary"] for r in c.execute("SELECT summary FROM events")]
        roll = [dict(r) for r in c.execute("SELECT * FROM daily_rollup")]
        sess = [r["session_id"] for r in c.execute("SELECT session_id FROM sessions")]
        dec = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    assert ev == ["recent"]                      # old events pruned
    assert roll and roll[0]["events"] == 2       # ...but rolled up first
    assert roll[0]["tool_calls"] == 1 and roll[0]["completed"] == 1
    assert sess == ["live"]                      # stale ended session pruned
    assert dec == 0                              # old decision pruned


def test_history_merges_live_and_rollup(fresh_db):
    now = time.time()
    amc.upsert_session("h1", "claude-code", status="working", ts=now)
    amc.add_event("h1", "tool_use", "today-event", ts=now)
    amc.add_usage("claude-code", "claude-opus-4-8", 1000, 100)
    old_day = amc.local_day(now - 5 * 86400)
    with amc.db() as c:
        c.execute("INSERT INTO daily_rollup(day,agent,events,tool_calls,completed)"
                  " VALUES(?,?,?,?,?)", (old_day, "codex", 40, 30, 3))
    days = amc.history_days(30)
    by_day = {d["day"]: d for d in days}
    assert by_day[amc.local_day()]["tool_calls"] == 1
    assert by_day[amc.local_day()]["tokens_in"] == 1000
    assert by_day[amc.local_day()]["est_cost"] is not None
    assert by_day[old_day]["events"] == 40
    assert "codex" in by_day[old_day]["agents"]


def test_history_endpoint(fresh_db):
    client = TestClient(amc.app)
    amc.add_usage("custom", "claude-haiku-4-5", 10, 5)
    r = client.get("/api/history?days=7").json()
    assert r["days"] and r["days"][0]["tokens_in"] == 10


# ---------------------------------------------------------------- endpoints

@pytest.fixture()
def client(fresh_db):
    return TestClient(amc.app)


def test_state_and_summary_endpoints(fresh_db, client):
    run(amc.handle_generic({"agent": "custom", "session_id": "e1",
                            "status": "working", "task": "t",
                            "project": "proj"}))
    run(amc.handle_generic({"agent": "custom", "session_id": "e2",
                            "status": "waiting_input", "summary": "approve?"}))
    state = client.get("/api/state").json()
    assert {s["session_id"] for s in state["sessions"]} == {"e1", "e2"}
    summary = client.get("/summary").json()
    assert summary["live"] == 2
    assert [w["session_id"] for w in summary["waiting"]] == ["e2"]


def test_ingest_endpoint_roundtrip(fresh_db, client):
    r = client.post("/ingest", json={"agent": "custom", "session_id": "web1",
                                     "status": "working", "task": "hello"})
    assert r.json() == {"ok": True}
    ev = client.get("/api/session/web1/events").json()
    assert ev["events"]


def test_daily_endpoint(fresh_db, client):
    amc.upsert_session("d1", "custom", status="working")
    amc.add_event("d1", "tool_use", "x")
    days = client.get("/api/daily").json()["days"]
    assert days and days[-1]["n"] >= 1


# ---------------------------------------------------------------- proc cards

def _status(sid):
    with amc.db() as c:
        r = c.execute("SELECT status FROM sessions WHERE session_id=?",
                      (sid,)).fetchone()
    return r["status"] if r else None


def test_reconcile_proc_retires_card_when_real_session_live(fresh_db):
    """A real hook/tailer session makes the safety-net card redundant."""
    amc.upsert_session("real-abc", "claude-code", status="working")
    amc.upsert_session("claude-code-proc", "claude-code", status="idle",
                       task="Process detected (no log data yet)")
    assert amc.reconcile_proc_card("claude-code") is True
    assert _status("claude-code-proc") == "ended"
    assert _status("real-abc") == "working"


def test_reconcile_proc_creates_then_stays_idle(fresh_db):
    """No real session: create the card once, then keep it idle (no flip-flop,
    no redundant broadcast)."""
    assert amc.reconcile_proc_card("codex") is True          # created
    assert _status("codex-proc") == "idle"
    assert amc.reconcile_proc_card("codex") is False         # refresh only
    assert _status("codex-proc") == "idle"                   # not reaped/ended


def test_reconcile_proc_revives_ended_card(fresh_db):
    """Card ended earlier (real session came and went); process still runs and
    is again our only signal -> revive it."""
    amc.upsert_session("cursor-proc", "cursor", status="ended",
                       task="Process detected (no log data yet)")
    assert amc.reconcile_proc_card("cursor") is True
    assert _status("cursor-proc") == "idle"

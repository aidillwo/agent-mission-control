"""Tests for Agent Mission Control. Run: .venv/bin/pytest -q"""
import asyncio
import importlib
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
    asyncio.run(coro)


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

import json
import sqlite3
import time
from pathlib import Path


def _load_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.context_engine.lcm import LcmContextEngine

    return LcmContextEngine()


def test_lcm_engine_exposes_recovery_tools(tmp_path, monkeypatch):
    engine = _load_engine(tmp_path, monkeypatch)

    names = {schema["name"] for schema in engine.get_tool_schemas()}

    assert {"lcm_describe", "lcm_grep", "lcm_expand", "lcm_ingest"}.issubset(names)


def test_lcm_engine_ingests_current_session_messages_for_freshness(tmp_path, monkeypatch):
    engine = _load_engine(tmp_path, monkeypatch)
    engine.on_session_start("session-live", platform="tui", model="test-model")

    response = json.loads(
        engine.handle_tool_call(
            "lcm_ingest",
            {},
            messages=[
                {"role": "user", "content": "fresh lcm sentinel user text"},
                {"role": "assistant", "content": "fresh lcm sentinel assistant text"},
            ],
        )
    )

    assert response["ok"] is True
    assert response["ingested_messages"] == 2
    status = engine.get_status()
    assert status["readiness"] == "GREEN"
    assert status["current_session_id"] == "session-live"
    assert status["current_session_messages"] == 2

    db_path = tmp_path / "lcm.db"
    rows = sqlite3.connect(db_path).execute(
        "select role, content from messages where session_id = ? order by store_id",
        ("session-live",),
    ).fetchall()
    assert rows == [
        ("user", "fresh lcm sentinel user text"),
        ("assistant", "fresh lcm sentinel assistant text"),
    ]


def test_lcm_grep_searches_current_session_after_ingest(tmp_path, monkeypatch):
    engine = _load_engine(tmp_path, monkeypatch)
    engine.on_session_start("session-search")
    engine.handle_tool_call(
        "lcm_ingest",
        {},
        messages=[
            {"role": "user", "content": "alpha unique current-session phrase"},
            {"role": "assistant", "content": "beta response"},
        ],
    )

    result = json.loads(engine.handle_tool_call("lcm_grep", {"query": "alpha unique"}))

    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["session_id"] == "session-search"
    assert "alpha unique current-session phrase" in result["results"][0]["content"]


def test_lcm_readiness_yellow_when_db_is_old_but_available(tmp_path, monkeypatch):
    engine = _load_engine(tmp_path, monkeypatch)
    engine.on_session_start("old-session")
    db = engine._db_path
    con = sqlite3.connect(db)
    try:
        con.execute(
            "insert into messages(session_id, source, role, content, timestamp, token_estimate) values (?, ?, ?, ?, ?, ?)",
            ("old-session", "test", "user", "old content", time.time() - 7200, 3),
        )
        con.commit()
    finally:
        con.close()

    status = engine.get_status()

    assert status["readiness"] == "YELLOW"
    assert status["database_exists"] is True
    assert status["current_session_messages"] == 1


def test_lcm_engine_deepcopy_drops_runtime_handles(tmp_path, monkeypatch):
    import copy

    engine = _load_engine(tmp_path, monkeypatch)
    engine.on_session_start("copy-source")
    engine.update_model(model="test-model", context_length=128000, provider="test")

    cloned = copy.deepcopy(engine)

    assert cloned is not engine
    assert cloned.name == "lcm"
    assert cloned.context_length == 128000
    assert cloned._session_id == "copy-source"
    assert cloned._db_lock is not engine._db_lock

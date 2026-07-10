"""LCM context engine plugin.

This engine keeps the built-in compressor for actual token-budget compaction and
adds a live SQLite-backed context surface for current-session recovery.  It is
purposefully local and dependency-free so Hermes can still start when external
MCP services are down.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine

logger = logging.getLogger(__name__)

_GREEN_MAX_AGE_SECONDS = 5 * 60
_YELLOW_MAX_AGE_SECONDS = 60 * 60
_SOURCE_LIVE = "lcm-engine-live"


LCM_DESCRIBE_SCHEMA = {
    "name": "lcm_describe",
    "description": "Return LCM context-engine readiness, freshness evidence, and database status.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

LCM_GREP_SCHEMA = {
    "name": "lcm_grep",
    "description": "Search LCM current and historical messages using SQLite FTS/LIKE fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "session_id": {"type": "string", "description": "Optional session id filter."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

LCM_EXPAND_SCHEMA = {
    "name": "lcm_expand",
    "description": "Expand an LCM summary node, message store id, or latest/current-session context.",
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Summary node id to expand."},
            "store_id": {"type": "integer", "description": "Message store id to center around."},
            "session_id": {"type": "string", "description": "Session id for latest/current-session expansion."},
            "window": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "additionalProperties": False,
    },
}

LCM_INGEST_SCHEMA = {
    "name": "lcm_ingest",
    "description": "Ingest the live in-memory current session into LCM and return freshness status.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


class LcmContextEngine(ContextEngine):
    """Context engine that preserves live session context in ``lcm.db``.

    The plugin intentionally delegates compaction to the built-in compressor;
    LCM adds durable/fresh recovery surfaces and tools rather than replacing the
    summarizer algorithm in one risky step.
    """

    threshold_percent = 0.75
    protect_first_n = 3
    protect_last_n = 6

    def __init__(self, *, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else self._default_db_path()
        self._db_lock = threading.RLock()
        self._session_id = ""
        self._session_context: Dict[str, Any] = {}
        self._compressor = None
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self._ensure_schema()

    @property
    def name(self) -> str:
        return "lcm"

    def __deepcopy__(self, memo):
        cloned = type(self)(db_path=self._db_path)
        cloned._session_id = self._session_id
        cloned._session_context = copy.deepcopy(self._session_context, memo)
        cloned.last_prompt_tokens = self.last_prompt_tokens
        cloned.last_completion_tokens = self.last_completion_tokens
        cloned.last_total_tokens = self.last_total_tokens
        cloned.threshold_tokens = self.threshold_tokens
        cloned.context_length = self.context_length
        cloned.compression_count = self.compression_count
        # Do not deep-copy locks, sqlite handles, or the built-in compressor.
        # update_model() will lazily create a fresh compressor for the clone.
        return cloned

    def is_available(self) -> bool:
        try:
            self._ensure_schema()
            return True
        except Exception:
            return False

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        self.context_length = int(context_length or 0)
        self.threshold_tokens = int(self.context_length * self.threshold_percent) if self.context_length else 0
        try:
            from agent.context_compressor import ContextCompressor

            if self._compressor is None:
                self._compressor = ContextCompressor(
                    model=model,
                    threshold_percent=self.threshold_percent,
                    protect_first_n=self.protect_first_n,
                    protect_last_n=self.protect_last_n,
                    quiet_mode=True,
                    base_url=base_url,
                    api_key=api_key,
                    config_context_length=context_length,
                    provider=provider,
                    api_mode=api_mode,
                )
            else:
                self._compressor.update_model(
                    model=model,
                    context_length=context_length,
                    base_url=base_url,
                    api_key=api_key,
                    provider=provider,
                    api_mode=api_mode,
                )
        except Exception as exc:
            logger.debug("LCM engine could not initialize compressor delegate: %s", exc)
            self._compressor = None

    def bind_session_state(self, session_db=None, session_id: str = "") -> None:
        if session_id:
            self._session_id = session_id
        delegate = getattr(self._compressor, "bind_session_state", None)
        if callable(delegate):
            try:
                delegate(session_db=session_db, session_id=session_id)
            except Exception as exc:
                logger.debug("LCM compressor delegate bind_session_state failed: %s", exc)

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or self._session_id
        self._session_context = dict(kwargs or {})
        self._record_lifecycle(current_session_id=self._session_id, **self._session_context)
        delegate = getattr(self._compressor, "on_session_start", None)
        if callable(delegate):
            try:
                delegate(session_id, **kwargs)
            except Exception as exc:
                logger.debug("LCM compressor delegate on_session_start failed: %s", exc)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        if session_id:
            self._session_id = session_id
        self._ingest_messages(messages or [])
        delegate = getattr(self._compressor, "on_session_end", None)
        if callable(delegate):
            try:
                delegate(session_id, messages)
            except Exception as exc:
                logger.debug("LCM compressor delegate on_session_end failed: %s", exc)

    def on_session_reset(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0
        delegate = getattr(self._compressor, "on_session_reset", None)
        if callable(delegate):
            try:
                delegate()
            except Exception as exc:
                logger.debug("LCM compressor delegate on_session_reset failed: %s", exc)

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        delegate = getattr(self._compressor, "update_from_response", None)
        if callable(delegate):
            try:
                delegate(usage)
                self._mirror_delegate_counters()
                return
            except Exception as exc:
                logger.debug("LCM compressor delegate update_from_response failed: %s", exc)
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens") or (self.last_prompt_tokens + self.last_completion_tokens))

    def should_compress(self, prompt_tokens: int = None) -> bool:
        delegate = getattr(self._compressor, "should_compress", None)
        if callable(delegate):
            try:
                return bool(delegate(prompt_tokens))
            except Exception as exc:
                logger.debug("LCM compressor delegate should_compress failed: %s", exc)
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return bool(self.threshold_tokens and tokens and tokens >= self.threshold_tokens)

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        delegate = getattr(self._compressor, "should_compress_preflight", None)
        if callable(delegate):
            try:
                return bool(delegate(messages))
            except Exception as exc:
                logger.debug("LCM compressor delegate should_compress_preflight failed: %s", exc)
        return False

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        delegate = getattr(self._compressor, "should_defer_preflight_to_real_usage", None)
        if callable(delegate):
            try:
                return bool(delegate(rough_tokens))
            except Exception:
                return False
        return False

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        delegate = getattr(self._compressor, "has_content_to_compress", None)
        if callable(delegate):
            try:
                return bool(delegate(messages))
            except Exception:
                return True
        return True

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        self._ingest_messages(messages or [])
        delegate = getattr(self._compressor, "compress", None)
        if callable(delegate):
            result = delegate(messages, current_tokens=current_tokens, focus_topic=focus_topic)
            self._mirror_delegate_counters()
            return result
        self.compression_count += 1
        return messages

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [LCM_DESCRIBE_SCHEMA, LCM_GREP_SCHEMA, LCM_EXPAND_SCHEMA, LCM_INGEST_SCHEMA]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        args = args or {}
        if name == "lcm_ingest":
            messages = kwargs.get("messages") or []
            count = self._ingest_messages(messages)
            status = self.get_status()
            return json.dumps({"ok": True, "ingested_messages": count, "status": status}, ensure_ascii=False)
        if name == "lcm_describe":
            return json.dumps({"ok": True, "status": self.get_status()}, ensure_ascii=False)
        if name == "lcm_grep":
            return json.dumps(self._tool_grep(args), ensure_ascii=False)
        if name == "lcm_expand":
            return json.dumps(self._tool_expand(args), ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"Unknown LCM tool: {name}"}, ensure_ascii=False)

    def get_status(self) -> Dict[str, Any]:
        base = {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, self.last_prompt_tokens / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
            "database_path": str(self._db_path),
            "database_exists": self._db_path.exists(),
            "current_session_id": self._session_id,
        }
        try:
            stats = self._freshness_stats()
            base.update(stats)
            base["readiness"] = self._readiness_from_stats(stats)
        except Exception as exc:
            base.update({"readiness": "RED", "error": str(exc)})
        return base

    @staticmethod
    def _default_db_path() -> Path:
        try:
            from hermes_cli.config import get_hermes_home

            return Path(get_hermes_home()) / "lcm.db"
        except Exception:
            return Path.home() / ".hermes" / "lcm.db"

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        with self._db_lock:
            con = self._connect()
            try:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        store_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        source TEXT DEFAULT '',
                        role TEXT NOT NULL,
                        content TEXT,
                        tool_call_id TEXT,
                        tool_calls TEXT,
                        tool_name TEXT,
                        timestamp REAL NOT NULL,
                        token_estimate INTEGER DEFAULT 0,
                        pinned INTEGER DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, store_id);
                    CREATE INDEX IF NOT EXISTS idx_msg_session_ts ON messages(session_id, timestamp);
                    CREATE INDEX IF NOT EXISTS idx_msg_source_session ON messages(source, session_id, store_id);
                    CREATE TABLE IF NOT EXISTS summary_nodes (
                        node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        depth INTEGER NOT NULL DEFAULT 0,
                        summary TEXT NOT NULL,
                        token_count INTEGER DEFAULT 0,
                        source_token_count INTEGER DEFAULT 0,
                        source_ids TEXT NOT NULL DEFAULT '[]',
                        source_type TEXT NOT NULL DEFAULT 'messages',
                        created_at REAL NOT NULL,
                        earliest_at REAL,
                        latest_at REAL,
                        expand_hint TEXT DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_nodes_session_depth ON summary_nodes(session_id, depth, created_at);
                    CREATE INDEX IF NOT EXISTS idx_nodes_session_latest ON summary_nodes(session_id, latest_at, created_at);
                    CREATE TABLE IF NOT EXISTS lcm_lifecycle_state (
                        conversation_id TEXT PRIMARY KEY,
                        current_session_id TEXT,
                        last_finalized_session_id TEXT,
                        current_frontier_store_id INTEGER NOT NULL DEFAULT 0,
                        last_finalized_frontier_store_id INTEGER NOT NULL DEFAULT 0,
                        debt_kind TEXT,
                        debt_size_estimate INTEGER NOT NULL DEFAULT 0,
                        current_bound_at REAL,
                        last_finalized_at REAL,
                        debt_updated_at REAL,
                        last_maintenance_attempt_at REAL,
                        last_rollover_at REAL,
                        last_reset_at REAL,
                        updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                    );
                    CREATE TABLE IF NOT EXISTS lcm_migration_state (
                        step_name TEXT PRIMARY KEY,
                        completed_at REAL NOT NULL
                    );
                    """
                )
                try:
                    con.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='store_id')"
                    )
                    con.executescript(
                        """
                        CREATE TRIGGER IF NOT EXISTS msg_fts_insert
                        AFTER INSERT ON messages BEGIN
                            INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
                        END;
                        CREATE TRIGGER IF NOT EXISTS msg_fts_delete
                        AFTER DELETE ON messages BEGIN
                            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.store_id, old.content);
                        END;
                        CREATE TRIGGER IF NOT EXISTS msg_fts_update
                        AFTER UPDATE OF content ON messages BEGIN
                            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.store_id, old.content);
                            INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
                        END;
                        """
                    )
                except sqlite3.DatabaseError as exc:
                    logger.debug("LCM FTS unavailable; LIKE search fallback will be used: %s", exc)
                con.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', '4')"
                )
                con.commit()
            finally:
                con.close()

    def _record_lifecycle(self, **kwargs) -> None:
        session_id = kwargs.get("current_session_id") or self._session_id or "default"
        now = time.time()
        with self._db_lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    INSERT INTO lcm_lifecycle_state(conversation_id, current_session_id, current_bound_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        current_session_id=excluded.current_session_id,
                        current_bound_at=excluded.current_bound_at,
                        updated_at=excluded.updated_at
                    """,
                    (session_id, session_id, now, now),
                )
                con.commit()
            finally:
                con.close()

    def _ingest_messages(self, messages: List[Dict[str, Any]]) -> int:
        if not self._session_id or not messages:
            return 0
        now = time.time()
        rows = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "unknown")
            content = _content_to_text(msg.get("content"))
            tool_calls = msg.get("tool_calls")
            rows.append(
                (
                    self._session_id,
                    _SOURCE_LIVE,
                    role,
                    content,
                    str(msg.get("tool_call_id") or "") or None,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    str(msg.get("tool_name") or "") or None,
                    float(msg.get("timestamp") or now),
                    _estimate_tokens(content),
                    1 if msg.get("pinned") else 0,
                )
            )
        with self._db_lock:
            con = self._connect()
            try:
                con.execute(
                    "DELETE FROM messages WHERE session_id = ? AND source = ?",
                    (self._session_id, _SOURCE_LIVE),
                )
                con.executemany(
                    """
                    INSERT INTO messages(
                        session_id, source, role, content, tool_call_id, tool_calls,
                        tool_name, timestamp, token_estimate, pinned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                max_id = con.execute(
                    "SELECT COALESCE(MAX(store_id), 0) FROM messages WHERE session_id = ?",
                    (self._session_id,),
                ).fetchone()[0]
                con.execute(
                    """
                    INSERT INTO lcm_lifecycle_state(conversation_id, current_session_id, current_frontier_store_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        current_session_id=excluded.current_session_id,
                        current_frontier_store_id=excluded.current_frontier_store_id,
                        updated_at=excluded.updated_at
                    """,
                    (self._session_id, self._session_id, int(max_id or 0), time.time()),
                )
                con.commit()
            finally:
                con.close()
        return len(rows)

    def _freshness_stats(self) -> Dict[str, Any]:
        con = self._connect()
        try:
            total_messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            total_nodes = con.execute("SELECT COUNT(*) FROM summary_nodes").fetchone()[0]
            current_count = 0
            latest_ts = None
            if self._session_id:
                row = con.execute(
                    "SELECT COUNT(*) AS c, MAX(timestamp) AS max_ts FROM messages WHERE session_id = ?",
                    (self._session_id,),
                ).fetchone()
                current_count = int(row["c"] or 0)
                latest_ts = row["max_ts"]
            if latest_ts is None:
                latest_ts = con.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
            age = None if latest_ts is None else max(0, time.time() - float(latest_ts))
            return {
                "messages": int(total_messages or 0),
                "summary_nodes": int(total_nodes or 0),
                "current_session_messages": current_count,
                "latest_message_timestamp": latest_ts,
                "latest_message_age_seconds": age,
            }
        finally:
            con.close()

    def _readiness_from_stats(self, stats: Dict[str, Any]) -> str:
        if not self._db_path.exists():
            return "RED"
        current = int(stats.get("current_session_messages") or 0)
        age = stats.get("latest_message_age_seconds")
        if current and age is not None and age <= _GREEN_MAX_AGE_SECONDS:
            return "GREEN"
        if int(stats.get("messages") or 0) > 0:
            return "YELLOW"
        return "RED"

    def _tool_grep(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required", "results": []}
        limit = _clamp_int(args.get("limit"), 10, 1, 50)
        session_id = str(args.get("session_id") or "").strip()
        con = self._connect()
        try:
            try:
                if session_id:
                    rows = con.execute(
                        """
                        SELECT m.store_id, m.session_id, m.role, m.content, m.timestamp
                        FROM messages_fts f JOIN messages m ON m.store_id = f.rowid
                        WHERE messages_fts MATCH ? AND m.session_id = ?
                        ORDER BY rank LIMIT ?
                        """,
                        (query, session_id, limit),
                    ).fetchall()
                else:
                    rows = con.execute(
                        """
                        SELECT m.store_id, m.session_id, m.role, m.content, m.timestamp
                        FROM messages_fts f JOIN messages m ON m.store_id = f.rowid
                        WHERE messages_fts MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (query, limit),
                    ).fetchall()
            except sqlite3.DatabaseError:
                like = f"%{query}%"
                if session_id:
                    rows = con.execute(
                        "SELECT store_id, session_id, role, content, timestamp FROM messages WHERE content LIKE ? AND session_id = ? ORDER BY timestamp DESC LIMIT ?",
                        (like, session_id, limit),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT store_id, session_id, role, content, timestamp FROM messages WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
                        (like, limit),
                    ).fetchall()
            return {"ok": True, "query": query, "results": [_row_to_message_result(r) for r in rows]}
        finally:
            con.close()

    def _tool_expand(self, args: Dict[str, Any]) -> Dict[str, Any]:
        window = _clamp_int(args.get("window"), 10, 1, 50)
        con = self._connect()
        try:
            node_id = args.get("node_id")
            if node_id is not None:
                row = con.execute(
                    "SELECT * FROM summary_nodes WHERE node_id = ?",
                    (int(node_id),),
                ).fetchone()
                return {"ok": bool(row), "node": dict(row) if row else None}
            store_id = args.get("store_id")
            if store_id is not None:
                anchor = con.execute(
                    "SELECT session_id FROM messages WHERE store_id = ?",
                    (int(store_id),),
                ).fetchone()
                if not anchor:
                    return {"ok": False, "error": "store_id not found", "messages": []}
                sid = anchor["session_id"]
                rows = con.execute(
                    """
                    SELECT store_id, session_id, role, content, timestamp
                    FROM messages
                    WHERE session_id = ? AND store_id BETWEEN ? AND ?
                    ORDER BY store_id
                    """,
                    (sid, int(store_id) - window, int(store_id) + window),
                ).fetchall()
                return {"ok": True, "session_id": sid, "messages": [_row_to_message_result(r) for r in rows]}
            sid = str(args.get("session_id") or self._session_id or "").strip()
            if not sid:
                sid_row = con.execute("SELECT session_id FROM messages ORDER BY timestamp DESC LIMIT 1").fetchone()
                sid = sid_row["session_id"] if sid_row else ""
            rows = con.execute(
                """
                SELECT store_id, session_id, role, content, timestamp
                FROM messages
                WHERE session_id = ?
                ORDER BY store_id DESC LIMIT ?
                """,
                (sid, window),
            ).fetchall()
            rows = list(reversed(rows))
            return {"ok": True, "session_id": sid, "messages": [_row_to_message_result(r) for r in rows]}
        finally:
            con.close()

    def _mirror_delegate_counters(self) -> None:
        if self._compressor is None:
            return
        for attr in (
            "last_prompt_tokens",
            "last_completion_tokens",
            "last_total_tokens",
            "threshold_tokens",
            "context_length",
            "compression_count",
        ):
            try:
                setattr(self, attr, getattr(self._compressor, attr))
            except Exception:
                pass


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif item.get("type") in {"image", "image_url", "input_image"}:
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _clamp_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _row_to_message_result(row: sqlite3.Row) -> Dict[str, Any]:
    content = row["content"] or ""
    return {
        "store_id": row["store_id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "content": content,
        "timestamp": row["timestamp"],
    }


def register(ctx):
    ctx.register_context_engine(LcmContextEngine())

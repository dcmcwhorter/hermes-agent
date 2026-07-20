"""Failed start() must not leave ownerless parked MCPServerTask.run tasks.

Regression for CLI exit noise:

    Exception ignored in: <coroutine object MCPServerTask.run ...>
    ...
    File ".../mcp_tool.py", ... in _wait_for_reconnect_or_shutdown
        t.cancel()
    RuntimeError: Event loop is closed

Root cause: after the initial-connect budget is exhausted, run() parks in
``_wait_for_reconnect_or_shutdown`` and sets ``_ready`` + ``_error``. start()
used to raise ``_error`` without stopping the background task, and discover
never put the server into ``_servers``. CLI exit then closed the MCP loop
under the orphan parked coroutine; GC resumed its finally block on the dead
loop.
"""

from __future__ import annotations

import asyncio
import gc
import io
import sys

import pytest


@pytest.mark.no_isolate
def test_failed_start_reaps_parked_run_before_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_MAX_INITIAL_CONNECT_RETRIES", 0)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 60.0)

    class AlwaysDown(MCPServerTask):
        def _is_http(self):
            return False

        def _deregister_tools(self):
            self._registered_tool_names = []

        async def _run_stdio(self, config):
            raise RuntimeError("always down")

    async def _scenario():
        server = AlwaysDown("orphan")
        with pytest.raises(RuntimeError, match="always down"):
            await server.start({"command": "x"})
        assert server._task is not None
        assert server._task.done(), "start() must reap parked run() before raising"
        assert "orphan" not in mcp_tool._servers

    asyncio.run(_scenario())


@pytest.mark.no_isolate
def test_shutdown_after_failed_start_does_not_emit_closed_loop(monkeypatch, tmp_path):
    """End-to-end: failed connect + shutdown_mcp_servers must be quiet on GC."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask, shutdown_mcp_servers

    monkeypatch.setattr(mcp_tool, "_MAX_INITIAL_CONNECT_RETRIES", 0)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 60.0)

    class AlwaysDown(MCPServerTask):
        def _is_http(self):
            return False

        def _deregister_tools(self):
            self._registered_tool_names = []

        async def _run_stdio(self, config):
            raise RuntimeError("always down")

    async def _start_fail():
        server = AlwaysDown("orphan")
        with pytest.raises(RuntimeError, match="always down"):
            await server.start({"command": "x"})
        return server

    mcp_tool._ensure_mcp_loop()
    try:
        fut = asyncio.run_coroutine_threadsafe(_start_fail(), mcp_tool._mcp_loop)
        server = fut.result(timeout=5)
        assert server._task.done()

        buf = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = buf
            shutdown_mcp_servers()
            del server
            gc.collect()
        finally:
            sys.stderr = old_stderr

        noise = buf.getvalue()
        assert "Event loop is closed" not in noise, noise
        assert "MCPServerTask.run" not in noise, noise
    finally:
        # Ensure module globals are clean even if assertions fail mid-test.
        try:
            shutdown_mcp_servers()
        except Exception:
            pass
        mcp_tool._servers.clear()

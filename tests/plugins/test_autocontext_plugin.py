import json
from types import SimpleNamespace


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler, check_fn=None, **kwargs):
        self.tools[name] = {
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "kwargs": kwargs,
        }


def _json_result(text):
    payload = json.loads(text)
    if "result" in payload:
        return payload["result"]
    return payload


def test_autocontext_plugin_registers_control_plane_tools():
    from plugins.autocontext import register

    ctx = FakeContext()
    register(ctx)

    assert {
        "autocontext_status",
        "autocontext_evaluate_output",
        "autocontext_improve_output",
        "autocontext_artifact",
    }.issubset(ctx.tools)
    assert all(tool["toolset"] == "autocontext" for tool in ctx.tools.values())


def test_status_merges_cli_mcp_and_rest_surfaces(monkeypatch):
    from plugins import autocontext

    def fake_run(args, timeout=60):
        if args[:2] == ["/opt/ai-swarm/bin/autoctx", "--help"]:
            return {"ok": True, "returncode": 0, "stdout": "Usage: autoctx", "stderr": ""}
        if args[:2] == ["/opt/ai-swarm/bin/autoctx", "list"]:
            return {"ok": True, "returncode": 0, "stdout": '[{"run_id":"r1"}]', "stderr": ""}
        return {"ok": False, "returncode": 2, "stdout": "", "stderr": "bad command"}

    monkeypatch.setattr(autocontext, "_run_autoctx", fake_run)
    monkeypatch.setattr(autocontext.shutil, "which", lambda cmd: "/opt/ai-swarm/bin/autoctx" if cmd == "autoctx" else None)

    result = _json_result(autocontext._handle_status({"include_runs": True}))

    assert result["ok"] is True
    assert result["cli"]["available"] is True
    assert result["mcp"]["tool_count"] >= 10
    assert result["rest"]["base_path"] == "/api/openclaw"
    assert result["recent_runs"] == [{"run_id": "r1"}]


def test_evaluate_output_builds_json_judge_command(monkeypatch):
    from plugins import autocontext

    calls = []

    def fake_run(args, timeout=120):
        calls.append(args)
        return {"ok": True, "returncode": 0, "stdout": '{"score":0.91}', "stderr": ""}

    monkeypatch.setattr(autocontext, "_run_autoctx", fake_run)

    result = _json_result(autocontext._handle_evaluate_output({
        "task_prompt": "answer carefully",
        "output": "done",
        "rubric": "accurate",
    }))

    assert result["ok"] is True
    assert result["evaluation"] == {"score": 0.91}
    assert calls[0][:3] == ["/opt/ai-swarm/bin/autoctx", "judge", "--task-prompt"]
    assert "--json" in calls[0]


def test_improve_output_requires_prompt_output_and_rubric():
    from plugins.autocontext import _handle_improve_output

    result = _json_result(_handle_improve_output({"task_prompt": "x"}))

    assert "output" in result["error"]
    assert "rubric" in result["error"]


def test_artifact_fetch_uses_rest_when_url_configured(monkeypatch):
    from plugins import autocontext

    monkeypatch.setenv("AUTOCONTEXT_REST_URL", "http://autoctx.test")
    monkeypatch.setattr(
        autocontext,
        "_http_json",
        lambda url, timeout=30: {"url": url, "artifact": {"id": "a1"}},
    )

    result = _json_result(autocontext._handle_artifact({"artifact_id": "a1"}))

    assert result["ok"] is True
    assert result["artifact"]["url"] == "http://autoctx.test/api/openclaw/artifacts/a1"

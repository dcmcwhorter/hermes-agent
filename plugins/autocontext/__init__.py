"""AutoContext control-plane plugin.

The plugin exposes a small, stable Hermes tool surface for AutoContext while
leaving the richer MCP server as the typed control plane.  CLI execution is used
for one-shot judge/improve workflows; REST is used opportunistically when an
AutoContext HTTP server URL is configured.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from tools.registry import tool_error, tool_result

_AUTOCTX_DEFAULT = "/opt/ai-swarm/bin/autoctx"
_REST_BASE_PATH = "/api/openclaw"
_MCP_TOOLS = [
    "skill_advertise",
    "skill_runtime_health",
    "get_queue_status",
    "create_agent_task",
    "evaluate_output",
    "run_improvement_loop",
    "list_artifacts",
    "fetch_artifact",
    "export_skill",
    "export_package",
    "list_scenarios",
    "evaluate_strategy",
    "create_monitor_tool",
    "sandbox_create",
    "trigger_distillation",
]

STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "include_runs": {
            "type": "boolean",
            "default": False,
            "description": "Also call `autoctx list --json` for recent run evidence.",
        }
    },
    "additionalProperties": False,
}

EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_prompt": {"type": "string"},
        "output": {"type": "string"},
        "rubric": {"type": "string"},
    },
    "required": ["task_prompt", "output", "rubric"],
    "additionalProperties": False,
}

IMPROVE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_prompt": {"type": "string"},
        "output": {"type": "string"},
        "rubric": {"type": "string"},
        "rounds": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
    },
    "required": ["task_prompt", "output", "rubric"],
    "additionalProperties": False,
}

ARTIFACT_SCHEMA = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "description": "Artifact id to fetch through REST."},
        "scenario": {"type": "string", "description": "Optional scenario filter when listing."},
        "artifact_type": {"type": "string", "description": "Optional artifact type filter when listing."},
    },
    "additionalProperties": False,
}


def _autoctx_bin() -> str:
    if Path(_AUTOCTX_DEFAULT).exists():
        return _AUTOCTX_DEFAULT
    return shutil.which("autoctx") or _AUTOCTX_DEFAULT


def _check_autoctx_available() -> bool:
    path = _autoctx_bin()
    return bool(path and Path(path).exists())


def _run_autoctx(args: List[str], timeout: int = 120) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def _parse_json_or_text(text: str) -> Any:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except Exception:
        return cleaned


def _required(args: Dict[str, Any], *names: str) -> str | None:
    missing = [name for name in names if not str(args.get(name) or "").strip()]
    return ", ".join(missing) if missing else None


def _rest_url(path: str = "") -> str | None:
    base = os.environ.get("AUTOCONTEXT_REST_URL") or os.environ.get("AUTOCTX_REST_URL")
    if not base:
        return None
    return base.rstrip("/") + _REST_BASE_PATH + path


def _http_json(url: str, timeout: int = 30) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return _parse_json_or_text(data)


def _queue_status_from_sqlite() -> Dict[str, Any]:
    db_path = os.environ.get("AUTOCONTEXT_DB_PATH")
    if not db_path:
        home = Path(os.environ.get("AI_SWARM_AGENT_HOME") or os.environ.get("HOME") or str(Path.home()))
        db_path = str(home / "runs" / "autocontext.sqlite3")
    path = Path(db_path)
    if not path.exists():
        return {"database_path": str(path), "available": False}
    try:
        con = sqlite3.connect(str(path))
        try:
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
            if "task_queue" not in tables:
                return {"database_path": str(path), "available": True, "task_queue": None}
            rows = con.execute("select status, count(*) from task_queue group by status").fetchall()
            return {"database_path": str(path), "available": True, "task_queue": dict(rows)}
        finally:
            con.close()
    except Exception as exc:
        return {"database_path": str(path), "available": False, "error": str(exc)}


def _mcp_surface() -> Dict[str, Any]:
    return {
        "configured": True,
        "server_name": "autoctx-mcp",
        "tool_count": len(_MCP_TOOLS),
        "tools": list(_MCP_TOOLS),
        "note": "Use native MCP tools for scenario evaluation, monitors, sandbox, artifacts, package import/export, and distillation.",
    }


def _handle_status(args: Dict[str, Any], **kw) -> str:
    path = _autoctx_bin()
    help_result = _run_autoctx([path, "--help"], timeout=30) if Path(path).exists() else {
        "ok": False,
        "returncode": None,
        "stdout": "",
        "stderr": "autoctx binary not found",
    }
    payload: Dict[str, Any] = {
        "ok": True,
        "cli": {
            "path": path,
            "available": bool(help_result.get("ok")),
            "returncode": help_result.get("returncode"),
            "stderr": help_result.get("stderr"),
        },
        "mcp": _mcp_surface(),
        "rest": {
            "configured": _rest_url() is not None,
            "base_url": os.environ.get("AUTOCONTEXT_REST_URL") or os.environ.get("AUTOCTX_REST_URL"),
            "base_path": _REST_BASE_PATH,
        },
        "queue": _queue_status_from_sqlite(),
    }
    if args.get("include_runs"):
        runs = _run_autoctx([path, "list", "--json"], timeout=60)
        payload["recent_runs_command"] = runs
        payload["recent_runs"] = _parse_json_or_text(runs.get("stdout", "")) if runs.get("ok") else None
    return tool_result(payload)


def _handle_evaluate_output(args: Dict[str, Any], **kw) -> str:
    missing = _required(args, "task_prompt", "output", "rubric")
    if missing:
        return tool_error(f"Missing required field(s): {missing}")
    cmd = [
        _autoctx_bin(),
        "judge",
        "--task-prompt",
        str(args["task_prompt"]),
        "--output",
        str(args["output"]),
        "--rubric",
        str(args["rubric"]),
        "--json",
    ]
    result = _run_autoctx(cmd, timeout=120)
    if not result.get("ok"):
        return tool_error(f"autoctx judge failed: {result.get('stderr') or result.get('stdout')}")
    return tool_result({"ok": True, "evaluation": _parse_json_or_text(result.get("stdout", "")), "command": cmd})


def _handle_improve_output(args: Dict[str, Any], **kw) -> str:
    missing = _required(args, "task_prompt", "output", "rubric")
    if missing:
        return tool_error(f"Missing required field(s): {missing}")
    rounds = max(1, min(10, int(args.get("rounds") or 3)))
    cmd = [
        _autoctx_bin(),
        "improve",
        "--task-prompt",
        str(args["task_prompt"]),
        "--output",
        str(args["output"]),
        "--rubric",
        str(args["rubric"]),
        "--rounds",
        str(rounds),
        "--json",
    ]
    result = _run_autoctx(cmd, timeout=300)
    if not result.get("ok"):
        return tool_error(f"autoctx improve failed: {result.get('stderr') or result.get('stdout')}")
    return tool_result({"ok": True, "improvement": _parse_json_or_text(result.get("stdout", "")), "command": cmd})


def _handle_artifact(args: Dict[str, Any], **kw) -> str:
    base = _rest_url()
    if not base:
        return tool_result({
            "ok": False,
            "rest_configured": False,
            "message": "Set AUTOCONTEXT_REST_URL to fetch/list artifacts through REST, or use the AutoContext MCP artifact tools.",
            "mcp_tools": ["list_artifacts", "fetch_artifact", "publish_artifact", "export_package", "export_skill"],
        })
    artifact_id = str(args.get("artifact_id") or "").strip()
    if artifact_id:
        url = base + "/artifacts/" + urllib.parse.quote(artifact_id, safe="")
        return tool_result({"ok": True, "artifact": _http_json(url)})
    params = {}
    if args.get("scenario"):
        params["scenario"] = str(args["scenario"])
    if args.get("artifact_type"):
        params["artifact_type"] = str(args["artifact_type"])
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    return tool_result({"ok": True, "artifacts": _http_json(base + "/artifacts" + query)})


def register(ctx) -> None:
    ctx.register_tool(
        name="autocontext_status",
        toolset="autocontext",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        check_fn=_check_autoctx_available,
        emoji="🧪",
    )
    ctx.register_tool(
        name="autocontext_evaluate_output",
        toolset="autocontext",
        schema=EVALUATE_SCHEMA,
        handler=_handle_evaluate_output,
        check_fn=_check_autoctx_available,
        emoji="⚖️",
    )
    ctx.register_tool(
        name="autocontext_improve_output",
        toolset="autocontext",
        schema=IMPROVE_SCHEMA,
        handler=_handle_improve_output,
        check_fn=_check_autoctx_available,
        emoji="🔁",
    )
    ctx.register_tool(
        name="autocontext_artifact",
        toolset="autocontext",
        schema=ARTIFACT_SCHEMA,
        handler=_handle_artifact,
        check_fn=_check_autoctx_available,
        emoji="📦",
    )

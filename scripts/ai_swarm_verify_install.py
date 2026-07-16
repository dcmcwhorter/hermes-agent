#!/usr/bin/env python3
"""Verify the AI-Swarm canonical Hermes source/profile/runtime boundary."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CANONICAL_SOURCE = Path("/opt/ai-swarm/hermes-agent")
CANONICAL_VENV = Path.home() / ".hermes" / "venvs" / "hermes-ai-swarm"
ACTIVE_BIN = Path.home() / ".local" / "bin" / "hermes"
LEGACY_SOURCE = Path.home() / ".hermes" / "hermes-agent"
UPSTREAM_BASE = "e0240d7bf7ce0d665417d45de0bfa9a65cb0ab48"
REQUIRED_SUBJECTS = {
    "feat(memory): expose Hindsight curation tools",
    "fix(memory): include Hindsight embedded runtime dependency",
    "Add LCM context engine plugin",
    "feat(context): ingest LCM messages on turn finalization",
    "Add AutoContext control-plane plugin",
    "fix(web): avoid Firecrawl as default retrieval path",
    "fix: detect launchd user-domain gateway supervision",
    "fix: quiet macos dashboard browser launch",
    "fix: harden managed inbox delivery",
    "fix: align managed task lookup with gateway schema",
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(CANONICAL_SOURCE), *args], text=True).strip()


def verify(require_active: bool, require_legacy_removed: bool) -> dict:
    errors: list[str] = []
    if not (CANONICAL_SOURCE / ".git").exists():
        errors.append("canonical source is not a Git checkout")
    if not (CANONICAL_VENV / "bin" / "hermes").exists():
        errors.append("canonical venv console script is missing")

    head = git("rev-parse", "HEAD") if not errors else None
    branch = git("branch", "--show-current") if not errors else None
    subjects = set(git("log", "--format=%s", f"{UPSTREAM_BASE}..HEAD").splitlines()) if not errors else set()
    missing_subjects = sorted(REQUIRED_SUBJECTS - subjects)
    if missing_subjects:
        errors.append(f"missing carried customization subjects: {missing_subjects}")
    if branch != "ai-swarm-main":
        errors.append(f"unexpected branch: {branch}")

    source_runtime_artifacts = [
        str(path.relative_to(CANONICAL_SOURCE))
        for pattern in ("runs/*.sqlite3", "tools/*.bak.*")
        for path in CANONICAL_SOURCE.glob(pattern)
    ]
    if source_runtime_artifacts:
        errors.append(f"runtime artifacts found in source: {source_runtime_artifacts}")

    active_target = os.path.realpath(ACTIVE_BIN) if ACTIVE_BIN.exists() else None
    expected_target = str(CANONICAL_VENV / "bin" / "hermes")
    if require_active and active_target != expected_target:
        errors.append(f"active binary resolves to {active_target}, expected {expected_target}")
    if require_legacy_removed and LEGACY_SOURCE.exists():
        errors.append(f"legacy source checkout still exists: {LEGACY_SOURCE}")

    profile_markers = {
        "config": str(Path.home() / ".hermes" / "config.yaml"),
        "state_db": str(Path.home() / ".hermes" / "state.db"),
        "auth": str(Path.home() / ".hermes" / "auth.json"),
    }
    missing_profile_markers = [name for name, path in profile_markers.items() if not Path(path).exists()]
    if missing_profile_markers:
        errors.append(f"missing profile markers: {missing_profile_markers}")

    return {
        "status": "passed" if not errors else "failed",
        "canonical_source": str(CANONICAL_SOURCE),
        "canonical_venv": str(CANONICAL_VENV),
        "branch": branch,
        "head": head,
        "upstream_base": UPSTREAM_BASE,
        "carried_customization_subjects": sorted(REQUIRED_SUBJECTS),
        "missing_customization_subjects": missing_subjects,
        "active_binary": str(ACTIVE_BIN),
        "active_target": active_target,
        "legacy_source": str(LEGACY_SOURCE),
        "legacy_source_present": LEGACY_SOURCE.exists(),
        "profile_markers": profile_markers,
        "source_runtime_artifacts": source_runtime_artifacts,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-active", action="store_true")
    parser.add_argument("--require-legacy-removed", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.require_active, args.require_legacy_removed)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())

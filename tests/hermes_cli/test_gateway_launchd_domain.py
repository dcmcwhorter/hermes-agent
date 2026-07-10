"""Regression tests for macOS launchd domain probing."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway_cli


def test_probe_launchd_service_running_uses_print_when_list_is_empty(tmp_path, monkeypatch):
    """macOS service-user domains can have an empty `launchctl list` result.

    The real Porto host showed `launchctl list ai.hermes.gateway` exiting 0
    with no PID, while `launchctl print user/<uid>/ai.hermes.gateway` showed
    `state = running` and `pid = ...`. Status/snapshot must trust the resolved
    domain print output so a supervised user-domain LaunchAgent is not reported
    as a detached fallback process.
    """
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("plist", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            assert cmd[2] == "user/1001/ai.hermes.gateway"
            return SimpleNamespace(returncode=0, stdout="state = running\npid = 43790\n", stderr="")
        if cmd[:2] == ["launchctl", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "user/1001")
    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

    assert gateway_cli._probe_launchd_service_running() is True
    assert calls == [["launchctl", "print", "user/1001/ai.hermes.gateway"]]


def test_probe_launchd_service_running_falls_back_to_legacy_list(tmp_path, monkeypatch):
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("plist", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return SimpleNamespace(returncode=125, stdout="", stderr="unsupported")
        if cmd[:2] == ["launchctl", "list"]:
            return SimpleNamespace(returncode=0, stdout='"PID" = 12345;\n', stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/1001")
    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

    assert gateway_cli._probe_launchd_service_running() is True
    assert calls == [
        ["launchctl", "print", "gui/1001/ai.hermes.gateway"],
        ["launchctl", "list", "ai.hermes.gateway"],
    ]

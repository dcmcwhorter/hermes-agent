"""Dashboard browser-open behavior."""

from __future__ import annotations

import pytest

from hermes_cli import web_server


class _ImmediateThread:
    def __init__(self, target, daemon=None):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _run_open_inline(monkeypatch):
    monkeypatch.setattr(web_server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(web_server.threading, "Thread", _ImmediateThread)


def test_dashboard_open_uses_macos_open_url(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return object()

    _run_open_inline(monkeypatch)
    monkeypatch.setattr(web_server.sys, "platform", "darwin")
    monkeypatch.setattr(web_server.os, "geteuid", lambda: 501)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)

    web_server._maybe_open_browser("0.0.0.0", 9119, True, "")

    assert calls
    cmd, kwargs = calls[0]
    assert cmd == ["/usr/bin/open", "-u", "http://127.0.0.1:9119"]
    assert kwargs["stdin"] is web_server.subprocess.DEVNULL
    assert kwargs["stdout"] is web_server.subprocess.DEVNULL
    assert kwargs["stderr"] is web_server.subprocess.DEVNULL


def test_dashboard_open_targets_sudo_login_user_on_macos(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return object()

    _run_open_inline(monkeypatch)
    monkeypatch.setattr(web_server.sys, "platform", "darwin")
    monkeypatch.setattr(web_server.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "porto")
    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)

    web_server._maybe_open_browser("localhost", 9119, True, "default profile")

    assert calls
    cmd, _kwargs = calls[0]
    assert cmd == [
        "/usr/bin/sudo",
        "-u",
        "porto",
        "/usr/bin/open",
        "-u",
        "http://localhost:9119/?profile=default%20profile",
    ]


def test_dashboard_runner_treats_keyboard_interrupt_as_clean_stop():
    web_server._run_dashboard_until_interrupt(
        lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    )


def test_dashboard_runner_propagates_non_interrupt_errors():
    with pytest.raises(RuntimeError):
        web_server._run_dashboard_until_interrupt(
            lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )

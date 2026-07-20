"""MoA must not crash with OverflowError after ~30s of waiting.

Root cause: ``moa://local`` was classified as a local endpoint because the
host ``local`` has no dots. That set the non-stream stale timeout to
``float('inf')``. The 30s wait-notice path then did ``int(deadline)`` and
raised ``OverflowError: cannot convert float infinity to integer``, which
the conversation loop treated as a failed MoA API call.
"""

from __future__ import annotations

import math

import pytest

from agent.chat_completion_helpers import _format_timeout_seconds
from agent.model_metadata import is_local_endpoint


def test_moa_local_is_not_a_local_endpoint():
    assert is_local_endpoint("moa://local") is False
    assert is_local_endpoint("http://localhost:11434") is True
    assert is_local_endpoint("http://127.0.0.1:8080") is True


def test_format_timeout_seconds_handles_infinity():
    assert _format_timeout_seconds(float("inf")) == "disabled"
    assert _format_timeout_seconds(math.inf) == "disabled"
    assert _format_timeout_seconds(90.0) == "90s"
    assert _format_timeout_seconds(None) == "disabled"
    # Must never raise — this is the exact failure mode from MoA turns.
    _format_timeout_seconds(float("inf"))


def test_int_inf_still_raises_so_helpers_remain_necessary():
    with pytest.raises(OverflowError):
        int(float("inf"))

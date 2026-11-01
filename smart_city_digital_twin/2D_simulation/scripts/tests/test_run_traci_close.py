"""Unit tests for run_traci._close_quietly (no SUMO / TraCI connection needed)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_traci import _close_quietly  # noqa: E402


class _FakeTraci:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        if self.exc is not None:
            raise self.exc


def test_closes_normally(capsys):
    t = _FakeTraci()
    _close_quietly(t)
    assert t.closed == 1
    assert capsys.readouterr().out == ""


def test_desynced_connection_keyerror_is_swallowed(capsys):
    # What Ctrl-C mid-request produces: close() reads stale bytes -> KeyError: 64
    _close_quietly(_FakeTraci(KeyError(64)))
    assert "KeyError" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [OSError("broken pipe"), RuntimeError("connection closed by SUMO")])
def test_other_close_errors_are_swallowed(exc):
    _close_quietly(_FakeTraci(exc))


def test_second_ctrl_c_still_stops():
    # KeyboardInterrupt is a BaseException, not an Exception: a second Ctrl-C during
    # shutdown must still interrupt rather than be ignored.
    with pytest.raises(KeyboardInterrupt):
        _close_quietly(_FakeTraci(KeyboardInterrupt()))

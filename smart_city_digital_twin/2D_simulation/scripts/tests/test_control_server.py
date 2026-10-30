"""
Unit tests for the scenario control server — fake Popen, no real process/SUMO.

    python -m pytest tests/test_control_server.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from control_server import (  # noqa: E402
    PRESETS,
    SimRunner,
    build_command,
    build_roadblock_command,
    scenario_list,
)


class FakeProc:
    def __init__(self, cmd, cwd=None):
        self.cmd = cmd
        self.cwd = cwd
        self.pid = 4321
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _runner():
    created = []

    def fake_popen(cmd, cwd=None):
        p = FakeProc(cmd, cwd)
        created.append(p)
        return p

    return SimRunner(popen=fake_popen), created


def test_scenario_list_shape():
    items = scenario_list()
    assert {i["id"] for i in items} == set(PRESETS)
    assert all(i["label"] and i["description"] for i in items)


def test_build_command_known_and_unknown():
    cmd = build_command("crash_arterial", python="python3", script_dir="/x")
    assert cmd[0] == "python3"
    assert cmd[1].endswith("run_traci.py")
    assert "--scenario-id" in cmd and "crash_arterial" in cmd
    assert "--close-edge" in cmd
    with pytest.raises(KeyError):
        build_command("does_not_exist")


def test_start_status_stop_lifecycle():
    runner, created = _runner()
    assert runner.running() is False

    r = runner.start("baseline_am")
    assert r["ok"] is True and r["scenario"] == "baseline_am"
    assert runner.running() is True
    st = runner.status()
    assert st["running"] is True and st["scenario"] == "baseline_am" and st["pid"] == 4321
    # spawned with cwd = 2D_simulation (parent of scripts/)
    assert created[0].cwd.endswith("2D_simulation")

    r2 = runner.stop()
    assert r2["stopped"] is True
    assert runner.running() is False
    assert runner.status()["running"] is False


def test_start_refused_when_already_running():
    runner, _ = _runner()
    runner.start("baseline_am")
    r = runner.start("crash_arterial")
    assert r["ok"] is False and "already running" in r["error"]


def test_start_unknown_scenario_rejected():
    runner, created = _runner()
    r = runner.start("nope")
    assert r["ok"] is False and not created          # nothing spawned


def test_build_roadblock_command_uses_attached_close_edge():
    # a leading-dash (reverse-direction) edge must ride in the --close-edge=<...> form;
    # the block starts at the run's jump-to (23400) so it's active immediately
    cmd = build_roadblock_command("-1015728520#1", duration=600, script_dir="/x")
    assert cmd[1].endswith("run_traci.py")
    assert "--close-edge=-1015728520#1@23400:24000" in cmd
    assert "--scenario-id" in cmd and "roadblock" in cmd
    # no bare "--close-edge" token that argparse could mis-bind
    assert "--close-edge" not in cmd


def test_build_roadblock_open_ended_without_duration():
    cmd = build_roadblock_command("4891423", script_dir="/x")
    assert "--close-edge=4891423@23400" in cmd       # from run start, no ':end' when no duration


def test_roadblock_rejects_bad_edge():
    import pytest as _pt
    with _pt.raises(ValueError):
        build_roadblock_command("bad edge@evil")       # space + '@'
    runner, created = _runner()
    r = runner.start_roadblock("has space")
    assert r["ok"] is False and not created            # nothing spawned


def test_start_roadblock_lifecycle():
    runner, created = _runner()
    r = runner.start_roadblock("770109405#0", duration=600)
    assert r["ok"] is True and r["edge"] == "770109405#0"
    assert runner.running() is True
    assert "roadblock" in runner.status()["scenario"]


def test_start_also_spawns_writer_and_stop_kills_both():
    runner, created = _runner()                 # start_writer defaults True
    runner.start("baseline_am")
    # two procs spawned: run_traci then metrics_writer
    assert len(created) == 2
    assert created[0].cmd[1].endswith("run_traci.py")
    assert created[1].cmd[1].endswith("metrics_writer.py")
    assert runner.status()["writer"] is True
    runner.stop()
    assert created[1].terminated is True        # writer terminated on stop
    assert runner.running() is False


def test_no_writer_option_spawns_only_the_sim():
    created = []

    def fake_popen(cmd, cwd=None):
        p = FakeProc(cmd, cwd); created.append(p); return p

    runner = SimRunner(popen=fake_popen, start_writer=False)
    runner.start("baseline_am")
    assert len(created) == 1                     # just run_traci, no writer
    assert runner.status()["writer"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

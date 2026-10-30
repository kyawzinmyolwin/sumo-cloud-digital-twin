"""
Unit tests for the cloud control Lambda's pure logic + the token authorizer.

These live in the simulation test suite (so CI runs them) but import the handler
modules from infra/functions/ by path. Only the pure, no-AWS helpers are exercised;
the SSM/EC2 glue needs a live account.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


control = _load("cloud_control_handler", "infra/functions/traffic_control/handler.py")
authz = _load("cloud_control_authorizer", "infra/functions/control_authorizer/handler.py")


# --- traffic_control pure helpers --------------------------------------------
def test_scenario_list_shape():
    items = control.scenario_list()
    assert {i["id"] for i in items} == set(control.PRESETS)
    assert all(i["label"] and i["description"] for i in items)


def test_args_for_scenario_known_and_unknown():
    args = control.args_for_scenario("crash_arterial")
    assert "--scenario-id" in args and "crash_arterial" in args
    # closure rides in the --close-edge=<...> form (leading-dash edges stay safe)
    assert any(a.startswith("--close-edge=") for a in args)
    assert "--close-edge" not in args       # no bare token argparse could mis-bind
    # cloud args must NOT repeat the emit flags (the service adds --emit-target)
    assert "--emit" not in args and "--emit-target" not in args and "--emit-host" not in args
    with pytest.raises(KeyError):
        control.args_for_scenario("nope")


def test_roadblock_args_valid_and_window():
    args = control.roadblock_args("-1015728520#1", duration=600)
    assert "--close-edge=-1015728520#1@23400:24000" in args
    assert "--scenario-id" in args and "roadblock" in args


def test_roadblock_args_open_ended_without_duration():
    args = control.roadblock_args("4891423")
    assert "--close-edge=4891423@23400" in args
    assert all(":" not in a.split("@", 1)[1] for a in args if a.startswith("--close-edge="))


def test_roadblock_rejects_bad_edge():
    with pytest.raises(ValueError):
        control.roadblock_args("bad edge@evil")   # space + '@'


def test_scenario_args_string_is_shell_safe_join():
    s = control.scenario_args_string(control.args_for_scenario("baseline_am"))
    assert "--scenario-id baseline_am" in s
    for bad in (";", "|", "`", "$(", "&&", "\n"):
        assert bad not in s


# --- token authorizer ---------------------------------------------------------
def _event(auth):
    return {"headers": {"authorization": auth}} if auth is not None else {"headers": {}}


def test_authorizer_accepts_matching_bearer_token(monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", "s3cret-token")
    assert authz.lambda_handler(_event("Bearer s3cret-token"), None) == {"isAuthorized": True}
    # raw token (no Bearer prefix) also accepted
    assert authz.lambda_handler(_event("s3cret-token"), None) == {"isAuthorized": True}


def test_authorizer_rejects_wrong_or_missing(monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", "s3cret-token")
    assert authz.lambda_handler(_event("Bearer wrong"), None) == {"isAuthorized": False}
    assert authz.lambda_handler(_event(None), None) == {"isAuthorized": False}


def test_authorizer_denies_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("CONTROL_TOKEN", raising=False)
    assert authz.lambda_handler(_event("Bearer anything"), None) == {"isAuthorized": False}

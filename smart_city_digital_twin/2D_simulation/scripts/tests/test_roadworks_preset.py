"""
Every preset that passes ``--incident-file`` must point at a file that actually
exists (resolved from the run's cwd, 2D_simulation/), parses, and only names edges
that are in the network. Guards the old roadworks bug: the local preset pointed at a
missing path and the example file held REPLACE_EDGE_ID placeholders, so the
"Roadworks" Start button did nothing.

Covers both preset registries: control_server.py (local) and the cloud
traffic-control Lambda.

    python -m pytest tests/test_roadworks_preset.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from control_server import PRESETS as LOCAL_PRESETS  # noqa: E402
from incidents import parse_incidents  # noqa: E402

SIM_DIR = Path(__file__).resolve().parents[2]          # 2D_simulation/ (the run cwd)
REPO = Path(__file__).resolve().parents[4]
NET = SIM_DIR / "data/output/network/Christchurch_Central_City_main_streets.net.xml"


def _cloud_presets():
    spec = importlib.util.spec_from_file_location(
        "cloud_control_handler_rw", REPO / "infra/functions/traffic_control/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PRESETS


def _incident_files(presets):
    out = []
    for name, preset in presets.items():
        args = preset["args"]
        for i, a in enumerate(args):
            if a == "--incident-file":
                out.append((name, args[i + 1]))
    return out


CASES = ([("local",) + c for c in _incident_files(LOCAL_PRESETS)]
         + [("cloud",) + c for c in _incident_files(_cloud_presets())])


@pytest.fixture(scope="module")
def net_edges():
    if not NET.exists():
        pytest.skip("network file not present")
    return {e.get("id") for e in ET.parse(NET).getroot().iter("edge")
            if e.get("function") != "internal"}


def test_roadworks_uses_an_incident_file_in_both_registries():
    assert ("local", "roadworks", "scripts/scenarios/roadworks.json") in CASES
    assert ("cloud", "roadworks", "scripts/scenarios/roadworks.json") in CASES


@pytest.mark.parametrize("where,preset,relpath", CASES)
def test_preset_incident_file_is_real(where, preset, relpath, net_edges):
    path = SIM_DIR / relpath
    assert path.is_file(), f"{where} preset {preset!r}: {relpath} not found from 2D_simulation/"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "REPLACE" not in json.dumps(raw), f"{relpath} still has placeholder edge ids"
    incidents = parse_incidents(raw)
    assert incidents, f"{relpath} has no incidents"
    for inc in incidents:
        assert inc.target in net_edges, f"{relpath}: edge {inc.target!r} not in the network"

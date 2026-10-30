"""Put ``2D_simulation/scripts`` on ``sys.path`` for shared pipeline imports."""
from __future__ import annotations

import sys
from pathlib import Path

SIM_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SIM_ROOT / "scripts"
PROJECT_ROOT = SIM_ROOT.parent
DATA_DIR = SIM_ROOT / "data"
DATA_INPUT_DIR = DATA_DIR / "input"
DATA_OUTPUT_DIR = DATA_DIR / "output"
NETWORK_DIR = DATA_OUTPUT_DIR / "network"
DEMAND_DIR = DATA_OUTPUT_DIR / "demand"

for path in (SCRIPTS_DIR, SIM_ROOT):
    s = str(path)
    if s not in sys.path:
        sys.path.insert(0, s)

"""
Per-tick traffic metrics — COPY of scripts/metrics.py, shipped with the Lambda.

Keep this in sync with smart_city_digital_twin/2D_simulation/scripts/metrics.py.
It is duplicated (not imported) only so the Lambda zip is self-contained; the code
is identical and deliberately dependency-free.
"""
from __future__ import annotations

from typing import Any

SPEED_SLOW = 3.0
SPEED_FREE = 8.0


def compute_tick_metrics(snapshot: dict[str, Any], *, slow_speed: float = SPEED_SLOW) -> dict[str, Any]:
    vehicles = snapshot.get("vehicles") or []
    speeds = [
        float(v["speed"])
        for v in vehicles
        if v.get("speed") is not None and _is_number(v.get("speed"))
    ]
    count = len(vehicles)
    n_speeds = len(speeds)
    avg_speed = sum(speeds) / n_speeds if n_speeds else 0.0
    slow = sum(1 for s in speeds if s < slow_speed)
    congestion_index = slow / n_speeds if n_speeds else 0.0
    return {
        "simId": snapshot.get("simId", "unknown"),
        "scenarioId": snapshot.get("scenarioId", snapshot.get("simId", "unknown")),
        "simTime": snapshot.get("simTime"),
        "vehicleCount": count,
        "avgSpeed": round(avg_speed, 3),
        "stoppedCount": slow,
        "movingCount": n_speeds - slow,
        "congestionIndex": round(congestion_index, 3),
    }


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)

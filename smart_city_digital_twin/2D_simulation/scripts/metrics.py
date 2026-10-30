"""
Per-tick traffic metrics, computed from one emitter snapshot.

``compute_tick_metrics`` is a pure function: dict in, dict out, no I/O and no
third-party imports. That means it is unit-testable with plain dicts and no
InfluxDB / SUMO / network — and the exact same function becomes the body of the
future ``traffic-metrics`` Lambda (see infra/). metrics_writer.py wraps it with
a WebSocket client and an InfluxDB writer.
"""
from __future__ import annotations

from typing import Any

# Speed thresholds in m/s, kept consistent with the dashboard's colour bands
# (intersection_map.html: red < 3, amber 3-8, green >= 8).
SPEED_SLOW = 3.0   # at/below this a vehicle counts as congested
SPEED_FREE = 8.0   # at/above this a vehicle is free-flowing


def compute_tick_metrics(snapshot: dict[str, Any], *, slow_speed: float = SPEED_SLOW) -> dict[str, Any]:
    """Aggregate one emitter snapshot into scalar metrics.

    Parameters
    ----------
    snapshot:
        A dict in the emitter schema: ``{simId, simTime, vehicles: [{speed, ...}]}``.
    slow_speed:
        Speed (m/s) below which a vehicle is treated as congested.

    Returns
    -------
    dict of scalar metrics suitable for one InfluxDB point.
    """
    vehicles = snapshot.get("vehicles") or []

    # Guard against missing/None/non-numeric speeds rather than trusting the feed.
    speeds = [
        float(v["speed"])
        for v in vehicles
        if v.get("speed") is not None and _is_number(v.get("speed"))
    ]

    count = len(vehicles)
    n_speeds = len(speeds)
    avg_speed = sum(speeds) / n_speeds if n_speeds else 0.0
    slow = sum(1 for s in speeds if s < slow_speed)
    # Fraction of vehicles that are congested — a 0..1 index, easy to threshold.
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

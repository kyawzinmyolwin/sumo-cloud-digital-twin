# Decision-support twin — Increment 2 design note

Scope for Increment 2: turn the twin from "watch a scenario" into "compare a scenario
against a baseline and read the impact." This note fixes **which scenarios** we model
first, **what "success" means** for each, and **how** metrics are captured and compared —
so the build (scenario-tagged metrics → compare view) has a target before code.

## Principle: fair comparison
A scenario run and its baseline must be **identical apart from the one changed variable**.
Same network, same demand file, same `--jump-to`/`--end`, same emit interval — the *only*
difference is the injected incident. Otherwise a measured delta can't be attributed to the
incident. Each run is a named, reproducible unit:

    scenario = { scenario_id, sumocfg, incident spec (or none for baseline), fixed sim params }

The baseline is just the scenario with **no incident** (or an empty incident file).

## The first three scenarios (v1)
All on the calibrated street-net model, AM peak (`--jump-to 23400`).

| id | What changes | Incident | Compared against |
|----|--------------|----------|------------------|
| `baseline_am` | nothing | none | — (reference) |
| `crash_arterial` | a major arterial is blocked | `--close-edge <arterial>@23700:24300` | `baseline_am` |
| `roadworks_corridor` | a corridor's speed limit drops | `set_speed` on the corridor edges (e.g. 8 m/s) | `baseline_am` |
| `event_surge` *(optional)* | extra trips toward a venue | `add_vehicles` from an entry edge → venue edge | `baseline_am` |

Pick the arterial with `list_edges.py --sort length` (multi-lane, real traffic — e.g.
Moorhouse Ave / Fitzgerald Ave), not a single-lane side street.

## Success criteria (what a "result" looks like)
A scenario is meaningfully modelled if, versus the baseline over the same window, it shows a
**measurable, attributable delta** in the tick metrics (`compute_tick_metrics`: average speed,
vehicle count, congestion index):

- **crash_arterial** — network average speed drops and/or the congestion index rises during
  the closure window; the metric recovers after the incident reverts. Visible on the map as a
  queue on the closed corridor.
- **roadworks_corridor** — a smaller, sustained average-speed reduction for the duration of
  the limit; no hard queue.
- **event_surge** — vehicle count rises and average speed falls near the venue during the
  surge window.

"Success" for Increment 2 itself = the **compare view** shows the two runs' metric lines
side by side and the divergence is legible without reading raw numbers.

## Metrics capture & tagging (build item a)
Today `metrics_writer.py` tags points only with `simId`. Add a **`scenario_id` tag** so runs
are distinguishable in InfluxDB:

- `run_traci.py` gains a `--scenario-id <name>` (defaults to `--sim-id` when omitted); it is
  carried in the emitted snapshot / used by `metrics_writer.py` as an InfluxDB tag alongside
  the existing fields.
- No new measurement — reuse the existing metric fields; just add the tag. This keeps the
  cloud metrics Lambda and local writer paths unchanged apart from the tag.

## Compare view (build item b)
- Dashboard queries the replay/history endpoint for **two `scenario_id`s** over the same time
  range and overlays each metric (avg speed, count, congestion) as two lines on one chart.
- Reuses the existing "History (from InfluxDB)" panel + `?replay=` endpoint; adds a second
  series keyed by scenario_id and a small scenario picker.

## Out of scope for Increment 2 (deferred)
- Live click-to-inject from the map (Increment 3).
- Rerouting *around* a closure (vehicles currently queue; rerouting is a later increment).
- Added-delay / queue-length / affected-area reporting (Increment 4).

## Honest caveats (for the report)
- Incident scenarios are **synthetic** — openly framed as "what-if", not observed events.
- The baseline demand is Miovision-calibrated; the *incident* is an analyst's hypothesis.
- Comparisons are only fair when the two runs differ solely by the injected variable — the
  reproducible-scenario unit above is what guarantees that.

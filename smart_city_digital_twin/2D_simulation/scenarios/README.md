# Test scenarios

Small, self-contained SUMO scenarios for exercising the emitter → dashboard →
cloud pipeline without the full ~92,000-vehicle demand.

Two kinds live here:
- **Light test runs** (`low_traffic`, `low_traffic_flow`) — a handful of vehicles
  for quickly checking the pipeline / watching the dashboard.
- **Demand variants** (`morning_peak`, `offpeak`) — realistic slices of the real
  demand at different times of day, for the scenario-comparison feature.

## Demand variants — morning peak vs off-peak

Real vehicles from the demand, filtered to a time-of-day window (routes copied
verbatim, valid by construction). They contrast traffic levels for scenario
comparison:

| Scenario | Window | Vehicles | Config |
|----------|--------|----------|--------|
| Morning peak | 08:00–08:30 | ~6,200 | `morning_peak.sumocfg` |
| Off-peak (midday) | 11:00–11:30 | ~3,200 | `offpeak.sumocfg` |

Run each with a `--jump-to` matching its window start, so real-time playback
starts at the window (otherwise the emit loop idles from the default 06:30 jump).
Tag each run with a distinct `--sim-id` so downstream (InfluxDB / dashboard) can
tell them apart:

```bash
# Morning peak (08:00 start), sped up 10x so 30 min of sim plays in ~3 min:
python scripts/run_traci.py --sumocfg scenarios/morning_peak.sumocfg \
    --no-gui --emit --real-time --speed 10 --jump-to 28800 --sim-id am-peak

# Off-peak (11:00 start):
python scripts/run_traci.py --sumocfg scenarios/offpeak.sumocfg \
    --no-gui --emit --real-time --speed 10 --jump-to 39600 --sim-id offpeak
```

> **Note on picking a *date*:** you can select the time of day (above), but not a
> calendar date. The demand is a single *representative weekday* composited from
> Miovision counts that were surveyed across 45 different dates (2018–2025), each
> covering only a handful of the 93 intersections — so no single past date has
> network-wide coverage to reconstruct. See `IMPLEMENTATION.md` §"Selecting a date".

## low_traffic — 8 vehicles

`low_traffic.sumocfg` + `low_traffic.rou.xml`: the same real Christchurch network,
but only the first 8 vehicles from `data/output/demand/traffic_trips.routed.rou.xml`
(all depart 06:30 / sim time 23400). Their routes are copied verbatim from the
routed demand, so they are valid on the network by construction.

Regenerate with a different count by re-running the extractor (change `N`):

```python
import xml.etree.ElementTree as ET
N = 8
src = "data/output/demand/traffic_trips.routed.rou.xml"
out = ['<?xml version="1.0" encoding="UTF-8"?>', '<routes>',
       '    <vType id="car" vClass="passenger" />',
       '    <vType id="bus" vClass="bus" color="0,122,135" />']
n = 0
for _, el in ET.iterparse(src, events=("end",)):
    if el.tag == "vehicle":
        out.append("    " + ET.tostring(el, encoding="unicode").strip())
        n += 1
        if n >= N:
            break
        el.clear()
out.append("</routes>")
open("scenarios/low_traffic.rou.xml", "w").write("\n".join(out) + "\n")
```

### Run it (from `2D_simulation/`)

One command via the `--sumocfg` override on `run_traci.py`:

```bash
python scripts/run_traci.py --sumocfg scenarios/low_traffic.sumocfg --no-gui --emit
```

Or attach to a SUMO you launch yourself (no `--sumocfg` needed):

```bash
sumo -c scenarios/low_traffic.sumocfg --remote-port 8813 --step-length 1 --start &
python scripts/run_traci.py --connect --emit
```

The 8 vehicles appear at the 06:30 jump and run until their routes finish, so the
sim goes idle and the loop stops on its own — a short, deterministic run. Because
the load is tiny, the emit loop runs flat out and this finishes in ~a second of
wall-clock. It's ideal for a quick/CI check, not for watching live — for that use
the flow scenario below with `--real-time`.

## low_traffic_flow — a steady few, for a few minutes

`low_traffic_flow.sumocfg` + `low_traffic_flow.rou.xml`: instead of one batch,
three `<flow>`s inject a steady trickle (~4–6 vehicles at a time) along 3 real
routes for 6 min of sim time (06:30–06:36). The network stays populated, so the
dashboard keeps showing movement instead of draining after the first batch.

Pair it with **real-time pacing** so playback tracks the wall clock (a light
scenario otherwise flies past in a second):

```bash
python scripts/run_traci.py --sumocfg scenarios/low_traffic_flow.sumocfg \
    --no-gui --emit --real-time
```

That runs for ~6 minutes of real time. `--speed 2` plays at twice real time (~3
min); `--speed 0.5` at half. Stop early any time with Ctrl-C. `--real-time` only
affects emit mode and is off by default, so nothing else changes.

# Test-scenario support — engineering documentation

A full record of how the `scenarios/` test-run capability was built: the thought
process and decisions, every file touched, and an annotated walkthrough of the
code with what each part does.

- **Branch:** `claude/next-step-nun1nw` (PR #19)
- **Commits:** `2bbf6cc` (8-vehicle scenario + `--sumocfg`), `f3edd00` (`--real-time`
  + flow scenario), `6c42b11`/this (docs)
- **User-facing usage:** see `README.md`. This file is the *why and how*.

---

## 1. Thought process

### 1.1 The problem

The project's demand file (`data/output/demand/traffic_trips.routed.rou.xml`) holds
**~92,000 vehicles**. That's the right load for a realistic run, but wrong for
day-to-day work on the pipeline: to check that the emitter serialises correctly,
that the dashboard draws markers, or that the cloud path ingests without flooding
API Gateway, you want **a handful of vehicles you can reason about**, started with
one command, reproducibly.

So the goal became: *a small, deterministic scenario that reuses the real network
but a tiny demand, runnable without disturbing the production config.*

### 1.2 Options considered

| Option | Verdict | Reason |
|--------|---------|--------|
| SUMO `--scale 0.01` on the full demand | Rejected as the primary path | Can't pin an exact count; density varies over the day; and `run_traci.py` doesn't pass `--scale` through, so it needs `--connect` anyway. Kept only as a quick one-off. |
| Hand-write a `.rou.xml` with invented trips | Rejected | I'd have to pick real edge IDs and hope they connect; a bad edge means a routing failure at run time. |
| Subset a time window out of the routed file | Rejected | Fragile parsing; still thousands of vehicles unless combined with more filtering. |
| **Extract the first N whole `<vehicle>` blocks from the routed file** | **Chosen** | Each vehicle already carries its full `<route edges=…>`, routed on *this* network, so the subset is valid **by construction** — no routing step, no invalid-edge risk. Deterministic. |

### 1.3 How to run an alternate config

`run_traci.py` launches SUMO with a fixed `SUMOCFG` (imported from `sim_pipeline`).
Two ways to point it at a test config:

- Its existing `--connect` mode ignores `SUMOCFG` and attaches to a SUMO you start
  yourself — works today, no code change.
- A small additive `--sumocfg PATH` override for a **one-command** run.

I added `--sumocfg` because a one-liner is what you actually want for repeated test
runs and for CI, and it's a two-line, backward-compatible change (falls back to
`SUMOCFG` when absent). This matches how `--emit` was originally added *alongside*
the existing loop rather than replacing it.

### 1.4 The second problem: "I can't see it"

After shipping the 8-vehicle scenario the user reported the emit run ended before
they could watch. Diagnosis found **two independent causes**, both inherent to a
light scenario, neither a bug:

1. **The network drains and the loop exits.** `_should_stop()` returns `True` once
   `traci.simulation.getMinExpectedNumber() <= 0`. Eight vehicles departing together
   at 06:30 finish in ~1–2 min of *sim* time → network empty → loop ends.
2. **No real-time pacing.** `_run_emitting` steps flat out (`await asyncio.sleep(0)`).
   With ~no vehicles there's ~no per-step computation, so minutes of sim time pass in
   ~1 s of wall-clock. (Insight: the full 92k run only *felt* watchable because its
   heavy per-step cost incidentally paced it.)

### 1.5 Options for "watchable for a few minutes"

- **Keep vehicles present:** one-shot vehicles drain; **`<flow>`** with a period keeps
  a bounded few vehicles on the map across a multi-minute window. Chosen.
- **Pace playback:**
  - `sumo-gui --delay` — rejected: under TraCI control the *client* drives stepping,
    so the GUI delay doesn't throttle the loop.
  - Fixed `sleep(step_length)` per step — rejected: drifts if a step runs long, and
    ignores `--speed`.
  - **Anchor wall-clock to elapsed sim time and sleep the difference** — chosen:
    accurate regardless of step count/length, self-correcting, and `--speed`-aware.

---

## 2. Files touched

| File | New / Modified | Purpose |
|------|----------------|---------|
| `scripts/run_traci.py` | Modified (additive) | 3 new CLI flags + real-time pacing in `_run_emitting`; config-selection line in `main()`. |
| `scenarios/low_traffic.rou.xml` | New (generated) | 8 real vehicles, all depart 06:30. Deterministic quick/CI check. |
| `scenarios/low_traffic.sumocfg` | New | Config: real net + the 8-vehicle demand, short window. |
| `scenarios/low_traffic_flow.rou.xml` | New (generated) | 3 flows → steady ~4–6 vehicles for 6 min sim time. Watchable run. |
| `scenarios/low_traffic_flow.sumocfg` | New | Config for the flow demand. |
| `scenarios/morning_peak.{sumocfg,rou.xml}` | New (generated) | ~6,200 real vehicles, 08:00–08:30 AM peak. Demand variant. |
| `scenarios/offpeak.{sumocfg,rou.xml}` | New (generated) | ~3,200 real vehicles, 11:00–11:30 midday. Demand variant. |
| `scenarios/README.md` | New | User-facing how-to-run. |
| `scenarios/IMPLEMENTATION.md` | New | This document. |

No production files were modified: `sim_pipeline.py`, the network, and the routed
demand are untouched. The scenarios only *read* the committed network + one sampled
subset of the demand.

---

## 3. Code walkthrough

### 3.1 Generating the 8-vehicle route file

Run once to produce `low_traffic.rou.xml`. It streams the 30 MB routed file and
copies out the first 8 vehicles verbatim:

```python
import xml.etree.ElementTree as ET
N = 8
src = "data/output/demand/traffic_trips.routed.rou.xml"
out = ['<?xml version="1.0" encoding="UTF-8"?>',
       '<routes>',
       '    <vType id="car" vClass="passenger" />',
       '    <vType id="bus" vClass="bus" color="0,122,135" />']
count = 0
for ev, el in ET.iterparse(src, events=("end",)):   # (a) stream, don't load 30 MB
    if el.tag == "vehicle":                          # (b) each <vehicle> has its route inline
        out.append("    " + ET.tostring(el, encoding="unicode").strip())  # (c) copy verbatim
        count += 1
        if count >= N:
            break                                    # (d) stop after N
        el.clear()                                   # (e) free processed nodes
out.append("</routes>")
open("scenarios/low_traffic.rou.xml", "w").write("\n".join(out) + "\n")
```

- **(a)** `iterparse` with `events=("end",)` processes the file as a stream, so we
  never hold the whole 30 MB tree in memory.
- **(b)** we react on the *end* of each `<vehicle>` element — at that point its child
  `<route edges=…>` is fully parsed.
- **(c)** `ET.tostring` re-serialises the element exactly as-is, so the route (real,
  already valid on this net) is preserved unchanged.
- **(d)** stop once we have N.
- **(e)** `el.clear()` releases each processed element so memory stays flat while
  streaming.

The two `<vType>` lines (`car`, `bus`) are declared up front because the copied
`<vehicle type="car">` entries reference them.

### 3.2 `low_traffic.sumocfg` — the deterministic scenario

```xml
<sumoConfiguration ...>
    <input>
        <net-file value="../data/output/network/Christchurch_Central_City_main_streets.net.xml" />
        <route-files value="low_traffic.rou.xml" />
        <additional-files value="../data/output/network/Christchurch_Central_City_main_streets.add.xml" />
    </input>
    <time>
        <begin value="23400" /><end value="24000" /><step-length value="1" />
    </time>
</sumoConfiguration>
```

- Paths are **relative to the config file** (`scenarios/`), hence the `../data`
  prefix to reach the shared network.
- `net-file` reuses the real network; `route-files` points at our tiny demand;
  `additional-files` is the polygon layer (cosmetic — keeps `sumo-gui` looking right).
- `begin=23400` is 06:30 (where the vehicles depart); a short `end` bounds the run.

### 3.3 `low_traffic_flow.rou.xml` — the watchable scenario

Generated from the 8-vehicle file by lifting 3 real routes and wrapping each in a
`<flow>`:

```xml
<routes>
    <vType id="car" vClass="passenger" />
    <route id="r0" edges="…" />          <!-- 3 real routes reused from the 8-vehicle file -->
    <route id="r1" edges="…" />
    <route id="r2" edges="…" />
    <flow id="f0" type="car" route="r0" begin="23400" end="23760" period="45" />
    <flow id="f1" type="car" route="r1" begin="23400" end="23760" period="45" />
    <flow id="f2" type="car" route="r2" begin="23400" end="23760" period="45" />
</routes>
```

- A `<flow>` inserts one vehicle every `period` seconds from `begin` to `end`. Here:
  `period=45 s` over a `23400→23760` (6 min) window → ~8 vehicles per flow spread
  across the run.
- With three flows and each trip lasting ~1–2 min, roughly **4–6 vehicles are on the
  network at any instant** — a "few", never a flood — and the network never fully
  drains until `end`, so the dashboard keeps moving.
- Routes are reused from the 8-vehicle extraction, so they're valid by construction.

### 3.4 `run_traci.py` — the three CLI flags

Added in `build_parser()`, after `--no-gui` and before the emitter flags:

```python
p.add_argument(
    "--sumocfg", default=None, metavar="PATH",
    help="Override the SUMO config to launch (launch mode only; ignored with "
         "--connect). Use e.g. scenarios/low_traffic.sumocfg for a small test run.",
)
p.add_argument(
    "--real-time", action="store_true",
    help="Pace the emitting loop to wall-clock time so a light scenario is "
         "watchable (otherwise it runs as fast as possible). Emit mode only.",
)
p.add_argument(
    "--speed", type=float, default=1.0, metavar="X",
    help="Playback multiplier for --real-time (2 = twice real time; default: 1).",
)
```

- `--sumocfg` defaults to `None` → argparse exposes it as `args.sumocfg`.
- `--real-time` is a boolean flag (`store_true`), off by default.
- `--speed` is a float multiplier, only meaningful with `--real-time`.

### 3.5 `run_traci.py` — using the config override in `main()`

The launch command's config argument changed from a constant to a conditional:

```python
cmd = [
    binary,
    "-c",
    str(args.sumocfg) if args.sumocfg else str(SUMOCFG),   # <-- override or default
    "-b", str(args.begin),
    ...
]
```

When `--sumocfg` is given, that config is launched; otherwise the original project
`SUMOCFG` is used — so every existing invocation behaves exactly as before. This
branch is only reached in launch mode; `--connect` returns earlier and never builds
`cmd`, which is why the help text says "ignored with --connect".

### 3.6 `run_traci.py` — real-time pacing in `_run_emitting`

Before the loop, anchor the two clocks:

```python
speed = args.speed if args.speed and args.speed > 0 else 1.0  # guard against 0/negative
sim_start = traci.simulation.getTime()   # sim time at the first emitted step
wall_start = time.monotonic()            # real time at the same instant
```

- `speed` is validated (a non-positive value would divide badly), falling back to 1.
- `sim_start` / `wall_start` capture the two clocks at the same moment so we can
  compare how far they've diverged as the loop runs. `time.monotonic()` is used (not
  `time.time()`) because it never goes backwards on clock adjustments.

At the end of each iteration, replace the old unconditional yield with pacing:

```python
lag = 0.0
if args.real_time:
    sim_elapsed = traci.simulation.getTime() - sim_start   # sim seconds advanced
    lag = (sim_elapsed / speed) - (time.monotonic() - wall_start)
await asyncio.sleep(lag if lag > 0 else 0)
```

- `sim_elapsed / speed` is *how much wall-clock time this much sim time should have
  taken* at the chosen speed. Subtracting the wall time actually elapsed gives `lag`:
  how far **ahead** of schedule we are.
- If `lag > 0` (we're ahead — the usual case for a light sim) we `await asyncio.sleep(lag)`,
  which both slows playback to real time **and** yields to the event loop so the
  WebSocket server / cloud forwarder can flush frames.
- If `lag <= 0` (behind schedule) or `--real-time` is off, `lag` stays `0` and the
  call degrades to `await asyncio.sleep(0)` — exactly the original event-loop yield,
  so non-real-time behaviour is unchanged.
- Anchoring to *elapsed sim time* (rather than sleeping a fixed amount per step) means
  the pace stays correct no matter how many steps run or how long any one step takes.

No new imports were needed — `time` and `asyncio` were already imported at the top of
the module.

---

### 3.7 Demand variants — morning peak vs off-peak

Same extraction technique as §3.1, but filtered by *departure time* instead of
count, to produce realistic slices for scenario comparison. The generator streams
the routed demand and keeps vehicles whose `depart` falls in `[start, end)`:

```python
for _, el in ET.iterparse(src, events=("end",)):
    if el.tag == "vehicle":
        d = float(el.get("depart"))
        if start <= d < end:                 # keep only this window's vehicles
            out.append("    " + ET.tostring(el, encoding="unicode").strip())
        if d >= end:                         # depart-ordered file: stop past the window
            el.clear(); break
        el.clear()
```

Windows were chosen from the demand's actual hourly histogram:

| Scenario | Window | Vehicles | Rationale |
|----------|--------|----------|-----------|
| `morning_peak` | 08:00–08:30 (28800–30600) | ~6,200 | Busiest AM hour (07–09 carries ~12k/hr). |
| `offpeak` | 11:00–11:30 (39600–41400) | ~3,200 | Midday inter-peak, ~half the peak — clean contrast. |

Each `.sumocfg` sets `begin`/`end` to its window and points at its filtered route
file. Because `run_traci.py` fast-forwards to `--jump-to` (default 06:30) *before*
the real-time-paced emit loop, each variant must be run with a `--jump-to` matching
its window start — otherwise the emit loop would idle in real time from 06:30 to the
window. The README documents the exact commands, including a distinct `--sim-id` per
variant so InfluxDB/dashboard can keep the two runs apart (this is the data backing
the scenario-comparison extension feature).

## Selecting a date

A recurring question: can we run "a particular date"? Findings from the source
data (`data/output/demand/traffic_18MAY2026_130842.csv`):

- **Time of day: yes** — via `--jump-to` / `--end`, or the demand variants above.
- **Calendar date (current or past): no.** The simulation has no date axis (it runs
  seconds-of-day, 0–86400), and the counts can't reconstruct one: the file holds
  **45 distinct `survey_date`s spanning 2018–2025**, and each date covers only a
  handful of the **93 intersections** (Miovision surveys one intersection at a time).
  So no single calendar date has network-wide coverage. The committed demand is a
  *representative weekday* composited from all of them onto a common time-of-day.
- A `--survey-date` filter on the demand builder is technically feasible (the parser
  already reads `survey_date`/`date_time`) but would yield a near-empty network for
  any one date — not worth it. The meaningful axis is **scenario/time-of-day**, not
  calendar date, consistent with the project's "historical data, not live" decision.

## 4. Testing / verification

SUMO is **not installed in the build environment**, so no live run was possible
there. What was verified statically:

- `python -m py_compile scripts/run_traci.py` — compiles.
- All four scenario files parse as well-formed XML. *(This caught a real bug: the
  first draft put the literal string `--real-time` inside an XML comment, and `--` is
  illegal inside `<!-- -->`. Reworded the comments.)*
- The three flags are present and wired (`args.sumocfg` / `args.real_time` / `args.speed`).
- The `../data/...` net and additional-file paths resolve from `scenarios/`.

**Still to confirm on a machine with SUMO** (e.g. the Vagrant VM, from a venv with
`pyproj` + `websockets`): a real `--real-time` run of the flow scenario streams for
~6 minutes with a few vehicles visible on the dashboard throughout.

---

## 5. How to run (quick reference)

```bash
# From 2D_simulation/

# Deterministic quick/CI check (finishes in ~1 s):
python scripts/run_traci.py --sumocfg scenarios/low_traffic.sumocfg --no-gui --emit

# Watchable ~6-minute live run:
python scripts/run_traci.py --sumocfg scenarios/low_traffic_flow.sumocfg \
    --no-gui --emit --real-time          # --speed 2 → ~3 min; Ctrl-C to stop early
```

Point `intersection_map.html` at `ws://localhost:8765` to watch the markers.

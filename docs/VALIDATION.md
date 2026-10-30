# Model validation — simulation vs real counts

This is the **validation** companion to the unit tests (which are *verification* — "did we
build it right?"). It checks that the running baseline simulation reproduces the real
Christchurch City Council / Miovision turning-movement counts that generated its demand —
i.e. "did we build a faithful model of the real data?".

## What is compared

- **Observed** — the real counts, as loaded onto the network. The demand file
  `data/output/demand/traffic_trips.rou.xml` encodes every surveyed movement as a SUMO
  `<flow>` with a vehicle `number` on a specific `from` edge in a 15-minute slot. Summing
  those numbers per `from` edge over a window gives the observed demand placed on each edge.
- **Simulated** — per-edge throughput from a SUMO `edgeData` output over the same window
  (`departed` = vehicles the microsimulation actually inserted on the edge).
- **Metric** — the **GEH statistic**, the standard traffic-engineering measure for
  modelled-vs-observed volumes:

  GEH = sqrt( 2·(M − C)² / (M + C) ),  where M = modelled (simulated), C = observed count.

  | GEH | Interpretation |
  |---|---|
  | < 5 | good match (accept) |
  | 5–10 | caution — investigate |
  | > 10 | poor — likely error |

  The usual calibration target is **GEH < 5 for ≥ 85 % of links**.

> **Honest framing.** Because the demand was *generated from* these counts, this validates
> that the end-to-end pipeline (demand build → routing → microsimulation, including vehicle
> insertion under congestion) faithfully reproduces the surveyed volumes at the surveyed
> locations. It is a calibration/consistency check, not an independent field validation —
> state it that way in the report.

## How to produce the numbers (on a machine with SUMO)

The AM-peak window 06:30–07:30 = sim seconds **23400–27000** (matches the `baseline_am`
preset and gives GEH its hourly basis).

```bash
cd smart_city_digital_twin/2D_simulation

# 1. Run the baseline headless with the validation edgeData collector attached.
sumo -c Christchurch_Central_City_main_streets.sumocfg \
     --additional-files data/output/network/Christchurch_Central_City_main_streets.add.xml,scripts/validation/edgedata_am.add.xml \
     --end 27000
# -> writes edgedata_am.out.xml in the working directory

# 2. Compare simulated throughput against the real counts.
python scripts/validate_against_counts.py --sim edgedata_am.out.xml --begin 23400 --end 27000
```

This prints a Markdown table (per edge: observed, simulated, diff, %, GEH) plus a summary
line (links compared, total observed vs simulated, % of links with GEH < 5, mean |error|) —
ready to paste into the report's Testing & Validation section.

### Preliminary check without SUMO (loading fidelity only)

`--routed` compares against the routed demand instead of a live run. It confirms routing
placed the counted vehicles on the right edges but does **not** capture congestion/insertion
effects, so it is not the figure to cite:

```bash
python scripts/validate_against_counts.py \
    --routed data/output/demand/traffic_trips.routed.rou.xml --begin 23400 --end 27000
```

On the current demand this reports 24 links, total observed 7159 vs loaded 7072 (−1.2 %),
GEH < 5 for 100 % of links — i.e. the demand is loaded essentially intact; the `--sim` run
is what measures whether the *running* simulation still matches once congestion is in play.

## Result (current model, AM peak 06:30–07:30)

From a headless `--sim` run over the 24 busiest origin links:

| Metric | Value |
|---|---|
| Links compared | 24 |
| GEH < 5 | **88 %** (21/24) — meets the ≥ 85 % target |
| GEH < 10 | 92 % (22/24) |
| Mean absolute error | 8 % |
| Total observed vs simulated | 7159 vs 5596 (**−21.8 %**) |

The baseline reproduces the real counts on the large majority of links to within a few
percent. The total deficit is **concentrated at one saturated bottleneck**: Durham Street
South southbound at the Durham South / Moorhouse intersection (`-139811056#1`, I2859 — per
`scripts/edge_to_intersection.py`) inserts only 215 of 1353
demanded vehicles, accounting for ~73 % of the whole shortfall; the next worst
(`-988781906`) accounts for most of the remainder. This is **insertion backlog under peak
congestion** — SUMO holds vehicles at the entry edge when the downstream network is
saturated — not lost demand. Re-running with a longer window (e.g. `--end 30000`) lets the
backlog clear and the deficit shrink; keep the 1-hour window for the GEH figure itself,
since GEH is defined on hourly flows.

## Options

- `--begin` / `--end` — window in sim seconds (default 23400–27000).
- `--field` — edgeData field to sum (default `departed`; use `entered` for total throughput
  including through-traffic — note that over-counts vs origin-only observed demand).
- `--min-count` — ignore low-volume edges (GEH is noisy on tiny counts).
- `--top N` — show only the busiest N edges in the table.

The scoring/parse helpers (`geh`, `compare`, `summarise`, and the parsers) are pure and
unit-tested in `scripts/tests/test_validate_against_counts.py`.

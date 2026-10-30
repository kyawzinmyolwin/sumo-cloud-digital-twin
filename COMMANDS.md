# Commands reference — Christchurch Digital Twin

A per-command cheat-sheet for the scripts you actually run. For end-to-end
*workflows* (local → cloud, A–E) see [`running_guide.md`](running_guide.md); this
file is the "what does each flag do" reference.

**Conventions**
- Run Python scripts from `smart_city_digital_twin/2D_simulation/` (paths below assume that cwd).
- Use the project venv: `pip install -r requirements.txt` (the emitter needs `pyproj` + `websockets`).
- `SUMO_HOME` must be set; `run_traci.py --no-gui` for headless (VM).
- Sim time: **23400 s = 06:30** (when the calibrated demand starts).

---

## 1. Run the simulation — `run_traci.py`

Steps SUMO via TraCI; optionally emits vehicle state and/or injects incidents.

```bash
python scripts/run_traci.py --no-gui --jump-to 23400 --end 23430    # plain headless run
```

| Flag | Meaning |
|------|---------|
| `--no-gui` / `--gui` | Headless `sumo` vs `sumo-gui` (default gui) |
| `--sumocfg PATH` | Use a scenario config instead of the default calibrated sim |
| `--jump-to SEC` | Advance to this sim time before the loop (default 23400 = 06:30; use `0` for counts→CBD scenarios) |
| `--end SEC` | Stop at this sim time (default: run until no vehicles remain) |
| `--chunk SEC` | Advance the jump in chunks (default 3600) |
| `--connect` | Attach to an already-running sumo-gui instead of launching |
| **Emitter** | |
| `--emit` | Broadcast per-step vehicle JSON over WebSocket |
| `--emit-host H` | WS bind host (default `localhost`; use `0.0.0.0` to reach it from another machine, e.g. Mac→VM) |
| `--emit-port P` | WS port (default 8765) |
| `--emit-interval N` | Emit every Nth step (default 1) |
| `--emit-target WSS_URL` | Also forward each snapshot to a cloud WebSocket (API Gateway) |
| `--sim-id ID` | Scenario id echoed to clients / used as a metrics tag |
| `--scenario-id NAME` | Decision-support scenario name tagged onto stored metrics (`scenario_id`) for the compare view; defaults to `--sim-id` |
| **Pacing** | |
| `--real-time` | Pace to wall-clock so a light run is watchable (else runs flat-out) |
| `--speed X` | Playback multiplier for `--real-time` (e.g. `10` = 10× real time) |
| **Incidents** (see §2) | `--incident-file PATH`, `--close-edge EDGE@START[:END]` |

Typical live run (emitter reachable from the Mac browser):
```bash
python scripts/run_traci.py --no-gui --emit --emit-host 0.0.0.0 --real-time --speed 10 --jump-to 23400
```

---

## 2. Incidents (what-if) — `--close-edge` / `--incident-file`

Perturb the running sim. Quick closure inline, or a multi-incident JSON spec.

```bash
# close an edge from 06:35 to 06:45
python scripts/run_traci.py --no-gui --emit --emit-host 0.0.0.0 --real-time --speed 10 \
  --jump-to 23400 --close-edge <EDGE_ID>@23700:24300

# close from 06:30 for the rest of the run (omit the :END)
... --close-edge <EDGE_ID>@23400

# multiple / typed incidents from a spec file
... --incident-file scripts/scenarios/incident_example.json
```

Incident spec (`scenarios/incident_example.json`) — one JSON list, times in sim seconds:
```json
{"incidents": [
  {"type": "close_edge", "edge": "<id>", "start": 23700, "end": 24300},
  {"type": "close_lane", "lane": "<id>_0", "start": 23400},
  {"type": "set_speed",  "edge": "<id>", "speed": 4.0, "start": 23400, "end": 25200},
  {"type": "add_vehicles", "target": "<from-edge>", "to": "<to-edge>", "count": 100, "start": 23400, "end": 24600}
]}
```
- `close_edge` / `close_lane` — road closure / crash (lane speed → crawl).
- `set_speed` — roadworks / temporary limit.
- `add_vehicles` — event demand surge, injected across the window.

---

## 3. Find an edge id to use in an incident

```bash
python scripts/list_edges.py --name "Moorhouse"        # edges of a street, with id/length/lanes
python scripts/list_edges.py --sort length --limit 10  # biggest arterials (best for a visible block)
python scripts/edges_geojson.py                        # -> data/output/network/edges.geojson
python scripts/edges_geojson.py --check                # validate the projection (~5 m)
```
Then on the dashboard, tick **"show roads"** and click a road to read its id (see §6).

---

## 4. Scenarios from CCC counts (the real-data baseline)

Build a CBD scenario from historical counts (baseline path, not the headline feature).

```bash
# which survey dates the ETL has ingested
python scripts/fetch_counts_for_date.py --list-dates --bucket $ETL_DATA_BUCKET

# fetch one date's counts -> CSV
python scripts/fetch_counts_for_date.py --date 2025-08-13 --output scenarios/counts_2025-08-13.csv --bucket $ETL_DATA_BUCKET

# build a CBD scenario (from a CSV, or straight from the ETL by date)
python scripts/make_cbd_scenario.py --name am_2025 --traffic-csv scenarios/counts_2025-08-13.csv \
  --start-time 07:00 --end-time 09:00 --timeline calendar
python scripts/make_cbd_scenario.py --name am_2025 --from-etl-date 2025-08-13 \
  --start-time 07:00 --end-time 09:00 --etl-bucket $ETL_DATA_BUCKET

# play it (demand starts at t=0)
python scripts/run_traci.py --sumocfg scenarios/am_2025.sumocfg --no-gui --emit --emit-host 0.0.0.0 --real-time --jump-to 0
```

`make_cbd_scenario.py` flags: `--name`, `--traffic-csv` | `--from-etl-date`/`--etl-bucket`,
`--survey-date`, `--start-time`/`--end-time` (HH:MM window), `--period AM|PM|ALL`,
`--timeline stacked|calendar`, `--min-count`.

> Data reality: CCC surveys are peak-only (~07:00–09:00 and ~13:00–18:00) across 45 survey
> days (2016–2025) — no arbitrary date/mid-morning window.

Rebuilding the intersection-graph net (only if `christchurch_intersections.net.xml` is missing):
```bash
python scripts/fill_intersection_direction_neighbours.py data/output/intersection_geo.csv   # rewrites the CSV in place — back it up
python scripts/sumo_network_from_geo.py --csv data/output/intersection_geo.csv
```

---

## 5. Metrics + storage (InfluxDB)

```bash
# local InfluxDB (from repo root or wherever docker-compose.yml lives)
docker compose up -d                    # (Colima on old macOS: colima start first)

# stream metrics from the emitter into InfluxDB
python scripts/metrics_writer.py --ws-url ws://localhost:8765
```
`metrics_writer.py`: `--ws-url`, `--influx-url`/`--org`/`--bucket`/`--token` (default to
`$INFLUXDB_*` env / localhost), `--measurement`, `--sample-every N`, `--max-points N`,
`--connect-timeout`.

```bash
# local read endpoint the dashboard's history/compare charts query
python scripts/replay_server.py         # serves http://localhost:8788/metrics
```
`replay_server.py`: `--influx-url`/`--org`/`--bucket`/`--token`, `--port`.
Query params: `field`, `start`, `stop`, `every`, and `scenario` (filter by `scenario_id`,
used by the compare view). e.g. `GET /metrics?field=avgSpeed&start=-1h&scenario=crash_arterial`.

### Compare two scenarios (Increment 2)
Run a baseline and an incident with the *same* window, tagged distinctly, then overlay them:
```bash
# baseline (no incident)
python scripts/run_traci.py --no-gui --emit --emit-host 0.0.0.0 --real-time \
  --jump-to 23400 --end 25200 --scenario-id baseline_am
# incident (same window, one edge closed)
python scripts/run_traci.py --no-gui --emit --emit-host 0.0.0.0 --real-time \
  --jump-to 23400 --end 25200 --scenario-id crash_arterial --close-edge <id>@23700:24300
```
Run `metrics_writer.py` alongside each so both land in InfluxDB tagged by `scenario_id`.
Then in the dashboard's **Compare scenarios** panel, enter the two names (e.g. `baseline_am`
and `crash_arterial`), pick a field/range, and Compare — the two series overlay on one chart.
The **Impact report** button (same panel) produces a baseline-vs-incident table — avg speed,
congestion, stopped (mean & peak), vehicles — each with a directional Δ (speed drop /
congestion rise shown red). Narrow the range to the incident window for a sharper figure.

---

## 5b. Start/stop scenarios from the browser — `control_server.py`

For a non-IT user: run this on the machine with SUMO so the dashboard's **Scenario
control** panel can start/stop the sim with a click (no command line).
```bash
python scripts/control_server.py            # serves http://localhost:8799
```
It starts **named presets** (baseline / crash / roadworks — editable in the `PRESETS`
registry) and a **road block on a chosen edge**. Endpoints: `GET /scenarios`,
`GET /status`, `POST /start?scenario=<id>`, `POST /start_roadblock?edge=<id>[&duration=<s>]`,
`POST /stop`. The edge id is validated (letters/digits/`_.#:` + optional leading `-`) and
passed as `--close-edge=<id>` so it can't become a stray flag; no arbitrary args reach the shell.
In the dashboard's **Scenario control** panel, click a road on the map (its id fills the box)
and hit **Start road block** — no command line.
The dashboard reads it via `?control=<url>` (default `http://localhost:8799`); after
Start it auto-connects the live feed. Local/VM use only — it spawns processes, so run
it on a trusted machine, and the hosted CloudFront page can't reach a localhost server.

## 6. Dashboard — `intersection_map.html`

Serve from **`2D_simulation/`** (so `../data` paths resolve), open `/scripts/…`:
```bash
cd smart_city_digital_twin/2D_simulation
python3 -m http.server 8000 --bind 0.0.0.0
```
Open (Mac browser, ports 8000/8765 forwarded from the VM):
```
http://localhost:8000/scripts/intersection_map.html?edges=/data/output/network/edges.geojson
```

URL params:
| Param | Purpose |
|-------|---------|
| `?ws=` | Live emitter WebSocket URL (default `ws://localhost:8765`) |
| `?replay=` | History/compare read endpoint (default `http://localhost:8788/metrics`) |
| `?counts=` | CCC counts query endpoint (ETL) |
| `?edges=` | Road-overlay GeoJSON (from `edges_geojson.py`) |
| `?control=` | Scenario control server (start/stop the sim from the browser; default `http://localhost:8799`) |

In the page: **Connect** (live vehicles) · **show roads** then click a road for its edge id ·
**Compare scenarios** panel overlays two `scenario_id` runs (baseline vs incident) ·
**Congestion alerts** panel flags slow segments on the live feed.

**Live road-block toggle (local ws only):** while Connected to a local emitter
(`run_traci.py --emit`, reachable in the browser), click a road → **Close live** closes
that edge in the *running* sim immediately; the button becomes **Reopen road (resume)**,
which lifts the closure without stopping the sim (traffic recovers as the queue drains).
Sent over the WebSocket; the cloud API Gateway path does not read these commands.

---

## 7. Cloud / deploy (from `infra/`, on the VM)

```bash
./infra/dashboard_url.sh                 # print the wired dashboard URL from terraform outputs
./infra/deploy_dashboard.sh              # upload the HTML + invalidate CloudFront + print URL
./infra/deploy_dashboard.sh --rebuild-names   # also refresh the counts id→name index
```
Cloud sim host + full stack: see [`running_guide.md`](running_guide.md) §D–E and
[`infra/README.md`](infra/README.md). Always tear the sim host down after a demo
(`terraform apply -var 'sim_host_enabled=false'`).

---

## 8. Tests

```bash
cd smart_city_digital_twin/2D_simulation/scripts
python3 -m pytest -q                     # all: emitter, metrics, incidents, ETL, bridge
python3 -m pytest tests/test_incidents.py -q
python3 -m pytest etl/tests/ -q
```

---

## Not covered here (internal build pipeline — do not modify)
`sim_pipeline.py`, `build_simulation.py`, `create_network.py`, `create_demand.py`,
`calibrate_*`, `traffic_counts_parser.py`, and the other one-time network/demand build
scripts. See `smart_city_digital_twin/2D_simulation/README.md`.

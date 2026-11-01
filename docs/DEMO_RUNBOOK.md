# Demo Runbook — Christchurch CBD Decision-Support Digital Twin

Presentation demo plan: what to run, in what order, and what to say while it runs.
All paths are relative to the repo root unless stated otherwise.

---

## 0. The recommended "show-off" order (≈4–5 min of live demo)

| # | Segment | Why it impresses | Time | Risk |
|---|---------|------------------|------|------|
| 1 | **Start a scenario from the browser** (no terminal) | Non-IT usability; one click spawns SUMO + metrics writer | 30 s | Low |
| 2 | **Live map + charts** | Real-time pipeline: SUMO → WebSocket → browser in ms | 30 s | Low |
| 3 | ⭐ **Click a road → "Close live" → watch the queue → "Reopen road"** | *The* digital-twin moment: perturb the running model and see impact + recovery | 60–90 s | Low (local) |
| 4 | ⭐ **Compare + Impact report** (baseline vs crash) | Decision support: quantified answer ("speed −X %, congestion +Y") | 60 s | Low if data pre-loaded |
| 5 | ⭐ **GEH validation result** (88 % of links GEH < 5) | Credibility: model meets the DfT TAG ≥85 % target | 30 s | None (pre-run output) |
| 6 | **Cloud: hosted dashboard + AWS console / Terraform** | Engineering breadth: IaC, serverless, fan-out | 60 s | Medium (network) |
| 7 | **Tests + CI** (pytest / GitHub Actions green) | Quality discipline | 20 s | None |

⭐ = the three strongest moments. If time is cut, keep **3, 4, 5**.

**Rule of thumb:** run the interactive parts **locally** (reliable). Show the cloud
as **pre-opened tabs / screenshots**, plus the live hosted map if the sim host is up.
Cloud live Close/Reopen is built and tested offline but **not yet `terraform apply`-ed** —
don't demo it live unless you've tested it in the research account first.

---

## 1. Pre-demo setup (do this 15–30 min before)

### 1a. Local stack (on the VM / Mac)
```bash
cd <repo>
export SUMO_HOME=/usr/share/sumo        # or ~/sumo/sumo-1.27.1 on the Mac
source smart_city_digital_twin/.venv/bin/activate
./start_demo.sh                         # InfluxDB :8086, web :8000, replay :8788, control :8799
```
Open the printed URL: `http://localhost:8000/scripts/intersection_map.html`
(VM: forward ports 8000, 8765, 8788, 8799.)

### 1b. Pre-load comparison data (so Compare/Impact is instant)
A full preset run takes real minutes; do it **before** the talk:
```bash
cd smart_city_digital_twin/2D_simulation
python scripts/run_scenarios.py --fast          # runs baseline_am, crash_arterial, roadworks in turn
#   or one at a time:
python scripts/run_scenarios.py --scenario baseline_am
python scripts/run_scenarios.py --scenario crash_arterial
```
Check: the dashboard's Compare panel → `baseline_am` vs `crash_arterial` → Load shows two lines.

### 1c. Pre-run the validation (output to show, not to run live)
```bash
cd smart_city_digital_twin/2D_simulation
sumo -c Christchurch_Central_City_main_streets.sumocfg \
     --additional-files data/output/network/Christchurch_Central_City_main_streets.add.xml,scripts/validation/edgedata_am.add.xml \
     --end 27000
python scripts/validate_against_counts.py --sim edgedata_am.out.xml --begin 23400 --end 27000
```
Keep this terminal open (or screenshot) — the summary line is the money shot:
**24 links, GEH < 5 for 88 % (21/24)**.

### 1d. Cloud (optional, only if showing the hosted live feed)
```bash
cd infra
terraform apply -var 'sim_host_enabled=true'    # ~5 min for EC2 boot + SUMO install
./dashboard_url.sh                              # prints the hosted URL (ws + replay + counts + edges)
```
Open it in a tab **before** you present. Do **not** add `&control=…&token=…` to any
URL that will be on screen.

### 1e. Tabs to have open (in order)
1. Local dashboard (`localhost:8000/...`)
2. Terminal with validation output
3. Hosted dashboard (CloudFront URL)
4. AWS console: Lambda list / CloudWatch log of `traffic-metrics` (account ID blurred/cropped)
5. InfluxDB Cloud Data Explorer (`SELECT … FROM traffic_metrics WHERE scenario_id='baseline_am'`)
6. GitHub → Actions (green CI run)
7. `docs/ARCHITECTURE.md` / system flow diagram (fallback explanation)

---

## 2. Live demo script

### Segment 1 — Start from the browser (30 s)
- Dashboard → **Scenario control** → **Start "Baseline — morning peak"**.
- Say: *"No command line. The control server spawns SUMO and the metrics writer
  from a safe preset allowlist — never arbitrary arguments."*
- Status shows `running … writer: true`; the live feed auto-connects.

### Segment 2 — Live map + charts (30 s)
- Point at vehicles coloured by speed (red < 3 m/s, amber 3–8, green ≥ 8), the
  **sim clock** (06:3x), and the three charts (count / avg speed / congestion).
- Click **Pause** → *"pause freezes the view only; the simulation keeps running."* → Resume.

### Segment 3 — ⭐ Close a road live, then reopen (60–90 s)
1. Toggle the road overlay, **click a busy arterial** (e.g. edge `770109405#0`, the one
   the crash preset uses) → its id fills in.
2. **Close live** → within seconds speeds on approaches turn red, a queue forms,
   **Congestion alerts** panel lists slow segments, highlighted red on the map.
3. Say: *"This is the core of a digital twin: we perturb the calibrated model via TraCI
   while it's running and watch the impact."*
4. **Reopen road (resume)** → the queue drains, markers go green, speed chart recovers.
5. Say: *"No restart — the block is lifted in the same run, so you see recovery too."*

### Segment 4 — ⭐ Compare + Impact report (60 s)
- Compare panel: `baseline_am` vs `crash_arterial`, range covering the run → **Load**.
  Two lines on the **sim clock** (aligned even though runs happened at different times).
- Click **Impact report** → avg speed drop %, congestion rise, stopped (mean & peak),
  vehicles — worse values in red.
- Say: *"This turns the twin into decision support: a planner gets a quantified
  answer to 'what if this road closes during the peak?'"*

### Segment 5 — ⭐ Validation (30 s)
- Switch to the validation terminal.
- Say: *"The baseline is calibrated against the real CCC/Miovision counts. Using the
  GEH statistic — the UK DfT TAG standard — 88 % of links are under GEH 5, meeting the
  85 % target. The deficit is concentrated at one bottleneck (Durham St South), an
  insertion backlog under congestion — and I frame this honestly as a consistency
  check, not independent field validation."*

### Segment 6 — Cloud (60 s)
- Hosted CloudFront dashboard: same page, live vehicles via **API Gateway WebSocket**.
- Optional proof the browser is receiving data: DevTools → Network → WS → Messages.
- AWS console: Lambda functions (ingest / metrics / replay / control); CloudWatch log line
  for `traffic-metrics` ↔ matching point in InfluxDB (same `scenario_id`).
- Terminal:
  ```bash
  cd infra && terraform output          # ⚠ redact! or show `terraform state list` instead
  terraform state list | head -30       # safe: shows the whole stack is code
  ```
- Say: *"Everything is Terraform — the stack can be rebuilt or torn down reproducibly;
  costs ~US$0.40/month idle, with a US$5 billing alarm."*

### Segment 7 — Quality (20 s)
```bash
cd smart_city_digital_twin/2D_simulation
python -m pytest scripts/tests scripts/etl/tests -q     # all green, no SUMO needed
```
Or just show GitHub Actions (matrix Python 3.11 + 3.12) green.

---

## 3. Full command reference (by component)

| Purpose | Command |
|---|---|
| Start local stack | `./start_demo.sh` (`BIND=127.0.0.1 ./start_demo.sh` to restrict) |
| Stop local stack | `./stop_demo.sh` (`--wipe` removes the InfluxDB container, keeps volumes) |
| Docker alternative | `docker compose up` (see `DOCKER.md`) |
| Run sim manually + local feed | `python scripts/run_traci.py --no-gui --emit --scenario-id baseline_am --jump-to 23400 --end 27000` |
| Close an edge from the start | `python scripts/run_traci.py --no-gui --emit --close-edge=770109405#0@23700:24300 --scenario-id crash_arterial` |
| Incident file | `python scripts/run_traci.py --no-gui --emit --incident-file scripts/scenarios/incident_example.json` |
| Forward to cloud | `python scripts/run_traci.py --no-gui --emit-target wss://<api>/prod --emit-interval 5` |
| Metrics writer (if not using control server) | `python scripts/metrics_writer.py` |
| Batch preset runs | `python scripts/run_scenarios.py [--scenario NAME] [--fast] [--no-writer]` |
| Control server only | `python scripts/control_server.py` (port 8799) |
| Replay server only | `python scripts/replay_server.py` (port 8788) |
| Validation | `python scripts/validate_against_counts.py --sim edgedata_am.out.xml --begin 23400 --end 27000` |
| Find an edge's intersection | `python scripts/edge_to_intersection.py --edge <edge_id>` |
| List edges | `python scripts/list_edges.py --name "Durham"` |
| Tests | `python -m pytest scripts/tests scripts/etl/tests -q` |
| Deploy infra | `cd infra && terraform init && terraform apply` |
| Sim host on / off | `terraform apply -var 'sim_host_enabled=true'` / `=false` |
| Upload dashboard | `./infra/deploy_dashboard.sh` |
| Hosted dashboard URL | `./infra/dashboard_url.sh` |
| URL with cloud control (private only!) | `echo "$(./dashboard_url.sh)&control=$(terraform output -raw control_api_url)&token=$(terraform output -raw control_api_token)"` |
| Teardown (safe preview) | `./infra/teardown.sh --expect-account <ID> --dry-run` |

---

## 4. Fallback plan

| If this fails… | Do this |
|---|---|
| SUMO won't start ("Could not locate SUMO") | `export SUMO_HOME=/usr/share/sumo`; else switch to screen recording |
| Live feed doesn't connect | Click Connect again; check port 8765 is forwarded; fall back to hosted tab |
| Compare shows nothing | Widen the range (e.g. last 24 h); check scenario ids; show screenshot |
| Wi-Fi / AWS unreachable | Skip Segment 6 live; show report Appendix B screenshots (Figures B1–B7) |
| Anything unexpected | Pre-recorded 2-min demo video — **record it before the presentation** |

---

## 5. Security checklist before going on screen

- [ ] No `control_api_token` or InfluxDB token in any visible URL, terminal or env print.
- [ ] Don't run bare `terraform output` on screen (shows the token output name/endpoints) —
      use `terraform state list`, or `terraform output -raw cloudfront_url` only.
- [ ] AWS account ID cropped/blurred (console top-right, ARNs).
- [ ] Browser history/autocomplete won't reveal a `&token=` URL.
- [ ] Close terminals that hold `.env` / tfvars content.

---

## 6. Likely viva follow-ups tied to the demo

- *"Is that live Christchurch traffic?"* → No: simulation output, calibrated on historical
  counts; "real-time" = pipeline latency (see CLAUDE.md "What real-time means").
- *"Why does closing a road queue instead of rerouting?"* → Rerouting/detours is a scoped,
  deliberately deferred extension (`adaptTraveltime` + `rerouteTraveltime`).
- *"How do you compare runs from different times?"* → `scenario_id` tag + align on the sim
  clock; Impact report uses per-series aggregates, no timestamp alignment needed.
- *"Why is the emitter a client of API Gateway?"* → Lambda can't host a long-lived
  socket; the producer dials out (Q9 in `docs/Q&A.md`).
- *"Why EC2, not Lambda, for SUMO?"* → 15-min limit, stateful process; Fargate is planned.

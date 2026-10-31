# Q&A — demo & viva preparation

Prepared answers to likely questions about the Christchurch decision-support digital
twin. Each answer is traced to the real code and AWS resources so it can be defended.

---

## Q1. When I click "Start: Baseline — morning peak", what is the complete flow?

This traces the **cloud** path (the deployed demo). The same dashboard button also
drives the **local** `control_server.py` — the only differences are noted at the end.

### One-line summary
Browser → authenticated control API → a Lambda that tells the EC2 sim host (via SSM)
to run the baseline scenario → SUMO streams vehicle snapshots back up through the
WebSocket API → they fan out to the browser (live map) and are aggregated into
InfluxDB (history/compare).

### Step by step

**1. Browser — the click.**
The button was created from the preset list (`GET /scenarios`) when the panel loaded.
Clicking it calls `scnStart("baseline_am")`, which sends:
```
POST  <control-api>/start?scenario=baseline_am
Header: Authorization: Bearer <token>
```
The `scnFetch()` helper (in `intersection_map.html`) prepends the `?control=` API URL
and attaches the `?token=` bearer header.

**2. CORS preflight.**
Because it's a cross-origin POST with a custom `Authorization` header, the browser
first sends an `OPTIONS` preflight. The control HTTP API's CORS config answers it
(allowed origin/methods/headers). Preflight is **not** authorized.

**3. API Gateway (control HTTP API) → token authorizer.**
The `POST /start` route has a custom Lambda authorizer. API Gateway invokes the
**`control-authorizer`** Lambda, which constant-time-compares the bearer token against
its `CONTROL_TOKEN` env var and returns `{"isAuthorized": true}`. A wrong/missing token
→ **401**, and the flow stops here. (This is why the public dashboard is read-only
without the token.)

**4. API Gateway → `traffic-control` Lambda.**
With authorization granted, API Gateway proxies the request (payload v2) to the
**`traffic-control`** Lambda. The event has `routeKey = "POST /start"` and
`queryStringParameters = {scenario: "baseline_am"}`.

**5. `traffic-control` Lambda — validate, then command the sim host.**
(`infra/functions/traffic_control/handler.py`)
- Validates `"baseline_am"` is in the **preset allowlist** (`PRESETS`). Unknown → 400.
- `_sim_host_running()` → `ec2:DescribeInstances` filtered by `tag:Name =
  christchurch-digital-twin-prod-sim-host` + state `running`. None → **409 "sim host not
  running"**.
- Builds the scenario args from the preset (never from arbitrary input):
  `--real-time --speed 5 --jump-to 23400 --end 27000 --scenario-id baseline_am`.
- Sends an **SSM Run Command** (`ssm:SendCommand`, `AWS-RunShellScript`, targeted by
  the sim-host tag) whose shell body:
  ```
  cat > /etc/sumo-emitter.env <<'SUMOEOF'
  SCENARIO_ARGS=--real-time --speed 5 --jump-to 23400 --end 27000 --scenario-id baseline_am
  SUMOEOF
  systemctl restart sumo-emitter
  ```
- Records intent in **SSM Parameter Store** (`/christchurch-digital-twin/prod/sim-control/state`)
  so `GET /status` can report it.
- Returns `200 {"ok": true, "scenario": "baseline_am"}`.

**6. SSM → the EC2 sim host.**
The SSM agent on the box (outbound-only; no open ports) receives the command and runs
it as root: it rewrites `/etc/sumo-emitter.env` with the new `SCENARIO_ARGS` and does
`systemctl restart sumo-emitter`.

**7. The sim host runs the simulation.**
systemd restarts the `sumo-emitter` service, whose `ExecStart` (with `$SCENARIO_ARGS`
expanded from the env file) is:
```
python scripts/run_traci.py --no-gui --emit-interval 10 --emit-target "wss://<ws-api>/prod" \
        --real-time --speed 5 --jump-to 23400 --end 27000 --scenario-id baseline_am
```
`run_traci.py` launches headless SUMO via TraCI, jumps to sim time 23400 (06:30), and
steps in real time (×5). Every 10th tick it forwards a vehicle-state snapshot (tagged
`scenarioId = baseline_am`) to the cloud **WebSocket API Gateway** via the
`CloudForwarder` (`{"action":"sendmessage","data":{…}}`).

**8. Browser — connect to the live feed.**
After the POST resolves, `scnStart` shows "started… connecting", waits ~2.5 s, then
`connectWs()` opens a WebSocket to the `?ws=` URL (the WebSocket API Gateway). It also
starts polling `GET /status` every 3 s (which reads the Parameter Store state).

**9. Cloud data path — how vehicles appear and history fills.**
Each snapshot the sim host sends hits the **WebSocket API Gateway** (`sendmessage`) →
**`traffic-ingest`** Lambda, which:
- looks up connected browsers in the **DynamoDB connections table** and **fans the
  snapshot out** to them (`postToConnection`) — the browser draws the speed-coloured
  vehicles on the Leaflet map and updates the live Chart.js panels + sim clock; and
- async-invokes **`traffic-metrics`**, which runs `compute_tick_metrics` and writes the
  per-tick metrics to **InfluxDB Cloud** (tagged `simId` + `scenario_id`). That feeds
  the **History / Compare / Impact** panels via the **`traffic-replay`** Lambda.

### Compact sequence
```
click → scnFetch POST /start (Bearer token)
      → [CORS preflight] → control API Gateway
      → control-authorizer Lambda (token ok?)
      → traffic-control Lambda → EC2 DescribeInstances + SSM SendCommand + Parameter Store
      → SSM agent on sim host → rewrite env file → systemctl restart sumo-emitter
      → run_traci.py + SUMO (baseline, real-time) → emit snapshots
      → WebSocket API Gateway → traffic-ingest Lambda
            ├─ fan-out to browser WS → live vehicles + charts
            └─ traffic-metrics Lambda → InfluxDB Cloud → (replay) History/Compare/Impact
```

### Safety points (worth stating in the viva)
- The control endpoint is **token-authenticated** because starting a run triggers
  compute/cost.
- The command is composed **only from a fixed preset allowlist** (and, for a road
  block, a regex-validated edge id) — never from free-form input — so the SSM/shell
  command can't be turned into arbitrary execution.
- IAM is **least-privilege**: `ssm:SendCommand` is restricted to the sim host **by tag**.

### How the local path differs
With `?control=http://localhost:8799` (no token), the same button hits
`control_server.py` on the SUMO machine, which **spawns `run_traci.py` directly** (and
auto-starts `metrics_writer.py`) instead of going through API Gateway → SSM → EC2. The
HTTP contract (`/scenarios`, `/start`, `/stop`, `/status`, `/start_roadblock`) is
identical, which is why one dashboard panel drives both.

---

## Q2. We only have Baseline and Crash preset buttons. If the product owner / examiner asks for more scenario comparisons, how do we fulfil that?

**Key point:** the **Compare view compares any two `scenario_id`s that have data in
InfluxDB** — the preset buttons are just shortcuts for producing tagged runs. So "more
comparisons" means "more tagged runs". There are several ways, increasing in effort:

1. **Click-a-road closure (no code change, examiner-driven).** Click any road on the
   map → **Start road block** → a run that closes that edge. The examiner chooses the
   scenario live. *(Caveat: by default every road block is tagged `scenario_id =
   "roadblock"`, so two different closures aren't independently comparable — see the
   enhancement below.)*
2. **Compare any two existing runs (no change).** Every run is tagged and stored; enter
   any two `scenario_id`s in the Compare panel (e.g. `baseline_am` vs `roadworks`, the
   3rd preset). You are not limited to two scenarios.
3. **Add named preset buttons (small change + redeploy).** Add entries to the `PRESETS`
   registry — kept in **two** places: `scripts/control_server.py` (local — just restart)
   and `infra/functions/traffic_control/handler.py` (cloud — `terraform apply`
   repackages the Lambda). Each preset is a `scenario_id` + args: a different
   `--close-edge=<edge>@<window>` or an `--incident-file` (reduced-speed corridor or
   demand surge via `incidents.py`).
4. **Local CLI (maximum flexibility).** `run_traci.py --scenario-id <any> --close-edge …
   / --incident-file …` tags a run with any id, which then appears in Compare. The
   **cloud is deliberately preset-only** (no arbitrary args over the network) for safety.

**Recommended enhancement.** Tag each click-a-road closure with a **unique id per edge**
(`rb_<edge>`) so every road on the map becomes its own comparable scenario — giving the
examiner unlimited, live-chosen comparisons with no per-scenario code change. Plus a few
more preset buttons for variety (longer full-morning closure, reduced-speed corridor,
demand surge).

**Safety framing (for the viva):** arbitrary scenario definitions are never accepted over
the cloud control API — only a fixed preset allowlist plus a regex-validated edge id.
That is why the cloud path is preset-only while the local CLI is unrestricted.

---

## Q3. Why doesn't "Close live" work in the cloud demo?

**It is expected, not a bug — "Close live" is local-only by design.**

"Close live" (and its "Reopen road (resume)" toggle) injects a closure into the
**already-running** simulation by sending a control frame **up the live WebSocket** to
the emitter's inbound `ControlInbox` (the Inc 3 click-to-inject mechanism). That requires
a two-way channel between the browser and `run_traci.py`.

In the cloud that back-channel does not exist, for two reasons:

1. **No inbound server on the sim host.** The cloud sim host runs the emitter in
   forward-only mode — `run_traci.py ... --emit-target wss://…` with **no `--emit` /
   `--emit-host`** — so there is **no local WebSocket server and no `ControlInbox`**
   listening for control frames.
2. **The cloud live feed is one-way fan-out.** The emitter *dials out* and posts
   snapshots to the WebSocket API Gateway → `traffic-ingest` Lambda → fanned out to
   browsers. The browser is a **consumer** of that feed; there is no route for it to send
   a control frame back to that specific emitter.

So in the cloud there is nothing on the other end to receive "Close live".

**What to use in the cloud instead:** **Scenario control → "Start road block"** (click a
road → Start road block). That goes through the authenticated cloud control API and starts
a **fresh run with the edge closed from the start** — which does work in the cloud, and
still demonstrates incident impact via Compare / Impact report.

| Action | Local | Cloud |
|---|---|---|
| **Close live** — inject into the *running* sim; watch recovery on reopen | yes | no (by design) |
| **Start road block** — fresh run with the edge closed | yes | yes |

Documented in `CLAUDE.md` and the report's §11 limitations ("Cloud click-to-inject is
local-only"). A true cloud live-inject is a deferred larger build (see CHECKLIST planned
improvements).

---

## Q4. Why does the cloud pipeline use AWS Lambda, but the local pipeline doesn't?

**Key point: Lambda is AWS-managed serverless compute — it only exists on AWS. Locally,
the *same pure logic* runs inside an ordinary long-running process instead. The behaviour
is identical; only the host that runs it differs.**

The metrics step is the clearest example. The actual computation is one pure function,
`compute_tick_metrics` (`scripts/metrics.py`):

- **Cloud:** that function is the body of the **`traffic-metrics` Lambda** — the file is
  copied verbatim into `infra/functions/traffic_metrics/` and invoked per snapshot.
- **Local:** the **same** `compute_tick_metrics` is imported and called by
  `metrics_writer.py`, a long-running process that reads the WebSocket feed and writes to
  local InfluxDB.

So the logic is identical today, but it lives in **two copies**: the Lambda bundles its own
copy of `metrics.py`, which must be kept in sync with `scripts/metrics.py` by hand. (A single
shared package used by both would remove that risk.)

### Why a different host in each environment

1. **Lambda is an AWS service; it can't run on a laptop.** Off-AWS there is no API
   Gateway or Lambda. You *could* emulate it (AWS SAM / LocalStack), but that is heavy
   tooling for no benefit in a self-contained dev/demo stack.
2. **Each model fits its environment.** In the cloud, an **event-driven, serverless**
   function is ideal: it **scales to zero** (near-zero idle cost — the whole stack is
   ~$1–2/month idle), auto-scales per message, and needs no server to manage — exactly
   right for a bursty pipeline on a student budget. Locally, the stack is already **one
   machine up via Docker Compose**, so a **plain long-running container process** is
   simpler: no API Gateway, no cold-start latency, no per-invoke overhead, nothing to
   emulate.
3. **The design deliberately separates pure logic from its runtime.** Because
   `compute_tick_metrics` is a pure, unit-tested function, the same code (one function,
   two copies) is hosted by a serverless function in the cloud and a long-running process
   locally, and both produce **identical metrics as long as the copies match**. That is
   what makes the two pipelines interchangeable.

### The full local ↔ cloud mapping

| Role | Cloud (AWS) | Local (Docker Compose) |
|---|---|---|
| Ingest / fan-out to browsers | `traffic-ingest` Lambda (via WebSocket API Gateway + DynamoDB connections) | the emitter's own WebSocket server broadcasts directly (point-to-point on one machine — no separate ingest needed) |
| Per-tick metrics → InfluxDB | `traffic-metrics` Lambda (`compute_tick_metrics`) | `metrics_writer.py` (same `compute_tick_metrics`) |
| History / replay read API | `traffic-replay` Lambda | `replay_server.py` (stdlib HTTP server) |
| Time-series store | InfluxDB Cloud | InfluxDB container |

### "Local writes to InfluxDB directly — so why can't the cloud do it without Lambda?"

Both paths **do** write directly. Locally, `metrics_writer.py` reads the snapshot from the
WebSocket, computes the metrics and sends them to the container at `localhost:8086`. In the
cloud, the `traffic-metrics` Lambda **is** that direct writer — it does exactly the same job
and sends to InfluxDB Cloud. The real question is **what runs the writing code**.

Writing a metric takes code, not just forwarding: receive the snapshot → aggregate it
(`compute_tick_metrics`) → format InfluxDB line protocol → send it with the database token.

| | Local | Cloud |
|---|---|---|
| Who runs that code | `metrics_writer.py`, a process you start and leave running | `traffic-metrics` Lambda |
| When it runs | All the time, while the process is up | Only when a snapshot arrives |
| Where InfluxDB is | Docker container, `localhost:8086` | InfluxDB Cloud (public HTTPS) |
| Where the token lives | Local `.env` | Secrets Manager |

- **API Gateway can't do it alone.** It only receives messages and passes them on; it can't
  aggregate, convert JSON to line protocol, or safely add the token. Locally your always-running
  process does that work; in AWS there is no always-running process of yours unless you pay for
  one, and **Lambda is code that runs only when a message arrives**.
- **The cloud can't use the local container.** It lives on your machine, which isn't reachable
  from the internet, and `localhost` inside a Lambda is the Lambda itself — hence InfluxDB Cloud.

**Could the cloud skip Lambda? Yes, two ways, each with a cost:**
1. **The producer writes to InfluxDB Cloud itself** — run `metrics_writer.py` next to the emitter
   with `--influx-url`/`--token` pointed at the cloud. It works, but the database token then sits
   on every producer machine instead of in Secrets Manager, every producer needs an extra process
   (the "forgot the writer, history is empty" trap returns), and storage becomes the producer's job.
2. **An always-on server runs `metrics_writer`** listening to the WebSocket API — also works, but it
   costs money around the clock and needs patching, which breaks the ~US$1–2/month idle target.

Lambda avoids both: any producer's metrics are stored automatically, the token stays in AWS, and
you pay only per snapshot.

**Viva one-liner:** "Both paths write directly to InfluxDB. The difference is what runs the writing
code: locally a process I keep running, in AWS a Lambda that runs only when a snapshot arrives,
because API Gateway can only pass messages on, not process them."

**Framing for the viva:** this is a *separation of concerns* — application logic
(pure functions) vs. the infrastructure that hosts it. The cloud uses serverless Lambda
because it is cost-efficient and auto-scaling for an intermittent public pipeline; the
local stack uses a long-running process because, on a single already-running machine, that
is the simplest right-sized host. Using the same `compute_tick_metrics` in both (kept in sync)
means the local and cloud results match, and the shared logic is unit-tested once in
`scripts/metrics.py`.

---

## Q5. How were the SUMO input files (`*.net.xml`, `traffic_trips.routed.rou.xml`, `*.add.xml`) built?

**Key point: these three are the *calibrated simulation core* — built once by the Python
pipeline from real OpenStreetMap + Christchurch City Council data, then consumed unchanged.**
They are prior/inherited assets (marked "do not modify" in `CLAUDE.md`); this project adds
the streaming / cloud / decision-support layer on top of them. The main SUMO config wires
all three together:

```
<net-file          value="…/Christchurch_Central_City_main_streets.net.xml" />
<route-files       value="…/traffic_trips.routed.rou.xml" />
<additional-files  value="…/Christchurch_Central_City_main_streets.add.xml" />
```

### 1. `Christchurch_Central_City_main_streets.net.xml` — the road network
Built by **`create_network.py`** (the "network" step in `sim_pipeline.py`):
1. Take an **OpenStreetMap** extract of the central city (`Christchurch_Central_City.osm.xml`).
2. Filter it to the **main streets** (drop footpaths, service lanes, etc.).
3. Run SUMO's **`netconvert`** to turn OSM ways into a routable SUMO network — lanes,
   junctions, connections, speeds — then **rebuild the traffic-light logic (TLS)**.
   (The net is `lefthand="true"` — New Zealand drives on the left.)

The same script's "map" step also produces `intersection_to_edges.csv` by matching the
**CCC OpenData / ArcGIS intersection geometry** (`intersection_geo.csv`, the 96 surveyed
intersections) to the nearest net edges — the mapping the dashboard's road overlay and
`edge_to_intersection.py` use.

### 2. `Christchurch_Central_City_main_streets.add.xml` — map-context polygons
Generated by SUMO's **`polyconvert`** (per the file's own header, v1.26.0) from the **same
OSM file** plus the net: it extracts **area polygons** — buildings, parks/leisure,
amenities, parking — pruned to the network's bounds (`prune.in-net=true`). This is purely
**visual context** for `sumo-gui` (the grey/green shapes behind the roads); it contains **no
detectors or traffic logic** (a common misconception — "additional file" ≠ sensors here).

### 3. `traffic_trips.routed.rou.xml` — the vehicle demand (routes)
Built by **`create_demand.py`** (the "trips" then "duarouter" steps), from the **real CCC
Miovision turning-movement counts**:
1. Count workbooks (Excel) → **`traffic_counts_parser.py`** → one long CSV of surveyed
   movements (intersection, approach, movement, time slot, vehicle count).
2. **`sumo_demand_from_traffic_csv.py`** turns each surveyed movement into a SUMO **`<flow>`**
   — a vehicle `number` on a `from`→`to` edge pair, in a 15-minute slot — producing
   `traffic_trips.rou.xml` (origin/destination demand, not yet routed).
3. SUMO's **`duarouter`** computes a shortest-path **route through the net** for every
   OD pair, producing `traffic_trips.routed.rou.xml` — fully-routed vehicles with explicit
   edge lists, which is what SUMO actually runs. (Buses are built the same way from a
   timetable into `traffic_trips_timetable_buses.routed.rou.xml`.)

### The chain, end to end
```
OpenStreetMap  ──netconvert──▶  net.xml          (roads, junctions, TLS)
OpenStreetMap  ──polyconvert─▶  add.xml          (building/park polygons — gui only)
CCC intersection geometry ─match▶ intersection_to_edges.csv

CCC Miovision counts ─parser▶ CSV ─flows▶ traffic_trips.rou.xml ─duarouter▶ traffic_trips.routed.rou.xml
```

**Framing for the viva:** the network and demand are *calibrated from real data* — OSM for
the road geometry, CCC OpenData for intersection placement, and CCC/Miovision counts for how
many vehicles make each movement. That real-data calibration is exactly what makes the
baseline defensible (and what the GEH validation in §4.2 checks the simulation reproduces).
The build is deterministic and re-runnable (`create_network.py` → `create_demand.py`), but
the committed outputs are treated as fixed inputs so the streaming/cloud work builds on a
stable model.

### Relationship to the ETL pipeline (report §3.5)

The **demand** chain above (`CCC Miovision counts → parser → CSV → flows → duarouter →
routed.rou.xml`) has an automated, cloud-native front-end: the **ETL pipeline** documented in
report §3.5 (`scripts/etl/`). They use the **same data source** (CCC Miovision counts) and the
**same parser** (`traffic_counts_parser.py`, which `etl/counts_etl.py` wraps), and they meet at
one file — the **counts CSV**:

```
CCC Google Drive ─drive_client▶ counts_etl (wraps parser) ─▶ S3 processed JSON ─scenario_bridge▶ counts CSV
                                                                                                    │
                                        ┌───────────────────────────────────────────────────────────┘
                                        ▼
                        sumo_demand_from_traffic_csv.py ─flows▶ …rou.xml ─duarouter▶ routed.rou.xml
                        (Q5 step 2 resumes here — identical from the CSV onward)
```

Three points for the viva:
- **Scope.** The ETL feeds only the **demand** branch (counts → `rou.xml`). It does **not** touch
  `net.xml` or `add.xml` — those come from OpenStreetMap via `netconvert`/`polyconvert`, not counts.
- **One-time vs repeatable.** The Q5 chain was run **once** to produce the committed, "do-not-modify"
  baseline core. The ETL is the **repeatable acquisition/refresh** version of its counts front-end —
  automated Drive fetch, manifest change-detection, defensive rejection of non-vehicle files, S3 storage.
- **It is the "rebuild from updated data" path.** If the council publishes newer counts, the ETL pulls
  and cleans them and `scenario_bridge.py` lands a fresh CSV; `sumo_demand_from_traffic_csv.py` →
  `duarouter` then rebuilds `rou.xml` — i.e. this Q5 chain re-run on ETL-sourced data.

**Honest note:** the *delivered* baseline's `routed.rou.xml` was built by this Q5 chain from counts
obtained directly, not routed through the live ETL. The ETL is the productionised, cloud-engineering
version of that counts-acquisition front-end (and a standalone AWS showcase) — it demonstrates *how*
the demand is sourced and refreshed at scale, feeding the exact same build from the CSV onward.

## Q6. In the Comparative Analysis we claim Deng et al. (2024) "does not incorporate decision-support features such as alerts, scenario comparison, or historical replay." How do we defend that negative claim?

**Context.** This line (report §2, Comparative Analysis) is about a *comparator system*, not ours.
Deng et al. (2024) is a peer-reviewed system that — like this project — streams SUMO data through a
serverless AWS pipeline. We cite it to show that an AWS + SUMO streaming architecture is viable, then
distinguish it to establish our contribution.

**The defense (scope, not shortcoming).** The claim is phrased as *out-of-scope*, not *failure*:
"designed to evaluate a specific control algorithm, not to function as a general-purpose dashboard.
Consequently, it does not incorporate…". Their paper's purpose is a vehicle **speed-advisory control
algorithm**; a planner-facing dashboard with alerts, baseline-vs-incident comparison, and historical
replay is simply **not what that system set out to build**. So the absence is by design, and asserting
it is fair — we are describing their stated scope, not grading them against ours.

**If an examiner pushes:**
- *"How do you know it lacks those features?"* — From the paper's own stated scope and reported
  feature set: it is an evaluation of a control algorithm with latency targets, not a general-purpose
  decision-support tool. We claim absence only for features outside that scope, and we hedge the
  sentence with "designed to evaluate a specific control algorithm, not… a general-purpose dashboard".
- *"Isn't that an unfair comparison?"* — No: we are not saying their system is deficient. We are
  mapping the **feature landscape** to locate the gap this project fills. Deng et al. proves the cloud
  streaming pipeline is feasible; it just was not built to be a planner's what-if tool.

**The role of the line in the argument.** It is one half of the "neither comparator does the whole
thing" case the Comparative Analysis makes:
- **keshu-bara (2025):** a live SUMO dashboard, but single-machine, polling not streaming, no
  persistence, no real cloud.
- **Deng et al. (2024):** a real serverless AWS streaming pipeline, but no decision-support / dashboard
  features *(this line)*.
- **This project:** combines a calibrated real-city simulation + persistent cloud storage + a dashboard
  *with* decision-support features — the integration neither comparator offers.

So the sentence does rhetorical work: it shows that even the strongest existing cloud-SUMO system still
lacks the planner-facing capabilities that are this project's contribution. The safe framing in the
viva is always **"out of their scope," not "a gap in their work."**

## Q7. In the compare view, a crash scenario sometimes shows *lower* congestion, labelled "better" (e.g. `congestion: baseline_am 0.4 → crash_arterial 0.3 (-6%, better)`). Why — did the crash improve traffic?

**No. "better" is a mechanical directional label, not a causal claim.** The compare summary
reads **B vs A** (here A = `baseline_am`, B = `crash_arterial`). For each metric it knows which
direction is bad — for congestion *a rise is worse*, for avg speed *a drop is worse* — and it
colours the change green/"better" or red/"worse" accordingly. Congestion fell (0.4 → 0.3), and
lower congestion is good, so it prints **"better"** (green). It is only saying *run B sits on the
good side of run A for this metric* — nothing about why.

**Why the displayed numbers don't match the %.** The `%` is computed from the *unrounded* means
(`(mB − mA)/|mA|`), but `0.4` and `0.3` are each rounded to one decimal for display. So the real
means are ~0.37 → 0.35 (a true −6 %), not 0.40 → 0.30 (which would read −25 %). The percentage is
the accurate figure; the decimals are cosmetic.

**Why a crash can genuinely read lower-congestion (the important caveat).** The congestion index
is a **network-wide average over vehicles currently on the network**. When an arterial is closed,
SUMO holds vehicles at their entry edge (**insertion backlog**) rather than inserting them into a
saturated network — the same effect quantified in the GEH validation (§4.2 / `docs/VALIDATION.md`,
where the Durham St bottleneck inserted only 215 of 1353 demanded vehicles). Those un-inserted
vehicles don't contribute to "congestion on the network", so the average can *fall* even though
demand is being **suppressed, not served**. The network didn't get better — it got **starved**.

**How to read it honestly (and what to say in the viva).** Never read congestion alone for a
closure scenario — pair it with `vehicleCount` / `stoppedCount` (which is exactly why the **Impact
report** reports these fields together, not congestion in isolation):
- congestion down **and** vehicles-served up → genuinely smoother.
- congestion down **but** vehicles-served/stopped also down → demand withheld (insertion backlog),
  not an improvement — the "better" label is an artefact of the averaging, not a real gain.

So the correct viva framing: *"the tool flags the directional change per metric; for an incident I
interpret congestion together with throughput, because a closure can lower the network-average
simply by keeping cars from entering."*

## Q8. How do the live "Close live" / "Reopen road" buttons work — closing a road in the *running* simulation without restarting it?

**In one line:** the dashboard sends a small JSON command back up the **same WebSocket** that
streams vehicles; the simulation loop drains those commands each tick and applies/reverts them
against the live TraCI connection — closing a road is "set the edge's lane speeds to ~0", reopening
is "restore the saved speeds", with no restart. This is the **local** path (`--emit`); see Q3 for why
the cloud uses *Start road block* instead.

**The chain, end to end:**

1. **Browser — one toggle button.** Click a road on the map to select its edge id. The button flips
   between *Close live* and *Reopen road (resume)*. On click it calls `sendControl(...)`, which is just
   `ws.send(JSON.stringify(...))` over the live feed socket — so the WebSocket is **bidirectional**:
   the browser receives snapshots *and* sends control frames up the same connection.
   - Close:  `{ "action": "incident", "type": "close_edge", "edge": "<id>" }`
   - Reopen: `{ "action": "reopen" }`

2. **Emitter — a control inbox.** The emitter's WebSocket server reads inbound frames; any whose
   `action` is in `{"incident", "reopen"}` are pushed onto a thread-safe `ControlInbox` queue instead
   of being ignored. Queuing matters because TraCI is single-threaded — the async socket must not call
   TraCI directly while the sim is stepping.

3. **Sim loop — drain once per tick.** Each simulation step, before stepping, `run_traci.py` drains the
   inbox:
   - `reopen` → `controller.reopen_all(t)`.
   - `incident` → `incident_from_control(cmd, t)` builds an `Incident(type=close_edge, target=edge,
     start=now, end=now+duration or None)`, then `controller.add(inc)`.
   Then `controller.step(traci, t)` applies/reverts against the live connection. Draining at a fixed
   point in the tick is what keeps TraCI access single-threaded and safe.

4. **IncidentController — apply/revert with saved state.**
   - **Close** = for each lane of the edge, **save the current max speed**, then `setMaxSpeed(lane, ~0)`
     so vehicles can't proceed (the road is blocked). `add()` appends the incident at runtime; `step()`
     picks it up on the next tick (applied-state is keyed by index, so live injection is safe).
   - **Reopen** = `reopen_all(now)` sets each still-active incident's `end = now`, so the next `step()`
     **reverts** it — restoring the saved max speeds. No sim restart; the running simulation keeps going
     and the queued vehicles drain on their own, so traffic **recovers realistically** rather than being
     teleported or reset.

**Why it feels impressive (the design points to mention in the viva):**
- **No restart, fully reversible** — closing saves the exact state it changes; reopening restores it.
  The sim clock never stops, so you watch the queue build and then drain.
- **One socket, two directions** — locally there's no separate control service; the feed socket carries
  commands upstream, and a queue decouples the async I/O from the single-threaded TraCI stepping.
- **Open-ended by design** — a live click closes the road until you reopen (no hidden timer), matching
  the toggle UX; a timed closure is the same mechanism with an `end`.

**Scope:** local `--emit` path only. The cloud feed is one-way fan-out (forward-only `--emit-target`),
so live click-to-inject over AWS is a deferred build (Q3 / CHECKLIST "Planned improvements"); the cloud
equivalent today is **Start road block**, which starts a fresh run with the edge closed from the start.

## Q9. Locally `run_traci.py --emit` hosts a WebSocket server. Why does the AWS version need the emitter to be a WebSocket *client* (`--emit-target`)?

**Because the hub moves.** Someone has to be the server everyone else connects to. Locally that's
the emitter; in AWS it's API Gateway — and API Gateway only *accepts* connections, it never dials out.
So the emitter joins as one more client.

```
LOCAL — emitter is the hub
  browser ──┐
  browser ──┼──► emitter  ws://host:8765   (listens)
  writer  ──┘

AWS — API Gateway is the hub
  emitter ──┐
  browser ──┼──► API Gateway  wss://…/prod  (listens) ──► Lambdas
  browser ──┘
```

**Why it must be that way:**
1. **API Gateway can't connect to the emitter.** A WebSocket API is a public endpoint clients connect
   *in* to; it has no way to open a socket to a backend WebSocket server. Each incoming message
   invokes a Lambda by route, so the emitter has to connect and post on the `sendmessage` route —
   hence the `{"action":"sendmessage","data":{…}}` wrapper (route selection is `$request.body.action`).
2. **The emitter usually isn't reachable from the internet.** It runs on a laptop, the VM or the EC2
   sim host, behind NAT/firewalls. Dialing *out* passes through those; *listening* would need an open
   inbound port. The sim host deliberately has **no open ports** (access is via SSM) — client mode is
   what makes that possible.
3. **The emitter shouldn't serve every viewer.** Locally it pushes each tick to every connected
   browser itself, which is fine for one or two. In the cloud it keeps **one** upstream connection
   regardless of audience; API Gateway + `traffic-ingest` fan out, tracking viewers in DynamoDB.
4. **Lambdas don't hold connections.** They run per message and exit; API Gateway is the only
   long-lived piece, so it must own the sockets.

**Consequences worth stating in the viva:**
- **The two modes compose.** `--emit` (serve locally) and `--emit-target` (forward to the cloud) can
  run together, so a local map and the hosted map can watch the same run.
- **It's why "Close live" is local-only (Q3, Q8).** Locally the browser is connected straight to the
  emitter, so control commands travel back up the same socket. In AWS the browser and the emitter are
  both clients of API Gateway and fan-out goes only *to* browsers, so there's no path back to the
  producer. Closing that gap means a Lambda pushing the command to the emitter's connection
  (`postToConnection`) — the "cloud live-inject" item in CHECKLIST.md.

## Q10. Why use API Gateway's WebSocket API for the live feed? What were the alternatives, and why weren't they chosen?

**Short answer:** the live map needs the server to **push** every tick to the browser, it must cost
about nothing when idle, and there should be no server to run. The WebSocket API is the option that
meets all of that *and* keeps the same protocol as the local setup.

**What the live path had to do:**
1. **Push to browsers in near real time** — several messages a second, without the browser asking.
2. **Accept data from a producer behind NAT** — the emitter dials out; nothing dials in (Q9).
3. **Cost ~$0 when idle** — a student project with a US$5 billing alarm that sits idle most days.
4. **No servers to patch, all defined in Terraform.**
5. **Same protocol as local** — the dashboard's WebSocket client works unchanged against
   `ws://localhost:8765` and `wss://…/prod`, so one front end serves both modes.

**Alternatives and why not:**

| Option | What it would look like | Why not |
|---|---|---|
| **HTTP API + browser polling** | Emitter POSTs snapshots; browser polls for the latest | Polling adds lag and wasted requests (5 Hz × every viewer) and needs a "latest snapshot" store. *Right* for history — which is why replay uses an HTTP API. |
| **Server-Sent Events** | One-way push stream to the browser | API Gateway doesn't support long-lived SSE; Lambda response streaming needs Function URLs (403 on this account). One-way, so no future control path. |
| **Own WebSocket server on EC2 / Fargate** | Run a relay (or the emitter) publicly | Closest to the local code, but always-on cost (~US$8–30/month), TLS certificates, patching, an open inbound port, a single point of failure — breaks the ~US$1–2/month idle target. |
| **AWS IoT Core (MQTT over WebSockets)** | Managed publish/subscribe | Built for this fan-out, but browser auth needs Cognito or signed requests, and MQTT is a second protocol. Overkill for one producer and a few viewers. |
| **AppSync (GraphQL subscriptions / AppSync Events)** | Managed real-time publish/subscribe | Credible — AppSync Events is a close fit today. GraphQL adds schema overhead; API Gateway + Lambda + DynamoDB shows more transferable patterns with explicit control. |
| **Kinesis Data Streams / Firehose** | Streaming ingestion | Great for high-volume ingest/analytics, but can't push to browsers (still need a delivery layer); provisioned shards bill around the clock. |
| **Pusher / Ably / Firebase** | Third-party real-time service | Easy, but outside AWS (the platform + IaC choice), vendor free-tier limits, less to demonstrate. |
| **SQS / SNS** | Managed queue / notifications | Neither delivers to browsers. |

The rationale recorded in CLAUDE.md's decisions table is the WebSocket API's free tier (1M messages
and 750k connection-minutes a month for 12 months); the table above is the fuller argument.

**Costs accepted (say these before being asked):**
- **Hand-written fan-out** — a DynamoDB connections table and `postToConnection` once per viewer per
  tick, so work grows with viewers × tick rate. Fine at demo scale.
- **Hard limits** — 128 KB per message (hence throttling; a ~600-vehicle snapshot is ~50 KB);
  connections close after 10 minutes idle or 2 hours total, so clients need reconnect logic (the
  forwarder has it).
- **Pay per message past the free tier** — at city scale with many viewers, managed publish/subscribe
  (IoT Core or AppSync Events) would beat hand-rolled fan-out. That's the honest "what changes at
  scale" answer.

**Viva framing:** match the tool to the job — **push for live data, request/response for history.**
That's why the system has a WebSocket API *and* two HTTP APIs (replay, control), each where it fits.

## Q11. When 10 browsers are connected, how does `traffic-ingest` know where to send a new snapshot so all 10 receive it?

**It keeps its own guest list.** API Gateway doesn't broadcast; it only gives every connection a
unique **connectionId**. `traffic-ingest` records each id in DynamoDB as it connects, and on every
snapshot it walks that list and sends to each one.

1. **Join on connect.** A browser opening `wss://…/prod` triggers `$connect`; the Lambda writes its
   connectionId to the DynamoDB connections table (`put_item`). `$disconnect` deletes it. With 10
   browsers open the table holds **11 ids** — the 10 browsers plus the emitter, which connected the
   same way.
2. **Snapshot arrives.** The emitter sends `{"action":"sendmessage","data":{…}}`; API Gateway routes it
   to `traffic-ingest` with the **sender's connectionId** in the event.
3. **Metrics, separately.** The Lambda async-invokes `traffic-metrics` (`InvocationType="Event"`), so
   the InfluxDB write never slows the broadcast.
4. **Fan-out (`_broadcast`).** It builds the API Gateway management endpoint
   (`https://{domainName}/{stage}`), **scans** DynamoDB for every connectionId, **skips the sender**
   (the emitter doesn't get its own snapshot back), and calls `post_to_connection` once for each of
   the other **10**.
5. **Cleanup.** A browser that vanished without a clean `$disconnect` makes the post fail with
   `GoneException`; the Lambda deletes that id, so the list stays accurate with no cleanup job.

```
emitter ──sendmessage──► API Gateway ──► traffic-ingest
                                          ├─ async ─► traffic-metrics ─► InfluxDB Cloud
                                          ├─ scan DynamoDB → [emitter, b1 … b10]
                                          └─ skip emitter; post_to_connection × 10 ──► API Gateway ──► b1 … b10
```

**As the audience grows:**
- **Cost grows with viewers.** Each snapshot = one scan + one HTTPS call *per browser*, sent one after
  another. 10 viewers → 10 posts per tick; 1,000 → 1,000.
- **The scan isn't paginated.** A single DynamoDB scan returns at most 1 MB — thousands of ids, fine
  here, but large scale would need pagination (or a query) and parallel posts.
- That's the Q10 trade-off: API Gateway offers per-connection pushes but no "send to everyone" call.
  At large scale, managed publish/subscribe (IoT Core, AppSync Events) does the fan-out for you.

**Viva one-liner:** "API Gateway only knows connection ids, so the ingest Lambda keeps them in
DynamoDB; on every snapshot it scans the table and posts to each id except the sender, deleting any
that have gone away."

## Q12. How is the scenario comparison built, step by step?

**The idea is a controlled experiment:** run the same simulation twice, changing only the incident,
label every stored metric with the run's name, then pull the two runs back out and line them up on
the simulation clock.

**Step 1 — Define the scenarios as named presets.** Designed in
`scripts/scenarios/DECISION_SUPPORT_DESIGN.md`, implemented in `PRESETS` in `control_server.py`:
- `baseline_am` — the calibrated morning peak, no incident.
- `crash_arterial` — the *same* arguments plus `--close-edge 770109405#0@23700:24300` (a major
  arterial closed 06:35–06:45).

Both start from the shared `_COMMON` argument list, so they can't drift apart.

**Step 2 — Make the runs comparable.**
- Same window: `--jump-to 23400 --end 27000` (06:30–07:30).
- Same demand: the same `.rou.xml` built from the council counts.
- Same randomness: SUMO uses a fixed default random seed unless told to randomise, and neither the
  config nor `run_traci.py` turns that on.

So **the only difference between the two runs is the incident**.

**Step 3 — Label everything with `scenario_id`, end to end.** `run_traci.py --scenario-id …` → the
emitter puts `scenarioId` in every snapshot → `compute_tick_metrics()` carries it → stored as the
InfluxDB **tag** `scenario_id`, in both `metrics_writer.py` (local) and the `traffic-metrics` Lambda
(cloud). Tags are indexed, so filtering by scenario is fast and cheap.

**Step 4 — Run both and store them.** Click the two Start buttons in turn, or run
`scripts/run_scenarios.py`, which runs the pair back to back (they share ports) and starts the metrics
writer for each.

**Step 5 — Query one scenario at a time, safely.** The Compare panel calls the replay API with
`&scenario=<name>`. The value is checked against `^[A-Za-z0-9_.:-]{1,64}$` and rejected otherwise, and
only then added to the Flux query as `filter(r.scenario_id == "…")` — this blocks query injection. The
cloud replay Lambda has the same check.

**Step 6 — Line the runs up on the simulation clock.** The runs happen at different real times
(say 10:00 and 10:15), so plotting by timestamp puts them side by side, not on top of each other.
For each scenario the panel also fetches the `simTime` series; `_toSimXY()` joins each metric point
to its `simTime` by timestamp and plots it at `x = simTime`. Both lines now share the 06:30 → 07:30
axis, so the crash dip shows up at 06:35.

**Step 7 — One-line summary.** `renderCompareSummary()` shows each run's **mean**, the **% change**
from A to B, and a direction word that knows which way is bad per metric (speed drop = worse,
congestion rise = worse). See Q7 for why "better" can mislead.

**Step 8 — Impact report.** `computeImpact()` fetches **raw points** (not averaged buckets, so peaks
survive) for avg speed, congestion, stopped vehicles and vehicles in the network, and compares **mean
and peak** per run, coloured red/green. It uses per-run totals, so no time alignment is needed.

```
PRESETS ──► run baseline_am ──┐                         ┌─► scenario=baseline_am ──┐
            run crash_arterial ┴► snapshots → metrics → InfluxDB (tag scenario_id) ┤                          ├─► align on simTime → overlay chart
                                                         └─► scenario=crash_arterial ┘    summary (% change) + impact report
```

**Limits to state honestly:**
- **Averages dilute short incidents** — a 10-minute closure averaged over 60 minutes looks small;
  hence peaks in the impact report and the shape-over-time chart.
- **Held-back vehicles can make an incident look "better"** — SUMO keeps vehicles waiting at their
  entry point when the network is jammed, and they don't count as on the road. Read congestion with
  vehicle counts (Q7).
- **One run per scenario** — reproducible (fixed seed) but a single sample. Several seeds with a
  reported range would be more rigorous.

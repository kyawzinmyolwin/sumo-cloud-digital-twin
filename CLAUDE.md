# CLAUDE.md — sumo-cloud-digital-twin

Project context for Claude Code. Read this before touching any file.

---

---

## Project roadmap (current status)

### Phase 0 — Pre-build (complete)
- [x] Project proposal written and submitted for examiner approval
- [x] Architecture, hour budget, and feature set locked in
- [x] CLAUDE.md committed to repo root
- [x] AWS chosen as cloud platform
- [x] Terraform chosen as IaC tool
- [x] All AWS resources identified and diagrammed

### Phase 1 — Core build (~200 hours, Weeks 1–21)
- [x] Wk 1–2: Environment setup (15h)
      SUMO built from source on macOS (~/sumo/sumo-1.27.1); existing sim confirmed running.
- [x] Wk 3–6: JSON emitter + WebSocket server (25h)
      DONE. scripts/emitter.py + run_traci.py --emit. Unit-tested and verified live against
      real SUMO. Branch phase1-websocket-emitter (2 commits, pushed; PR not yet opened).
- [x] Wk 7–12: Cloud data pipeline (50h)
      DONE — deployed & verified end-to-end. infra/ Terraform: WebSocket API Gateway,
      3 Lambdas (ingest/metrics/replay), DynamoDB connections table, S3+CloudFront,
      Secrets Manager, CloudWatch logs + billing alarm. Live on AWS ap-southeast-2 +
      InfluxDB Cloud. Open: wire the emitter to the cloud (client mode) ← NEXT.
- [~] Wk 13–18: Live dashboard (55h) — CORE DONE (built early, out of order)
      intersection_map.html has the WebSocket client, speed-coloured vehicle markers,
      3 live Chart.js panels (count / avg speed / congestion) and a pause/resume
      button — all verified live. S3+CloudFront hosting is deployed (infra); still to
      do: upload the HTML to S3, and wire the historical charts to the replay endpoint.
- [~] Wk 19–21: CI/CD, docs, demo recording (40h)
      CI/CD DONE (wk of 9 Oct): `.github/workflows/ci.yml` runs the unit tests on every
      push/PR (matrix Python 3.11 + 3.12; 86 tests green), test deps in
      `2D_simulation/requirements-dev.txt`. Plus the existing `deploy-dashboard.yml` (Pages).
      DOCS DONE (wk of 9 Oct): portfolio `README.md` (rewritten from the old SUMO/Unity blurb),
      `docs/ARCHITECTURE.md` (Mermaid: data-flow + local + cloud views + component/port ref),
      and the final technical report `docs/FINAL_REPORT.md` + Word export `docs/FINAL_REPORT.docx`
      (pandoc; regenerate after edits). Still to do: SCREENSHOTS in the report + README (needs
      the running system — then re-export the `.docx`), a live-demo URL, and the 2-min demo video.

### Phase 2 — Extension features (~97 hours, Weeks 22–28)
**DIRECTION CHANGE (2026-09-16): the project pivots to a "scenario / decision-support
twin".** Instead of replaying historical traffic, the twin now *perturbs the calibrated
street-net model* (crash / road closure / event demand surge, injected via TraCI) and
measures the impact — the actual point of a digital twin. Rationale: the historical
counts→CBD path is estimation on a schematic graph (sparse peak-only surveys, connectivity
gaps) and hard to defend as "real"; incident/what-if scenarios build on the *strong*
calibrated model, are honestly framed as synthetic, and are technically smaller work. The
counts + ETL pipeline are NOT dropped — they become the real-data-calibrated **baseline**
to compare incidents against, plus a legitimate cloud-engineering showcase.

Decision-support increments (supersede the old scenario-comparison / alerts line items):
- [x] Inc 1: Incident-injection hook in run_traci.py — DONE (wk of 21 Sep). `incidents.py`
      (close_edge/close_lane/set_speed/add_vehicles via TraCI; applies+reverts; 9 unit tests),
      `--incident-file`/`--close-edge`. Plus supporting tooling: ETL→scenario data bridge
      (`scenario_bridge.py`, `fetch_counts_for_date.py`), edge-picking helpers
      (`list_edges.py`, `edges_geojson.py`) and a clickable edge overlay on the dashboard.
- [x] Inc 2: Scenario metrics + compare view — DONE (wk of 30 Sep).
      (a) `scenario_id` tagged end-to-end: run_traci `--scenario-id` → emitter snapshot →
          compute_tick_metrics → scenario_id InfluxDB tag (local metrics_writer AND the cloud
          metrics Lambda, kept in sync);
      (b) dashboard COMPARE VIEW overlays two scenario_id runs (baseline vs incident), backed
          by a validated `scenario` filter on replay_server.py and the traffic-replay Lambda,
          plus a quantified summary line (mean of each + % change, directional);
      (c) design note at scripts/scenarios/DECISION_SUPPORT_DESIGN.md.
      Absorbed the old "scenario comparison (30h)" item.
- [x] Inc 3: Dashboard controls + congestion-alert overlay — DONE (wk of 2 Oct).
      Part 1: "Congestion alerts" panel flags segments slow (speed < threshold) for N
      consecutive live ticks; lists them and highlights them red on the road overlay
      (folds in the deferred threshold-metrics-panel idea).
      Part 2: local-ws CLICK-TO-INJECT — click a road → "Close live" closes that edge in the
      running sim (emitter ControlInbox + inbound WS read → incident_from_control →
      IncidentController.add each tick). LOCAL ws path only; the cloud API Gateway control
      path is a separate larger build (see future extension). Absorbed the old "congestion
      alerts (15h)" item.
      Part 2 refinements (wk of 3 Oct): the live button is a TOGGLE — "Close live" (open-ended)
      ↔ "Reopen road (resume)", which sends {action:"reopen"} → IncidentController.reopen_all(t)
      to lift the block WITHOUT stopping the sim (traffic recovers as the queue drains); the
      dashboard shows the SIM CLOCK (HH:MM from snapshot simTime); and the Scenario-control panel
      gained a "Start road block" field+button that takes the clicked edge id (validated,
      --close-edge=<id>) and starts a fresh run with it closed from the run start (06:30), so a
      non-IT user never types a sim-time or a command.
- [x] GUI scenario control — DONE (wk of 3 Oct, scope addition for non-IT users).
      `control_server.py` (stdlib HTTP, port 8799) runs on the SUMO machine and spawns
      run_traci.py for NAMED PRESETS only (baseline_am / crash_arterial / roadworks in its
      PRESETS registry) — never arbitrary args, so the spawn endpoint stays safe. Endpoints:
      /scenarios, /status, POST /start?scenario=, POST /stop; one sim at a time. Dashboard
      "Scenario control" panel (Start buttons + Stop + status) drives it via `?control=`
      (default http://localhost:8799) and auto-connects the live feed after Start — so a
      non-IT user runs the whole thing from the browser, no command line. 5 unit tests
      (fake Popen). LOCAL/VM only (spawns processes; hosted CloudFront can't reach a
      localhost control server — a cloud start/stop via API Gateway + SSM is a later build).
- [x] Inc 4 (v1): Impact report — DONE (wk of 5 Oct). An "Impact report" button on the
      Compare panel computes baseline-vs-incident impact from the stored metrics over the
      selected range: avg speed (km/h) + drop%, congestion index + rise, stopped (mean & peak),
      vehicles (mean), each with a directional Δ (speed drop / congestion rise = worse, red).
      Pure `computeImpact()` uses per-series aggregates (mean/max) — no timestamp alignment
      (the two runs happen at different wall-clock times). Fetches raw points (no aggregation)
      so peaks are real. Reuses the Compare panel's two scenario_id inputs + range.
      v2 (deferred, +4–5h): true per-vehicle delay via `traci.vehicle.getTimeLoss` and
      affected-area (distinct congested edges) — both need new emitted fields; worth it only
      if time allows. Recovery-time also deferred (needs careful post-reopen alignment).

Future extension (deferred by choice, not scheduled yet):
- [ ] Incident REROUTING / detours (~6–9h incl. on-VM tuning): when an edge is closed,
      divert approaching traffic around it instead of queueing/crawling through. TraCI
      `edge.adaptTraveltime(closed_edge, huge)` + `vehicle.rerouteTraveltime(v)` for vehicles
      whose route still includes the edge, reset on revert; needs `--time-to-teleport -1`
      (a `--no-teleport` passthrough in run_traci.py) so stuck cars divert instead of
      teleporting through. NOT a proposal M4 feature — an enhancement to the incident hook;
      considered and deliberately deferred (2026-09) to protect Inc 2 (the on-schedule,
      proposal-committed scenario comparison) and the untouched M5. Do after Inc 3 (now done).
- [x] Cloud control path — DONE (wk of 12 Oct). The HOSTED dashboard can now start/stop
      named-preset runs + start a road-block on a clicked edge, over the cloud. New
      `infra/control_api.tf`: an authenticated HTTP API → `control-authorizer` Lambda
      (shared-token check) → `traffic-control` Lambda that SSM-rewrites the sim host's
      `/etc/sumo-emitter.env` scenario args and restarts the service (sim host userdata
      parameterised with an `EnvironmentFile` + `$SCENARIO_ARGS`). Mirrors
      control_server.py's routes (`/scenarios`,`/status`,`/start`,`/stop`,`/start_roadblock`)
      so the SAME dashboard panel drives it — just `?control=<api>&token=<token>` (a
      `scnFetch` helper sends the bearer header). SAFETY: preset allowlist + validated edge
      only (no arbitrary args); least-priv IAM (ssm:SendCommand restricted to the sim host
      by tag). 9 unit tests (`test_cloud_control.py`). Guide: `docs/CLOUD_CONTROL.md`.
      v1 limits: sim host must already be running (/start switches scenario, doesn't boot
      EC2); presets + click-a-road (not a free-form builder). Not yet `terraform apply`-ed.

Deploy polish — DONE: `deploy_dashboard.sh` now ships `edges.geojson` and prints the
fully-wired URL (ws + replay + counts + edges); `dashboard_url.sh` matches. The hosted
dashboard serves live vehicles, the road overlay, history and the compare view (the old
replay-URL 403 is gone — `?replay=` uses the API Gateway endpoint, not the Function URL).

Non-IT usability — DONE (wk of 6 Oct):
- `control_server.py` now AUTO-STARTS `metrics_writer.py` with each scenario (spawned after
  the sim, terminated on Stop; `status()` reports `"writer"`; `--no-writer` opts out). This
  closes the "forgot the writer" trap — the GUI path fills InfluxDB with no separate terminal.
  New tests in `test_control_server.py` (writer spawned + stopped; `--no-writer` spawns only
  the sim). 11 unit tests pass.
- One-command local demo: `start_demo.sh` / `stop_demo.sh` at repo root. `start_demo.sh`
  brings up the four long-running services — InfluxDB (docker compose → docker-compose →
  `docker run` fallback, since the VM has no compose), dashboard web :8000 (serves
  `2D_simulation/`), replay :8788, control :8799 — backgrounded with pidfiles+logs in `.demo/`
  (gitignored), health-checked, then prints the dashboard URL and the VM ports to forward. It
  does NOT start the sim/writer — those come from the dashboard's Start button (control_server
  + auto-writer). `stop_demo.sh` kills by pidfile and stops the InfluxDB container (keeps data;
  `--wipe` removes the container). Binds 0.0.0.0 by default for VM port-forwarding (`BIND=127.0.0.1`
  to restrict). So the whole local demo is: `./start_demo.sh` → open the URL → click Start.

Deferred / de-prioritised by the pivot:
- [~] Historical replay scrub bar (25h) — counts replay is now the baseline, not a headline
      feature; keep only if time allows
- [ ] Threshold metrics panel (12h) — folds into Inc 3's congestion overlay
- [x] Docker Compose one-command demo (15h) — DONE (wk of 9 Oct). `docker compose up` runs
      the whole local stack: InfluxDB + sim (SUMO via `ppa:sumo/stable` + run_traci --emit) +
      metrics writer + replay read-API + dashboard web. `docker/sim.Dockerfile` (SUMO) +
      `docker/app.Dockerfile` (stdlib/py services); code+data bind-mounted (the 2D_simulation
      tree is ~0.6 GB, so not baked in). Validated with `docker compose config`; build/run not
      exercised in the cloud dev box (no daemon) but the SUMO recipe matches the verified
      `sim_host.tf`. Guide: `DOCKER.md`. Local-only, parallel to the AWS deploy.

### Phase 3 — Portfolio wrap-up (Weeks 29–30)
- [x] Portfolio README (root `README.md`) + architecture diagrams (`docs/ARCHITECTURE.md`)
- [x] Final technical report — `docs/FINAL_REPORT.md` (+ `.docx` export)
- [ ] Screenshots — add to the final report where needed (dashboard live view, compare view,
      impact report, congestion overlay) + README, then re-export the report `.docx`
- [ ] Live demo URL
- [ ] 2-min demo recording
- [ ] CV update and LinkedIn write-up
- [ ] Start applying at week 21 — do not wait until hour 300

---

## Current status (updated 2026-08-19)

<!-- ============================================================= -->
## SESSION LOG — 2026-10-14 (report finalisation + real-data validation + cloud evidence)

**Working branch:** `claude/next-step-nun1nw` (all work below committed + pushed; CI green; PR #19 open).
Main is far behind — keep developing on this branch.

**The report (two lineages — don't confuse them):**
- The author's authoritative doc is now **`Draft_Final_Report_v5`** (they hand-edit it in Word; added
  real screenshots; evidence moved to Appendix A `Figure_A1..`, deployment evidence in Appendix B
  `Figure_B1..B7`). We only have the **PDF** of v5, not the .docx.
- Earlier, we maintained **`Draft_Final_Report_v1_REVISED.docx`** (edited via unzip→edit
  `word/document2.xml`→rezip, in scratchpad `draftreview/unpacked/`). The author has since moved to
  v5, so v1_REVISED is superseded — but the XML-editing workflow there is the template for docx edits.
- `docs/FINAL_REPORT.md`/`.docx` in the repo is an OLDER version (not synced to the author's draft).
- Report structure follows the official COMP693 template; project is **Industry-Mentored**, host
  **JIX Reality**, author **Kyaw Zin Myo Lwin (ID 1169799)**, mentor **Hansi Udapola**, date 18 Oct 2026.

**Decision-support twin — Option A validation (DONE):** `scripts/validate_against_counts.py` validates
the baseline SUMO run against the real CCC/Miovision counts that generated its demand, scored with the
**GEH statistic** (industry standard; DfT TAG M3.1 defines the "GEH<5 for ≥85% of links" target).
- Observed demand = per-`from`-edge sum of `<flow number>` in `traffic_trips.rou.xml` (the counts, as
  loaded). Simulated = per-edge `departed` from a SUMO `edgeData` run. Join + GEH + summary.
- Helpers (`geh`, `compare`, `summarise`, parsers) pure + **11 unit tests** (`test_validate_against_counts.py`).
- `scripts/validation/edgedata_am.add.xml` = edgeData collector for AM peak (06:30–07:30 = sim 23400–27000).
- `docs/VALIDATION.md` = methodology + run recipe + honest framing (calibration/consistency check,
  NOT independent field validation) + the real result.
- **Real result (cited in report):** 24 links, GEH<5 for **88% (21/24)** — meets ≥85% target; GEH<10 92%;
  mean |err| 8%; total observed 7159 vs simulated 5596 (**−21.8%**). Deficit concentrated at ONE bottleneck
  — Durham Street South southbound `-139811056#1` (at I2859, Durham South/Moorhouse; verified via
  scripts/edge_to_intersection.py — the demand CSV's "Manchester St" approach header mislabels it)
  (215/1353 inserted, ~73% of shortfall) = insertion backlog
  under peak congestion, not lost demand.

**Test count is now 111** (was 100): 66 core (`scripts/tests`) + 45 ETL (`scripts/etl/tests`). Core
breakdown: incidents 14, control_server 11, cloud control 9, **validation 11**, emitter 6, metrics 6,
capture_results 5, replay_server 4. Reproduce per-file counts:
`python3 -m pytest scripts/tests scripts/etl/tests --collect-only -q | sed 's/::.*//' | grep '\.py$' | sort | uniq -c`.
(In a fresh cloud box the ETL tests may fail on moto version — pins live in `requirements-dev.txt`; CI is green.)

**Dashboard fixes (pushed; author re-deployed, confirmed live):**
- `infra/deploy_dashboard.sh` now uploads `intersection_geo.csv` to `s3://<bucket>/data/output/` — fixes
  the hosted "Could not load intersection_geo.csv" banner (the HTML fetches `../data/output/…` →
  `/data/output/…` at the CloudFront root; only HTML+edges.geojson were uploaded before).
- `intersection_map.html` History panel now plots on the **sim clock** (fetches `simTime` series, joins via
  `_toSimXY`, HH:MM axis, wall-clock fallback) — matches Live metrics + Compare. `node --check` passes.

**Cloud evidence helpers added:**
- `infra/functions/traffic_metrics/handler.py` now `print`s the written line-protocol on success, so a
  CloudWatch RequestId ↔ the InfluxDB point correlate 1:1 (same scenario_id + field values). **Needs
  `terraform apply` to take effect.** (Option 2 of the CloudWatch↔InfluxDB-join question.)
- Evidence recipes given to author (not code): R2 Lambda metrics via `aws lambda invoke` (payload tagged
  `scenario_id=r2_metrics_evidence` → `{ok:true,vehicleCount:5}` + CloudWatch + InfluxDB point); WS API
  "receiving data" via a two-client `wscat` demo (send on A, fan-out received on B; route selection
  `$request.body.action`, fan-out skips sender). Redaction: blur AWS account IDs (secret ARNs), live
  endpoint ids, `sim_host_public_ip`; never share `control_api_token`/InfluxDB token.
- Two clocks: InfluxDB row timestamp = wall-clock write time (keeps runs separate; retention/range
  queries work); `simTime` is a field the dashboard plots against. Defensible event-time-vs-processing-time
  design — do NOT set the row timestamp to sim-time (would collide across runs).

**Report limitations added (in v5 + v1_REVISED):** data-dependency limitation — the whole model's accuracy
is bounded by the CCC/Miovision counts (historical, peak-snapshot, surveyed-intersections-only, aged).
Added to both §5.3 Conclusions (full) and Executive Summary (one line, in v1_REVISED).

**OPEN — v5 report compliance fixes (requested a check vs COMP693 grading rubric; not yet applied —
author must send the v5 .docx):**
1. Unify test count to **111** (Exec Summary says "100", Conclusions "100+", §4.2 "111").
2. §4.2 figure numbering broken: lead-in cites Figs 8/9/10 but labels are 9/10/12, **Figure 11 skipped**,
   "Figure _10" stray space. Renumber testing figs 9(CI)/10(local)/11(terraform)/12(validation) + fix refs.
3. **Duplicate `Figure_A3`** (Lambda Functions + Lambda metrics) → renumber Appendix A A1–A9; fix Requirements
   table (R2 Lambda metrics points to A1, should point to the Lambda-metrics figure).
4. References: move **Department for Transport (2024)** into alphabetical order (after Deng, before Docker).
5. **Cite DfT TAG in-text** at the GEH ≥85% claim in §4.2 (currently uncited reference).
6. Standardise figure caption punctuation; remove title-page grey highlight; add in-text citations for
   tool refs (SUMO=Lopez et al. 2018, etc.); verify Appendix A has the GitHub + deployed CloudFront URLs.

**Rubric note:** report-relevant criteria are **1–6** (7–8 = presentation/viva, deferred). Report content
+ structure fully comply; open items are accuracy/citation polish. Grading = single global grade, mid-point
of band = mark; greatest weight on appropriateness/quality/method/originality/reflection.

**docx-editing workflow (for future report edits):** `pip install defusedxml lxml`; unpack with
`unzip`; main part is `word/document2.xml` (NOT document.xml — merge_runs.py hardcodes the wrong name, so
edit XML directly); runs are mostly whole (Google-Docs export); escape `& < >`; rezip `zip -Xqr`; render
check via `soffice.py --convert-to pdf` + `pdftoppm` (pdftotext page numbers are off-by-one vs pdftoppm).
Export md→docx via `pandoc --resource-path=docs` with arrows ASCII-replaced (`→`→`->`) for font compat.
Mermaid→PNG via `npx @mermaid-js/mermaid-cli@11.4.2 -p puppeteer.json` (chromium at
`/opt/pw-browsers/chromium-1194/chrome-linux/chrome`, `--no-sandbox`).

**Commit footer (this session):**
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` +
`Claude-Session: https://claude.ai/code/session_01JPrcUrs3JqCy2UmTJDjuip`. No model id in commits/PRs.
<!-- ============================================================= -->

**Done (all merged to main)**
- Phase 1 emitter: `scripts/emitter.py` (`serialize_vehicles`, `Broadcaster`, async WS server)
  plus `run_traci.py --emit`. Unit-tested (`scripts/tests/test_emitter.py`), verified live.
- Live dashboard core: `intersection_map.html` — WebSocket client, speed-coloured markers
  (red <3 m/s, amber 3–8, green ≥8), 3 Chart.js panels (count / avg speed / congestion),
  pause/resume. Verified live.
- Local metrics pipeline: `scripts/metrics.py` (`compute_tick_metrics`, pure + unit-tested,
  `tests/test_metrics.py`) and `scripts/metrics_writer.py` (WS feed → compute → InfluxDB).
  Verified end-to-end: SUMO → emitter → writer → local InfluxDB.
- Local InfluxDB: `docker-compose.yml` + `.env` (InfluxDB 2.7). On the Intel Mac Mini use
  **Colima** (Docker Desktop is unsupported on macOS 13): `colima start && docker compose up -d`.
- **Phase 2 cloud pipeline — BUILT, DEPLOYED, VERIFIED end-to-end.** `infra/` Terraform:
  WebSocket API Gateway, 3 Lambdas (ingest/metrics/replay), DynamoDB connections table,
  S3+CloudFront dashboard host, Secrets Manager, CloudWatch logs + billing alarm. Deployed to
  AWS `ap-southeast-2` + InfluxDB Cloud. Verified: a test message → API Gateway → ingest →
  metrics (`compute_tick_metrics`) → InfluxDB Cloud (the `5.5` point). `metrics.py` is copied
  into `infra/functions/traffic_metrics/` as the Lambda body — keep the two in sync.
- **Emitter → cloud client mode DONE & verified with REAL data.** `run_traci.py --emit-target
  wss://…` (`CloudForwarder` + `wrap_sendmessage` in emitter.py) dials out to API Gateway and
  posts `{"action":"sendmessage","data":{…}}` per tick — resilient (reconnects, never crashes
  the sim), usable with or without `--emit`. Verified end-to-end from the Vagrant VM: live SUMO
  traffic → API Gateway → Lambdas → InfluxDB Cloud. Throttle with `--emit-interval N` (every
  tick would flood API Gateway); WebSocket messages cap at 128 KB (a ~600-vehicle snapshot ~50 KB).
- **Dashboard historical charts DONE (local) + verified.** intersection_map.html has a "History
  (from InfluxDB)" panel (field + range + Load) that fetches from a read endpoint and renders a
  Chart.js line. Endpoint is `scripts/replay_server.py` — the local dev mirror of the traffic-replay
  Lambda (holds the token, proxies Flux range queries, CORS). Configurable via `?replay=<url>`
  (default `http://localhost:8788/metrics`). Verified: browser → replay_server → local InfluxDB → chart.
- **Deployed dashboard fix**: intersection_map.html had a fatal duplicate-chart-block from a bad
  merge (two `let paused`/`const MAX_POINTS` → SyntaxError killed the whole page). Removed the
  stale block. The branches that carried the broken version (phase1-websocket-emitter,
  phase2-cloud-pipeline, docs/add-tutorial) have since been **deleted**, so the recurring
  re-breakage root cause is resolved; main is the source of truth.
- **On-demand cloud sim host DONE & verified.** `infra/sim_host.tf` — an EC2 box gated by
  `var.sim_host_enabled` (default false → no cost) that installs SUMO (PPA), clones the repo, and
  runs the emitter as a `sumo-emitter` systemd service forwarding to `wss://…/prod`. Access via
  SSM (no open ports). `terraform apply -var 'sim_host_enabled=true'` to demo; `=false` to tear
  down. Runs the whole producer in AWS — no laptop needed. See running_guide.md §E.
  Gotcha: user_data must start with `#!` or cloud-init skips it (hit this once).

**Next**
- Replay **public-URL access** (403): the Lambda Function URL denies anonymous access on this
  account even with AuthType NONE + a public resource policy (NOT an org SCP — the account
  isn't in an org; cause unresolved, likely an account restriction). The replay *function*
  works (verified via `aws lambda invoke`). To make the DEPLOYED dashboard's history charts work,
  front replay with API Gateway (or IAM-signed requests) and point the page at it via
  `?replay=<url>` — a public Function URL won't work here.
- Upload `intersection_map.html` to the S3 bucket (aws s3 cp) + CloudFront invalidation so the
  hosted dashboard serves the current version.

**Runtime gotchas (learned the hard way)**
- The emitter needs `pyproj` at runtime (via `sumolib.convertXY2LonLat`) — without it it crashes
  on the first broadcast. Run from a venv populated with `pip install -r requirements.txt`.
- Working SUMO on the Mac is the **from-source build**: `SUMO_HOME=~/sumo/sumo-1.27.1`.
  `sim_pipeline.py` finds SUMO via a Windows-only path; `run_traci.py`'s `_ensure_sumo_tools`
  adds a `$SUMO_HOME` fallback so macOS/Linux work without touching the protected file.
- Ports: **8813** TraCI · **8765** emitter WebSocket · serve the map on **8000** (never 8765),
  from the `smart_city_digital_twin/` folder so its CSV fetch resolves.
- **InfluxDB Cloud ≠ local Docker InfluxDB.** The AWS Lambda can't reach `localhost:8086`; it
  writes to hosted **InfluxDB Cloud** (separate free account; `influxdb_url`/`influxdb_org` in
  tfvars, All-Access token in Secrets Manager — NOT the local `.env` token).
- **Secrets Manager blocks `destroy` → `apply`.** Deleting a secret schedules a 7-day recovery
  window; the next apply can't recreate a same-named secret, so the whole Lambda chain fails to
  create (partial state). Fix: `recovery_window_in_days = 0` in `storage.tf`, or
  `aws secretsmanager delete-secret --force-delete-without-recovery` then re-apply.
- **Lambda Function URL (auth NONE) needs an explicit `aws_lambda_permission`**
  (`lambda:InvokeFunctionUrl`, principal `*`) — the console adds it, Terraform doesn't. Even
  with it, anonymous access may still 403 on some accounts (see "Next").
- **S3 bucket names**: lowercase, hyphens (no underscores/uppercase), globally unique.
- **Region**: `ap-southeast-2` (closest to NZ + InfluxDB Cloud). Billing alarm must be
  `us-east-1` (provider alias in `observability.tf`). Pin `--region` on CLI calls, or
  `aws configure set region ap-southeast-2`.
- **Cloud is run from the Vagrant VM** (ubuntu/jammy) — terraform + aws CLI live there, not on
  the Mac Mini (old macOS). Infra costs ~$0.40/mo idle (Secrets Manager); $5 billing alarm set.

---

## AWS resources (Terraform — not yet written)

### To be created
- aws_apigatewayv2_api (WebSocket API — public endpoint)
- aws_apigatewayv2_route ($connect, $disconnect, sendmessage, $default)
- aws_apigatewayv2_stage (prod, auto-deploy)
- aws_lambda_function: traffic-ingest, traffic-metrics, traffic-replay
- aws_iam_role + aws_iam_role_policy (Lambda execution role)
- aws_secretsmanager_secret (InfluxDB token — never hardcode)
- aws_s3_bucket: dashboard hosting + Terraform state
- aws_dynamodb_table: tf-state-lock (Terraform state locking)
- aws_cloudfront_distribution (HTTPS CDN for dashboard)
- aws_cloudwatch_log_group (one per Lambda, 14-day retention)
- aws_cloudwatch_metric_alarm (billing alert at $5 threshold)
- influxdb_bucket (via InfluxDB Terraform provider — external to AWS)

### Terraform file structure (to create)
infra/
├── main.tf          ← provider config, backend (S3 + DynamoDB)
├── variables.tf     ← region, project name, environment
├── outputs.tf       ← API Gateway URL, CloudFront URL, S3 bucket name
├── api_gateway.tf   ← WebSocket API, routes, stage
├── lambda.tf        ← all three Lambda functions + IAM role
├── storage.tf       ← InfluxDB bucket, Secrets Manager, S3 buckets
├── cdn.tf           ← CloudFront distribution
├── observability.tf ← CloudWatch log groups and billing alarm
└── versions.tf      ← required_providers with pinned versions

### Setup order (do this before terraform apply)
1. Create AWS account, enable billing alerts, set up IAM user with least-privilege
2. Create S3 state bucket manually (aws s3 mb s3://your-tf-state-bucket)
3. Create DynamoDB lock table manually
4. Sign up for InfluxDB Cloud free tier, create org and bucket, copy token
5. Store InfluxDB token in Secrets Manager manually (first time only)
6. Then terraform init && terraform plan && terraform apply

### Expected monthly cost
Secrets Manager: ~$0.40/secret/month
Everything else: AWS free tier (12-month or always-free)
Total: ~$1–2/month

## What this project is

A Christchurch CBD traffic simulation (SUMO-based) extended with a cloud data pipeline and a live browser dashboard. The simulation already exists and works. Everything cloud-related is new — nothing has been built yet.

**Repo:** `kyawzinmyolwin/sumo-cloud-digital-twin`
**Student:** Lincoln University, NZ — COMP 693 Industry Project
**Budget:** 300 hours total (200h core build + 100h extension features)
**Cloud platform:** AWS (primary choice)

---

## What already exists — do not modify these

### Simulation core (complete, production-quality)
- `smart_city_digital_twin/2D_simulation/Christchurch_Central_City_main_streets.sumocfg` — main SUMO config
- `smart_city_digital_twin/2D_simulation/data/output/network/` — road network built from real Christchurch City Council + OpenStreetMap data
- `smart_city_digital_twin/2D_simulation/data/output/demand/traffic_trips.routed.rou.xml` — vehicle demand from real Miovision traffic counts
- `smart_city_digital_twin/2D_simulation/scripts/sim_pipeline.py` — 4000+ line core library, do not touch
- `smart_city_digital_twin/3D_simulation/` — Unity 3D twin, out of scope for this project

### Existing scripts to extend (not rewrite)
- `smart_city_digital_twin/2D_simulation/scripts/run_traci.py` — TraCI control loop. **Now extended** with the `--emit` JSON/WebSocket emitter (see Current status). The original stepping/`_print_status` behaviour is unchanged when `--emit` is omitted.
- `smart_city_digital_twin/2D_simulation/scripts/intersection_map.html` — Leaflet map. **Now extended** with a WebSocket client and speed-coloured vehicle markers.

### What run_traci.py currently does
Connects to SUMO via TraCI on port 8813. Jumps to sim time 23400s (06:30). Steps the simulation in a `while True` loop. Calls `_print_status()` every 60 sim seconds — this just prints to terminal. No JSON output, no WebSocket, no data persistence. The per-step hook is here:

```python
traci.simulationStep()
if int(t) % 60 == 0:
    _print_status(traci)
```

This is where the emitter call goes.

### What intersection_map.html does
A Leaflet map centred on Christchurch CBD (-43.53, 172.636). Fetches `../data/output/intersection_geo.csv` on load and renders 96 intersection nodes and their directional links (Leaflet 1.9.4 from CDN). **It now also has a live vehicle layer:** a WebSocket client (`ws://localhost:8765`, Connect/Disconnect buttons) that consumes the emitter schema and draws one circle marker per vehicle, colour-coded by speed via `colorForSpeed` (red <3, amber 3–8, green ≥8 m/s), adding/moving/removing markers each tick. Still missing (roadmap Wk 13–18): the 3 Chart.js panels and a pause/resume control.

---

## What we are building — in order

### Phase 1: JSON emitter + WebSocket server (first task)
Extend `run_traci.py` to emit vehicle state as JSON over WebSocket every simulation step.

**Target JSON schema:**
```json
{
  "tick": 1720123456789,
  "simId": "christchurch-cbd-001",
  "simTime": 23460.1,
  "vehicleCount": 214,
  "vehicles": [
    {
      "id": "veh_001",
      "lat": -43.5321,
      "lng": 172.6362,
      "speed": 13.4,
      "lane": "edge_42_0",
      "accel": 0.2,
      "type": "car"
    }
  ]
}
```

**Implementation notes:**
- SUMO XY coordinates must be converted to WGS84 lat/lon using `sumolib.net.convertXY2LonLat()` — the net file is at `data/output/network/Christchurch_Central_City_main_streets.net.xml`
- Use `asyncio` + `websockets` library for the WebSocket server
- Run the WebSocket server and TraCI loop concurrently — asyncio event loop wrapping the sync TraCI calls
- Server listens on port 8765
- Emit every step (not just every 60s) when clients are connected
- Send a snapshot of current state to any new client on connect (don't wait for next tick)
- Configurable tick rate via `--emit-interval` arg (default: every step)
- Unit test the JSON serialiser independently from the TraCI connection

### Phase 2: Cloud data pipeline
- AWS API Gateway (WebSocket API) as the public-facing endpoint
- AWS Lambda for metrics aggregation per tick (flow, avg speed, congestion index)
- InfluxDB Cloud (free tier) for time-series storage
- Infrastructure as code: AWS CDK or Terraform
- GitHub Actions CI/CD pipeline

### Phase 3: Live dashboard (extends intersection_map.html)
- Add WebSocket client to existing Leaflet map
- Animated vehicle markers — colour-coded by speed (green/amber/red)
- 3 Chart.js panels: vehicle count, avg speed, density over time
- Pause/resume button calling the API
- Deploy static HTML/JS to GitHub Pages or S3+CloudFront

### Extension features (after core build, ~100h budget)
1. Congestion alerts — flag segments where avg speed < threshold for N consecutive ticks
2. Scenario comparison — two parallel SUMO runs tagged by scenario_id, metrics shown side by side
3. Historical replay — scrub bar querying time range from InfluxDB, playback at variable speed
4. Threshold metrics panel — Chart.js threshold lines showing normal vs congested ranges
5. Docker Compose — one command brings up sim + API server + DB

---

## Key technical decisions (already made, do not relitigate)

| Decision | Choice | Reason |
|---|---|---|
| Cloud platform | AWS | Better WebSocket API Gateway free tier, larger NZ community |
| Real-time framing | Simulation output only | CCC data sources are historical download-only, not live sensors |
| Frontend base | Extend intersection_map.html | Already has working Leaflet setup for Christchurch CBD |
| Containerisation | Docker + Docker Compose | Right-sized for this project; Kubernetes is a stretch-goal only |
| Time-series store | InfluxDB Cloud (free tier) | Designed for tick data; free tier sufficient for prototype |
| CI/CD | GitHub Actions | Already using GitHub; most NZ employers recognise it |

---

## Data sources (context only — no code changes needed)

- **CCC Intersection Traffic Counts** — historical Miovision survey data, download-only, no API. Already parsed by `traffic_counts_parser.py`. Used to build demand, not streamed live.
- **CCC ArcGIS Hub** — intersection geometry. Has a REST API (`gis.ccc.govt.nz/arcgis/rest/services/OpenData`) but used as a one-time calibration source. The downloaded `intersection_geo.csv` is what the map reads.
- **SUMO via TraCI** — the actual real-time data source. Every simulation step produces fresh vehicle state.

---

## Project structure (current)

```
sumo-cloud-digital-twin/
├── CLAUDE.md                          ← this file
├── Unity_fundamental/                 ← unrelated tutorial project, ignore
└── smart_city_digital_twin/
    ├── README.md
    ├── 2D_simulation/
    │   ├── Christchurch_Central_City_main_streets.sumocfg
    │   ├── requirements.txt
    │   ├── data/
    │   │   ├── input/                 ← source data (Miovision, OpenData)
    │   │   └── output/
    │   │       ├── network/           ← .net.xml files
    │   │       ├── demand/            ← .rou.xml files
    │   │       └── intersection_geo.csv
    │   └── scripts/
    │       ├── run_traci.py           ← extended with --emit (Phase 1, done)
    │       ├── emitter.py             ← NEW: serialiser + WebSocket broadcaster
    │       ├── tests/test_emitter.py  ← NEW: serialiser unit tests (no SUMO needed)
    │       ├── sim_pipeline.py        ← do not modify
    │       ├── intersection_map.html  ← extended with WS client + vehicle markers
    │       └── [other pipeline scripts — do not modify]
    └── 3D_simulation/                 ← Unity project, out of scope
```

---

## Environment setup

```bash
cd smart_city_digital_twin
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r 2D_simulation/requirements.txt   # includes websockets + pyproj — both needed by the emitter
```

> The emitter needs `pyproj` at runtime (sumolib coordinate conversion) and `websockets`
> for the server. Both are in requirements.txt, so always run the emitter from a venv you
> populated with `pip install -r requirements.txt` — a minimal venv will crash on first broadcast.

SUMO must be installed and on PATH. Project tested with SUMO 1.27.1 (also fine for the
1.26 target — no config changes needed between them).
SUMO_HOME must be set (e.g. `C:\Sumo` on Windows or `/usr/share/sumo` on Linux).

Run existing simulation to confirm it works before touching anything:
```bash
cd smart_city_digital_twin/2D_simulation
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg
```

### macOS setup (this machine, verified 2026-07-15)

The `dlr-ts/sumo` Homebrew tap is dead — its formula uses a removed Homebrew API
(`cxxstdlib_check`) and fails to install on current Homebrew. conda-forge's `sumo`
package and PyPI's plain `sumo` package are also **not** this project's SUMO — they're
an unrelated materials-science bandstructure tool. The real package on PyPI is
`eclipse-sumo`, but it only ships up to 1.20.0 and needs exact-version system libs
(xerces-c 3.2, proj 25, FOX 1.6) that are painful to match by hand.

**What actually works: build from source.**

1. Download the source tarball from https://sumo.dlr.de/docs/Downloads.php (there is
   no signed macOS binary/installer — source is the only macOS-relevant download) and
   extract it, e.g. to `~/sumo/sumo-1.27.1`.
2. Install build deps via Homebrew: `brew install cmake xerces-c proj`.
3. Headless build (no GUI, no Xcode needed — this is all `run_traci.py` / TraCI
   control needs):
   ```bash
   cd ~/sumo/sumo-1.27.1
   mkdir -p build/cmake-build-headless && cd build/cmake-build-headless
   cmake ../.. -DCMAKE_BUILD_TYPE=Release
   cmake --build . --target sumo netconvert duarouter netgenerate od2trips \
     jtrrouter marouter activitygen polyconvert dfrouter -j$(sysctl -n hw.ncpu)
   ```
   Binaries land in `~/sumo/sumo-1.27.1/bin/`. Confirm the "Enabled features" cmake
   log line does *not* mention GUI — that's what avoids the dependency chain below.
4. Set in `~/.bash_profile` (or shell equivalent):
   ```bash
   export SUMO_HOME="$HOME/sumo/sumo-1.27.1"
   export PATH="$SUMO_HOME/bin:$PATH"
   ```
5. `run_traci.py --no-gui` is the flag to use — the script defaults to `--gui`, which
   needs the GUI build below.

**Adding `sumo-gui` without installing full Xcode:**

Homebrew's `fox` formula (SUMO's GUI toolkit) depends on `mesa`, and current `mesa`
(post–Dec 2023) hard-depends on `molten-vk`, which refuses to build without a full
Xcode.app install (Command Line Tools alone aren't enough) — a non-starter if you're
not upgrading Xcode/macOS. The fix: `mesa` had a bottled, Xcode-free formula revision
right up until that dependency was added. Pin to it via a throwaway local tap:

```bash
brew tap-new local/legacy-gl
TAP=/usr/local/Homebrew/Library/Taps/local/homebrew-legacy-gl/Formula
curl -s https://raw.githubusercontent.com/Homebrew/homebrew-core/83d2ce266d/Formula/m/mesa.rb -o "$TAP/mesa.rb"
curl -s https://raw.githubusercontent.com/Homebrew/homebrew-core/190c1da9ec/Formula/m/mesa-glu.rb -o "$TAP/mesa-glu.rb"
curl -s https://raw.githubusercontent.com/Homebrew/homebrew-core/f6f5852d56716a3f288dde936c47b855f91fbcd0/Formula/fox.rb -o "$TAP/fox.rb"
```

Then edit those three files:
- In `mesa.rb`: delete the `:build`-only deps (`bison`, `meson`, `ninja`, `pkg-config`,
  `pygments`, `python-mako`, `python-setuptools`, `python@3.12`, `xorgproto`) — they've
  since been renamed/removed upstream and aren't needed since we're installing from
  the bottle, not compiling.
- In `mesa-glu.rb`: replace `depends_on "meson"/"ninja"/"pkg-config" => :build` and
  `depends_on "mesa"` with just `depends_on "local/legacy-gl/mesa"` (otherwise Homebrew's
  solver pulls the *current* `mesa` — the one needing `molten-vk` — as the dependency).
- In `fox.rb`: replace `depends_on "mesa"` / `"mesa-glu"` with
  `depends_on "local/legacy-gl/mesa"` / `"local/legacy-gl/mesa-glu"` for the same reason.

Then:
```bash
brew install local/legacy-gl/mesa local/legacy-gl/mesa-glu local/legacy-gl/fox
```
All three pour from prebuilt `ventura` bottles — no compiling, no Xcode.

Rebuild SUMO with GUI enabled (same source tree, a separate build dir so the headless
one is untouched):
```bash
cd ~/sumo/sumo-1.27.1
mkdir -p build/cmake-build-gui && cd build/cmake-build-gui
cmake ../.. -DCMAKE_BUILD_TYPE=Release   # "Enabled features" line should now say GUI
cmake --build . --target sumo-gui -j$(sysctl -n hw.ncpu)
```
`sumo-gui` lands in the same `~/sumo/sumo-1.27.1/bin/` (SUMO's CMake install rule
targets the source tree's top-level `bin/` regardless of build dir).

**Running `sumo-gui`:** this FOX build is X11-based, not native Cocoa, so it needs
XQuartz as a display server (`brew install --cask xquartz` — this step needs an
interactive sudo password, so run it yourself in a real terminal, not through an
agent's non-interactive shell):
```bash
open -a XQuartz        # start the X server once per login (or set it to auto-launch)
export DISPLAY=:0
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg
```

Sanity checks after setup:
```bash
sumo --version && sumo-gui --version
python3 -c "import sys,os; sys.path.insert(0, os.environ['SUMO_HOME']+'/tools'); import sumolib, traci; print('OK')"
python3 scripts/run_traci.py --no-gui --jump-to 23400 --end 23430   # real TraCI run
```

---

## Where to start — next task

Phase 1 (emitter), the live-map client, and the Phase 2 cloud pipeline are all **done** (see
Current status). **The project has pivoted to a scenario / decision-support twin** (see the
Phase 2 direction-change note above). **Increment 1 (the incident-injection hook) is DONE** —
`incidents.py` + `run_traci.py --incident-file/--close-edge`, plus the ETL→scenario bridge and
the clickable edge overlay. The **current task is Increment 2: scenario-tagged metrics +
compare view** — (a) write metrics into InfluxDB tagged by a `scenario_id` (metrics_writer.py
tags only `simId` today), (b) a dashboard compare view (baseline vs one scenario side by side),
(c) a short design note fixing the first 2–3 scenarios and success criteria before building.
Then Inc 3 (click-to-inject live + congestion overlay) → Inc 4 (impact metrics).

The counts→CBD scenario tooling (make_cbd_scenario.py, fetch_counts_for_date.py, the ETL
bridge) stays as the real-data **baseline** path, not the headline feature.

<details><summary>Original Phase 1 emitter brief (completed — kept for reference)</summary>

1. Load the SUMO net file with `sumolib` to get the coordinate converter
2. Write a `serialize_vehicles(traci, net)` function that returns the JSON schema above
3. Write an async WebSocket server using `websockets` that broadcasts to all connected clients
4. Wrap the existing synchronous TraCI loop so it runs inside an asyncio event loop
5. Call `serialize_vehicles()` each step and broadcast if any clients are connected
6. Add `--emit-interval SEC` argument (default 1 step, i.e. every step)
7. Write a unit test for `serialize_vehicles()` using a mock traci object

Existing TraCI connection logic, argument parser structure, and `_print_status` were left
unchanged — the emitter was added alongside, not in place of, them.
</details>

---

## Coding conventions (match existing repo style)

- Python 3.10+ type hints where the existing code uses them
- `from __future__ import annotations` at top (existing pattern)
- Argparse for all CLI arguments (existing pattern)
- No f-string format for SUMO commands — use list concatenation (existing pattern)
- Keep new functions at module level, not nested inside `main()`
- One commit per logical step

---

## What "real-time" means in this project

The simulation generates fresh vehicle position and speed data every step. The emitter pushes that data to connected clients within milliseconds of each step completing. That pipeline latency is what "real-time" refers to — not live sensor feeds from physical Christchurch roads. The council data sources are historical calibration inputs, used once at setup time.

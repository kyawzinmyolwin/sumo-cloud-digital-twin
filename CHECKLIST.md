# Project checklist — demo_smart_city_digital_twin

COMP 693 Industry Project. Snapshot of what's done and what's left.
Source of truth for detail is `CLAUDE.md`; this is the quick status view.
_Updated 2026-10-03._

## ✅ Done

### Core build (Phase 1 + cloud pipeline)
- [x] JSON emitter + WebSocket server (`run_traci.py --emit`, `emitter.py`) — unit-tested
- [x] Live dashboard: WebSocket vehicle layer, speed-coloured markers, 3 Chart.js panels, pause/resume
- [x] Cloud pipeline (Terraform): WebSocket API Gateway → ingest/metrics/replay Lambdas → InfluxDB Cloud; S3+CloudFront; Secrets Manager; billing alarm — deployed & verified
- [x] Emitter → cloud forwarding (`--emit-target`); on-demand EC2 sim host
- [x] Local metrics pipeline (`metrics.py`, `metrics_writer.py`) + InfluxDB; history charts

### Decision-support twin (the pivot)
- [x] Incident-injection hook — close edge/lane, speed limit, demand surge (`incidents.py`, `--incident-file`/`--close-edge`)
- [x] Edge tooling — `list_edges.py`, `edges_geojson.py`, clickable road overlay
- [x] ETL → scenario baseline bridge (`scenario_bridge.py`, `fetch_counts_for_date.py`)
- [x] Scenario-tagged metrics (`--scenario-id` → InfluxDB tag, local + cloud)
- [x] Compare view (baseline vs incident) + quantified summary line
- [x] Congestion-alert overlay (slow-segment flagging + red highlight)
- [x] Click-to-inject (local ws) — close a road live from the map, with a **Close live ↔ Reopen road** toggle (block then resume in one run) + a visible sim clock
- [x] GUI scenario control (`control_server.py` + panel) — start/stop from the browser, no CLI; **click a road → Start road block** (immediate close from 06:30)
- [x] Cloud deploy wired (`deploy_dashboard.sh`: live feed, overlay, history, compare on hosted page)
- [x] Impact report (Inc 4 v1) — baseline-vs-incident metrics table (avg speed, congestion, stopped mean/peak, vehicles) with directional Δ
- [x] Auto-start `metrics_writer` from `control_server.py` with each scenario (stop on Stop; `--no-writer` to opt out) — the GUI path now fills InfluxDB with no separate writer terminal
- [x] One-command local demo: `start_demo.sh` / `stop_demo.sh` (InfluxDB + dashboard web + replay + control servers, pidfiles/logs in `.demo/`) — a non-IT user runs `./start_demo.sh` then clicks a Start button

## ⏳ To do

### Committed / planned
- [ ] **Increment 4 v2 (optional)** — true per-vehicle delay (`timeLoss`) + affected-area; needs new emitted fields
- [x] **Milestone 5 — Docker Compose one-command bring-up** — `docker compose up` runs the full stack: InfluxDB + sim (SUMO + emitter) + metrics writer + replay read-API + dashboard web. SUMO image via `ppa:sumo/stable` (same recipe as `sim_host.tf`); code/data bind-mounted. See `DOCKER.md`. (`docker-compose.yml`, `docker/sim.Dockerfile`, `docker/app.Dockerfile`)
- [x] **Milestone 5 — GitHub Actions CI/CD** — `.github/workflows/ci.yml` runs the unit tests on every push/PR, matrix Python 3.11 + 3.12 (both verified green locally: 86 tests = 41 `scripts/tests` + 45 `scripts/etl/tests`). Test deps pinned in `2D_simulation/requirements-dev.txt` (`pytest`, `boto3`, `moto`). Joins the existing `deploy-dashboard.yml` (GitHub Pages)
- [x] Final report — `docs/FINAL_REPORT.md` + Word export `docs/FINAL_REPORT.docx`, **restructured to the official COMP693 Final Report template** (title page, Executive Summary with Problem/Goal/Methods/Outcome, Authorship Statement, Glossary, 1 Introduction → 7 Appendices), with the embedded architecture diagram (`docs/img/architecture.png`, Figure 1), a Results section (Table 1 + Figures scaffold), Reflections incl. a Use-of-Generative-AI subsection, APA 7th References, and Appendix A (GitHub URL + deployed-app URL). Regenerate the `.docx` from the `.md` after edits (pandoc, `--resource-path=docs`)
  - [x] Corrected against the actual proposal (PDF): project type **Industry-Mentored** (host **JIX Reality**), author **Kyaw Zin Myo Lwin** (ID 1169799), mentor **Hansi Udapola**; proposal title, goal + 5 milestone sub-goals + success metrics, Scrum-for-solo method, the real comparative analysis (keshu-bara 2025; Deng et al. 2024) and risk list
  - [ ] **Confirm the submission date** on the title page (placeholder) and the Authorship/AI wording to your own process
  - [ ] **Capture the Results evidence** — run baseline/crash/roadworks, then `python scripts/capture_results.py` to fill Table 1; grab screenshots for Figures 2–5; re-export the `.docx`

### Phase 3 — portfolio wrap-up
- [x] Portfolio README (root `README.md`) — what it is, architecture, 3 run paths (Docker/native/AWS), tech stack, testing/CI, doc index
- [x] Architecture diagram — `docs/ARCHITECTURE.md` (Mermaid: data flow + local + cloud deployment views + component/port reference); summary diagram embedded in README
- [ ] Screenshots — add to the final report (`FINAL_REPORT.md` → re-export `.docx`) and the README; needs the running system
- [ ] Live demo URL in the README
- [ ] 2-min demo recording
- [ ] CV / LinkedIn write-up

### Planned improvements (to add later)
- [ ] **Cloud live-inject ("Close live" over the cloud).** Today "Close live" /
      "Reopen road" (inject into the *running* sim) is local-only — the cloud emitter runs
      forward-only (`--emit-target`, no local WS/`ControlInbox`) and the cloud feed is
      one-way fan-out, so there's no back-channel to the running sim. To support it: add a
      control route on the WebSocket API (or drive injection through the HTTP control API)
      **and** have the producer hold/read an inbound connection that applies
      `incident_from_control` each tick on the sim host. **Concrete approach (~8–12h):**
      browser → `POST /inject?edge=` (token) → control Lambda `postToConnection()` to the
      producer's WS connection (API Gateway WS is bidirectional); the emitter's
      `CloudForwarder` reads inbound frames into a control inbox that `run_traci` drains each
      tick → `IncidentController`. New bits: producer-side inbound read; register the
      producer's connectionId by role on `$connect` (re-register on reconnect, in the
      connections table); the `/inject` route + `execute-api:ManageConnections` IAM on the
      control Lambda. Reuses `incidents.py`, the auth token and the connections table. Needs
      live-AWS testing (reconnect edge-cases). Cloud workaround today is **Start road block**
      (fresh run with the edge closed). See `docs/Q&A.md` Q3.
- [ ] **More scenario comparisons — unique per-edge road-block id + extra presets.**
      (1) Tag each click-a-road closure with a **unique `scenario_id` per edge** (e.g.
      `rb_<sanitised-edge>`, stripping chars not in the scenario filter `[A-Za-z0-9_.:-]`)
      instead of the shared `"roadblock"`, so two different closures are independently
      comparable — makes every road on the map its own comparable scenario.
      (2) Add **2–3 more preset buttons** for variety (e.g. full-morning closure of the
      known arterial `770109405#0`, a reduced-speed corridor, a demand surge via an
      `--incident-file`). Edit BOTH registries in sync — `scripts/control_server.py`
      (local, restart) and `infra/functions/traffic_control/handler.py` (cloud,
      `terraform apply` repackages the Lambda) — and update the unit tests
      (`test_control_server.py`, `test_cloud_control.py`). See `docs/Q&A.md` Q2 for
      rationale. Low-risk, high demo value.
- [ ] **Live-feed latency / smoothness on the hosted dashboard.** The perceived lag is NOT
      CloudFront (it only serves the static page once) — the live path is EC2 emitter →
      API Gateway WebSocket → `traffic-ingest` Lambda fan-out → browser. Today the sim host
      runs `--real-time --speed 5 --emit-interval 10` (~5 msgs/s, ~50 KB each) and the map
      renders markers with Leaflet's default SVG renderer. Levers, cheapest first:
      (1) **Canvas renderer (biggest easy win, ~1 line):** create the map with
      `L.map("map", { preferCanvas: true })` — canvas draws hundreds of markers far faster
      than SVG; markers are already reused (`setLatLng`/`setStyle`), so this is the main
      remaining render lever. (2) **Shrink + slow the feed:** round `lat`/`lng` to 5 dp
      (~1 m) instead of 6, drop `accel` if unplotted (`scripts/emitter.py` `serialize_vehicles`);
      lower `--speed 5 → 2–3` (fewer msgs/s, feels *more* real-time) rather than raising
      `--emit-interval` (choppier). (3) **Client-side interpolation (the proper fix, more
      work):** tween marker positions between messages with `requestAnimationFrame` so motion
      is 60 fps-smooth regardless of the 2–5 Hz data cadence. (4) **Region reality (not
      tunable):** API Gateway WS is `ap-southeast-2` and is not accelerated by CloudFront;
      ~30–40 ms from NZ is inherent. Diagnose by symptom: choppy motion → (1); view running
      behind real-time → (2); periodic freezes → Lambda cold-start/GC. (1)+(2) are safe local
      changes (~1–2h); (3) is ~4–6h.
- [ ] **Move the cloud sim host from EC2 to ECS Fargate (~8–12h incl. live-AWS testing).**
      Lambda can't run the simulation (15-min limit, no long-lived state, can't accept incoming
      connections); Fargate runs containers with no time limit and no server to manage.
      **Why:** billed only while a run is going (the container exits at `--end`, so no forgotten
      VM); `/start` can *launch* a run instead of needing a running host; no OS patching; several
      scenarios can run in parallel (e.g. baseline + crash at once). **Keep EC2 for the
      submission** — it's built, verified and in the report; present Fargate as the next step.
      **Changes:** (1) build `docker/sim.Dockerfile` into an image and push to ECR (could be a
      GitHub Actions job); (2) replace `infra/sim_host.tf` + `sim_host_userdata.sh.tftpl` with an
      ECS cluster, task definition (scenario args as command overrides), task + execution IAM
      roles, security group and log group; (3) in `infra/functions/traffic_control/`, swap
      `ssm:SendCommand` for `ecs:RunTask` / `ecs:StopTask`, keeping the preset allowlist and edge
      validation; (4) update the IAM policy and `docs/CLOUD_CONTROL.md`. **Gotchas:** run the task
      in a **public subnet with a public IP and no inbound rules** — a NAT gateway alone costs
      ~US$40+/month; **bake the network + demand files into the image** (Compose bind-mounts the
      ~0.6 GB data tree today) or fetch them from S3 at startup; every run is a **cold start**
      (provision + image pull, tens of seconds to a minute), slower than a scenario switch on a
      warm EC2 host; **parallel runs share one live map** — both producers fan out on the same
      WebSocket API, so the map would need to filter by `scenarioId` (metrics stay separate by
      `scenario_id`); **Fargate Spot** is much cheaper but can be interrupted mid-run (fine for
      metric collection, risky in a live demo); debugging needs CloudWatch Logs or ECS Exec instead
      of an SSM shell. Hourly cost is roughly similar to a t3.medium for a comparable size — the
      saving comes from not paying between runs.

### Deferred by choice (not scheduled)
- [ ] Incident rerouting / detours (~6–9h)
- [x] **Cloud control path** — DONE (wk of 12 Oct): authenticated HTTP API → authorizer + `traffic-control` Lambda → SSM restarts the sim host's `sumo-emitter` with the chosen preset/road-block. Same dashboard panel via `?control=&token=`. `infra/control_api.tf`, `infra/functions/traffic_control|control_authorizer`, parameterised sim-host userdata, token header in the dashboard; 9 tests (100 total). Guide `docs/CLOUD_CONTROL.md`. **Pending: `terraform apply` on your account (needs `sim_host_enabled=true`).**
- [ ] Historical replay scrub bar — de-prioritised (counts replay is now the baseline)

## 🎯 Recommended allocation of the remaining ~100 h budget

The committed build is done, so this budget is discretionary. Priority: turn "built"
into "demonstrable + evidenced" first, then add depth that strengthens the
decision-support claim — not more features. **A = non-negotiable, B = the
differentiator, C/D = fill the rest.** Biggest single lever: a working hosted URL +
real scenario results in the report.

### A. Make it demonstrable & evidenced — ~30 h (do first, highest ROI)
- [ ] Finish the hosted deploy → real public URL: upload dashboard to S3 + CloudFront invalidation; front replay Lambda with API Gateway so History/Compare work on the hosted page (the two CLAUDE.md "Next" items). ~12 h
- [ ] Run the locked scenarios end-to-end (baseline / crash / roadworks); capture quantified results (Δ speed, congestion rise, recovery) + panel screenshots. ~8 h
- [ ] Insert results + screenshots into the report (re-export `.docx`) and README; add the live-demo URL. ~6 h
- [ ] Record the 2-min demo video. ~4 h

### B. Depth that backs the decision-support claim — ~30 h (the differentiator)
- [ ] Incident rerouting / detours — closures divert instead of crawl (TraCI `adaptTraveltime` + `rerouteTraveltime`, `--no-teleport`). ~6–9 h
- [ ] Impact report v2 — true per-vehicle delay (`getTimeLoss`), affected-area, recovery-time. ~8–10 h
- [ ] A third scenario (event demand surge) for breadth. ~4 h
- [ ] Actually build + run the Docker `sim` image on the VM (so far only `docker compose config`-validated); fix anything. ~4 h

### C. Engineering credibility — ~20 h
- [ ] End-to-end/integration test (emitter → writer → InfluxDB → replay round-trip), ideally a CI job with an InfluxDB service container. ~8 h
- [ ] Security-review pass (`/security-review`) over public-facing bits — `control_server` spawn safety, replay input validation, API Gateway. ~4 h
- [ ] Buffer / loose ends. ~8 h

### D. Career & write-up — ~15 h
- [ ] CV update + LinkedIn write-up. ~6 h
- [ ] Export architecture diagrams to PNG, tidy docs, polish the portfolio README. ~5 h
- [ ] Contingency. ~4 h

### ~0 h but important
- [ ] Confirm the scope pivot with the supervisor at the next review.
- [ ] Do **not** spend on the historical-replay scrub bar (old 25 h item) — de-prioritised by the pivot; that time goes to A and B.

## 📌 Loose ends worth closing
- [ ] Confirm the scope pivot (decision-support twin) with the supervisor at a milestone review
- [ ] On the hosted page, re-run `terraform apply` + fresh `scenario_id` runs so the cloud compare view has tagged data
- [ ] Rebuild intersection-graph net if you want the counts→CBD scenarios (needs `christchurch_intersections.net.xml`)

## 🧹 Cleanup candidates (unused / stale files)

Found by scanning tracked files for references across the repo (imports, docs, CI,
compose, shell, Terraform; Unity trees excluded). **Verify before deleting — git history
preserves everything, and some are legitimate one-time build artefacts worth keeping.**

### A. Safe to remove — backups / one-off / superseded (0 references)
- [ ] `smart_city_digital_twin/2D_simulation/scripts/run_traci_original.py` — pre-`--emit`
      backup of `run_traci.py` (175 vs 439 lines); superseded, and history has it.
- [ ] `smart_city_digital_twin/2D_simulation/scripts/probe_drive.py` — one-off Google
      Drive API probe from the ETL build.
- [ ] `smart_city_digital_twin/2D_simulation/scripts/bench_build_simulation.py` — one-off
      build benchmark.
- [ ] `smart_city_digital_twin/2D_simulation/scripts/calibrate_christchurch_icp.py` —
      superseded ICP calibration variant (`calibrate_christchurch.py` is the one referenced).

### B. Out-of-scope (3D / Unity-only; FBX calibration — 0 references in the 2D/cloud project)
- [ ] `smart_city_digital_twin/2D_simulation/scripts/calibrate_fbx_landmarks.py`
- [ ] `smart_city_digital_twin/2D_simulation/scripts/calibrate_fbx_sumo_transform.py`

### C. One-time build / export tools (0 references; keep for reproducibility OR move to a `scripts/tools/` subdir)
These generated data that is already committed; they are not called by active code, tests
or CI. Keeping them documents how the network/demand were built — decide keep vs archive.
- [ ] `scripts/create_timetable_buses.py`, `scripts/export_bus_route_edges.py`,
      `scripts/export_intersections_from_ids.py`
- [ ] `scripts/randomTrips.py` — a vendored copy of SUMO's stock tool (ships in
      `$SUMO_HOME/tools`); unused here.

### D. Stale / orphaned docs
- [ ] `TUTORIAL.md` — **0 references** anywhere; orphaned.
- [ ] `docs/FINAL_REPORT.md` + `docs/FINAL_REPORT.docx` — an **older** report version,
      superseded by the author's external v5. Decide: refresh the repo copy from v5, or
      drop it and keep the authoritative report external. (`.docx` is also the only tracked
      binary blob.)
- [ ] `docs/img/architecture.png` — only referenced by the stale `FINAL_REPORT.md`; the
      current figures use `integrated_architecture.png` / `existing_pipeline.png`. Drop
      together with the old report.
- [ ] `RECAP.md` — linked only from `METRICS_REPLAY.md`; review whether it is
      portfolio-facing or a personal recap that can go.

### E. Large out-of-scope directories (decide separately — structural change)
- [ ] `Unity_fundamental/` — **155 tracked files**; CLAUDE.md calls it an "unrelated
      tutorial project, ignore". Strongest single de-bloat (repo is 2012 files; this + 3D
      are ~22%). Remove from this repo unless it's intentionally part of the portfolio.
- [ ] `smart_city_digital_twin/3D_simulation/` — **284 tracked files**; the Unity 3D twin,
      "out of scope for this project". Keep only if the portfolio means to show the host
      company's broader system; otherwise it belongs in its own repo.

> Suggested order: do A + D first (low-risk, clear wins), then decide C (keep vs `tools/`),
> then B and E (bigger structural calls). A quick `git rm` per item + one commit; CI (the
> 111 tests) should stay green since none of A–D are imported by tested code.

---

**Headline:** the decision-support feature set, **Milestone 5 (Docker Compose + CI/CD)**,
the portfolio README, the architecture diagrams, and the **final technical report**
(`.md` + `.docx`) are all complete. What remains is portfolio polish that needs the
running system — **screenshots** (to drop into the report + README), a live-demo URL, and a
2-minute demo recording — plus optional enhancements (Increment 4 v2, incident rerouting).
The committed build is effectively done.

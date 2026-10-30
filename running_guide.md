# Running Guide — Christchurch Digital Twin

How to run the system in each mode. Commands assume you start from the repo root
(`sumo-cloud-digital-twin/`) unless a `cd` says otherwise.

## Prerequisites

- **SUMO** installed with `SUMO_HOME` set.
  - Mac Mini (from-source build): `export SUMO_HOME="$HOME/sumo/sumo-1.27.1"`
  - Vagrant VM (provisioned): `SUMO_HOME=/opt/sumo` (set by `provision.sh`)
- **Python venv** with deps: `pip install -r smart_city_digital_twin/2D_simulation/requirements.txt`
  (needs `websockets` + `pyproj`; `influxdb-client` for the writer).
  Activate it: `source smart_city_digital_twin/.venv/bin/activate`
- **Local InfluxDB** (for the metrics pipeline / history): Colima + Docker.
- **Cloud** (for the AWS pipeline): `terraform` + `aws` CLI in the Vagrant VM, infra deployed
  (see `infra/README.md`).

## Ports

| Port | What |
|---|---|
| 8813 | TraCI (SUMO ↔ `run_traci.py`) |
| 8765 | Emitter WebSocket server (`--emit`) |
| 8000 | Web server hosting the dashboard |
| 8086 | Local InfluxDB API + UI |
| 8788 | Local replay server (history reads) |

> Serve the dashboard on **8000**, never 8765. Serve it from the
> `smart_city_digital_twin/` folder so the map's `../data/output/intersection_geo.csv`
> fetch resolves.

---

## A. Fully local — map + live vehicles

Two terminals.

**T1 — serve the dashboard:**
```bash
cd smart_city_digital_twin
python3 -m http.server 8000
```

**T2 — run the emitter (hosts a WS server on :8765):**
```bash
cd smart_city_digital_twin/2D_simulation
python3 scripts/run_traci.py --no-gui --emit
```
- Running **inside the Vagrant VM**? Add `--emit-host 0.0.0.0` so the forwarded port
  (Vagrantfile `guest: 8765 → host: 8765`) can reach it.
- Wait for `Emitter live on ws://…:8765` — the server starts *after* the jump to 06:30.

**Browser:** open
`http://localhost:8000/2D_simulation/scripts/intersection_map.html`, set the WS field to
`ws://localhost:8765`, click **Connect**. Speed-coloured vehicles + the 3 live charts appear.

---

## B. Local metrics pipeline — store to InfluxDB

**Start local InfluxDB (once):**
```bash
colima start                 # Mac Mini: Colima, NOT Docker Desktop (unsupported on macOS 13)
docker compose up -d          # InfluxDB 2.7 on :8086; data persists in volumes
```

Then run the emitter (A/T2) **and** the metrics writer:
```bash
cd smart_city_digital_twin/2D_simulation
python3 scripts/metrics_writer.py --sample-every 10
```
- Reads the WS feed → `compute_tick_metrics()` → writes points to InfluxDB.
- View them: InfluxDB UI at `http://localhost:8086` (log in with `.env` creds) → Data Explorer
  → bucket `traffic-metrics`. Set the time range to cover *now*.

You need **all three** running for live data: emitter → writer → InfluxDB. Miss the writer and
you get a live map but an empty database.

### Populate the Compare view — run both scenarios with one command

To get a baseline-vs-incident **Compare / Impact** view you need two runs in InfluxDB, each
tagged by `scenario_id`. `scripts/run_scenarios.py` runs them **back-to-back** (they share the
TraCI/emitter ports, so they can't run at once) and auto-starts the metrics writer per run — so
you don't have to launch the emitter and writer by hand or remember the second run:

```bash
cd smart_city_digital_twin/2D_simulation
python3 scripts/run_scenarios.py              # baseline_am → crash_arterial (the default pair)
python3 scripts/run_scenarios.py --fast       # skip real-time pacing — fills InfluxDB quickly
python3 scripts/run_scenarios.py --scenario baseline_am    # just one run
python3 scripts/run_scenarios.py --no-writer  # if you start metrics_writer yourself
```
- Needs InfluxDB up (the `docker compose up -d` above). Uses the same presets as the dashboard's
  Start buttons (`control_server.PRESETS`), so the two never drift.
- Each preset self-terminates at 07:30 (`--end 27000`); at real-time `--speed 5` that's ~12 min
  per run (~24 min for the pair) — use `--fast` for a quick data fill.
- Then open the dashboard **Compare** panel and set the two scenario ids (`baseline_am` vs
  `crash_arterial`), or click **Impact report**.

---

## C. Historical charts — read stored data into the dashboard

Needs InfluxDB up with data (run B first).

**Start the replay server (the browser-reachable read endpoint):**
```bash
cd smart_city_digital_twin/2D_simulation
python3 scripts/replay_server.py        # :8788, queries local InfluxDB, CORS-enabled
```

**Browser:** on the dashboard, use the **History (from InfluxDB)** panel — pick a field + range,
click **Load**. The chart renders from stored data.
- The endpoint is configurable: `…/intersection_map.html?replay=<url>` (default
  `http://localhost:8788/metrics`).

---

## D. Cloud — live through AWS

Infra must be deployed (see `infra/README.md`) and the InfluxDB token stored in Secrets Manager.

**Get the endpoints (from `infra/`):**
```bash
cd infra
terraform output -raw websocket_url     # wss://<id>.execute-api.ap-southeast-2.amazonaws.com/prod
terraform output -raw dashboard_url      # CloudFront HTTPS URL
```

**Order matters — connect the browser FIRST, then run the emitter:**

1. **Browser:** open the dashboard (CloudFront `dashboard_url`, or
   `http://localhost:8000/…?ws=<websocket_url>`), set the WS field to the **`wss://…/prod`** URL,
   click **Connect** → status must say **connected**.
   - On an HTTPS page you *must* use `wss://` (browsers block `ws://` from `https://`).
   - A browser must be connected, or the ingest Lambda's broadcast has no recipient (data still
     reaches InfluxDB, but you see nothing).

2. **Emitter (Mac or VM):** forward snapshots to the cloud:
   ```bash
   cd smart_city_digital_twin/2D_simulation
   python3 scripts/run_traci.py --no-gui --emit-interval 10 \
     --emit-target "wss://<id>.execute-api.ap-southeast-2.amazonaws.com/prod"
   ```

Vehicles appear in the browser, and metrics land in **InfluxDB Cloud**.

- `--emit-interval 10` throttles it — every tick would flood API Gateway (and the free tier).
- **Stop the emitter (Ctrl-C) when done** — every forwarded tick is a billable API Gateway message.
- History charts on the *deployed* dashboard need an API-Gateway-fronted replay endpoint (the
  public Function URL is 403-blocked on this account) — a follow-up.

---

## E. Cloud sim host — run SUMO in AWS, on demand (no laptop)

Runs the whole producer in the cloud: an EC2 box runs SUMO + the emitter, forwarding to your
API Gateway. Nothing runs on your machine. It's **on-demand and hourly-billed**, so you bring it
up for a demo and tear it down after. Defined in `infra/sim_host.tf`, gated by
`var.sim_host_enabled` (default `false` → no cost).

**Prereqs (in the VM):** the AWS **Session Manager plugin** (for shell access):
```bash
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb
```

**1. Bring it up** (from `infra/`):
```bash
terraform apply -var 'sim_host_enabled=true'
```
Creates an EC2 instance whose boot script (user_data) installs SUMO (PPA), clones the repo, and
starts a `sumo-emitter` systemd service forwarding to `wss://…/prod`. First boot takes ~3–5 min.

**2. Verify** (a few minutes after apply):
```bash
aws ssm start-session --target "$(terraform output -raw sim_host_instance_id)"   # from the VM
# then inside the box:
sudo cloud-init status --wait          # want: status: done  (not "degraded"/"error")
systemctl is-active sumo-emitter       # want: active
sudo journalctl -u sumo-emitter -n 20 --no-pager   # want: "Running simulation ..."
```
Provisioning log (for failures): `sudo cat /var/log/cloud-init-output.log`.

**3. Watch it live:** open the dashboard, set the WS field to your `wss://…/prod`, Connect.

**4. Tear it down when done** (this is the billed piece — always do this):
```bash
terraform apply -var 'sim_host_enabled=false'   # removes just the sim host
#   or:  terraform destroy                       # everything
```

**Gotchas we hit (so you don't again):**
- `terraform plan` shows nothing unless you pass `-var 'sim_host_enabled=true'` — the resources
  are `count`-gated on that flag (default false). This is intentional (on-demand cost control).
- The `user_data` script **must start with `#!/usr/bin/env bash`** on line 1, or cloud-init
  classifies it as a plain comment and silently skips it (symptoms: no `sumo-emitter` service,
  empty `cloud-init-output.log`, `cloud-init status` = degraded).
- Editing the script + re-applying **replaces the instance** (`user_data_replace_on_change = true`),
  giving a new instance id — SSM into the new one.
- Access is via **SSM**, not SSH (no inbound ports). Run `aws ssm start-session` from the VM, not
  the EC2.

---

## `--emit` vs `--emit-target`

| Flag | Does | View via |
|---|---|---|
| `--emit` | hosts a local WS server on `:8765` | local page → `ws://localhost:8765` |
| `--emit-target wss://…/prod` | forwards each tick to the cloud | dashboard connected to `wss://…/prod` |
| both | local server **and** cloud forward | either |

---

## Common gotchas

- **`localhost` differs by machine.** Inside the VM, `localhost` is the VM. The emitter must bind
  `--emit-host 0.0.0.0` for Vagrant's port-forward to reach it from the Mac.
- **`ws://` vs `wss://`.** Local = `ws://localhost:8765` (plain HTTP page only). Cloud/HTTPS =
  `wss://…/prod`.
- **Headless VM** = only `sumo` (no `sumo-gui`), so always `--no-gui` there.
- **`pyproj` at runtime** — the emitter crashes on the first broadcast without it. Run from the
  venv where you `pip install -r requirements.txt`.
- **Live vs stored.** The WS feed is ephemeral (needs the emitter running now). InfluxDB is durable
  (survives restarts). The live charts read the feed; the history charts read InfluxDB.

## Stopping / cleanup

```bash
# Ctrl-C the emitter / writer / servers in their terminals
docker compose stop      # pause local InfluxDB (data kept)
colima stop              # shut the Docker VM
# Cloud sim host (the hourly-billed piece) — always tear down after a demo:
#   from infra/  →  terraform apply -var 'sim_host_enabled=false'
# Cloud everything:
#   from infra/  →  terraform destroy   (set recovery_window_in_days = 0 in storage.tf
#                   first, or the Secrets Manager 7-day window blocks the next apply)
```

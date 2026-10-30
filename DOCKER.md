# Docker Compose — one-command local demo

Bring up the **entire** Christchurch decision-support twin on one machine with a
single command. Everything runs locally in containers — **no AWS, no cloud, no
cost**. The only prerequisite is Docker.

This is the portable, reproducible counterpart to the AWS deployment: clone the
repo, `docker compose up`, done. (It is separate from `start_demo.sh`, which runs
the same services as host processes and needs Python + SUMO installed on the box —
see *"vs. start_demo.sh"* below.)

---

## What comes up

| Service | Port | What it is |
|---|---|---|
| `influxdb` | 8086 | InfluxDB 2.7 time-series store (metrics) |
| `sim` | 8765 | SUMO + `run_traci.py --emit` — the live vehicle WebSocket feed |
| `writer` | — | `metrics_writer.py`: reads the sim feed, writes metrics to InfluxDB |
| `replay` | 8788 | HTTP read endpoint the dashboard's History/Compare panels call |
| `web` | 8000 | Serves the dashboard (`intersection_map.html`) + map data |

Data flow: **sim → (writer) → InfluxDB → (replay) → dashboard**, and **sim → dashboard** directly for the live map.

---

## Prerequisites

- **Docker Engine + the Compose plugin.** Check with `docker compose version`.
  On the Ubuntu VM: `sudo apt-get install -y docker.io docker-compose-v2` (or
  Docker's official repo). On the Intel Mac Mini: Colima (`colima start`).
- Free host ports: **8086, 8765, 8788, 8000**.
- Disk: the `app` image is tiny; the `sim` image is ~1–2 GB and takes a few
  minutes to build the **first** time (it installs SUMO). Rebuilds are cached.

## Run it

```bash
cp .env.example .env         # set INFLUXDB_PASSWORD (8+ chars) and INFLUXDB_TOKEN
docker compose up            # first run builds the images, then starts everything
#   (add -d to run detached in the background)
```

Then open:

```
http://localhost:8000/scripts/intersection_map.html
```

In the **live panel** click **Connect** — vehicles appear and the Chart.js panels
start moving. **History** and **Compare** read from InfluxDB via the replay service.

> Running on the VM? Forward host ports **8000, 8765, 8788** (and 8086 if you want
> the InfluxDB UI) so your laptop browser can reach them.

## Stop it

```bash
docker compose down          # stop everything; InfluxDB data is kept
docker compose down -v       # also wipe InfluxDB data (fresh next time)
```

---

## Choosing the scenario

By default `sim` runs the **calibrated morning baseline** (06:30→07:30, real-time
×5, `--scenario-id baseline_am`). The run lasts ~12 min of wall time, then the
`sim` container exits (the rest stay up, and the data it wrote stays queryable).

Re-run the sim without restarting the stack:

```bash
docker compose up -d sim
```

Run a different scenario by overriding the command — e.g. an arterial closure:

```bash
docker compose run --rm --service-ports sim \
  python3 scripts/run_traci.py --no-gui --emit --emit-host 0.0.0.0 \
    --real-time --speed 5 --jump-to 23400 --end 27000 \
    --scenario-id crash_arterial --close-edge "770109405#0@23700:24300"
```

Both runs are tagged by `--scenario-id`, so you can line them up in the dashboard's
**Compare** panel (e.g. `baseline_am` vs `crash_arterial`).

---

## Notes & limitations

- **The `control_server` "Scenario control" panel is not wired in the Compose
  stack.** That server spawns SUMO processes, which doesn't fit the one-container-
  per-service model. In Compose you pick the scenario via the sim `command` (above).
  For click-to-start-from-the-browser, use `start_demo.sh` on a host with SUMO.
- **SUMO comes from `ppa:sumo/stable`** — the same source `infra/sim_host.tf` uses
  on EC2, which is verified to load this repo's net/demand files.
- Code + data are **bind-mounted** from the repo, not baked into the images (the
  `2D_simulation` tree is ~0.6 GB). So the containers always run your current
  checkout; no rebuild needed after editing a script.

---

## vs. `start_demo.sh`

Same services, different level. `start_demo.sh` runs them as **host processes** and
needs Python 3.10+, all pip deps, and SUMO installed on the machine. Docker Compose
runs them as **containers** and needs only Docker — reproducible on any box, nothing
to pre-install. Use `start_demo.sh` when your VM is already set up; use Compose for a
clean, portable, "clone and run" demo.

## vs. the AWS deployment

Compose is the **local** demo. The cloud path (Terraform → API Gateway, Lambda,
InfluxDB Cloud, S3/CloudFront) is the **hosted** demo. They're parallel: same system,
one on your machine, one in AWS. Compose does not touch or depend on the cloud.

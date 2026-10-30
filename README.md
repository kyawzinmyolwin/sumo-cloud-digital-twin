# Tutorial: Christchurch Smart-City Digital Twin

This repository contains a decision-support traffic digital twin for Christchurch's central city. It combines a SUMO traffic simulation with a browser dashboard, scenario controls, a time-series metrics pipeline, and an optional AWS-hosted pipeline. This guide explains the project structure, local setup, configuration, runtime flow, tests, and safe first modifications.

> **Project context:** COMP 693 Industry Project, Lincoln University, New Zealand. The traffic network and demand are calibrated from Christchurch City Council, OpenStreetMap, and Miovision data. Incidents are synthetic what-if scenarios injected into the running simulation; the project does not claim to consume live road-sensor feeds.

## 1. Project Overview

The project lets a user explore questions such as “What if a road is closed?” or “How does an incident change traffic compared with the baseline?”

The dashboard can display moving vehicles on a Leaflet map, show live traffic indicators, start predefined scenarios, compare a baseline run with an incident run, and display an impact summary. The local pipeline stores aggregated metrics in InfluxDB so the dashboard can request historical data.

There are two main deployment paths:

- **Local demo:** SUMO and the Python services run with Docker Compose; InfluxDB stores metrics locally.
- **AWS deployment:** Terraform provisions API Gateway, Lambda functions, InfluxDB Cloud integration, S3, CloudFront, and supporting resources.

Both paths use the same SUMO producer and vehicle-snapshot format. The local setup is the recommended first step because it does not require an AWS account or cloud resources.

## 2. Tech Stack

| Technology | Category | Purpose | Where it is used |
|---|---|---|---|
| Python | Language | Runs the simulation controller, emitter, metrics, replay, and control services | `smart_city_digital_twin/2D_simulation/scripts/` |
| Eclipse SUMO | Traffic simulator | Simulates vehicles on the Christchurch road network | SUMO configuration and network/demand data |
| TraCI / sumolib | Simulation interfaces | Control SUMO, read vehicle state, and convert coordinates | `run_traci.py`, simulation helper modules |
| `asyncio` and `websockets` | Python libraries | Stream vehicle snapshots to WebSocket clients | `emitter.py`, `run_traci.py`, metrics writer |
| InfluxDB 2.7 | Time-series database | Stores aggregated per-tick traffic metrics in the local demo | Docker Compose and `metrics_writer.py` |
| InfluxDB Cloud | Managed time-series database | Stores metrics for the AWS deployment | AWS Lambda integration |
| Leaflet | JavaScript mapping library | Renders vehicle positions on a map | `intersection_map.html` |
| Chart.js | JavaScript chart library | Renders traffic metric charts | `intersection_map.html` |
| Docker Compose | Local orchestration | Runs the local database, simulator, writer, replay API, and web server together | `docker-compose.yml` |
| Terraform | Infrastructure as code | Defines the AWS pipeline and its supporting resources | `infra/*.tf` |
| AWS API Gateway, Lambda, S3, CloudFront, DynamoDB, SSM, Secrets Manager | Cloud services | WebSocket ingestion/fan-out, metric processing, replay/control APIs, dashboard hosting, secrets, and optional simulation host | `infra/` |
| pytest | Test framework | Runs unit tests without needing a live SUMO or AWS environment | `requirements-dev.txt`, `scripts/tests/`, `scripts/etl/tests/` |
| GitHub Actions | CI | Runs Python unit tests on pushes and pull requests | `.github/workflows/ci.yml` |

## 3. Repository Structure

The following tree highlights the files most useful when getting started. Some data directories contain large simulation inputs and generated outputs, so they are not expanded here.

```text
sumo-cloud-digital-twin/
├── README.md
├── .env.example                 # Example settings for the local InfluxDB
├── docker-compose.yml           # Local multi-service stack
├── docker/
│   ├── app.Dockerfile           # Lightweight Python image for writer/replay/web
│   └── sim.Dockerfile           # SUMO runtime image
├── DOCKER.md                    # Detailed Docker walkthrough
├── start_demo.sh                # Native-process demo launcher
├── stop_demo.sh                 # Stops the native-process demo
├── docs/
│   ├── ARCHITECTURE.md           # Main architecture and data-flow diagrams
│   ├── SYSTEM_FLOW.md            # Detailed cloud/system flow
│   └── CLOUD_CONTROL.md          # Cloud scenario-control notes
├── infra/
│   ├── README.md                 # AWS/Terraform setup guide
│   ├── *.tf                      # Terraform resources, variables, outputs
│   ├── functions/                # AWS Lambda source and ETL packaging helper
│   ├── terraform.tfvars.example  # Example deployment variables
│   └── backend.hcl.example       # Example remote Terraform state configuration
└── smart_city_digital_twin/
    ├── README.md                 # Simulation and data-pipeline guide
    └── 2D_simulation/
        ├── Christchurch_Central_City_main_streets.sumocfg
        ├── requirements.txt      # Runtime Python dependencies
        ├── requirements-dev.txt  # Test dependencies
        ├── data/
        │   ├── input/            # Source data and calibration inputs
        │   └── output/           # Network, demand, and map data
        └── scripts/
            ├── run_traci.py      # Main SUMO/TraCI runner and emitter orchestration
            ├── emitter.py        # Vehicle snapshot serialization and WebSocket feed
            ├── incidents.py      # What-if incident definitions and application
            ├── metrics.py        # Pure per-snapshot metric calculations
            ├── metrics_writer.py # WebSocket consumer that writes metrics to InfluxDB
            ├── replay_server.py  # Local HTTP API for historical metrics
            ├── control_server.py # Local scenario start/stop HTTP API
            ├── intersection_map.html
            ├── etl/              # Traffic-count ETL components
            ├── tests/            # Unit tests for simulation-side logic
            └── etl/tests/        # ETL tests
```

**Where to start reading:** first read this file and `DOCKER.md`; then look at `docker-compose.yml`, `run_traci.py`, `emitter.py`, and `metrics.py`. Read `infra/README.md` only when you are ready to explore the cloud deployment.

## 4. Prerequisites

### Recommended: Docker Compose local demo

The Docker workflow is the easiest starting point. It requires:

- Docker Engine with the Docker Compose plugin.
- Git, to clone the repository.
- Free local ports **8000**, **8086**, **8765**, and **8788**.
- Enough disk space to build the SUMO image; the first build can be around 1–2 GB and take several minutes.

Check the installation:

```bash
git --version
docker --version
docker compose version
```

You do **not** need AWS credentials, a local SUMO installation, or a Python virtual environment for the Docker Compose path.

### Native-process demo (alternative)

The `start_demo.sh` workflow is intended for a machine/VM that already has Python and SUMO installed. The simulation guide documents Python 3.10+, SUMO 1.20+ (the repository notes testing with 1.26), and the SUMO tools `sumo`, `sumo-gui`, and `netconvert` on `PATH`. Some workflows also require the Python dependencies in `smart_city_digital_twin/2D_simulation/requirements.txt`.

### AWS deployment (optional)

The cloud workflow additionally requires Terraform, AWS CLI v2, an AWS account with appropriately scoped permissions, an InfluxDB Cloud account, and the AWS Systems Manager Session Manager plugin if you need to open a shell on the optional EC2 simulation host. AWS resources can incur charges; read `infra/README.md` and review the Terraform plan before applying.

## 5. Installation

### Step 1 — Clone the repository

```bash
git clone https://github.com/kyawzinmyolwin/sumo-cloud-digital-twin.git
cd sumo-cloud-digital-twin
```

If you already have a checkout, change into its root directory instead.

### Step 2 — Configure the local InfluxDB

Create your local environment file from the example:

```bash
cp .env.example .env
```

Edit `.env` and replace the example `INFLUXDB_PASSWORD` and `INFLUXDB_TOKEN` values with local development values. The password must be at least eight characters. These settings initialize the **local** InfluxDB container; they are not the credentials for InfluxDB Cloud.

The `.env` file is git-ignored. Do not commit real passwords or tokens.

### Step 3 — Build and start the stack

```bash
docker compose up --build
```

Docker builds the simulator and Python service images, creates the local services, and prints their logs in the terminal. The first build takes longer because the SUMO image installs the simulator. Keep this terminal open while using the demo.

### Step 4 — Open the dashboard

Visit:

```text
http://localhost:8000/scripts/intersection_map.html
```

Click **Connect** in the live panel. The live map receives snapshots from the simulation; History and Compare use the replay service and stored InfluxDB metrics.

### Step 5 — Stop the stack

Press `Ctrl+C` in the terminal, then run:

```bash
docker compose down
```

This stops the containers but preserves the named InfluxDB volumes. To deliberately remove the local database volumes and their stored data, use `docker compose down -v`. Do not use the `-v` option unless you are happy to lose the local time-series data.

## 6. Configuration

### Local Docker configuration

The root `.env.example` provides these settings:

| Variable | Purpose | Required? | Example/default in example file |
|---|---|---|---|
| `INFLUXDB_USERNAME` | Initial local InfluxDB username | Has a Compose default | `admin` |
| `INFLUXDB_PASSWORD` | Initial local InfluxDB password | Yes; at least 8 characters | Replace example value |
| `INFLUXDB_ORG` | Initial InfluxDB organization | Has a Compose default | `christchurch` |
| `INFLUXDB_BUCKET` | Initial bucket for traffic metrics | Has a Compose default | `traffic-metrics` |
| `INFLUXDB_TOKEN` | Initial local InfluxDB admin token | Yes | Replace example value |
| `INFLUXDB_RETENTION` | Local bucket retention period | Has a Compose default | `30d` |

Compose requires `INFLUXDB_PASSWORD` and `INFLUXDB_TOKEN` to be set. Values in `.env` are for the local demo only.

The metrics writer and replay service also accept InfluxDB configuration through command-line flags and environment variables, and look for a nearby `.env` file. Their documented precedence is **CLI flag > environment variable > `.env` file > default**. The writer defaults to `http://localhost:8086` and bucket `traffic-metrics`; the replay server has the same local URL and bucket defaults.

### Cloud configuration

Cloud deployment is configured separately. Copy the example files inside `infra/`:

```bash
cd infra
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
```

Fill in your own remote state bucket/table details in `backend.hcl`, and unique S3 bucket names, alarm email, InfluxDB Cloud URL, and organization in `terraform.tfvars`. Both local files are git-ignored. The InfluxDB Cloud token and ETL Drive API key are added to AWS Secrets Manager after the first apply, as described in `infra/README.md`; do not put secret values into Terraform variables or commit them.

## 7. Running the Project

### Local Docker Compose — recommended

From the repository root:

```bash
docker compose up
```

Open `http://localhost:8000/scripts/intersection_map.html`. The local services are:

| Service | Port | Responsibility |
|---|---:|---|
| `web` | 8000 | Serves the dashboard and map data |
| `influxdb` | 8086 | Stores local time-series metrics |
| `sim` | 8765 | Runs SUMO and exposes the live WebSocket feed |
| `replay` | 8788 | Provides historical metric reads to the dashboard |
| `writer` | — | Reads snapshots and writes aggregated metrics to InfluxDB |

To run it in the background, use `docker compose up -d`. View service logs with `docker compose logs -f`, or focus on one service with `docker compose logs -f sim`. Stop background services with `docker compose down`.

### Native-process demo

If the host already has the required Python/SUMO setup, run from the repository root:

```bash
./start_demo.sh
```

Follow the URL and scenario instructions printed by the script. Stop the native demo with:

```bash
./stop_demo.sh
```

### AWS deployment

The cloud setup is not required for local development. It creates real AWS resources and may incur costs. Follow the ordered bootstrap, configuration, plan, apply, secrets, dashboard deployment, and smoke-test instructions in `infra/README.md`. Always inspect `terraform plan` before `terraform apply`, and disable the optional hourly-billed simulation host when you are not using it.

## 8. How the Code Works

### Simulation and snapshot generation

`smart_city_digital_twin/2D_simulation/scripts/run_traci.py` is the main simulation runner. It starts or connects to SUMO through TraCI, advances simulation time, handles incident commands, and calls the emitter when emission is enabled.

`emitter.py` converts SUMO vehicle positions into geographic coordinates and builds a JSON snapshot. The dashboard uses latitude/longitude, while SUMO uses projected coordinates, so the conversion is important. The WebSocket broadcaster sends the current snapshot to connected clients; the cloud-forwarding path can send the same producer data to the AWS WebSocket endpoint.

### Incidents and scenario control

`incidents.py` defines time-bounded what-if changes such as closing an edge/lane, lowering a speed limit, or injecting additional vehicles. It records original road settings so temporary changes can be reverted when the incident ends.

`control_server.py` exposes local HTTP endpoints for listing presets, checking status, starting a named scenario, and stopping the running simulation. It uses a fixed preset registry rather than accepting arbitrary shell commands from the browser.

### Metrics and historical reads

`metrics.py` contains `compute_tick_metrics()`, a pure function that transforms one snapshot into scalar values such as vehicle count, average speed, congestion index, and stopped count. “Pure” means the function calculates a result without making network or database calls, making it straightforward to test.

`metrics_writer.py` consumes the WebSocket snapshots, calls the metric calculation, and writes the aggregated results to InfluxDB. `replay_server.py` provides an HTTP read endpoint so the browser can request time ranges and metric fields without exposing the InfluxDB token to browser code.

### Cloud pipeline

Under `infra/`, Terraform defines the AWS API Gateway WebSocket routes, Lambda processing, DynamoDB connection registry, HTTP replay/control APIs, secrets, logging, dashboard S3 bucket, and CloudFront distribution. The InfluxDB Cloud bucket itself is created in the InfluxDB Cloud UI, not by Terraform. See `infra/README.md` for the exact bootstrap sequence and required values.

### High-level data flow

```text
SUMO simulation
    │ TraCI vehicle state
    ▼
run_traci.py ──► emitter.py ──► WebSocket snapshots ──► browser live map
                                      │
                                      ▼
                               metrics_writer.py
                                      │
                                      ▼
                                   InfluxDB
                                      │
                                      ▼
                              replay_server.py
                                      │
                                      ▼
                         dashboard History / Compare

Cloud option: emitter ─► API Gateway WebSocket ─► Lambda ─► InfluxDB Cloud
               dashboard hosted by S3 + CloudFront; replay/control use HTTP APIs
```

## 9. Step-by-Step Walkthrough: From SUMO to a Dashboard Metric

1. **SUMO advances the simulation.** `run_traci.py` uses TraCI to read vehicle IDs, positions, speeds, lanes, and other vehicle state from the running simulation.
2. **The emitter creates a snapshot.** `emitter.py` converts vehicle coordinates for map display and serializes a JSON object containing simulation metadata and the vehicle list.
3. **The snapshot is broadcast.** The WebSocket service sends the snapshot to connected clients. In cloud mode, the forwarding component wraps the payload for the API Gateway route expected by the cloud ingestion Lambda.
4. **The browser updates the live view.** `intersection_map.html` consumes the live feed and updates vehicle markers and live metric panels.
5. **The writer calculates metrics.** `metrics_writer.py` receives a snapshot and calls `compute_tick_metrics()` in `metrics.py`.
6. **InfluxDB stores the aggregate.** The writer writes scalar metrics with simulation/scenario metadata so separate runs can be compared.
7. **The dashboard requests history.** The browser calls the replay endpoint with a time range and allowed metric field. The replay service queries InfluxDB and returns points for charts and comparison/impact views.

For more detailed diagrams, read `docs/ARCHITECTURE.md` and `docs/SYSTEM_FLOW.md`.

## 10. Example Modification: Adjust the Congestion Threshold

### Goal

The metrics function currently treats vehicles below a speed threshold as slow/congested. The threshold constant in `scripts/metrics.py` is `SPEED_SLOW = 3.0` metres per second. You can adjust it to explore how a stricter or more lenient definition changes the congestion index.

### File to modify

`smart_city_digital_twin/2D_simulation/scripts/metrics.py`

### Implementation

Find the constant near the top of the file:

```python
SPEED_SLOW = 3.0
```

For example, to classify vehicles moving below 4 m/s as congested, change it to:

```python
SPEED_SLOW = 4.0
```

This is an illustrative change; make it in your working branch rather than directly on the main branch.

### Explanation

`compute_tick_metrics()` uses this constant as the default value for its `slow_speed` argument. Raising it means more vehicles may be counted as slow, so the congestion index can increase even when the simulation itself has not changed. This changes the metric definition, not the SUMO traffic behaviour.

### Verification

From the repository root:

```bash
cd smart_city_digital_twin/2D_simulation
python -m pip install -r requirements-dev.txt
python -m pytest scripts/tests scripts/etl/tests -q
```

The tests validate the current code paths; review or add an assertion for the threshold behaviour if you want the new definition explicitly locked in by a test. The test command is defined in the repository's CI workflow. This guide has not executed the tests in your environment.

## 11. Troubleshooting

| Problem | Likely cause | How to diagnose | Solution |
|---|---|---|---|
| Compose refuses to start because a required variable is missing | `.env` was not created or the password/token is blank | Check that `.env` exists and contains the required keys; do not paste secret values into logs or chats | Copy `.env.example` to `.env` and set a local password of at least 8 characters plus a non-empty token |
| A container cannot bind to a port | Another process already uses 8000, 8086, 8765, or 8788 | Check container output and use your OS's port-inspection tools | Stop the conflicting service or change the port mapping and corresponding endpoint configuration consistently |
| Dashboard opens but live vehicles do not appear | Simulation still starting, WebSocket feed unavailable, or browser has not connected | Inspect `docker compose logs -f sim` and the browser console; click **Connect** in the live panel | Resolve the reported SUMO/startup error, ensure port 8765 is reachable, and reconnect |
| History/Compare has no data | Writer has not connected or no snapshots have been stored yet | Inspect `docker compose logs -f writer` and `docker compose logs -f influxdb` | Ensure the simulation is emitting snapshots and the writer's InfluxDB URL/org/bucket/token match the local setup |
| `traci` or `sumolib` cannot be imported in native mode | SUMO tools are not on the Python path or `SUMO_HOME` is unset/incorrect | Check `SUMO_HOME`, SUMO installation, and the simulation guide | Use the Docker workflow, or configure the host SUMO tools path as documented in `smart_city_digital_twin/README.md` |
| Terraform init cannot access its state backend | `backend.hcl` has placeholder/wrong values or the bucket/table do not exist | Review the backend configuration and AWS region; run `aws sts get-caller-identity` to check the active AWS identity | Create the backend resources as documented in `infra/README.md`, fill in `backend.hcl`, then initialize again |
| Cloud Lambda cannot write/read metrics | Cloud InfluxDB URL/org/bucket/token is missing or mismatched | Inspect the relevant CloudWatch logs and the Lambda configuration without printing secret values | Follow the post-apply Secrets Manager steps in `infra/README.md`; confirm the cloud token is for the correct organization/bucket |
| Terraform apply proposes unexpected resources or changes | Variables, provider region, or current state differ from expectations | Read the complete `terraform plan` before approving it | Do not apply until you understand the changes; confirm the workspace, variables, region, and backend |

The table lists common diagnostic possibilities, not a claim that each failure is currently occurring in this repository.

## 12. Next Steps

1. **Understand the snapshot contract:** read `emitter.py` and its tests, then compare the produced fields with the dashboard's expectations.
2. **Understand the metrics:** read `metrics.py` and the tests that cover `compute_tick_metrics()`.
3. **Explore scenario behaviour:** read `incidents.py` and the preset registry in `control_server.py`.
4. **Learn historical queries:** follow a request from the dashboard to `replay_server.py`, then inspect the cloud replay Lambda.
5. **Run the tests before changing code:** use the same pytest command as CI. Add a focused regression test for each behaviour change.
6. **Explore infrastructure last:** read `infra/README.md`, then inspect `infra/variables.tf`, `infra/api_gateway.tf`, `infra/lambda.tf`, and `infra/replay_api.tf`. Always review the Terraform plan and cloud costs before applying.
7. **Try another small change:** add a test for a metric edge case (for example, an empty vehicle list or missing speed values) before changing how that case is calculated.

## Additional Documentation

- [Docker local setup](DOCKER.md)
- [Architecture and data-flow diagrams](docs/ARCHITECTURE.md)
- [System flow](docs/SYSTEM_FLOW.md)
- [AWS/Terraform setup](infra/README.md)
- [End-to-end running guide](running_guide.md)
- [Command cheat sheet](COMMANDS.md)
- [SUMO simulation and data pipeline](smart_city_digital_twin/README.md)

## Data Sources and Interpretation

The project uses Christchurch City Council traffic-count/road-intersection data and OpenStreetMap for model inputs, and SUMO as the source of simulated vehicle states. The downloaded traffic-count data is used for calibration/demand inputs; it is not a live sensor stream. Incident scenarios are synthetic perturbations used to compare outcomes against the baseline.

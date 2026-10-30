# Architecture

The Christchurch decision-support twin has one **producer** (SUMO + the TraCI
emitter) feeding one of two interchangeable **pipelines** (local or AWS), and one
**dashboard** that points at whichever pipeline's endpoints you give it. This doc
shows the data flow, the two deployment topologies, and a component reference.

> Diagrams are [Mermaid](https://mermaid.js.org/) — GitHub renders them inline. To
> export an image for a report/slide, paste a block into <https://mermaid.live> and
> download SVG/PNG.

---

## 1. Data flow (end to end)

```mermaid
flowchart LR
    SUMO["SUMO micro-sim<br/>CBD net + demand"] <-->|"TraCI :8813"| RT

    subgraph PRODUCER["Producer"]
      RT["run_traci.py --emit<br/>serialize_vehicles()"]
      INC["incidents.py<br/>close edge / lane,<br/>speed limit, surge"]
      INC -->|"per-tick apply/revert"| RT
    end

    RT -->|"snapshot JSON / tick<br/>{tick, simTime, vehicles[], scenarioId}"| WS(("WebSocket<br/>:8765"))

    WS --> MAP["Dashboard live map<br/>+ Chart.js panels"]
    WS --> MW["metrics_writer.py"]
    MW -->|"compute_tick_metrics()<br/>count · avgSpeed · congestion · stopped"| DB[("InfluxDB<br/>measurement: traffic_metrics<br/>tags: simId, scenario_id")]
    RD["replay endpoint"] -->|"Flux range query"| DB
    RD --> HIST["Dashboard history /<br/>compare / impact report"]
```

The emitter schema is the contract between producer and everything downstream:

```json
{ "tick": 1720123456789, "simId": "christchurch-cbd-001", "simTime": 23460.1,
  "scenarioId": "crash_arterial", "vehicleCount": 214,
  "vehicles": [ { "id": "veh_001", "lat": -43.5321, "lng": 172.6362,
                  "speed": 13.4, "lane": "edge_42_0", "accel": 0.2, "type": "car" } ] }
```

`compute_tick_metrics()` is a **pure function** (unit-tested without SUMO) and is the
*same code* on both pipelines — a local module for `metrics_writer.py` and copied
into the `traffic-metrics` Lambda, kept in sync.

---

## 2. Local deployment (Docker Compose / `start_demo.sh`)

Everything on one machine. `docker compose up` runs each box as a container; the
browser reaches published host ports.

```mermaid
flowchart TB
    subgraph HOST["One machine — Docker"]
      direction TB
      SIM["sim<br/>SUMO + run_traci.py --emit<br/>:8765"]
      WRITER["writer<br/>metrics_writer.py"]
      INFLUX[("influxdb<br/>:8086")]
      REPLAY["replay<br/>replay_server.py :8788"]
      WEB["web<br/>http.server :8000<br/>(serves the dashboard)"]
      SIM -->|"ws://sim:8765"| WRITER
      WRITER --> INFLUX
      REPLAY --> INFLUX
    end

    BROWSER["Browser<br/>intersection_map.html"]
    WEB -->|"page"| BROWSER
    SIM -->|"ws://localhost:8765 (live)"| BROWSER
    REPLAY -->|"http://localhost:8788 (history)"| BROWSER
```

`start_demo.sh` runs the same services as **host processes** instead of containers,
and adds `control_server.py` (:8799) so scenarios start from the dashboard. See
[DOCKER.md](../DOCKER.md) and the start/stop scripts.

---

## 3. Cloud deployment (AWS, Terraform)

The producer dials out to API Gateway; metrics land in InfluxDB Cloud; the dashboard
is served from CloudFront/S3 and reads history via an HTTP-fronted replay Lambda.

```mermaid
flowchart TB
    PROD["run_traci.py --emit-target wss://…<br/>(laptop, VM, or EC2 sim host)"]
    PROD -->|"sendmessage / tick"| APIGW["API Gateway<br/>WebSocket API<br/>$connect · sendmessage · $disconnect"]
    APIGW --> ING["λ traffic-ingest"]
    ING --> MET["λ traffic-metrics<br/>compute_tick_metrics()"]
    MET --> IC[("InfluxDB Cloud")]
    DDB[("DynamoDB<br/>connections")] --- APIGW

    REP["λ traffic-replay<br/>+ HTTP API"] --> IC
    CF["CloudFront"] --> S3[("S3<br/>dashboard")]
    USER["Browser"] --> CF
    USER -->|"history / compare"| REP

    SEC["Secrets Manager<br/>InfluxDB token"] -.-> MET
    SEC -.-> REP
    CW["CloudWatch logs<br/>+ $5 billing alarm"] -.-> MET
```

Region `ap-southeast-2`. Idle cost ≈ the Secrets Manager secret (~$0.40/mo); a $5
billing alarm guards the rest. An optional on-demand EC2 **sim host**
(`infra/sim_host.tf`, gated off by default) runs the producer in AWS so no laptop is
needed. Details in [infra/README.md](../infra/README.md).

---

## 4. Component reference

| Component | File | Role |
|---|---|---|
| TraCI control loop + emitter | `scripts/run_traci.py` | Drives SUMO; emits per-tick snapshots over WebSocket and/or to the cloud (`--emit`, `--emit-target`); injects incidents (`--close-edge`, `--incident-file`, `--scenario-id`). |
| Serialiser + WS server | `scripts/emitter.py` | `serialize_vehicles()`, broadcaster, inbound control inbox (live close/reopen). |
| Incident model | `scripts/incidents.py` | `IncidentController` — close edge/lane, speed limit, demand surge; apply + revert. |
| Metrics (pure) | `scripts/metrics.py` | `compute_tick_metrics()` — count, avg speed, congestion index, stopped/moving. |
| Local writer | `scripts/metrics_writer.py` | WS feed → metrics → InfluxDB (tags `simId`, `scenario_id`). |
| Local read API | `scripts/replay_server.py` | Flux range queries → JSON for the dashboard (CORS; `scenario` filter). |
| Scenario control | `scripts/control_server.py` | HTTP server that starts/stops named-preset runs (and the writer) from the browser. |
| Dashboard | `scripts/intersection_map.html` | Leaflet live map, Chart.js panels, compare view, impact report, congestion alerts, click-to-inject. |
| Cloud Lambdas | `infra/functions/traffic_{ingest,metrics,replay}/` | Ingest ticks → aggregate (same `metrics.py`) → store; serve replays. |
| ETL (baseline) | `scripts/etl/`, `scripts/scenario_bridge.py` | Council counts → calibrated demand/baseline; S3 + query Lambdas. |
| IaC | `infra/*.tf` | API Gateway, Lambdas + IAM, DynamoDB, S3/CloudFront, Secrets Manager, CloudWatch. |

## Ports

| Port | Service |
|---|---|
| 8813 | SUMO TraCI |
| 8765 | Emitter WebSocket (live feed) |
| 8086 | InfluxDB (local) |
| 8788 | replay_server (local read API) |
| 8799 | control_server (start/stop scenarios) |
| 8000 | Dashboard web server |

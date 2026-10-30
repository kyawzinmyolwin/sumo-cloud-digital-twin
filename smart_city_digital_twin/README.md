# Smart City Digital Twin — Simulation Instructions

Christchurch central-city traffic simulation: **2D SUMO**, **3D Unity digital twin** (TraCI), and optional **traffic-count data pipeline**.

All commands below assume your shell starts in the **`smart_city_digital_twin/`** folder unless noted otherwise.

---

## 1. Prerequisites

| Tool | Version / notes |
|------|-----------------|
| **Python** | 3.10+ (for data pipeline only) |
| **Eclipse SUMO** | 1.20+ (project tested with 1.26). `sumo`, `sumo-gui`, and `netconvert` on `PATH` |
| **Unity** | 2022.3 LTS or Unity 6 — for 3D only; project at `3D_simulation/` |
| **Git / OS** | macOS or Windows; SUMO may run in a VM while Unity runs on the host |

### Python environment (data pipeline only)

```bash
cd smart_city_digital_twin
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r 2D_simulation/requirements.txt
```

Calibration scripts (optional) also need: `numpy`, `ufbx` (`pip install numpy ufbx`).

---

## 2. Project layout (simulation files)

```
smart_city_digital_twin/
├── README.md
├── 2D_simulation/
│   ├── Christchurch_Central_City_main_streets.sumocfg   ← main 2D/3D SUMO config
│   ├── README.md
│   ├── requirements.txt
│   ├── data/
│   │   ├── input/                                       ← OpenData, Miovision workbooks
│   │   │   └── source_data/
│   │   └── output/                                      ← geo CSV, SUMO network + demand
│   │       ├── network/                                 ← .net.xml
│   │       ├── demand/                                  ← traffic CSV, routes
│   │       └── intersection_geo.csv
│   ├── scripts/                                         ← pipeline + parsers, calibration, map
└── 3D_simulation/                         ← Unity project
    └── Assets/StreamingAssets/Christchurch/
        └── calibration.json
```

**Optional (traffic-count neighbour network — see §6):**

```
2D_simulation/scripts/
├── sumo_network_from_geo.py          ← generates plain XML + christchurch_intersections.net.xml
└── sumo_demand_from_traffic_csv.py   ← generates christchurch_demand.rou.xml
```

Generated outputs (not stored in repo): `data/output/network/sumo_plain_*.xml`, `data/output/network/christchurch_intersections.net.xml`, `data/output/demand/christchurch_demand.rou.xml`.

---

## 3. Simulation overview

| What | Config / files | Visualisation |
|------|----------------|---------------|
| **2D SUMO** | `Christchurch_Central_City_main_streets.sumocfg` | SUMO-GUI |
| **3D digital twin** | Same `.sumocfg` + `--remote-port 8813` | SUMO-GUI + Unity TraCI |
| **2D intersection map** | `scripts/intersection_map.html` | Browser (Leaflet) |
| **Count-driven network** *(optional)* | `data/output/network/christchurch_intersections.net.xml` + `data/output/demand/christchurch_demand.rou.xml` | SUMO-GUI |

2D and 3D share the **same OSM main-streets network** (`data/output/network/`) and **`data/output/demand/traffic_trips.routed.rou.xml`**.  
3D adds TraCI and Unity on top of the same SUMO run.

---

## 4. 2D SUMO simulation

### 4.1 Run in SUMO-GUI

From `smart_city_digital_twin/2D_simulation/`:

```bash
cd 2D_simulation
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg
```

The config loads:

| Input | File |
|-------|------|
| Network | `data/output/network/Christchurch_Central_City_main_streets.net.xml` |
| Routes | `data/output/demand/traffic_trips.routed.rou.xml` |
| Additional | `data/output/network/Christchurch_Central_City_main_streets.add.xml` (polygons / background) |

Default simulation: **06:30–24:00** (`begin=23400`, `end=86400`), step length **1 s**.

Press **Run** in SUMO-GUI to start traffic.

### 4.2 Headless run

```bash
cd 2D_simulation
sumo -c Christchurch_Central_City_main_streets.sumocfg
```

### 4.3 2D intersection map (Leaflet)

Separate from SUMO — shows the 96 georeferenced intersections:

```bash
cd smart_city_digital_twin
python3 -m http.server 8765
```

Open: `http://localhost:8765/2D_simulation/scripts/intersection_map.html`

---

## 5. 3D Unity digital twin (TraCI)

Uses the **same** `.sumocfg` as 2D, plus a TraCI port for Unity.

### 5.1 Startup order

**SUMO first → Unity Play second.**

```
SUMO (TraCI :8813)  ←——TCP——→  Unity (SumoVehicleSpawner)
```

### 5.2 Start SUMO with TraCI

```bash
cd 2D_simulation
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg \
  --remote-port 8813 --start
```

Keep SUMO running.

| Setup | Unity `serverIP` |
|-------|------------------|
| SUMO and Unity on same Mac/PC | `127.0.0.1` |
| SUMO in VMware Windows, Unity on Mac | VM IPv4 from `ipconfig`; allow TCP **8813** in Windows Firewall |

### 5.3 Start Unity

1. Open **`3D_simulation/`** in Unity Hub.  
2. Open **`Assets/Scenes/SampleScene.unity`**.  
3. On **`SumoVehicleSpawner`**:

   | Field | Value |
   |-------|-------|
   | Server IP | `127.0.0.1` (or VM IP) |
   | Server Port | `8813` |
   | Step Length Seconds | `1` (match `.sumocfg`) |
   | Apply Christchurch Calibration On Start | ✓ |
   | Map Root | `Christchurch_Central_City_3D` FBX root |

4. Press **Play** (connects with up to 60 retries).

Yellow cubes = cars; teal larger cubes = buses. Navigate with **`SimpleFlyCamera`**.

### 5.4 Calibration (optional)

If vehicles are offset from roads after network/map changes:

```bash
python3 2D_simulation/scripts/calibrate_christchurch.py --update-scene
```

Updates `Assets/StreamingAssets/Christchurch/calibration.json`.

---

## 6. Optional — traffic-count data pipeline

Use this when you want demand built from **Miovision turning-movement counts** on the simplified **96-intersection neighbour graph** (not the main-streets OSM network).

```bash
python3 2D_simulation/scripts/traffic_counts_parser.py
python3 2D_simulation/scripts/match_intersection_opendata.py      # if geo CSV stale
python3 2D_simulation/scripts/fill_intersection_direction_neighbours.py
python3 2D_simulation/scripts/sumo_network_from_geo.py
python3 2D_simulation/scripts/sumo_demand_from_traffic_csv.py \
  --traffic-csv 2D_simulation/data/output/demand/traffic_18MAY2026_130842.csv
```

Run neighbour-network SUMO (from `2D_simulation/`):

```bash
sumo-gui -n data/output/network/christchurch_intersections.net.xml \
  -r data/output/demand/christchurch_demand.rou.xml --end 3600
```

> `christchurch_demand.rou.xml` is **not** compatible with `Christchurch_Central_City_main_streets.sumocfg` — different network topology.

---

## 7. Script reference

| Script | Purpose |
|--------|---------|
| `2D_simulation/scripts/traffic_counts_parser.py` | Workbooks → `data/output/demand/traffic_*.csv` |
| `2D_simulation/scripts/match_intersection_opendata.py` | Build / refresh `data/output/intersection_geo.csv` |
| `2D_simulation/scripts/sumo_network_from_geo.py` | Geo CSV → `data/output/network/christchurch_intersections.net.xml` *(neighbour net)* |
| `2D_simulation/scripts/sumo_demand_from_traffic_csv.py` | Traffic CSV → `data/output/demand/christchurch_demand.rou.xml` *(neighbour net)* |
| `2D_simulation/scripts/calibrate_christchurch.py` | FBX + SUMO alignment → `calibration.json` |
| `2D_simulation/scripts/intersection_map.html` | Leaflet map |

### Unity components

| Component | Role |
|-----------|------|
| `SumoVehicleSpawner.cs` | TraCI connect, step sim, spawn/update vehicles |
| `ChristchurchCalibration.cs` | SUMO (x,y) → Unity world position |
| `ChristchurchIntersectionsGizmo.cs` | Intersection gizmos in Scene view |
| `SimpleFlyCamera.cs` | Fly camera |

---

## 8. Troubleshooting

| Problem | Likely cause | Fix |
|---------|--------------|-----|
| SUMO fails to load config | Missing `data/output/demand/traffic_trips.routed.rou.xml` | Run from `2D_simulation/`; check file exists under `data/output/demand/` |
| Unity: “Could not connect to SUMO” | SUMO not running or wrong IP/port | Start SUMO with `--remote-port 8813` **before** Play |
| Unity: “peer shutdown” | Two TraCI clients | Restart SUMO; one Unity instance only |
| No vehicles | Sim time before trip departures | Press Run in SUMO-GUI; check routes in `data/output/demand/traffic_trips.routed.rou.xml` |
| Vehicles offset from roads | Stale calibration | Re-run `calibrate_christchurch.py`; assign **Map Root** |
| `netconvert` not found | SUMO not on PATH | Install SUMO; add `SUMO_HOME/bin` to PATH |

---

## 9. Quick command cheat sheet

```bash
# --- 2D SUMO (main) ---
cd smart_city_digital_twin/2D_simulation
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg

# --- 3D SUMO + Unity ---
cd smart_city_digital_twin/2D_simulation
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg --remote-port 8813 --start
# then Unity → Play

# --- 2D Leaflet map ---
cd smart_city_digital_twin && python3 -m http.server 8765
# open http://localhost:8765/2D_simulation/scripts/intersection_map.html
```

---

## 10. Data sources

- Christchurch City Council — [Intersection traffic counts](https://ccc.govt.nz/transport/improving-our-transport-and-roads/traffic-count-data/intersection-traffic-counts-database)  
- Christchurch OpenData — [Road intersections](https://opendata-christchurchcity.hub.arcgis.com/datasets/4912c568d9a742caa630873278554932_6/explore)  
- Eclipse SUMO — https://sumo.dlr.de/docs/

---

*Smart City Digital Twin — COMP 693 Industry Project · Jix Reality · Lincoln University*

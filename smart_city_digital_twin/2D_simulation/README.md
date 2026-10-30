# 2D simulation

SUMO traffic simulation and supporting data tools for Christchurch central city.

**Full instructions:** [`../README.md`](../README.md)

## Root (this folder)

| File | Role |
|------|------|
| `Christchurch_Central_City_main_streets.sumocfg` | Main 2D/3D SUMO config |
| `requirements.txt` | Python dependencies for data pipeline |

## Subfolders

| Folder | Contents |
|--------|----------|
| `data/input/` | OpenData, street lookup, Miovision `source_data/` |
| `data/output/network/` | OSM inputs, `.net.xml`, junction joins |
| `data/output/demand/` | Traffic CSV, trip/route XML |
| `data/output/` | Also: `intersection_geo.csv`, ID maps, bus-route exports |
| `scripts/` | Pipeline + parsers, calibration, Leaflet map |

## Run 2D SUMO

From this folder:

```bash
sumo-gui -c Christchurch_Central_City_main_streets.sumocfg
```

## Build pipeline

```bash
python3 scripts/create_network.py
python3 scripts/create_demand.py
```

## Scripts (examples)

| Script | Role |
|--------|------|
| `scripts/traffic_counts_parser.py` | Parse `data/input/source_data/` → `data/output/demand/traffic_*.csv` |
| `scripts/match_intersection_opendata.py` | Build / refresh `data/output/intersection_geo.csv` |
| `scripts/calibrate_christchurch.py` | FBX + SUMO alignment → `calibration.json` |
| `scripts/intersection_map.html` | Leaflet map of `data/output/intersection_geo.csv` |

## Leaflet map

From `smart_city_digital_twin/`:

```bash
python3 -m http.server 8765
```

Open: `http://localhost:8765/2D_simulation/scripts/intersection_map.html`

Dependencies: `pip install -r requirements.txt`

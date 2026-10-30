#!/usr/bin/env python3
"""
Christchurch Central City: calibrate FBX map + SUMO vehicle frame from landmark lat/lon.

- WGS84 -> SUMO network XY: affine fit on OSM junction IDs (net.xml + osm.xml).
- FBX map Unity transform: landmark road snaps + yaw / Z-flip search.
- Vehicle cubes: raw TraCI SUMO XY = Unity XZ (no recenter); written to JSON for Unity.

Usage:
  python3 calibrate_christchurch.py --update-scene
"""
from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from _sim_root import PROJECT_ROOT as root

try:
    import ufbx
except ImportError as e:
    raise SystemExit("pip install ufbx") from e


LANDMARKS_WGS84: list[tuple[str, float, float, float]] = [
    # name, lon, lat, weight (higher = more trusted for map fit)
    ("ChristChurch Cathedral", 172.6378899, -43.5310073, 3.0),
    ("Cathedral Square", 172.6356666, -43.5308537, 2.0),
    ("Victoria Square", 172.6363273, -43.5287022, 2.5),
    ("Bus Interchange", 172.6376917, -43.5342022, 2.5),
    ("Te Kaha stadium", 172.6422, -43.5330, 1.5),
    ("The Arts Centre", 172.6278, -43.5316, 1.0),
    ("Christchurch Hospital", 172.6215, -43.5345, 1.0),
]


def fit_wgs84_to_sumo(net_path: Path, osm_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    juncs: dict[str, tuple[float, float]] = {}
    for j in ET.parse(net_path).getroot().findall("junction"):
        if j.get("type") == "internal":
            continue
        juncs[j.get("id")] = (float(j.get("x")), float(j.get("y")))

    nodes: dict[str, tuple[float, float]] = {}
    for n in ET.parse(osm_path).getroot().findall("node"):
        nodes[n.get("id")] = (float(n.get("lon")), float(n.get("lat")))

    pairs = [(nodes[j][0], nodes[j][1], xy[0], xy[1]) for j, xy in juncs.items() if j in nodes]
    a = np.column_stack([np.array(pairs)[:, 0], np.array(pairs)[:, 1], np.ones(len(pairs))])
    cx, _, _, _ = np.linalg.lstsq(a, np.array(pairs)[:, 2], rcond=None)
    cy, _, _, _ = np.linalg.lstsq(a, np.array(pairs)[:, 3], rcond=None)
    return cx, cy, len(pairs)


def wgs_to_sumo(lon: float, lat: float, cx: np.ndarray, cy: np.ndarray) -> np.ndarray:
    v = np.array([lon, lat, 1.0])
    return np.array([float(v @ cx), float(v @ cy)])


def load_osm_nodes(osm_map: Path) -> dict[str, tuple[float, float]]:
    nodes: dict[str, tuple[float, float]] = {}
    for ev, el in ET.iterparse(osm_map, events=("end",)):
        if el.tag == "node":
            nodes[el.get("id")] = (float(el.get("lon")), float(el.get("lat")))
        el.clear()
    return nodes


def te_kaha_centroid(osm_map: Path, nodes: dict[str, tuple[float, float]]) -> tuple[float, float]:
    for el in ET.parse(osm_map).getroot().iter("way"):
        name = next((t.get("v") for t in el.findall("tag") if t.get("k") == "name"), None)
        if name != "Te Kaha":
            continue
        pts = [nodes[r] for r in (nd.get("ref") for nd in el.findall("nd")) if r in nodes]
        if pts:
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return 172.6422, -43.5330


def load_fbx_road_vertices(fbx_path: Path) -> np.ndarray:
    scene = ufbx.load_file(str(fbx_path))
    pts: list[tuple[float, float]] = []
    for node in scene.nodes:
        if not node.mesh:
            continue
        name = node.mesh.name or node.name or ""
        if "osm_roads_" not in name:
            continue
        for v in node.mesh.vertices:
            pts.append((float(v.x), float(v.y)))
    if not pts:
        raise RuntimeError("No road meshes in FBX")
    return np.asarray(pts, dtype=np.float64)


def snap_road(roads: np.ndarray, fbx_xy: np.ndarray) -> np.ndarray:
    return roads[int(np.argmin(np.linalg.norm(roads - fbx_xy, axis=1)))]


def fit_map_transform(
    fbx_pts: np.ndarray, sumo_pts: np.ndarray, weights: np.ndarray, *, flip_z_only: bool | None = None
) -> dict:
    best: dict | None = None
    w = weights / weights.sum()
    flip_options = (flip_z_only,) if flip_z_only is not None else (False, True)
    for flip_z in flip_options:
        src = fbx_pts.copy()
        if flip_z:
            src[:, 1] *= -1.0
        for yaw in range(-180, 181, 1):
            rad = math.radians(yaw)
            c, s = math.cos(rad), math.sin(rad)
            rot = np.array([[c, -s], [s, c]])
            rotated = src @ rot.T
            t = (sumo_pts * w[:, None]).sum(0) - (rotated * w[:, None]).sum(0)
            pred = rotated + t
            err = np.linalg.norm(pred - sumo_pts, axis=1)
            weighted_mean = float((err * weights).sum() / weights.sum())
            cand = {
                "mean_m": float(err.mean()),
                "weighted_mean_m": weighted_mean,
                "max_m": float(err.max()),
                "yaw": float(yaw),
                "flip_z": flip_z,
                "position": [float(t[0]), float(t[1])],
                "per_landmark_m": err.tolist(),
            }
            if best is None or weighted_mean < best["weighted_mean_m"]:
                best = cand

    assert best is not None
    yaw = best["yaw"]
    unity = {
        "position": {"x": best["position"][0], "y": 0.0, "z": best["position"][1]},
        "rotation_euler": {"x": 0.0, "y": yaw, "z": 0.0},
        "rotation_quaternion": {
            "x": 0.0,
            "y": math.sin(math.radians(yaw) / 2.0),
            "z": 0.0,
            "w": math.cos(math.radians(yaw) / 2.0),
        },
        "local_scale": {"x": 1.0, "y": 1.0, "z": -1.0 if best["flip_z"] else 1.0},
    }
    return {"best": best, "unity_transform": unity}


def sumo_to_unity_world(sx: float, sy: float, yaw_deg: float, tx: float, tz: float) -> tuple[float, float]:
    """TraCI SUMO (x,y) -> Unity world (x,z) for map with scale Z=1 and rotation Y."""
    rad = math.radians(yaw_deg)
    c, s = math.cos(rad), math.sin(rad)
    dx, dy = sx - tx, sy - tz
    # Inverse of sumo = R @ [fx, fy] + t  with R = [[c,-s],[s,c]]
    fx = c * dx + s * dy
    fy = -s * dx + c * dy
    wx = c * fx + s * fy + tx
    wz = -s * fx + c * fy + tz
    return wx, wz


def patch_scene(scene_path: Path, unity: dict) -> None:
    text = scene_path.read_text(encoding="utf-8")
    p = unity["position"]
    q = unity["rotation_quaternion"]
    s = unity["local_scale"]
    yaw = unity["rotation_euler"]["y"]

    replacements = [
        (r"(propertyPath: m_LocalPosition\.x\n\s+value: )[-\d.]+", f"\\g<1>{p['x']:.4f}"),
        (r"(propertyPath: m_LocalPosition\.z\n\s+value: )[-\d.]+", f"\\g<1>{p['z']:.4f}"),
        (r"(propertyPath: m_LocalRotation\.w\n\s+value: )[-\d.]+", f"\\g<1>{q['w']:.8f}"),
        (r"(propertyPath: m_LocalRotation\.y\n\s+value: )[-\d.]+", f"\\g<1>{q['y']:.8f}"),
        (r"(propertyPath: m_LocalRotation\.x\n\s+value: )[-\d.]+", "\\g<1>0"),
        (r"(propertyPath: m_LocalRotation\.z\n\s+value: )[-\d.]+", "\\g<1>0"),
        (r"(propertyPath: m_LocalScale\.z\n\s+value: )[-\d.]+", f"\\g<1>{s['z']:.1f}"),
        (r"(propertyPath: m_LocalEulerAnglesHint\.y\n\s+value: )[-\d.]+", f"\\g<1>{yaw:.1f}"),
    ]
    for pat, repl in replacements:
        text, n = re.subn(pat, repl, text, count=1)
    scene_path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fbx", type=Path, default=root / "3D_simulation/Assets/Christchurch_Central_City_3D.fbx")
    parser.add_argument("--net", type=Path, default=root / "2D_simulation/data/output/network/Christchurch_Central_City_main_streets.net.xml")
    parser.add_argument("--osm-net", type=Path, default=root / "2D_simulation/data/output/network/Christchurch_Central_City_main_streets.osm.xml")
    parser.add_argument("--osm-map", type=Path, default=root / "2D_simulation/data/output/network/Christchurch_Central_City.osm.xml")
    parser.add_argument("--json-out", type=Path, default=root / "3D_simulation/Assets/StreamingAssets/Christchurch/calibration.json")
    parser.add_argument("--scene", type=Path, default=root / "3D_simulation/Assets/Scenes/SampleScene.unity")
    parser.add_argument("--update-scene", action="store_true")
    parser.add_argument(
        "--map-scale-z",
        type=float,
        default=None,
        help="Force map scale Z in output (e.g. 1). Use with --cubes-only to avoid changing map pose.",
    )
    parser.add_argument(
        "--cubes-only",
        action="store_true",
        help="Only write cube calibration; do not patch map position/rotation in scene.",
    )
    args = parser.parse_args()

    cx, cy, n_junction = fit_wgs84_to_sumo(args.net, args.osm_net)
    osm_nodes = load_osm_nodes(args.osm_map)
    tk_lon, tk_lat = te_kaha_centroid(args.osm_map, osm_nodes)
    roads = load_fbx_road_vertices(args.fbx)

    flip_for_map = False if args.map_scale_z == 1 else None
    t0 = np.array([1241.27, 1156.88])

    sumo_pts: list[np.ndarray] = []
    fbx_pts: list[np.ndarray] = []
    weights: list[float] = []
    rows: list[dict] = []

    for name, lon, lat, wt in LANDMARKS_WGS84:
        if name.startswith("Te Kaha"):
            lon, lat = tk_lon, tk_lat
        s = wgs_to_sumo(lon, lat, cx, cy)
        guess = np.array([s[0] - t0[0], s[1] - t0[1]])
        f = snap_road(roads, guess)
        sumo_pts.append(s)
        fbx_pts.append(f)
        weights.append(wt)
        rows.append(
            {
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "sumo_x": float(s[0]),
                "sumo_y": float(s[1]),
                "unity_world_x": float(s[0]),
                "unity_world_z": float(s[1]),
                "fbx_local_x": float(f[0]),
                "fbx_local_y": float(f[1]),
            }
        )

    src = np.asarray(fbx_pts)
    dst = np.asarray(sumo_pts)
    w = np.asarray(weights)
    fit = fit_map_transform(src, dst, w, flip_z_only=flip_for_map)
    best = fit["best"]
    unity = fit["unity_transform"]
    if args.map_scale_z is not None:
        unity["local_scale"]["z"] = float(args.map_scale_z)

    yaw = unity["rotation_euler"]["y"]
    tx = unity["position"]["x"]
    tz = unity["position"]["z"]

    cube_errors: list[float] = []
    for i, row in enumerate(rows):
        wx, wz = sumo_to_unity_world(row["sumo_x"], row["sumo_y"], yaw, tx, tz)
        row["unity_world_x"] = wx
        row["unity_world_z"] = wz
        cube_errors.append(
            math.hypot(wx - row["sumo_x"], wz - row["sumo_y"])
        )

    for i, res in enumerate(best["per_landmark_m"]):
        rows[i]["map_residual_m"] = res

    out = {
        "description": "Christchurch Central City: landmarks lat/lon -> SUMO network XY (TraCI) = Unity XZ for cubes; FBX map uses unityMapTransform.",
        "wgs84ToSumo": {
            "sumoX": {"lon": float(cx[0]), "lat": float(cx[1]), "constant": float(cx[2])},
            "sumoY": {"lon": float(cy[0]), "lat": float(cy[1]), "constant": float(cy[2])},
            "junctionPairsUsed": n_junction,
        },
        "landmarks": rows,
        "mapFit": {
            "meanResidualM": best["mean_m"],
            "weightedMeanResidualM": best["weighted_mean_m"],
            "maxResidualM": best["max_m"],
        },
        "unityMapTransform": unity,
        "cubeTransform": {
            "yawDegrees": yaw,
            "translationX": tx,
            "translationZ": tz,
            "mapScaleZ": unity["local_scale"]["z"],
        },
        "vehicleSpawner": {
            "autoRecenterOnFirstVehicle": False,
            "sumoPlaneManualOffset": {"x": 0.0, "y": 0.0},
            "coordinateScale": 1.0,
        },
    }

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if args.update_scene and not args.cubes_only:
        patch_scene(args.scene, unity)
    elif args.cubes_only and args.map_scale_z is not None:
        text = args.scene.read_text(encoding="utf-8")
        text, _ = re.subn(
            r"(propertyPath: m_LocalScale\.z\n\s+value: )[-\d.]+",
            f"\\g<1>{args.map_scale_z:.1f}",
            text,
            count=1,
        )
        args.scene.write_text(text, encoding="utf-8")

    print(f"Junction WGS84->SUMO fit: {n_junction} pairs")
    print(f"Map landmarks: weighted mean error {best['weighted_mean_m']:.1f} m  (mean {best['mean_m']:.1f} m)")
    for r in rows:
        print(
            f"  {r['name']:28s}  SUMO ({r['sumo_x']:.0f},{r['sumo_y']:.0f})  "
            f"-> Unity ({r['unity_world_x']:.0f},{r['unity_world_z']:.0f})  map {r['map_residual_m']:.1f}m"
        )
    p = unity["position"]
    print(f"\nMap Transform: pos ({p['x']:.2f}, 0, {p['z']:.2f})  yaw {yaw:.1f}°  scale Z {unity['local_scale']['z']}")
    print("Cubes: TraCI SUMO -> Unity via inverse/forward map transform (scale Z=1, no mirror)")
    print(f"Wrote {args.json_out}")
    if args.update_scene:
        print(f"Updated {args.scene}")


if __name__ == "__main__":
    main()

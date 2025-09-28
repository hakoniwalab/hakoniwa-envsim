from __future__ import annotations
import json
from hakoniwa_envsim.creator.zone import ZoneEffect
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Tuple


class CreatorBuilder:
    def __init__(self, env_model: Dict[str, Any]):
        self.env = env_model
        self.areas: List[Dict[str, Any]] = []
        self.props: List[Dict[str, Any]] = []         # {"id": str, "properties": {...}}
        self.links: List[Dict[str, Any]] = []
        self.base_prop: Dict[str, Any] = {}

    # ---- Step1: base 解析 -------------------------------------------------
    def build_base(self) -> "CreatorBuilder":
        base = self.env["base"]
        wind = base["wind"]
        # WindBase は vector_ms or (dir_deg + speed_ms) の oneOf。今回は vector_ms 前提。
        if "vector_ms" in wind:
            w = list(wind["vector_ms"])
        else:
            # 念のための簡易フォールバック
            import math
            deg = float(wind["dir_deg"]); spd = float(wind["speed_ms"])
            rad = math.radians(deg)
            w = [spd * math.cos(rad), spd * math.sin(rad), 0.0]

        self.base_prop = {
            "wind_velocity": w,
            "temperature": float(self.env["base"].get("temperature_C", 20.0)),
            "sea_level_atm": float(self.env["base"].get("pressure_atm", 1.0)),
            "gps_strength": float(self.env["base"].get("gps_strength", 1.0))
        }
        return self

    # ---- Step2: グリッド → area.json --------------------------------------
    def build_grid(self) -> "CreatorBuilder":
        grid = self.env["grid"]
        extent_x, extent_y, extent_z = map(float, grid["extent_m"])
        dx, dy, dz = map(float, grid["cell_m"])
        nx, ny = int(extent_x // dx), int(extent_y // dy)

        for iy in range(ny):
            for ix in range(nx):
                aid = f"area_{iy}_{ix}"
                xmin, xmax = ix * dx, (ix + 1) * dx
                ymin, ymax = iy * dy, (iy + 1) * dy
                zmin, zmax = 0.0, extent_z
                self.areas.append({
                    "area_id": aid,
                    "bounds": {
                        "min": {"x": xmin, "y": ymin, "z": zmin},
                        "max": {"x": xmax, "y": ymax, "z": zmax}
                    }
                })
        return self

    # ---- Step3: base → 各エリアの property --------------------------------
    def build_properties(self) -> "CreatorBuilder":
        for a in self.areas:
            pid = f"prop_{a['area_id']}"
            self.props.append({
                "id": pid,
                "properties": dict(self.base_prop)  # ディープコピー不要な構造
            })
        return self

    # ---- Step4: zone 適用 -------------------------------------------------
    def apply_zones(self) -> "CreatorBuilder":
        zone_defs = self.env.get("zones", [])
        if not zone_defs:
            return self

        zones = [ZoneEffect(z) for z in zone_defs]
        zones_sorted = sorted(zones, key=lambda z: z.priority, reverse=True)

        for prop, area in zip(self.props, self.areas):
            cx, cy = _cell_center(area["bounds"])
            pos = (cx, cy, 0.0)

            # Base wind & gps
            wind = np.array(prop["properties"]["wind_velocity"], dtype=float)
            gps = float(prop["properties"].get("gps_strength", 1.0))

            # Apply zones
            for z in zones_sorted:
                if z.contains(pos):
                    wind = z.apply(wind, pos)
                    gps = z.apply_gps(gps, pos)

            # Update props
            prop["properties"]["wind_velocity"] = list(map(float, wind))
            prop["properties"]["gps_strength"] = gps

        return self


    # ---- Step5: link 生成 --------------------------------------------------
    def build_links(self) -> "CreatorBuilder":
        for a in self.areas:
            pid = f"prop_{a['area_id']}"
            self.links.append({"area_id": a["area_id"], "area_property_id": pid})
        return self

    def result(self) -> tuple[Dict, Dict, Dict]:
        return (
            {"space_areas": self.areas},
            {"area_properties": self.props},
            {"links": self.links},
        )


# ----------------- ヘルパ -----------------

def _cell_center(bounds: Dict[str, Any]) -> Tuple[float, float]:
    mn, mx = bounds["min"], bounds["max"]
    cx = (float(mn["x"]) + float(mx["x"])) * 0.5
    cy = (float(mn["y"]) + float(mx["y"])) * 0.5
    return cx, cy




# ----------------- CLI -----------------

def load_environment_model(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create datasets from environment_model.json")
    parser.add_argument("--infile", default="../examples/datasets/kobe/environment_model.json")
    parser.add_argument("--outdir", default="../examples/datasets/kobe/generated")
    parser.add_argument("--no-zones", action="store_true", help="Ignore zones even if present")
    args = parser.parse_args()

    env = load_environment_model(args.infile)
    builder = CreatorBuilder(env)

    builder.build_base().build_grid().build_properties()
    if not args.no_zones:
        builder.apply_zones()
    builder.build_links()

    area, props, links = builder.result()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "area.json").write_text(json.dumps(area, indent=2), encoding="utf-8")
    (outdir / "property.json").write_text(json.dumps(props, indent=2), encoding="utf-8")
    (outdir / "link.json").write_text(json.dumps(links, indent=2), encoding="utf-8")
    print(f"Generated dataset in {outdir.resolve()}")


if __name__ == "__main__":
    main()

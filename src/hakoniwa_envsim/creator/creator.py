from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List


class CreatorBuilder:
    def __init__(self, env_model: Dict[str, Any]):
        self.env = env_model
        self.areas: List[Dict[str, Any]] = []
        self.props: List[Dict[str, Any]] = []
        self.links: List[Dict[str, Any]] = []
        self.base_prop: Dict[str, Any] = {}

    def build_base(self) -> "CreatorBuilder":
        base = self.env["base"]
        self.base_prop = {
            "wind_velocity": list(base["wind"]["vector_ms"]),
            "temperature": base.get("temperature_C", 20.0),
            "sea_level_atm": base.get("pressure_atm", 1.0),
        }
        return self

    def build_grid(self) -> "CreatorBuilder":
        grid = self.env["grid"]
        extent_x, extent_y, extent_z = grid["extent_m"]
        dx, dy, dz = grid["cell_m"]
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

    def build_properties(self) -> "CreatorBuilder":
        for a in self.areas:
            pid = f"prop_{a['area_id']}"
            self.props.append({
                "id": pid,
                "properties": dict(self.base_prop)  # コピー
            })
        return self

    def build_links(self) -> "CreatorBuilder":
        for a in self.areas:
            pid = f"prop_{a['area_id']}"
            self.links.append({
                "area_id": a["area_id"],
                "area_property_id": pid
            })
        return self

    def result(self) -> tuple[Dict, Dict, Dict]:
        return (
            {"space_areas": self.areas},
            {"area_properties": self.props},
            {"links": self.links},
        )


def load_environment_model(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create datasets from environment_model.json")
    parser.add_argument("--infile", default="../examples/datasets/wind_tunnel/environment_model.json")
    parser.add_argument("--outdir", default="../examples/datasets/wind_tunnel/generated")
    args = parser.parse_args()

    env = load_environment_model(args.infile)
    builder = CreatorBuilder(env)
    area, props, links = (
        builder.build_base()
               .build_grid()
               .build_properties()
               .build_links()
               .result()
    )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    (outdir / "area.json").write_text(json.dumps(area, indent=2), encoding="utf-8")
    (outdir / "property.json").write_text(json.dumps(props, indent=2), encoding="utf-8")
    (outdir / "link.json").write_text(json.dumps(links, indent=2), encoding="utf-8")

    print(f"Generated dataset in {outdir.resolve()}")


if __name__ == "__main__":
    main()

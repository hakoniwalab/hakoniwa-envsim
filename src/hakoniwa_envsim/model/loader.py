from __future__ import annotations
import pathlib, json, warnings
from typing import Any, List, Dict, Mapping, Iterable

from .models import SpaceArea, AreaProperty, Link, VisualArea, Point3D, AABB3D

try:
    from jsonschema import validate  # optional dependency
    _HAS_JSONSCHEMA = True
except Exception:
    _HAS_JSONSCHEMA = False


class ModelLoader:
    """JSONファイルを読み込んでモデル化する共通ローダ"""

    def __init__(self, validate_schema: bool = True, schema_dir: str | pathlib.Path | None = None):
        self.validate_schema = validate_schema
        # デフォルト: このパッケージの schemas ディレクトリ
        if schema_dir is None:
            self.schema_dir = pathlib.Path(__file__).parent.parent / "schemas"
        else:
            self.schema_dir = pathlib.Path(schema_dir)

    def _load_json(self, path: str | pathlib.Path) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _validate(self, instance: Any, schema_name: str) -> None:
        if self.validate_schema and _HAS_JSONSCHEMA:
            schema_path = self.schema_dir / schema_name
            schema = self._load_json(schema_path)
            validate(instance=instance, schema=schema)

    # --- 公開API ------------------------------------------------------

    def load_space_areas(self, path: str | pathlib.Path) -> List[SpaceArea]:
        """area.json → SpaceArea のリスト"""
        data = self._load_json(path)
        self._validate(data, "space_area.schema.json")

        areas: List[SpaceArea] = []
        for item in data["space_areas"]:
            mn, mx = item["bounds"]["min"], item["bounds"]["max"]
            areas.append(
                SpaceArea(
                    area_id=item["area_id"],
                    bounds=AABB3D(
                        min=Point3D(**{k: float(v) for k, v in mn.items()}),
                        max=Point3D(**{k: float(v) for k, v in mx.items()}),
                    ),
                )
            )
        return areas

    def load_area_properties(self, path: str | pathlib.Path) -> Dict[str, AreaProperty]:
        """property.json → id→AreaProperty の辞書"""
        data = self._load_json(path)
        self._validate(data, "area_properties.schema.json")

        props: Dict[str, AreaProperty] = {}
        for item in data["area_properties"]:
            pid = item["id"]
            p = item["properties"]
            props[pid] = AreaProperty(
                id=pid,
                wind_velocity=tuple(map(float, p["wind_velocity"])),
                temperature=float(p["temperature"]),
                sea_level_atm=float(p["sea_level_atm"]),
                gps_strength=float(p.get("gps_strength", 1.0))
            )
        return props

    def load_links(self, path: str | pathlib.Path) -> List[Link]:
        """link.json → Link のリスト"""
        data = self._load_json(path)
        self._validate(data, "link.schema.json")
        return [Link(**lk) for lk in data["links"]]

    def build_visual_areas(
        self,
        areas: Iterable[SpaceArea],
        props: Mapping[str, AreaProperty],
        links: Iterable[Link],
    ) -> List[VisualArea]:
        """SpaceArea + AreaProperty + Link を統合して VisualArea に変換"""
        area_map = {a.area_id: a for a in areas}
        visuals: List[VisualArea] = []
        linked_ids: set[str] = set()

        for lk in links:
            sa = area_map.get(lk.area_id)
            ap = props.get(lk.area_property_id)

            if not sa:
                warnings.warn(f"Unknown area_id {lk.area_id}")
                continue
            if not ap:
                warnings.warn(f"Unknown area_property_id {lk.area_property_id}")
                continue

            visuals.append(
                VisualArea(
                    area_id=sa.area_id,
                    aabb2d=sa.bounds.to_2d(),
                    temperature=ap.temperature,
                    sea_level_atm=ap.sea_level_atm,
                    wind_velocity=ap.wind_velocity,
                    gps_strength=ap.gps_strength,
                )
            )
            linked_ids.add(sa.area_id)

        for aid in area_map.keys() - linked_ids:
            warnings.warn(f"Area {aid} has no linked property")

        return visuals

# envbuilder.py
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any, List
import json

from hakoniwa_envsim.fastsearch.builder import AABB, build_bvh
from hakoniwa_envsim.fastsearch.search import search_point

@dataclass
class AreaRecord:
    id: str
    aabb: AABB

@dataclass
class AreaPropertyRecord:
    id: str
    props: dict

class Environment:
    """
    - area.json から AABB 群を生成し BVH を構築
    - link.json で area_id -> area_property_id を対応付け
    - property.json から area_property を保持
    - ポイントから area を特定し、対応する property を返せる
    """
    def __init__(
        self,
        areas: Dict[str, AreaRecord],
        links: Dict[str, str],
        properties: Dict[str, AreaPropertyRecord],
        bvh_root
    ):
        self.areas = areas
        self.links = links
        self.properties = properties
        self.bvh_root = bvh_root

    @classmethod
    def from_files(
        cls,
        area_json_path: str,
        link_json_path: str,
        property_json_path: str,
        *,
        max_depth: int = 7,
        leaf_capacity: int = 1
    ) -> "Environment":
        # --- areas ---
        with open(area_json_path, "r") as f:
            area_data = json.load(f)

        areas_list: List[AreaRecord] = []
        for a in area_data.get("space_areas", []):
            aid = a["area_id"]
            b = a["bounds"]
            amin = b["min"]; amax = b["max"]
            aabb = AABB(
                float(amin["x"]), float(amin["y"]), float(amin["z"]),
                float(amax["x"]), float(amax["y"]), float(amax["z"]),
                aid
            )
            areas_list.append(AreaRecord(id=aid, aabb=aabb))
        areas_map: Dict[str, AreaRecord] = {ar.id: ar for ar in areas_list}

        # BVH 構築（builder.py の leaf_capacity に対応している前提）
        bvh_root = build_bvh([ar.aabb for ar in areas_list],
                             max_depth=max_depth, leaf_capacity=leaf_capacity)

        # --- links ---
        with open(link_json_path, "r") as f:
            link_data = json.load(f)

        links_map: Dict[str, str] = {}
        for lk in link_data.get("links", []):
            area_id = lk["area_id"]
            prop_id = lk["area_property_id"]
            links_map[area_id] = prop_id

        # --- properties ---
        with open(property_json_path, "r") as f:
            prop_data = json.load(f)

        props_map: Dict[str, AreaPropertyRecord] = {}
        for ap in prop_data.get("area_properties", []):
            pid = ap["id"]
            props_map[pid] = AreaPropertyRecord(id=pid, props=ap.get("properties", {}))

        return cls(areas_map, links_map, props_map, bvh_root)

    # --- 検索系ユーティリティ ---

    def find_area_ids_at(self, x: float, y: float, z: float) -> List[str]:
        """BVH を使って (x,y,z) に含まれる area_id を列挙（通常は 0/1 件に収束）"""
        hits: List[str] = []
        stats = {"visited": 0}
        search_point(self.bvh_root, x, y, z, found=hits, stats=stats, precise=True)
        return hits

    def find_primary_area_at(self, x: float, y: float, z: float) -> Optional[str]:
        """重なりがない前提なら最初のヒットを採用"""
        hits = self.find_area_ids_at(x, y, z)
        return hits[0] if hits else None

    def get_property_for_area(self, area_id: str) -> Optional[dict]:
        """area_id から対応する area_property（辞書）を返す"""
        pid = self.links.get(area_id)
        if pid is None:
            return None
        ap = self.properties.get(pid)
        return ap.props if ap else None

    def get_property_at(self, x: float, y: float, z: float) -> Tuple[Optional[str], Optional[dict]]:
        """座標→area→property をワンショット取得"""
        aid = self.find_primary_area_at(x, y, z)
        if aid is None:
            return None, None
        return aid, self.get_property_for_area(aid)


# ===== Environment: Debug / Inspect Utilities =====

    def area_bounds(self, area_id: str):
        """area_id の AABB を (minx,miny,minz,maxx,maxy,maxz) で返す。無ければ None。"""
        ar = self.areas.get(area_id)
        if not ar:
            return None
        a = ar.aabb
        return (a.minx, a.miny, a.minz, a.maxx, a.maxy, a.maxz)

    def link_of(self, area_id: str):
        """area_id に対応づく area_property_id（link）を返す。無ければ None。"""
        return self.links.get(area_id)

    def area_property(self, area_property_id: str):
        """area_property_id のプロパティ辞書を返す。無ければ None。"""
        ap = self.properties.get(area_property_id)
        return ap.props if ap else None

    def inspect_area(self, area_id: str):
        """エリア1件の中身を丸ごと確認するための便覧。"""
        aabb = self.area_bounds(area_id)
        pid  = self.link_of(area_id)
        props = self.area_property(pid) if pid else None
        return {
            "area_id": area_id,
            "bounds": aabb,
            "linked_property_id": pid,
            "has_property": props is not None,
            "properties": props,
        }

    def debug_at(self, x: float, y: float, z: float, *, precise: bool = True):
        """
        点 (x,y,z) に対して：
          - BVH探索の訪問ノード数
          - ヒットした area_id とその AABB
          - 主ヒット（先頭）
          - link（area_property_id）および properties の有無
        をまとめて返す。
        """
        from hakoniwa_envsim.fastsearch.search import search_point  # 循環import回避のためここで
        stats = {"visited": 0}
        hits: list[str] = []
        search_point(self.bvh_root, x, y, z, found=hits, stats=stats, precise=precise)

        hit_details = []
        for aid in hits:
            aabb = self.area_bounds(aid)
            hit_details.append({"area_id": aid, "bounds": aabb})

        primary = hits[0] if hits else None
        pid = self.link_of(primary) if primary else None
        props = self.area_property(pid) if pid else None

        return {
            "point": (x, y, z),
            "visited_nodes": stats["visited"],
            "hits": hit_details,                 # 複数出る場合もここで確認
            "primary_area": primary,             # 通常は非重複なので1件に収束
            "linked_property_id": pid,
            "has_property": props is not None,
            "properties": props,
        }

    def validate_integrity(self):
        """
        データ整合性チェック：
          - リンク未設定のエリア
          - 存在しないプロパティを指すリンク
          - リンクが存在しない（参照されていない）プロパティ
          - リンクが存在しない（参照されていない）エリア（通常は起きない想定）
        """
        areas_without_link = [aid for aid in self.areas.keys() if aid not in self.links]
        links_to_missing_property = []
        for aid, pid in self.links.items():
            if pid not in self.properties:
                links_to_missing_property.append({"area_id": aid, "missing_property_id": pid})

        # 参照されていない property
        referenced_props = set(self.links.values())
        properties_unreferenced = [pid for pid in self.properties.keys() if pid not in referenced_props]

        # 参照されていない area（通常はゼロ、link側に未知areaがあれば検出）
        areas_unreferenced_by_links = [aid for aid in self.links.keys() if aid not in self.areas]

        return {
            "areas_without_link": areas_without_link,
            "links_to_missing_property": links_to_missing_property,
            "properties_unreferenced": properties_unreferenced,
            "links_to_missing_area": areas_unreferenced_by_links,
        }

    def explain_at(self, x: float, y: float, z: float, *, precise: bool = True) -> str:
        """
        人間向けの1行/複数行レポートを生成（ログ出力用）。
        """
        info = self.debug_at(x, y, z, precise=precise)
        lines = []
        lines.append(f"🎯 point=({x:.3f},{y:.3f},{z:.3f})  visited={info['visited_nodes']}")
        if not info["hits"]:
            lines.append("❌ Hit area: None")
            return "\n".join(lines)

        lines.append("🔍 Hits:")
        for h in info["hits"]:
            mnx,mny,mnz,mxx,mxy,mxz = h["bounds"]
            lines.append(f"  - {h['area_id']}  AABB: min({mnx},{mny},{mnz}) max({mxx},{mxy},{mxz})")

        lines.append(f"✅ Primary: {info['primary_area']}")
        lines.append(f"🔗 link (area_property_id): {info['linked_property_id']}")
        if info["has_property"]:
            lines.append("🧾 properties:")
            for k, v in info["properties"].items():
                lines.append(f"   - {k}: {v}")
        else:
            lines.append("⚠️ property not found")
        return "\n".join(lines)
# ===== End of Environment =====
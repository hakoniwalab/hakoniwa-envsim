#!/usr/bin/env python3
"""Open passages through building colliders where a carriageway runs through them.

PLATEAU models some buildings that straddle a road as one solid down to the
ground: the Tokyo Metropolitan Government's assembly building spans 都庁通り
with an arch cars drive through, but its LOD2 has a ground surface and a roof
across the road and no surface for the arch, so the building colliders fill the
road. The road data knows better: the LOD3 carriageway (TrafficArea function
1000 車道部, 1020 車道交差部, measured in 3D) runs through the footprint. Where
it does, this cuts the building colliders away above the carriageway up to a
vehicle clearance (4.5 m by default, the 建築限界), leaving everything else.

Every collider is convex (boxes and convex prisms), so each one that reaches
into a passage is split by the passage's planes into convex pieces outside it
(plane clipping of convex polyhedra), written back as convex meshes. The road surface is cut
at the carriageway polygon shrunk by a margin, so walls along a road are not
nicked. A receipt lists what was cut.

    python building_road_passages.py --mjcf buildings.xml --source <build/source> \\
        --world-frame world-frame.json --receipt road-passages.json [--clearance 4.5]
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from shapely.geometry import MultiPoint, Polygon, box as shapely_box
from shapely.strtree import STRtree

from citygml2glb import GlbError, _polygon_rings, triangulate_rings
from geodesy import SUPPORTED_CRS, project_to_local_enu, srs_code
from mjcf_prism import format_numbers
from world_frame import load_world_frame

GML = "http://www.opengis.net/gml"
TRAN = "http://www.opengis.net/citygml/transportation/2.0"
TRAFFIC_AREA = f"{{{TRAN}}}TrafficArea"
FUNCTION = f"{{{TRAN}}}function"
LOD3 = f"{{{TRAN}}}lod3MultiSurface"
GML_ID = f"{{{GML}}}id"
CARRIAGEWAY_FUNCTIONS = ("1000", "1020")  # 車道部, 車道交差部
MIN_VOLUME_M3 = 1e-6


class PassageError(RuntimeError):
    pass


def _crs(path: Path) -> int:
    head = path.read_bytes()[:65536].decode("utf-8", "replace")
    start = head.find('srsName="')
    found = srs_code(head[start + 9:head.find('"', start + 9)]) if start >= 0 else None
    return found if found in SUPPORTED_CRS else 6697


def carriageways(source: Path, frame: dict, functions=CARRIAGEWAY_FUNCTIONS, margin_m: float = 0.2,
                 clearance_m: float = 4.5) -> list[dict]:
    """The LOD3 carriageway polygons (MuJoCo frame X=North, Y=-East) with the
    height a passage through them must be clear up to."""
    origin = frame["origin"]
    latitude, longitude = float(origin["latitude"]), float(origin["longitude"])
    offset = float(origin["altitude_offset_m"])
    paths = sorted(source.rglob("*_tran_*_op.gml")) if source.is_dir() else [source]
    result, seen = [], set()
    for path in paths:
        epsg = _crs(path)
        inside_lod3 = 0
        function = None
        for event, element in ET.iterparse(path, events=("start", "end")):
            if event == "start":
                if element.tag == TRAFFIC_AREA:
                    function = None
                elif element.tag == LOD3:
                    inside_lod3 += 1
                continue
            if element.tag == FUNCTION:
                function = (element.text or "").strip()
            elif element.tag == LOD3:
                inside_lod3 -= 1
            elif element.tag == f"{{{GML}}}Polygon" and inside_lod3 and function in functions:
                polygon_id = element.get(GML_ID) or f"{path.name}:{len(result)}"
                if polygon_id in seen:
                    continue
                rings = _polygon_rings(element)
                if not rings:
                    continue
                local = [[(n, -e, z - offset) for e, n, z in project_to_local_enu(points, latitude, longitude, epsg)]
                         for _, points in rings]
                shape = Polygon([(x, y) for x, y, _ in local[0]], [[(x, y) for x, y, _ in ring] for ring in local[1:]])
                if not shape.is_valid:
                    shape = shape.buffer(0)
                shape = shape.buffer(-margin_m)
                if shape.is_empty:
                    continue
                seen.add(polygon_id)
                top = max(z for ring in local for _, _, z in ring) + clearance_m
                result.append({"id": polygon_id, "function": function, "shape": shape, "top": top,
                               "triangles": _triangles(shape)})
            if element.tag == TRAFFIC_AREA:
                element.clear()
    return result


# --- MJCF geoms as convex polyhedra ----------------------------------------------------------
#
# A polyhedron is a list of planar convex faces (lists of XYZ points). Boxes
# and the prisms obb2mjcf writes are convex, and MuJoCo collides with a mesh's
# convex hull, so face orientation does not matter: pieces are written back as
# fan-triangulated faces.

class Polyhedron:
    def __init__(self, faces):
        self.faces = [np.asarray(face, dtype=float) for face in faces if len(face) >= 3]

    @property
    def vertices(self) -> np.ndarray:
        if not self.faces:
            return np.zeros((0, 3))
        points = np.vstack(self.faces)
        return np.unique(np.round(points, 6), axis=0)

    @property
    def volume(self) -> float:
        vertices = self.vertices
        if len(vertices) < 4:
            return 0.0
        centre = vertices.mean(axis=0)
        total = 0.0
        for face in self.faces:
            for i in range(1, len(face) - 1):
                total += abs(np.linalg.det(np.array([face[0] - centre, face[i] - centre, face[i + 1] - centre]))) / 6.0
        return total

    @property
    def bounds(self):
        vertices = self.vertices
        return vertices.min(axis=0), vertices.max(axis=0)

    def triangles(self) -> np.ndarray:
        vertices = self.vertices
        lookup = {tuple(v): i for i, v in enumerate(vertices)}
        faces = []
        for face in self.faces:
            indices = [lookup[tuple(np.round(point, 6))] for point in face]
            for i in range(1, len(indices) - 1):
                if len({indices[0], indices[i], indices[i + 1]}) == 3:
                    faces.append((indices[0], indices[i], indices[i + 1]))
        return np.array(faces, dtype=int)


def _clip_face(face: np.ndarray, normal: np.ndarray, origin: np.ndarray, epsilon: float = 1e-9):
    """The part of a convex face on the side `normal` points to (Sutherland-Hodgman) and
    the segment where it crosses the plane (two points, or None)."""
    distances = (face - origin) @ normal
    kept, crossings = [], []
    for i in range(len(face)):
        p, q = face[i], face[(i + 1) % len(face)]
        dp, dq = distances[i], distances[(i + 1) % len(face)]
        if dp >= -epsilon:
            kept.append(p)
        if (dp > epsilon and dq < -epsilon) or (dp < -epsilon and dq > epsilon):
            t = dp / (dp - dq)
            point = p + t * (q - p)
            kept.append(point)
            crossings.append(point)
    return kept, crossings


def clip(polyhedron: Polyhedron, normal, origin) -> Polyhedron | None:
    """The convex part of the polyhedron on the side `normal` points to, capped; None when empty."""
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    origin = np.asarray(origin, dtype=float)
    faces, cap_points = [], []
    for face in polyhedron.faces:
        kept, crossings = _clip_face(face, normal, origin)
        if len(kept) >= 3:
            faces.append(np.array(kept))
        cap_points.extend(crossings)
    if cap_points:
        points = np.unique(np.round(np.array(cap_points), 6), axis=0)
        if len(points) >= 3:
            # The section of a convex solid is a convex polygon: order its points round their centre.
            centre = points.mean(axis=0)
            axis_u = points[0] - centre
            if np.linalg.norm(axis_u) < 1e-9:
                axis_u = points[1] - centre
            axis_u = axis_u - normal * (axis_u @ normal)
            axis_u /= np.linalg.norm(axis_u)
            axis_v = np.cross(normal, axis_u)
            angles = np.arctan2((points - centre) @ axis_v, (points - centre) @ axis_u)
            faces.append(points[np.argsort(angles)])
    result = Polyhedron(faces)
    return result if len(result.faces) >= 4 and result.volume > MIN_VOLUME_M3 else None


def _rotation(geom: ET.Element) -> np.ndarray:
    if "xyaxes" in geom.attrib:
        values = np.array(list(map(float, geom.attrib["xyaxes"].split())))
        x = values[:3] / np.linalg.norm(values[:3])
        y = values[3:] - np.dot(values[3:], x) * x
        y /= np.linalg.norm(y)
        return np.column_stack((x, y, np.cross(x, y)))
    if "euler" in geom.attrib:  # degrees, MuJoCo's default xyz sequence
        ex, ey, ez = (math.radians(float(v)) for v in geom.attrib["euler"].split())
        cx, sx, cy, sy, cz, sz = math.cos(ex), math.sin(ex), math.cos(ey), math.sin(ey), math.cos(ez), math.sin(ez)
        rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        return rx @ ry @ rz
    if "quat" in geom.attrib:
        w, x, y, z = map(float, geom.attrib["quat"].split())
        return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    return np.eye(3)


BOX_FACES = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))


def geom_polyhedron(geom: ET.Element, meshes: dict) -> Polyhedron | None:
    kind = geom.get("type", "sphere")
    rotation = _rotation(geom)
    pos = np.array(list(map(float, geom.get("pos", "0 0 0").split())))
    if kind == "box":
        size = np.array(list(map(float, geom.attrib["size"].split())))
        corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]) * size
        corners = corners @ rotation.T + pos
        return Polyhedron([corners[list(face)] for face in BOX_FACES])
    if kind == "mesh" and geom.get("mesh") in meshes:
        vertices, faces = meshes[geom.get("mesh")]
        if faces is None or len(faces) == 0:
            return None
        vertices = vertices @ rotation.T + pos
        return Polyhedron([vertices[list(face)] for face in faces])
    return None


def subtract_halfspaces(polyhedron: Polyhedron, planes) -> tuple[list[Polyhedron], float]:
    """polyhedron minus the convex volume inside every plane ((normal, origin),
    normals pointing out of that volume): the convex pieces outside it, in the
    planes' order, and the volume taken out."""
    outside, remainder = [], polyhedron
    for normal, origin in planes:
        part = clip(remainder, normal, origin)
        if part is not None:
            outside.append(part)
        remainder = clip(remainder, tuple(-c for c in normal), origin)
        if remainder is None:
            return outside, polyhedron.volume - sum(p.volume for p in outside)
    return outside, remainder.volume


def polyhedron_planes(polyhedron: Polyhedron) -> list:
    """(outward normal, a point) of each face of a convex polyhedron."""
    centre = polyhedron.vertices.mean(axis=0)
    planes = []
    for face in polyhedron.faces:
        normal = np.cross(face[1] - face[0], face[2] - face[0])
        length = np.linalg.norm(normal)
        if length < 1e-12:
            continue
        normal = normal / length
        if (centre - face[0]) @ normal > 0:
            normal = -normal
        planes.append((tuple(normal), tuple(face[0])))
    return planes


def box_polyhedron(corners) -> Polyhedron:
    """A box from its 8 corners in the order of BOX_FACES ((-,-,-), (-,-,+), (-,+,-), ...)."""
    corners = np.asarray(corners, dtype=float)
    return Polyhedron([corners[list(face)] for face in BOX_FACES])


def clip_triangles(positions: np.ndarray, attributes: dict, indices: np.ndarray, planes) -> tuple:
    """Surface triangles minus the convex volume inside every plane (as
    subtract_halfspaces, for a surface): (positions, attributes, indices) with
    the parts inside the volume cut away; attributes (normals, UVs, colours)
    interpolated along the cut edges. Triangles clear of the volume are kept."""
    positions = np.asarray(positions, dtype=float)
    names = list(attributes)
    out_positions, out_attributes, out_faces = [], {name: [] for name in names}, []
    cache: dict = {}

    def emit(point, values):
        key = tuple(np.round(point, 6)) + tuple(tuple(np.round(v, 5)) for v in values)
        if key not in cache:
            cache[key] = len(out_positions)
            out_positions.append(point)
            for name, value in zip(names, values):
                out_attributes[name].append(value)
        return cache[key]

    def clip_polygon(polygon, normal, origin, epsilon=1e-9):
        kept = []
        distances = [(p - origin) @ normal for p, _ in polygon]
        for i in range(len(polygon)):
            (p, vp), (q, vq) = polygon[i], polygon[(i + 1) % len(polygon)]
            dp, dq = distances[i], distances[(i + 1) % len(polygon)]
            if dp >= -epsilon:
                kept.append((p, vp))
            if (dp > epsilon and dq < -epsilon) or (dp < -epsilon and dq > epsilon):
                t = dp / (dp - dq)
                kept.append((p + t * (q - p), [a + t * (b - a) for a, b in zip(vp, vq)]))
        return kept if len(kept) >= 3 else None

    plane_arrays = [(np.asarray(n, float) / np.linalg.norm(n), np.asarray(o, float)) for n, o in planes]
    for face in np.asarray(indices).reshape(-1, 3):
        corners = [(positions[i], [np.asarray(attributes[name][i], float) for name in names]) for i in face]
        # Clear of the volume when all three corners are outside one plane.
        if any(all((p - o) @ n > 1e-9 for p, _ in corners) for n, o in plane_arrays):
            out_faces.append([emit(p, v) for p, v in corners])
            continue
        remainder = corners
        for n, o in plane_arrays:
            outside = clip_polygon(remainder, n, o)
            if outside:
                ids = [emit(p, v) for p, v in outside]
                out_faces.extend([ids[0], ids[i], ids[i + 1]] for i in range(1, len(ids) - 1))
            remainder = clip_polygon(remainder, -n, o)
            if remainder is None:
                break
    return (np.array(out_positions, dtype=float).reshape(-1, 3),
            {name: np.array(values, dtype=float) for name, values in out_attributes.items()},
            np.array(out_faces, dtype=int).reshape(-1, 3))


def subtract_prism(polyhedron: Polyhedron, triangle, top: float) -> tuple[list[Polyhedron], float]:
    """polyhedron minus the volume under `top` over a 2D triangle: the convex
    pieces outside it (split by the top plane first, then the triangle's
    edges), and the volume taken out."""
    planes = [((0.0, 0.0, 1.0), (0.0, 0.0, top))]  # above the top: outside
    (ax, ay), (bx, by), (cx, cy) = triangle
    ccw = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 0
    for (px, py), (qx, qy) in (((ax, ay), (bx, by)), ((bx, by), (cx, cy)), ((cx, cy), (ax, ay))):
        nx, ny = (qy - py, px - qx) if ccw else (py - qy, qx - px)  # outward normal of the edge
        length = math.hypot(nx, ny)
        if length < 1e-9:
            continue
        planes.append(((nx / length, ny / length, 0.0), (px, py, 0.0)))
    return subtract_halfspaces(polyhedron, planes)


def footprint(polyhedron: Polyhedron) -> Polygon:
    return MultiPoint([tuple(p) for p in polyhedron.vertices[:, :2]]).convex_hull


def cut_passages(polyhedron: Polyhedron, passages: list[dict]) -> tuple[list[Polyhedron], float]:
    """polyhedron minus every passage volume: convex pieces, and the volume removed."""
    pieces, removed = [polyhedron], 0.0
    for passage in passages:
        if polyhedron.bounds[0][2] >= passage["top"] or not footprint(polyhedron).intersects(passage["shape"]):
            continue
        for triangle in passage["triangles"]:
            next_pieces = []
            for piece in pieces:
                outside, gone = subtract_prism(piece, triangle, passage["top"])
                removed += gone
                next_pieces.extend(outside)
            pieces = next_pieces
            if not pieces:
                return [], removed
    return pieces, removed


def _triangles(shape) -> list:
    """The 2D triangles of a shapely polygon (its holes kept), by mapbox earcut."""
    triangles = []
    for polygon in (shape.geoms if hasattr(shape, "geoms") else [shape]):
        rings = [[(x, y, 0.0) for x, y in polygon.exterior.coords[:-1]]]
        rings += [[(x, y, 0.0) for x, y in ring.coords[:-1]] for ring in polygon.interiors]
        try:
            vertices, faces = triangulate_rings(rings)
        except (GlbError, ValueError):
            continue
        triangles.extend([tuple(vertices[i][:2]) for i in face] for face in faces)
    return triangles


# --- The MJCF ----------------------------------------------------------------------------------

def apply(mjcf_path: Path, source: Path, world_frame: Path, clearance_m: float, margin_m: float,
          functions=CARRIAGEWAY_FUNCTIONS) -> dict:
    frame = load_world_frame(world_frame)
    passages = carriageways(source, frame, functions, margin_m, clearance_m)
    tree = ET.parse(mjcf_path)
    root = tree.getroot()
    asset = root.find("asset")
    world = root.find("worldbody")
    meshes = {}
    if asset is not None:
        for mesh in asset.findall("mesh"):
            vertices = np.array(list(map(float, mesh.attrib["vertex"].split()))).reshape(-1, 3)
            faces = np.array(list(map(int, mesh.attrib["face"].split()))).reshape(-1, 3) if mesh.get("face") else None
            meshes[mesh.get("name")] = (vertices, faces)
    index = STRtree([p["shape"] for p in passages]) if passages else None
    report = {"clearance_m": clearance_m, "margin_m": margin_m, "functions": list(functions),
              "carriageway_polygon_count": len(passages), "geoms_cut": [], "geoms_removed": [],
              "pieces_written": 0, "volume_removed_m3": 0.0}
    if index is None or world is None:
        return report
    for geom in list(world.findall("geom")):
        hull = geom_polyhedron(geom, meshes)
        if hull is None or len(hull.vertices) < 4:
            continue
        (x0, y0, z0), (x1, y1, _) = hull.bounds
        candidates = [passages[i] for i in index.query(shapely_box(x0, y0, x1, y1))]
        candidates = [p for p in candidates if z0 < p["top"]]
        if not candidates:
            continue
        pieces, removed = cut_passages(hull, candidates)
        if removed <= MIN_VOLUME_M3:
            continue
        name = geom.get("name", "geom")
        attributes = {k: v for k, v in geom.attrib.items()
                      if k not in {"name", "type", "mesh", "pos", "size", "xyaxes", "euler", "quat"}}
        if asset is None:
            asset = ET.Element("asset")
            root.insert(list(root).index(world), asset)
        if geom.get("type") == "mesh" and geom.get("mesh") in meshes:
            old = asset.find(f"mesh[@name='{geom.get('mesh')}']")
            if old is not None:
                asset.remove(old)
        world.remove(geom)
        for number, piece in enumerate(pieces):
            piece_name = f"{name}_pass{number:02d}"
            ET.SubElement(asset, "mesh", {"name": piece_name, "vertex": format_numbers(piece.vertices.reshape(-1)),
                                          "face": " ".join(str(int(v)) for v in piece.triangles().reshape(-1))})
            ET.SubElement(world, "geom", {"name": piece_name, "type": "mesh", "mesh": piece_name, **attributes})
        report["pieces_written"] += len(pieces)
        report["volume_removed_m3"] += removed
        (report["geoms_cut"] if pieces else report["geoms_removed"]).append(
            {"geom": name, "pieces": len(pieces), "volume_removed_m3": round(removed, 3)})
    tree.write(mjcf_path, encoding="unicode", xml_declaration=False)
    with mjcf_path.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    report["volume_removed_m3"] = round(report["volume_removed_m3"], 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mjcf", type=Path, required=True, help="the building colliders MJCF, rewritten in place")
    parser.add_argument("--source", type=Path, required=True, help="the build's source folder (its *_tran_*_op.gml)")
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--clearance", type=float, default=4.5, help="metres clear above the carriageway (default 4.5)")
    parser.add_argument("--margin", type=float, default=0.2, help="metres the carriageway is shrunk by (default 0.2)")
    parser.add_argument("--functions", default=",".join(CARRIAGEWAY_FUNCTIONS),
                        help="TrafficArea function codes that are carriageways")
    args = parser.parse_args()
    if args.clearance <= 0:
        parser.error("--clearance must be positive")
    try:
        report = apply(args.mjcf, args.source, args.world_frame, args.clearance, args.margin,
                       tuple(code.strip() for code in args.functions.split(",") if code.strip()))
    except (PassageError, OSError, ET.ParseError) as exc:
        print(f"ERROR: {exc}")
        return 1
    args.receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: road passages: {len(report['geoms_cut'])} colliders cut into {report['pieces_written']} pieces, "
          f"{len(report['geoms_removed'])} removed, {report['volume_removed_m3']:.1f} m3 opened "
          f"over {report['carriageway_polygon_count']} carriageway polygons")
    print(f"OK: receipt: {args.receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

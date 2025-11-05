#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert LOD1-like JSON (vertices + zmin/zmax) to GLB/OBJ for Blender etc.

Input JSON schema:
{
  "version": "...",
  "crs": "EPSG:xxxx",
  "polygons": [
    {
      "id": "bldg_001",
      "vertices": [[x,y], ...],   # 2D footprint (meters, projected)
      "zmin": 28.45,
      "zmax": 36.02
    }, ...
  ]
}

Usage:
  pip install numpy shapely trimesh pygltflib
  python json2glb.py --in poly_from_gml.json --out buildings.glb --mode prism
  python json2glb.py --in poly_from_gml.json --out buildings_obb.glb --mode obb
  python json2glb.py --in poly_from_gml.json --out sample.obj --mode prism --format obj

Options:
  --mode prism|obb|both     : 押し出しプリズム / OBB箱 / 両方
  --format glb|gltf|obj     : 出力形式（既定 glb）
  --bbox xmin xmax ymin ymax: 表示範囲で切り出し
  --min-area A              : 面積A未満は捨てる（m^2）
  --simplify EPS            : 頂点をRDP簡略化（m）
  --sample N                : ランダムにN棟だけ出力
  --color r g b a           : 単色（0-1）。未指定時は自動ランダム
  --seed S                  : 色/サンプルの乱数種
"""

import argparse, json, random
from pathlib import Path
import numpy as np
from shapely.geometry import Polygon as ShpPoly, LineString as ShpLine
from shapely.ops import unary_union
import trimesh

# ---------- geometry helpers ----------
def poly_area_xy(pts):
    p = ShpPoly(pts)
    return float(p.area)

def simplify_poly(pts, eps):
    if eps <= 0: return np.asarray(pts, float)
    ls = ShpLine(pts).simplify(eps, preserve_topology=False)
    return np.asarray(ls.coords, float)

def rotation_matrix_z(yaw):
    c, s = np.cos(yaw), np.sin(yaw)
    M = np.eye(4)
    M[:3,:3] = np.array([[ c,-s,0],
                         [ s, c,0],
                         [ 0, 0,1]], float)
    return M

def min_area_rect_calipers(points):
    """2D: 最小外接矩形（回転キャリパ）。戻り (center(2,), half(2,), yaw, rect(4,2))"""
    pts = np.unique(np.asarray(points, float), axis=0)
    if len(pts) < 3:
        # 退避：線や点はAABB扱い
        minxy = pts.min(axis=0); maxxy = pts.max(axis=0)
        center = (minxy+maxxy)/2
        half = (maxxy-minxy)/2
        yaw = 0.0
        rect = np.array([[minxy[0],minxy[1]],[maxxy[0],minxy[1]],[maxxy[0],maxxy[1]],[minxy[0],maxxy[1]]], float)
        return center, half, yaw, rect

    # 凸包
    pts = pts[np.lexsort((pts[:,1], pts[:,0]))]
    def cross(o,a,b): return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
    lower=[]
    for p in pts:
        while len(lower)>=2 and cross(lower[-2],lower[-1],p)<=0: lower.pop()
        lower.append(tuple(p))
    upper=[]
    for p in pts[::-1]:
        while len(upper)>=2 and cross(upper[-2],upper[-1],p)<=0: upper.pop()
        upper.append(tuple(p))
    hull=np.array(lower[:-1]+upper[:-1],float)
    mu=hull.mean(axis=0)

    def rot(th):
        c,s=np.cos(th),np.sin(th)
        return np.array([[ c, s],[-s, c]],float)

    best={"area":np.inf}
    for i in range(len(hull)):
        e=hull[(i+1)%len(hull)]-hull[i]
        yaw=np.arctan2(e[1],e[0])
        R=rot(yaw)
        H=(hull-mu)@R.T
        xmin,ymin=H.min(axis=0); xmax,ymax=H.max(axis=0)
        area=(xmax-xmin)*(ymax-ymin)
        if area<best["area"]:
            best=dict(area=area,yaw=yaw,xmin=xmin,xmax=xmax,ymin=ymin,ymax=ymax,mu=mu)

    yaw=best["yaw"]; R=rot(yaw)
    xmin,xmax,ymin,ymax=best["xmin"],best["xmax"],best["ymin"],best["ymax"]
    rect_local=np.array([[xmin,ymin],[xmax,ymin],[xmax,ymax],[xmin,ymax]],float)
    rect=rect_local@R+best["mu"]
    cx,cy=(xmax+xmin)/2,(ymax+ymin)/2
    center=np.array([cx,cy])@R+best["mu"]
    sx,sy=(xmax-xmin)/2,(ymax-ymin)/2
    if sy>sx:
        sx,sy=sy,sx
        yaw=(yaw+np.pi/2+np.pi)%(2*np.pi)-np.pi
        R=rot(yaw)
        rect=rect_local[:,::-1]@R+best["mu"]
        center=np.array([cy,cx])@R+best["mu"]
    return center, np.array([sx,sy]), yaw, rect

# ---------- meshing ----------
def make_prism_mesh(verts2d, zmin, zmax):
    """Shapely→Trimesh: 2D多角形を高さhで押し出し、底面zminに配置"""
    poly = ShpPoly(verts2d)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area == 0:
        return None
    height = float(zmax - zmin)
    # trimeshのextrude_polygonはZ+方向へ押し出す（底面＝z=0）
    prism = trimesh.creation.extrude_polygon(poly, height=height)
    prism.apply_translation([0, 0, zmin])
    return prism

def make_obb_box(center, half, yaw, zmin, zmax):
    """OBB箱Mesh（中心=XY、回転=yaw、Zは[zmin,zmax]）"""
    extents = np.array([half[0]*2, half[1]*2, (zmax-zmin)], float)
    box = trimesh.creation.box(extents=extents)
    # 回転 + 平行移動
    T = np.eye(4)
    T[:3,3] = [center[0], center[1], (zmin+zmax)/2]
    box.apply_transform(rotation_matrix_z(yaw) @ T)
    return box

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", type=str, required=True)
    ap.add_argument("--out", type=str, required=True)
    ap.add_argument("--mode", choices=["prism","obb","both"], default="prism")
    ap.add_argument("--format", choices=["glb","gltf","obj"], default="glb")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN","XMAX","YMIN","YMAX"))
    ap.add_argument("--min-area", type=float, default=0.0)
    ap.add_argument("--simplify", type=float, default=0.0)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--color", type=float, nargs=4, metavar=("R","G","B","A"))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    random.seed(args.seed)
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    polys = data["polygons"]

    # サンプリング
    if args.sample and args.sample < len(polys):
        idx = list(range(len(polys)))
        random.shuffle(idx)
        polys = [polys[i] for i in idx[:args.sample]]

    meshes = []
    for p in polys:
        verts = np.asarray(p["vertices"], float)
        if args.bbox is not None:
            xmin,xmax,ymin,ymax = args.bbox
            if verts[:,0].max()<xmin or verts[:,0].min()>xmax or verts[:,1].max()<ymin or verts[:,1].min()>ymax:
                continue
        if args.simplify>0:
            verts = simplify_poly(verts, args.simplify)
        if args.min-area>0 and poly_area_xy(verts) < args.min_area:  # noqa
            continue

        zmin = float(p.get("zmin", 0.0))
        zmax = float(p.get("zmax", 10.0))

        color = np.array(args.color if args.color else
                         [random.random()*0.6+0.25, random.random()*0.6+0.25, random.random()*0.6+0.25, 1.0])

        # prism
        if args.mode in ("prism","both"):
            m = make_prism_mesh(verts, zmin, zmax)
            if m is not None:
                m.visual.face_colors = (color*255).astype(np.uint8)
                meshes.append(m)

        # obb
        if args.mode in ("obb","both"):
            c, half, yaw, _ = min_area_rect_calipers(verts)
            m = make_obb_box(c, half, yaw, zmin, zmax)
            m.visual.face_colors = (color*255).astype(np.uint8)
            meshes.append(m)

    if not meshes:
        print("[WARN] no meshes generated.")
        return

    scene = trimesh.Scene(meshes)
    out = Path(args.out)
    if args.format == "obj" or out.suffix.lower()==".obj":
        scene.export(out.with_suffix(".obj"))
        print(f"[OK] saved OBJ → {out.with_suffix('.obj')}")
    elif args.format in ("glb","gltf") or out.suffix.lower() in (".glb",".gltf"):
        # trimeshは拡張子で判断するので合わせる
        scene.export(out.with_suffix(f".{args.format}"))
        print(f"[OK] saved {args.format.upper()} → {out.with_suffix(f'.{args.format}')}")
    else:
        scene.export(out)
        print(f"[OK] saved → {out}")

if __name__ == "__main__":
    main()

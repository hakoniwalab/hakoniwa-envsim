# plot2d.py  —  2D可視化 + 地図オーバーレイ（緯度経度出力対応版）
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import List, Tuple

import numpy as np
import matplotlib.pyplot as plt
from matplotlib import patches
from matplotlib.colors import Normalize

# 地図オーバーレイ用（任意）
try:
    import contextily as ctx
    from pyproj import Transformer
    _HAS_MAP = True
except Exception:
    _HAS_MAP = False

from hakoniwa_envsim.model.models import VisualArea
from hakoniwa_envsim.model.loader import ModelLoader


def _resolve_provider(tiles: str):
    """
    'OpenStreetMap.Mapnik' のようなドット区切り表記を
    contextily.providers から段階的に解決して TileProvider を返す。
    直接URLが来た場合（http/https/{z}/{x}/{y} など）はそのまま返す。
    """
    if tiles.startswith(("http://", "https://")):
        return tiles  # そのままURLとして使う

    # ctx.providers.OpenStreetMap.Mapnik のように段階的に辿る
    prov = ctx.providers
    for part in tiles.split("."):
        if not part:
            continue
        prov = getattr(prov, part)
    return prov

def _auto_zoom_for_cell(cell_size_m: float,
                        px_per_cell: int,
                        provider) -> int:
    """セルの物理サイズと希望ピクセル数から最適zoomを推定して、providerの範囲内に丸める"""
    initial_res = 156543.03392804097  # m/px at z=0 (EPSG:3857, tile=256px)
    target_m_per_px = max(1e-9, cell_size_m / max(1, px_per_cell))
    zoom = int(round(np.log2(initial_res / target_m_per_px)))

    # providerの許容レンジに丸める（xyzservicesのTileProviderは属性で持っている）
    zmin = getattr(provider, "min_zoom", 0)
    zmax = getattr(provider, "max_zoom", 22)
    return int(np.clip(zoom, zmin, zmax))


def _cap_zoom_by_image_size(Xmin: float, Ymin: float, Xmax: float, Ymax: float,
                            zoom: int, max_px: int = 8192) -> int:
    """bboxを与えたとき、貼り合わせ画像の横幅が大きすぎるならzoomを落として抑制"""
    initial_res = 156543.03392804097
    m_per_px = initial_res / (2 ** zoom)
    img_w_px = (Xmax - Xmin) / m_per_px  # 貼り付け画像の目安横幅
    while img_w_px > max_px and zoom > 0:
        zoom -= 1
        m_per_px *= 2.0
        img_w_px /= 2.0
    return zoom


def _get_transformers() -> Tuple[Transformer, Transformer]:
    """(to_merc, to_geo) を返す。"""
    to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    to_geo = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return to_merc, to_geo


def _enu_to_lonlat(origin_lat: float, origin_lon: float,
                   ex: float, ny: float,
                   offset_x: float, offset_y: float) -> Tuple[float, float]:
    """
    ENU(ROS)座標 (x=east, y=north) → 緯度経度
    plot2d のオーバーレイ実装と同じく WebMercator 上で平行移動する。

    * MercatorX = X0 + (y + offset_y)
    * MercatorY = Y0 + (x + offset_x)
    その後 EPSG:4326 へ戻す。
    戻り値: (lat, lon)
    """
    to_merc, to_geo = _get_transformers()
    X0, Y0 = to_merc.transform(origin_lon, origin_lat)
    X = X0 + (ny + offset_y)
    Y = Y0 + (ex + offset_x)
    lon, lat = to_geo.transform(X, Y)
    return lat, lon


def _overlay_osm_image(
    ax: plt.Axes,
    areas: List[VisualArea],
    origin_lat: float,
    origin_lon: float,
    tiles: str = "OpenStreetMap.Mapnik",
    zoom: int | None = None,
) -> None:
    """
    ENU の描画範囲を WebMercator(EPSG:3857) に平行移動で写して、該当範囲のタイル画像を取得し
    ENU 軸（横=Y, 縦=X）のまま背景に貼る。図の矩形・矢印の座標は変更しない。
    """
    if not _HAS_MAP:
        raise RuntimeError("contextily/pyproj が見つかりません（pip install contextily pyproj xyzservices）")

    # ENU(=ROSローカル)の描画範囲
    xmin = min(a.aabb2d.xmin for a in areas); xmax = max(a.aabb2d.xmax for a in areas)
    ymin = min(a.aabb2d.ymin for a in areas); ymax = max(a.aabb2d.ymax for a in areas)

    # 原点(lat,lon)を3857に投影 → ENUの x=east, y=north をそのまま平行移動
    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    X0, Y0 = t.transform(origin_lon, origin_lat)
    Xmin, Ymin = X0 + ymin + args.offset_y, Y0 + xmin + args.offset_x
    Xmax, Ymax = X0 + ymax + args.offset_y, Y0 + xmax + args.offset_x

    # タイル画像を取得（3857のバウンディングボックス）
    provider = _resolve_provider(tiles)

    # zoom 自動算出：セル1マスを何pxにしたいか（例: 32px）
    if zoom is None:
        # 最小セルの一辺（X/Y小さい方）を採用
        cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
        cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
        cell_size_m = max(1e-6, min(cell_dx, cell_dy))

        desired_px_per_cell = 32  # ←好みで。20〜48あたりが実用的
        zoom = _auto_zoom_for_cell(cell_size_m, desired_px_per_cell, provider)

        # 画像が巨大化しないよう、bbox幅に基づいて上限チェック
        zoom = _cap_zoom_by_image_size(Xmin, Ymin, Xmax, Ymax, zoom, max_px=8192)

    # 取得
    img, extent_wm = ctx.bounds2img(
        Xmin, Ymin, Xmax, Ymax, source=provider, zoom=zoom, ll=False
    )

    # ENU軸(横=Y, 縦=X) の座標枠に合わせて貼り付け
    ax.imshow(
        img,
        extent=(ymax, ymin, xmin, xmax),  # (left, right, bottom, top) = (Ymin, Ymax, Xmin, Xmax)
        zorder=0,
        interpolation="bilinear",
    )


def plot_areas(
    areas: List[VisualArea],
    show_wind: bool = True,
    mode: str = "temperature",
    wind_scale: float = 1.0,
    overlay_map: bool = False,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    tiles: str = "OpenStreetMap.Mapnik",
    zoom: int | None = None,
) -> None:
    """
    mode: "temperature" or "gps"
    overlay_map=True のとき origin_lat/lon を指定（地図へ重ねる）。
    """
    fig, ax = plt.subplots(figsize=(10, 8), dpi=120)

    # ---- 値の選択＆色設定 ----
    if mode == "temperature":
        values = [a.temperature for a in areas if a.temperature is not None]
        get_value = lambda a: a.temperature if a.temperature is not None else 0.0
        cmap = plt.cm.coolwarm
        cbar_label = "Temperature [°C]"
        as_transparent_overlay = False
    elif mode == "gps":
        values = [a.gps_strength for a in areas if getattr(a, "gps_strength", None) is not None]
        get_value = lambda a: getattr(a, "gps_strength", 0.0)  # 強さ (0..1)
        cmap = plt.cm.Reds  # カラーバー用（弱さ表示）
        cbar_label = "GPS Weakness (red=low strength)"
        as_transparent_overlay = True
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    # ---- 正規化 ----
    if values:
        vmin, vmax = min(values), max(values)
        if abs(vmax - vmin) < 1e-6:
            vmin, vmax = vmin - 0.1, vmax + 0.1
    else:
        vmin, vmax = 0.0, 1.0
    norm = Normalize(vmin=vmin, vmax=vmax)

    # ---- 背景に地図を敷く（任意）----
    if overlay_map:
        if origin_lat is None or origin_lon is None:
            raise ValueError("overlay_map=True の場合は --origin-lat / --origin-lon が必須です。")
        _overlay_osm_image(ax, areas, origin_lat, origin_lon, tiles=tiles, zoom=zoom)

    # ---- 風矢印スケール自動調整 ----
    cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
    cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
    cell_size = max(1e-6, min(cell_dx, cell_dy))
    wind_mags = []
    for a in areas:
        if a.wind_velocity:
            wx, wy, wz = a.wind_velocity
            wind_mags.append((wx**2 + wy**2 + wz**2) ** 0.5)
    max_mag = max(wind_mags) if wind_mags else 1.0
    s = (0.5 * cell_size / max(1e-6, max_mag)) * wind_scale

    # ---- 描画（従来どおり：横=Y, 縦=X）----
    for area in areas:
        aabb = area.aabb2d
        val = get_value(area)

        if mode == "gps" and as_transparent_overlay:
            val = get_value(area)  # 0..1
            weakness = 1.0 - val   # 強=0, 弱=1
            alpha = min(0.5, weakness)  # 最大でも50%まで
            face_rgba = (1.0, 0.0, 0.0, alpha)
        else:
            face_rgba = cmap(norm(val))
            edgecolor = "black"

        rect = patches.Rectangle(
            (aabb.ymin, aabb.xmin),   # (left=Ymin, bottom=Xmin)
            aabb.ymax - aabb.ymin,    # width=ΔY
            aabb.xmax - aabb.xmin,    # height=ΔX
            linewidth=0,
            edgecolor=None,
            facecolor=face_rgba,
            zorder=2,
        )
        ax.add_patch(rect)

        # 風ベクトル
        if show_wind and area.wind_velocity:
            wx, wy, wz = area.wind_velocity
            mag = (wx**2 + wy**2 + wz**2) ** 0.5
            if mag > 1e-6:
                cx, cy = aabb.center()
                ax.arrow(
                    cy, cx, wy * s, wx * s,     # 横成分=wy, 縦成分=wx
                    head_width=0.5 * s,
                    head_length=0.25 * s,
                    fc="blue", ec="blue",
                    length_includes_head=True,
                    zorder=4,
                )

    # ---- 軸など（従来どおり）----
    xmin = min(a.aabb2d.xmin for a in areas); xmax = max(a.aabb2d.xmax for a in areas)
    ymin = min(a.aabb2d.ymin for a in areas); ymax = max(a.aabb2d.ymax for a in areas)
    ax.set_xlim(ymin - 1.0, ymax + 1.0)
    ax.set_ylim(xmin - 1.0, xmax + 1.0)
    ax.invert_xaxis()  # ROS: 左が +Y になるように左右反転
    ax.set_aspect("equal", adjustable="box")
    ax.grid(False)
    ax.set_xlabel("Y [m] (ROS left is +)")
    ax.set_ylabel("X [m] (ROS forward is +)")
    ax.set_title(f"Hakoniwa Environment Map ({mode})")

    # ---- カラーバー（GPSは“弱さ”0..1 を表示）----
    if mode == "gps" and as_transparent_overlay:
        from matplotlib.cm import ScalarMappable
        weak_norm = Normalize(vmin=0.0, vmax=1.0)
        sm = ScalarMappable(cmap=plt.cm.Reds, norm=weak_norm)
        sm.set_array(np.linspace(0, 1, 256))
        cbar = plt.colorbar(sm, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label("GPS Weakness (red=low strength)")
    else:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label(cbar_label)

    plt.tight_layout()
    plt.show()


def print_area_latlons(
    areas: List[VisualArea],
    origin_lat: float,
    origin_lon: float,
    offset_x: float,
    offset_y: float,
    which: str = "center",
) -> None:
    """
    標準出力に各エリアの緯度経度を出力。
    * which="center" で中心点のみ（デフォルト）
    将来 corners/both を増やす拡張前提で設計。
    出力形式: CSV  
      area_id, center_x_m, center_y_m, lat, lon
    """
    if not _HAS_MAP:
        raise RuntimeError("緯度経度出力には pyproj が必要です（pip install pyproj）")

    print("# area_id,center_x_m,center_y_m,lat,lon")
    for a in areas:
        cx, cy = a.aabb2d.center()  # (Xcenter, Ycenter) [m]
        lat, lon = _enu_to_lonlat(origin_lat, origin_lon, cx, cy, offset_x, offset_y)
        # area_id は a.id が無い場合に備えてフォールバック
        area_id = getattr(a, 'id', None)
        if area_id is None:
            # a.link などに識別子があるなら好みで拡張
            area_id = f"x{cx:.3f}_y{cy:.3f}"
        print(f"{area_id},{cx:.3f},{cy:.3f},{lat:.8f},{lon:.8f}")


def compute_shifted_origin(origin_lat: float, origin_lon: float, offset_x: float, offset_y: float) -> tuple[float, float]:
    """
    offset を 0 にしても同じ見た目になるような「シフト済み原点(緯度経度)」を返す。
    実装は WebMercator 上で原点を (offset_y, offset_x) だけ平行移動し、WGS84 に戻す。
      X0' = X0 + offset_y
      Y0' = Y0 + offset_x
    戻り値: (lat', lon')
    """
    if not _HAS_MAP:
        raise RuntimeError("shifted origin の計算には pyproj が必要です（pip install pyproj）")
    to_merc, to_geo = _get_transformers()
    X0, Y0 = to_merc.transform(origin_lon, origin_lat)
    Xs, Ys = X0 + offset_y, Y0 + offset_x
    lon_s, lat_s = to_geo.transform(Xs, Ys)
    return lat_s, lon_s


def _apply_config_defaults(parser: argparse.ArgumentParser, config_path: str | None) -> dict:
    """
    --config で指定された JSON を読み込み、parser に set_defaults で反映する。
    返り値はロードした辞書（無ければ空辞書）。
    キーは引数名（--origin-lat → origin_lat）で指定。
    例:
    {
      "area": ".../area.json",
      "property": ".../property.json",
      "link": ".../link.json",
      "overlay_map": true,
      "origin_lat": 34.6913,
      "origin_lon": 135.1880,
      "offset_x": -5000,
      "offset_y": -2000,
      "mode": "gps",
      "wind_scale": 1.0,
      "tiles": "OpenStreetMap.Mapnik",
      "zoom": null,
      "print_latlon": true,
      "print_shifted_origin": true
    }
    """
    cfg = {}
    if not config_path:
        return cfg
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config json must be an object")
    # argparse のデフォルトに上書き（CLI はこの後に再パースされ優先される）
    parser.set_defaults(**cfg)
    return cfg


if __name__ == "__main__":
    # 1st pass: --config だけ先に読む
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument("--config", help="引数をまとめたJSONファイル")

    parser = argparse.ArgumentParser(description="Visualize Hakoniwa Environment Areas in 2D / Map overlay",
                                     parents=[base_parser])
    parser.add_argument("--area", required=False)
    parser.add_argument("--property", required=False)
    parser.add_argument("--link", required=False)
    parser.add_argument("--no-wind", action="store_true")
    parser.add_argument("--mode", choices=["temperature", "gps"], default="gps")
    parser.add_argument("--wind-scale", type=float, default=1.0)

    # 地図オーバーレイ
    parser.add_argument("--overlay-map", action="store_true", help="背景に地図タイルをオーバーレイ")
    parser.add_argument("--origin-lat", type=float, help="ローカル原点の緯度")
    parser.add_argument("--origin-lon", type=float, help="ローカル原点の経度")
    parser.add_argument("--tiles", default="OpenStreetMap.Mapnik",
                        help="ctx.providers.* のキー（例: OpenStreetMap.Mapnik / Stamen.TonerLite / Esri.WorldImagery）")
    parser.add_argument("--zoom", type=int, default=None, help="地図タイルのズーム（未指定なら自動）")
    parser.add_argument("--offset-x", type=float, default=0.0, help="原点X方向[m]の補正")
    parser.add_argument("--offset-y", type=float, default=0.0, help="原点Y方向[m]の補正")

    # 追加: 緯度経度の標準出力
    parser.add_argument("--print-latlon", action="store_true", help="各エリア中心の緯度経度をCSVで標準出力")
    parser.add_argument("--print-which", choices=["center"], default="center",
                        help="今はcenterのみ。将来拡張用の指定")

    # 追加: offset を吸収した原点（緯度経度）を出力
    parser.add_argument("--print-shifted-origin", action="store_true",
                        help="offset を 0 にしても同じ結果になるシフト済み原点の緯度経度を標準出力")

    # 1st parse to get config path
    prelim, _ = parser.parse_known_args()
    _ = _apply_config_defaults(parser, getattr(prelim, "config", None))

    # 2nd parse with defaults overridden by CLI
    args = parser.parse_args()

    # --- 最低限の必須チェック（config 経由でも可） ---
    missing = [k for k in ("area", "property", "link") if getattr(args, k) in (None, "")]
    if missing:
        raise ValueError(f"missing required args (via CLI or config): {', '.join(missing)}")

    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(args.area)
    props = loader.load_area_properties(args.property)
    links = loader.load_links(args.link)
    scene = loader.build_visual_areas(areas, props, links)

    # 先に緯度経度出力（必要なら）
    if args.print_latlon:
        if args.origin_lat is None or args.origin_lon is None:
            raise ValueError("--print-latlon には --origin-lat / --origin-lon が必要です (config でも可)")
        print_area_latlons(
            scene,
            origin_lat=args.origin_lat,
            origin_lon=args.origin_lon,
            offset_x=args.offset_x,
            offset_y=args.offset_y,
            which=args.print_which,
        )

    # シフト済み原点の出力
    if args.print_shifted_origin:
        if args.origin_lat is None or args.origin_lon is None:
            raise ValueError("--print-shifted-origin には --origin-lat / --origin-lon が必要です (config でも可)")
        lat_s, lon_s = compute_shifted_origin(args.origin_lat, args.origin_lon, args.offset_x, args.offset_y)
        print("# shifted_origin_lat,shifted_origin_lon")
        print(f"{lat_s:.8f},{lon_s:.8f}")
        print("# 再現コマンド例: --origin-lat {0:.8f} --origin-lon {1:.8f} --offset-x 0 --offset-y 0".format(lat_s, lon_s))

    plot_areas(
        scene,
        show_wind=not args.no_wind,
        mode=args.mode,
        wind_scale=args.wind_scale,
        overlay_map=args.overlay_map,
        origin_lat=args.origin_lat,
        origin_lon=args.origin_lon,
        tiles=args.tiles,
        zoom=args.zoom,
    )
    parser = argparse.ArgumentParser(description="Visualize Hakoniwa Environment Areas in 2D / Map overlay")
    parser.add_argument("--area", required=True)
    parser.add_argument("--property", required=True)
    parser.add_argument("--link", required=True)
    parser.add_argument("--no-wind", action="store_true")
    parser.add_argument("--mode", choices=["temperature", "gps"], default="gps")
    parser.add_argument("--wind-scale", type=float, default=1.0)

    # 地図オーバーレイ
    parser.add_argument("--overlay-map", action="store_true", help="背景に地図タイルをオーバーレイ")
    parser.add_argument("--origin-lat", type=float, help="ローカル原点の緯度")
    parser.add_argument("--origin-lon", type=float, help="ローカル原点の経度")
    parser.add_argument("--tiles", default="OpenStreetMap.Mapnik",
                        help="ctx.providers.* のキー（例: OpenStreetMap.Mapnik / Stamen.TonerLite / Esri.WorldImagery）")
    parser.add_argument("--zoom", type=int, default=None, help="地図タイルのズーム（未指定なら自動）")
    parser.add_argument("--offset-x", type=float, default=0.0, help="原点X方向[m]の補正")
    parser.add_argument("--offset-y", type=float, default=0.0, help="原点Y方向[m]の補正")

    # 追加: 緯度経度の標準出力
    parser.add_argument("--print-latlon", action="store_true", help="各エリア中心の緯度経度をCSVで標準出力")
    parser.add_argument("--print-which", choices=["center"], default="center",
                        help="今はcenterのみ。将来拡張用の指定")

    # 追加: offset を吸収した原点（緯度経度）を出力
    parser.add_argument("--print-shifted-origin", action="store_true",
                        help="offset を 0 にしても同じ結果になるシフト済み原点の緯度経度を標準出力")

    args = parser.parse_args()

    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(args.area)
    props = loader.load_area_properties(args.property)
    links = loader.load_links(args.link)
    scene = loader.build_visual_areas(areas, props, links)

    # 先に緯度経度出力（必要なら）
    if args.print_latlon:
        if args.origin_lat is None or args.origin_lon is None:
            raise ValueError("--print-latlon には --origin-lat / --origin-lon が必要です")
        print_area_latlons(
            scene,
            origin_lat=args.origin_lat,
            origin_lon=args.origin_lon,
            offset_x=args.offset_x,
            offset_y=args.offset_y,
            which=args.print_which,
        )

    # シフト済み原点の出力
    if args.print_shifted_origin:
        if args.origin_lat is None or args.origin_lon is None:
            raise ValueError("--print-shifted-origin には --origin-lat / --origin-lon が必要です")
        lat_s, lon_s = compute_shifted_origin(args.origin_lat, args.origin_lon, args.offset_x, args.offset_y)
        # 検証に便利なコマンド例も一緒にコメントで出す
        print("# shifted_origin_lat,shifted_origin_lon")
        print(f"{lat_s:.8f},{lon_s:.8f}")
        print("# 再現コマンド例: --origin-lat {0:.8f} --origin-lon {1:.8f} --offset-x 0 --offset-y 0".format(lat_s, lon_s))

    plot_areas(
        scene,
        show_wind=not args.no_wind,
        mode=args.mode,
        wind_scale=args.wind_scale,
        overlay_map=args.overlay_map,
        origin_lat=args.origin_lat,
        origin_lon=args.origin_lon,
        tiles=args.tiles,
        zoom=args.zoom,
    )

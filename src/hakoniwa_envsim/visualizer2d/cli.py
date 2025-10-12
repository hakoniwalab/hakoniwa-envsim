# cli.py
import argparse, json
from .config import VizConfig, load_json
from .markers import Marker
from .projection import GeoProjector
from .overlay import TileOverlay
from .renderer import PlotRenderer
from .map_converter import to_map_frame_scene
from hakoniwa_envsim.model.loader import ModelLoader

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config")
    return p.parse_args()

def main():
    args = parse_args()
    cfg_dict = load_json(args.config)
    # JSONをデフォルトに、CLIで上書き
    for k, v in vars(args).items():
        if k == "config": continue
        if v is not None: cfg_dict[k] = v
    cfg = VizConfig(**cfg_dict)

    # モデルロード
    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(cfg.area)
    props = loader.load_area_properties(cfg.property)
    links = loader.load_links(cfg.link)
    scene = loader.build_visual_areas(areas, props, links)

    # 変換器
    projector = GeoProjector(
        origin_lat=cfg.origin_lat, origin_lon=cfg.origin_lon,
        offset_x=cfg.offset_x, offset_y=cfg.offset_y
    )

    # 標準出力オプション
    if cfg.print_shifted_origin:
        lat_s, lon_s = projector.shifted_origin()
        print("# shifted_origin_lat,shifted_origin_lon")
        print(f"{lat_s:.8f},{lon_s:.8f}")

    if cfg.print_latlon:
        print("# area_id,center_x_m,center_y_m,lat,lon")
        for a in scene:
            cx, cy = a.aabb2d.center()
            lat, lon = projector.enu_to_lonlat(cx, cy)
            area_id = getattr(a, "id", f"x{cx:.3f}_y{cy:.3f}")
            print(f"{area_id},{cx:.3f},{cy:.3f},{lat:.8f},{lon:.8f}")

    overlay = TileOverlay(cfg.tiles, cfg.zoom) if cfg.overlay_map else None

    # マーカー
    markers = []
    for mj in cfg.markers or []:
        markers.append(Marker(lat=float(mj["lat"]), lon=float(mj["lon"]), label=mj.get("label")))

    # === ROS → MAP に変換 ===
    map_scene = to_map_frame_scene(scene)  # 👈 ROS→MAP変換

    # 描画
    PlotRenderer(projector, overlay).draw(map_scene, cfg.mode, cfg.wind_scale, markers)

if __name__ == "__main__":
    main()

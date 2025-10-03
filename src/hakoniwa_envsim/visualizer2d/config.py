# config.py
from dataclasses import dataclass, field
from pathlib import Path
import json

@dataclass
class VizConfig:
    area: str
    property: str
    link: str
    overlay_map: bool = False
    origin_lat: float | None = None
    origin_lon: float | None = None
    offset_x: float = 0.0
    offset_y: float = 0.0
    mode: str = "gps"
    wind_scale: float = 1.0
    tiles: str = "OpenStreetMap.Mapnik"
    zoom: int | None = None
    print_latlon: bool = False
    print_shifted_origin: bool = False
    markers: list[dict] = field(default_factory=list)

def load_json(path: str | None) -> dict:
    if not path: return {}
    p = Path(path); 
    if not p.exists(): raise FileNotFoundError(path)
    with p.open("r", encoding="utf-8") as f: return json.load(f)

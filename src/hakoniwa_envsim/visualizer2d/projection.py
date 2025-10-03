# projection.py
from dataclasses import dataclass
from typing import Tuple, Protocol

class Projection(Protocol):
    def lonlat_to_xy(self, lon: float, lat: float) -> Tuple[float, float]: ...
    def xy_to_lonlat(self, x: float, y: float) -> Tuple[float, float]: ...

@dataclass(frozen=True)
class WebMercatorProjection:
    """EPSG:4326 <-> EPSG:3857"""
    def __post_init__(self):
        from pyproj import Transformer
        object.__setattr__(self, "_to_merc",
            Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True))
        object.__setattr__(self, "_to_geo",
            Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True))
    def lonlat_to_xy(self, lon, lat):
        return self._to_merc.transform(lon, lat)
    def xy_to_lonlat(self, x, y):
        return self._to_geo.transform(x, y)


@dataclass(frozen=True)
class LocalENUProjection:
    origin_lat: float
    origin_lon: float
    def __post_init__(self):
        from pyproj import Transformer
        proj_str = f"+proj=aeqd +lat_0={self.origin_lat} +lon_0={self.origin_lon} +datum=WGS84 +units=m +no_defs"
        object.__setattr__(self, "_to_local",
            Transformer.from_crs("EPSG:4326", proj_str, always_xy=True))
        object.__setattr__(self, "_to_geo",
            Transformer.from_crs(proj_str, "EPSG:4326", always_xy=True))
    def lonlat_to_xy(self, lon, lat):
        return self._to_local.transform(lon, lat)
    def xy_to_lonlat(self, x, y):
        return self._to_geo.transform(x, y)
    
@dataclass(frozen=True)
class GeoProjector:
    origin_lat: float
    origin_lon: float
    offset_x: float = 0.0
    offset_y: float = 0.0
    use_mercator: bool = True   # 切り替え用フラグ

    def __post_init__(self):
        if self.use_mercator:
            object.__setattr__(self, "proj", WebMercatorProjection())
        else:
            object.__setattr__(self, "proj", LocalENUProjection(self.origin_lat, self.origin_lon))

    # ENU(x=east, y=north) -> lon/lat
    def enu_to_lonlat(self, x: float, y: float) -> Tuple[float, float]:
        X0, Y0 = self.proj.lonlat_to_xy(self.origin_lon, self.origin_lat)
        X = X0 + (y + self.offset_y)   # 画像側の式に厳密一致
        Y = Y0 + (x + self.offset_x)
        lon, lat = self.proj.xy_to_lonlat(X, Y)
        return lat, lon

    # lon/lat -> ENU(x=east, y=north)
    def lonlat_to_enu(self, lat: float, lon: float) -> Tuple[float, float]:
        X0, Y0 = self.proj.lonlat_to_xy(self.origin_lon, self.origin_lat)
        Xp, Yp = self.proj.lonlat_to_xy(lon, lat)
        y = (Xp - X0) - self.offset_y
        x = (Yp - Y0) - self.offset_x
        return x, y

    # offsetを吸収した“新しい原点”（オフセット0で同じ見た目）
    def shifted_origin(self) -> Tuple[float, float]:
        X0, Y0 = self.proj.lonlat_to_xy(self.origin_lon, self.origin_lat)
        Xs, Ys = X0 + self.offset_y, Y0 + self.offset_x
        lon_s, lat_s = self.proj.xy_to_lonlat(Xs, Ys)
        return lat_s, lon_s

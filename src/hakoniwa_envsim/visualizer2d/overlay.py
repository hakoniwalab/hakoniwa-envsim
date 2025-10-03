# overlay.py
from dataclasses import dataclass
import numpy as np
import contextily as ctx

INITIAL_RES = 156543.03392804097  # m/px at z=0 (3857, 256px)

@dataclass(frozen=True)
class TileOverlay:
    tiles: str = "OpenStreetMap.Mapnik"
    zoom: int | None = None
    max_px: int = 8192

    def _resolve(self):
        if self.tiles.startswith(("http://", "https://")):
            return self.tiles
        prov = ctx.providers
        for p in self.tiles.split("."):
            if p: prov = getattr(prov, p)
        return prov

    def auto_zoom(self, cell_size_m: float, px_per_cell: int, provider) -> int:
        target_m_per_px = max(1e-9, cell_size_m / max(1, px_per_cell))
        zoom = int(round(np.log2(INITIAL_RES / target_m_per_px)))
        zmin = getattr(provider, "min_zoom", 0)
        zmax = getattr(provider, "max_zoom", 22)
        return int(np.clip(zoom, zmin, zmax))

    def cap_zoom(self, xmin, ymin, xmax, ymax, zoom) -> int:
        m_per_px = INITIAL_RES / (2 ** zoom)
        w_px = (xmax - xmin) / m_per_px
        while w_px > self.max_px and zoom > 0:
            zoom -= 1; m_per_px *= 2; w_px /= 2
        return zoom

    def fetch(self, Xmin, Ymin, Xmax, Ymax, cell_size_m: float | None):
        provider = self._resolve()
        z = self.zoom
        if z is None and cell_size_m is not None:
            z = self.auto_zoom(cell_size_m, 32, provider)
            z = self.cap_zoom(Xmin, Ymin, Xmax, Ymax, z)
        img, extent_wm = ctx.bounds2img(Xmin, Ymin, Xmax, Ymax, source=provider, zoom=z, ll=False)
        return img, extent_wm, z

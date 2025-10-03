# markers.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Marker:
    lat: float
    lon: float
    label: str | None = None

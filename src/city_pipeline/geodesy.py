"""Shared PLATEAU EPSG:6697 geodetic conversion helpers."""

from __future__ import annotations

import math


def project_epsg6697_to_local_enu(points, center_lat, center_lon):
    """Convert JGD2011 latitude/longitude/height to local ENU meters."""
    semi_major = 6378137.0
    inv_flattening = 298.257222101  # GRS80, used by JGD2011
    flattening = 1.0 / inv_flattening
    eccentricity_sq = flattening * (2.0 - flattening)

    def ecef(lat_deg, lon_deg):
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        radius = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat * sin_lat)
        return (
            radius * cos_lat * math.cos(lon),
            radius * cos_lat * math.sin(lon),
            radius * (1.0 - eccentricity_sq) * sin_lat,
        )

    origin = ecef(center_lat, center_lon)
    lat0, lon0 = math.radians(center_lat), math.radians(center_lon)
    sin_lat0, cos_lat0 = math.sin(lat0), math.cos(lat0)
    sin_lon0, cos_lon0 = math.sin(lon0), math.cos(lon0)
    output = []
    for lat, lon, z in points:
        point = ecef(lat, lon)
        dx, dy, dz = (point[index] - origin[index] for index in range(3))
        east = -sin_lon0 * dx + cos_lon0 * dy
        north = -sin_lat0 * cos_lon0 * dx - sin_lat0 * sin_lon0 * dy + cos_lat0 * dz
        output.append((east, north, z))
    return output

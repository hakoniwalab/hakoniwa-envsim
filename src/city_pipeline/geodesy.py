"""Geodetic conversion helpers for the CityGML the pipeline accepts.

Envsim reads three-dimensional geographic CityGML (``latitude longitude
height``) in one of these coordinate reference systems:

* EPSG:6697 -- JGD2011 + JGD2011 (vertical) height, the PLATEAU standard (GRS80).
* EPSG:4326 -- WGS 84 latitude/longitude with ellipsoidal or unspecified height,
  used for CityGML converted from worldwide data such as OpenStreetMap.

Both are projected to the same query-centered local ENU tangent plane on their
own ellipsoid. The datums are treated as coincident: JGD2011 and WGS 84 differ
by at most decimetres in Japan (crustal motion since the 2011 epoch), well below
the positional accuracy of data that arrives in EPSG:4326.
"""

from __future__ import annotations

import math
import re

SEMI_MAJOR_M = 6378137.0
# EPSG code -> (label, inverse flattening of its ellipsoid).
SUPPORTED_CRS = {
    6697: ("JGD2011 (GRS80)", 298.257222101),
    4326: ("WGS 84", 298.257223563),
}
_SRS_CODE = re.compile(r"(?:/|:)(\d+)$")


def epsg_label(epsg: int) -> str:
    return f"EPSG:{epsg}"


def srs_code(srs_name: str) -> int | None:
    """The trailing EPSG code of a gml srsName (URN, URL or ``EPSG:n``)."""
    match = _SRS_CODE.search((srs_name or "").strip().rstrip("/"))
    return int(match.group(1)) if match else None


def declared_crs(root, source, ns: dict | None = None) -> int:
    """The EPSG code every ``gml:Envelope`` of a CityGML document declares.

    Every envelope must name one supported CRS (the same one) with
    srsDimension=3; the coordinates are read as ``latitude longitude height``.
    """
    namespaces = ns or {"gml": "http://www.opengis.net/gml"}
    envelopes = root.findall(".//gml:Envelope", namespaces)
    if not envelopes:
        raise ValueError(f"CityGML gml:Envelope is missing: {source}")
    codes = set()
    for envelope in envelopes:
        srs_name = envelope.get("srsName", "")
        code = srs_code(srs_name)
        if code not in SUPPORTED_CRS:
            supported = ", ".join(epsg_label(item) for item in SUPPORTED_CRS)
            raise ValueError(f"CityGML must declare one of {supported}; found srsName={srs_name!r}: {source}")
        if envelope.get("srsDimension") != "3":
            raise ValueError(
                f"{epsg_label(code)} CityGML must declare srsDimension=3; "
                f"found {envelope.get('srsDimension')!r}: {source}"
            )
        codes.add(code)
    if len(codes) != 1:
        raise ValueError(f"CityGML mixes coordinate reference systems {sorted(codes)}: {source}")
    return codes.pop()


def project_to_local_enu(points, center_lat, center_lon, epsg: int = 6697):
    """Convert ``(latitude, longitude, height)`` to local ENU metres.

    The tangent plane touches the ellipsoid of ``epsg`` at the query centre;
    the height passes through unchanged (Envsim keeps altitudes and offsets
    them by the world frame's altitude_offset_m).
    """
    if epsg not in SUPPORTED_CRS:
        raise ValueError(f"unsupported CRS {epsg_label(epsg)}")
    flattening = 1.0 / SUPPORTED_CRS[epsg][1]
    eccentricity_sq = flattening * (2.0 - flattening)

    def ecef(lat_deg, lon_deg):
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        radius = SEMI_MAJOR_M / math.sqrt(1.0 - eccentricity_sq * sin_lat * sin_lat)
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


def project_epsg6697_to_local_enu(points, center_lat, center_lon):
    """Convert JGD2011 latitude/longitude/height to local ENU meters."""
    return project_to_local_enu(points, center_lat, center_lon, 6697)


def file_crs(path, default: int | None = None) -> int:
    """The EPSG code of a CityGML file, read from its first ``gml:Envelope``
    without parsing the rest of the document. ``default`` is used when the
    file declares no envelope (older inputs assumed PLATEAU's EPSG:6697)."""
    import xml.etree.ElementTree as ET

    for _event, element in ET.iterparse(path, events=("start",)):
        if element.tag == "{http://www.opengis.net/gml}Envelope":
            code = srs_code(element.get("srsName", ""))
            if code not in SUPPORTED_CRS or element.get("srsDimension") != "3":
                supported = " or ".join(epsg_label(item) for item in SUPPORTED_CRS)
                raise ValueError(
                    f"CityGML must declare a three-dimensional {supported}; "
                    f"found srsName={element.get('srsName')!r}: {path}"
                )
            return code
    if default is not None:
        return default
    raise ValueError(f"CityGML gml:Envelope is missing: {path}")


def local_enu_to_geodetic(points, center_lat, center_lon, epsg: int = 6697):
    """Invert project_to_local_enu: ``(east, north, height)`` to
    ``(latitude, longitude, height)`` on the tangent plane of ``epsg``.

    Newton steps on the forward projection converge to well under a
    millimetre in three iterations within a few kilometres of the centre.
    """
    if epsg not in SUPPORTED_CRS:
        raise ValueError(f"unsupported CRS {epsg_label(epsg)}")
    flattening = 1.0 / SUPPORTED_CRS[epsg][1]
    eccentricity_sq = flattening * (2.0 - flattening)
    phi = math.radians(center_lat)
    w = math.sqrt(1.0 - eccentricity_sq * math.sin(phi) ** 2)
    north_per_deg = math.radians(1.0) * SEMI_MAJOR_M * (1.0 - eccentricity_sq) / w ** 3
    east_per_deg = math.radians(1.0) * SEMI_MAJOR_M / w * math.cos(phi)
    output = []
    for east, north, height in points:
        lat = center_lat + north / north_per_deg
        lon = center_lon + east / east_per_deg
        for _ in range(4):
            e, n, _z = project_to_local_enu([(lat, lon, 0.0)], center_lat, center_lon, epsg)[0]
            lat += (north - n) / north_per_deg
            lon += (east - e) / east_per_deg
        output.append((lat, lon, height))
    return output

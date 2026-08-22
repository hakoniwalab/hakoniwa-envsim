"""PLATEAU Distribution Service client used by the component-owned CLI."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class PlateauError(RuntimeError):
    pass


def bounding_box(latitude: float, longitude: float, ns_m: float, ew_m: float) -> tuple[float, float, float, float]:
    """Return west, south, east, north for query-centered half extents."""
    lat_delta = ns_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(latitude))
    if abs(lon_scale) < 1.0:
        raise PlateauError("longitude range is undefined near the poles")
    lon_delta = ew_m / lon_scale
    return longitude - lon_delta, latitude - lat_delta, longitude + lon_delta, latitude + lat_delta


def search_url(api_base_url: str, feature_type: str, bbox: tuple[float, float, float, float]) -> str:
    condition = "m:" + ",".join(third_mesh_codes(bbox))
    query = urllib.parse.urlencode({"types": feature_type})
    return f"{api_base_url.rstrip('/')}/datacatalog/citygml/{condition}?{query}"


def third_mesh_codes(bbox: tuple[float, float, float, float]) -> list[str]:
    """Enumerate every Japanese third-level mesh intersecting a lon/lat bbox.

    The PLATEAU ``r:`` rectangle endpoint has been observed returning only the
    mesh files containing the two boundary coordinates.  Explicit ``m:``
    discovery prevents intermediate meshes from disappearing from a range.
    """
    west, south, east, north = bbox
    if not (west < east and south < north):
        raise PlateauError(f"invalid bbox ordering: {bbox}")
    # Third-level latitude cells are 30 arcseconds (1/120 degree), and
    # longitude cells are 45 arcseconds (1/80 degree). Treat the north/east
    # limits as exclusive so an exact cell boundary does not add another mesh.
    lat_first = math.floor(south * 120.0)
    lat_last = math.floor(math.nextafter(north, -math.inf) * 120.0)
    lon_first = math.floor((west - 100.0) * 80.0)
    lon_last = math.floor((math.nextafter(east, -math.inf) - 100.0) * 80.0)
    codes = []
    for lat_index in range(lat_first, lat_last + 1):
        first_lat, lat_remainder = divmod(lat_index, 80)
        second_lat, third_lat = divmod(lat_remainder, 10)
        for lon_index in range(lon_first, lon_last + 1):
            first_lon, lon_remainder = divmod(lon_index, 80)
            second_lon, third_lon = divmod(lon_remainder, 10)
            codes.append(
                f"{first_lat:02d}{first_lon:02d}{second_lat}{second_lon}{third_lat}{third_lon}"
            )
    if not codes:
        raise PlateauError(f"bbox resolved to no third-level mesh: {bbox}")
    return codes


def request_catalog(url: str, timeout_sec: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "hakoniwa-envsim/plateau-citygml"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PlateauError(f"PLATEAU catalog request failed: {url}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cities"), list):
        raise PlateauError("PLATEAU catalog response does not contain a cities array")
    return payload


def select_files(
    payload: dict[str, Any], feature_type: str, year: str | int, *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    cities = payload.get("cities", [])
    if year == "latest":
        latest: dict[str, dict[str, Any]] = {}
        for city in cities:
            code = str(city.get("cityCode", ""))
            rank = (int(city.get("year", 0)), int(city.get("registrationYear", 0)))
            current = latest.get(code)
            current_rank = (
                int(current.get("year", 0)), int(current.get("registrationYear", 0))
            ) if current else (-1, -1)
            if rank > current_rank:
                latest[code] = city
        selected_cities = list(latest.values())
    else:
        selected_cities = [city for city in cities if int(city.get("year", -1)) == int(year)]

    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for city in sorted(selected_cities, key=lambda item: (str(item.get("cityCode", "")), int(item.get("year", 0)))):
        files = city.get("files", {}).get(feature_type, [])
        for item in files:
            url = item.get("url")
            if not isinstance(url, str) or url in seen_urls:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise PlateauError(f"refusing non-HTTPS PLATEAU asset URL: {url!r}")
            if int(item.get("maxLod", 0)) < 1:
                continue
            seen_urls.add(url)
            selected.append({
                "city_code": str(city.get("cityCode", "")),
                "city_name": str(city.get("cityName", "")),
                "year": int(city.get("year", 0)),
                "registration_year": int(city.get("registrationYear", 0)),
                "spec": str(city.get("spec", "")),
                "code": str(item.get("code", "")),
                "max_lod": int(item.get("maxLod", 0)),
                "file_size": int(item.get("fileSize", 0)),
                "url": url,
            })
    if not selected and not allow_empty:
        raise PlateauError(f"no LOD1 {feature_type} CityGML files matched year={year!r}")
    return selected


def _safe_filename(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).name
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise PlateauError(f"unsafe PLATEAU asset filename in URL: {url}")
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(item: dict[str, Any], source_root: Path, timeout_sec: int = 180) -> dict[str, Any]:
    destination = source_root / f"{item['city_code']}-{item['year']}" / _safe_filename(item["url"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    declared_size = int(item.get("file_size", 0))
    reused = destination.is_file() and destination.stat().st_size > 0
    if not reused:
        temporary = destination.with_suffix(destination.suffix + ".part")
        request = urllib.request.Request(item["url"], headers={"User-Agent": "hakoniwa-envsim/plateau-citygml"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            if temporary.stat().st_size <= 0:
                raise PlateauError(f"downloaded an empty PLATEAU asset: {item['url']}")
            os.replace(temporary, destination)
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            if isinstance(exc, PlateauError):
                raise
            raise PlateauError(f"PLATEAU asset download failed: {item['url']}: {exc}") from exc
    actual_size = destination.stat().st_size
    if declared_size > 0 and actual_size != declared_size:
        print(
            "WARN: PLATEAU catalog fileSize differs from the downloaded object; "
            f"declared={declared_size}, actual={actual_size}, url={item['url']}"
        )
    return {
        **item,
        "path": str(destination),
        "bytes": actual_size,
        "sha256": sha256_file(destination),
        "mode": "reused" if reused else "downloaded",
    }

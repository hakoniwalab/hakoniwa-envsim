"""PLATEAU Distribution Service client used by the component-owned CLI."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import urllib.error
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


def search_url(
    api_base_url: str,
    feature_type: str,
    bbox: tuple[float, float, float, float],
    *,
    mesh_level: int = 3,
) -> str:
    if mesh_level == 3:
        mesh_codes = third_mesh_codes(bbox)
    elif mesh_level == 2:
        mesh_codes = second_mesh_codes(bbox)
    else:
        raise PlateauError(f"unsupported catalog mesh level: {mesh_level}")
    condition = "m:" + ",".join(mesh_codes)
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


def second_mesh_codes(bbox: tuple[float, float, float, float]) -> list[str]:
    """Return second-level mesh codes intersecting a bbox.

    Some sparse PLATEAU feature catalogs, including bridge models, are indexed
    at the broader second-level mesh even though the returned files retain
    third-level mesh codes.
    """
    return sorted({code[:6] for code in third_mesh_codes(bbox)})


def third_mesh_bounds(code: str) -> tuple[float, float, float, float]:
    """Return west, south, east, north for one 8-digit Japanese mesh code."""
    if re.fullmatch(r"\d{8}", code) is None:
        raise PlateauError(f"third-level mesh code must contain 8 digits: {code!r}")
    first_lat = int(code[0:2])
    first_lon = int(code[2:4])
    second_lat = int(code[4])
    second_lon = int(code[5])
    third_lat = int(code[6])
    third_lon = int(code[7])
    south = first_lat * (2.0 / 3.0) + second_lat / 12.0 + third_lat / 120.0
    west = 100.0 + first_lon + second_lon / 8.0 + third_lon / 80.0
    return west, south, west + 1.0 / 80.0, south + 1.0 / 120.0


def request_catalog(
    url: str, timeout_sec: int = 60, *, allow_not_found: bool = False
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "hakoniwa-envsim/plateau-citygml"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and allow_not_found:
            exc.close()
            return {
                "cities": [],
                "_catalog_status": {
                    "status": "not_available",
                    "http_status": 404,
                    "url": url,
                },
            }
        exc.close()
        raise PlateauError(f"PLATEAU catalog request failed: {url}: {exc}") from exc
    except Exception as exc:
        raise PlateauError(f"PLATEAU catalog request failed: {url}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cities"), list):
        raise PlateauError("PLATEAU catalog response does not contain a cities array")
    return payload


def request_dataset_catalog(
    api_base_url: str = "https://api.plateauview.mlit.go.jp",
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """Return the official PLATEAU municipality/dataset catalog.

    This is the coarse, nationwide availability layer.  Callers must still
    use ``request_catalog`` for the selected bbox before claiming that source
    files are available for generation.
    """
    url = f"{api_base_url.rstrip('/')}/datacatalog/plateau-datasets"
    request = urllib.request.Request(
        url, headers={"User-Agent": "hakoniwa-envsim/plateau-citygml"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PlateauError(f"PLATEAU dataset catalog request failed: {url}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("citygml"), list):
        raise PlateauError("PLATEAU dataset catalog response does not contain a citygml array")
    return payload


def select_files(
    payload: dict[str, Any], feature_type: str, year: str | int, *,
    allow_empty: bool = False, min_lod: int = 1,
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
            if int(item.get("maxLod", 0)) < min_lod:
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
        raise PlateauError(
            f"no LOD{min_lod} {feature_type} CityGML files matched year={year!r}"
        )
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


def _cache_object_path(item: dict[str, Any], cache_root: Path) -> Path:
    identity = f"{item['url']}\n{int(item.get('file_size', 0))}"
    url_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return cache_root / "objects" / url_key / _safe_filename(item["url"])


def _read_valid_cache(
    item: dict[str, Any], cache_path: Path,
) -> tuple[int, str] | None:
    receipt_path = cache_path.with_suffix(cache_path.suffix + ".cache.json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        actual_size = cache_path.stat().st_size
        if (
            actual_size <= 0
            or receipt.get("url") != item["url"]
            or receipt.get("catalog_file_size") != int(item.get("file_size", 0))
            or receipt.get("bytes") != actual_size
        ):
            return None
        actual_sha256 = sha256_file(cache_path)
        if receipt.get("sha256") != actual_sha256:
            return None
        return actual_size, actual_sha256
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _download_cache_object(
    item: dict[str, Any], cache_path: Path, timeout_sec: int,
) -> tuple[int, str]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    request = urllib.request.Request(
        item["url"], headers={"User-Agent": "hakoniwa-envsim/plateau-citygml"},
    )
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            prefix=f".{cache_path.name}.",
            suffix=".part",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        if temporary.stat().st_size <= 0:
            raise PlateauError(f"downloaded an empty PLATEAU asset: {item['url']}")
        actual_size = temporary.stat().st_size
        declared_size = int(item.get("file_size", 0))
        if declared_size > 0 and actual_size != declared_size:
            print(
                "WARN: PLATEAU catalog fileSize differs from the downloaded object; "
                f"declared={declared_size}, actual={actual_size}, url={item['url']}"
            )
        actual_sha256 = sha256_file(temporary)
        os.replace(temporary, cache_path)
        temporary = None
        cache_path.with_suffix(cache_path.suffix + ".cache.json").write_text(
            json.dumps({
                "schema_version": 1,
                "url": item["url"],
                "catalog_file_size": int(item.get("file_size", 0)),
                "bytes": actual_size,
                "sha256": actual_sha256,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return actual_size, actual_sha256
    except Exception as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(exc, PlateauError):
            raise
        raise PlateauError(f"PLATEAU asset download failed: {item['url']}: {exc}") from exc


def download_file(
    item: dict[str, Any], source_root: Path, timeout_sec: int = 180,
    *, cache_root: Path | None = None,
) -> dict[str, Any]:
    destination = source_root / f"{item['city_code']}-{item['year']}" / _safe_filename(item["url"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if cache_root is not None:
        cache_path = _cache_object_path(item, cache_root)
        cached = _read_valid_cache(item, cache_path)
        cache_hit = cached is not None
        actual_size, actual_sha256 = cached or _download_cache_object(
            item, cache_path, timeout_sec,
        )
        destination_valid = (
            destination.is_file()
            and destination.stat().st_size == actual_size
            and sha256_file(destination) == actual_sha256
        )
        if not destination_valid:
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.unlink(missing_ok=True)
            try:
                os.link(cache_path, temporary)
                materialization = "hardlink"
            except OSError:
                shutil.copy2(cache_path, temporary)
                materialization = "copy"
            os.replace(temporary, destination)
        else:
            materialization = "existing"
        return {
            **item,
            "path": str(destination),
            "bytes": actual_size,
            "sha256": actual_sha256,
            "mode": "cache-reused" if cache_hit else "downloaded",
            "cache": {
                "path": str(cache_path),
                "hit": cache_hit,
                "materialization": materialization,
            },
        }

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

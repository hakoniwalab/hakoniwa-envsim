# meshcode.py
def mesh_to_bbox(mesh: str):
    """
    8桁の3次メッシュコードから
    (lat_min, lat_max, lon_min, lon_max) を返す。単位は度。
    """
    mesh = mesh.strip()
    if len(mesh) != 8 or not mesh.isdigit():
        raise ValueError(f"invalid mesh code: {mesh}")

    p = int(mesh[0:2])
    q = int(mesh[2:4])
    r = int(mesh[4])
    s = int(mesh[5])
    t = int(mesh[6])
    u = int(mesh[7])

    # 基本メッシュの南西端 (p,q)
    lat = p * 2.0 / 3.0           # 40' = 2/3°
    lon = q + 100.0

    # 2次メッシュ (8分割)
    lat += (r * 2.0 / 3.0) / 8.0
    lon += (s * 1.0)       / 8.0

    # 3次メッシュ (10分割)
    lat += (t * 2.0 / 3.0) / 8.0 / 10.0
    lon += (u * 1.0)       / 8.0 / 10.0

    dlat = (2.0 / 3.0) / 8.0 / 10.0   # 約 0.008333°
    dlon = 1.0 / 8.0 / 10.0           # 0.0125°

    lat_min = lat
    lat_max = lat + dlat
    lon_min = lon
    lon_max = lon + dlon
    return lat_min, lat_max, lon_min, lon_max

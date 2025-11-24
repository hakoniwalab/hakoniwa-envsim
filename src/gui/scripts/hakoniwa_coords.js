// scripts/hakoniwa_coords.js
// proj4 が先に読み込まれている前提

(function (global) {
  // EPSG:6677 を一度だけ登録
  if (!proj4.defs["EPSG:6677"]) {
    proj4.defs(
      "EPSG:6677",
      "+proj=tmerc +lat_0=36 +lon_0=139.8333333333333 " +
        "+k=0.9999 +x_0=0 +y_0=0 +ellps=GRS80 +units=m +no_defs"
    );
  }

  // lat/lon (deg) → 原点基準 ENU[m] （x:東, y:北）
  function latlonToENU(originLat, originLon, lat, lon) {
    const p0 = proj4("EPSG:4326", "EPSG:6677", [originLon, originLat]); // [X0,Y0]
    const p1 = proj4("EPSG:4326", "EPSG:6677", [lon, lat]);             // [X1,Y1]
    const mx = p1[0] - p0[0]; // East
    const my = p1[1] - p0[1]; // North
    return [mx, my];
  }

  // 原点基準 ENU[m] → lat/lon (deg)
  function ENUToLatLon(originLat, originLon, x, y) {
    const p0 = proj4("EPSG:4326", "EPSG:6677", [originLon, originLat]); // [X0,Y0]
    const p1 = [p0[0] + x, p0[1] + y];                                  // [X1,Y1]
    const geo = proj4("EPSG:6677", "EPSG:4326", p1);                    // [lon,lat]
    const lon = geo[0];
    const lat = geo[1];
    return [lat, lon]; // [lat, lon]
  }

  // グローバルに公開
  global.HakoniwaCoords = {
    latlonToENU,
    ENUToLatLon,
  };
})(window);

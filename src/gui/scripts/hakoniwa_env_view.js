// scripts/hakoniwa_env_view.js

// env_zones.json を Leaflet 上に READONLY で描画するヘルパ
// 依存: Leaflet / HakoniwaCoords

const HakoniwaEnvView = (function() {

  function renderZonesReadonly(params) {
    const { obj, map, originLat, originLon, shapeLayerGroup, decoLayerGroup } = params;
    if (!obj) return;

    shapeLayerGroup.clearLayers();
    if (decoLayerGroup) decoLayerGroup.clearLayers();

    const zones = obj.zones || [];
    const axis = (obj.meta && obj.meta.axis ? String(obj.meta.axis).toUpperCase() : "ENU");
    const toENU = (axis === "ROS")
      ? (x,y,z)=>HakoniwaCoords.rosToEnuFrame(x,y,z)
      : (x,y,z)=>[x,y,z];

    zones.forEach(z => {
      let layer = null;

      // --- 図形本体（rect / circle） ---
      if (z.shape && z.shape.rect && z.shape.rect.min_m && z.shape.rect.max_m) {
        const min = z.shape.rect.min_m;
        const max = z.shape.rect.max_m;
        const [ex1, ey1] = toENU(min[0], min[1], 0);
        const [ex2, ey2] = toENU(max[0], max[1], 0);
        const [swLat, swLon] = HakoniwaCoords.ENUToLatLon(
          originLat, originLon,
          Math.min(ex1,ex2), Math.min(ey1,ey2)
        );
        const [neLat, neLon] = HakoniwaCoords.ENUToLatLon(
          originLat, originLon,
          Math.max(ex1,ex2), Math.max(ey1,ey2)
        );
        layer = L.rectangle([[swLat, swLon], [neLat, neLon]], {
          color:'#e67e22', weight:2, fillOpacity:0.08
        });
      } else if (z.shape && z.shape.circle && z.shape.circle.center_m && z.shape.circle.radius_m != null) {
        const c = z.shape.circle.center_m;
        const r = z.shape.circle.radius_m;
        const [ex, ey] = toENU(c[0], c[1], 0);
        const [lat, lon] = HakoniwaCoords.ENUToLatLon(originLat, originLon, ex, ey);
        layer = L.circle([lat, lon], {
          radius: r, color:'#27ae60', weight:2, fillOpacity:0.08
        });
      }

      if (!layer) return;

      // ここで effect 情報を layer._hakoniwa に軽く持たせておく
      const eff = z.effect || {};
      const meta = {
        name: z.name || "",
        priority: z.priority ?? null,
        mode: eff.mode || "absolute",
        gpsAbs: eff.gps_abs,
        windRos: eff.wind_ms,        // [x,y,z] (ROS)
        turbStd: eff.turbulence?.std_ms,
        turbSeed: eff.turbulence?.seed
      };
      layer._hakoniwa = meta;

      layer.addTo(shapeLayerGroup);

      // 図形にあわせて矢印/乱流を乗せる
      if (decoLayerGroup) {
        renderDecorationForLayer(layer, originLat, originLon, decoLayerGroup);
      }
    });
  }

  // レイヤ中心を取得（rect / circle 想定）
  function layerCenterLatLng(layer){
    if (layer instanceof L.Rectangle){
      return layer.getBounds().getCenter();
    } else if (layer instanceof L.Circle){
      return layer.getLatLng();
    }
    return null;
  }

  // 矢印 & 乱流円の描画（READONLY）
  function renderDecorationForLayer(layer, originLat, originLon, decoLayerGroup){
    const meta = layer._hakoniwa || {};
    const center = layerCenterLatLng(layer);
    if (!center) return;

    // absolute：wind_ms (ROS) → ENU → 流れ方向矢印
    if (meta.mode === "absolute" && Array.isArray(meta.windRos)) {
      const ros = meta.windRos;
      const enu = HakoniwaCoords.rosToEnuFrame(ros[0], ros[1], ros[2] || 0);
      const vx = enu[0], vy = enu[1];
      const speed = Math.hypot(vx, vy);
      if (speed < 1e-6) return;

      const scale = 50; // m → 矢印長さのスケール
      const enuStart = HakoniwaCoords.latlonToENU(originLat, originLon, center.lat, center.lng);
      const enuEnd   = [ enuStart[0] + vx*scale, enuStart[1] + vy*scale ];
      const [endLat, endLon] = HakoniwaCoords.ENUToLatLon(
        originLat, originLon, enuEnd[0], enuEnd[1]
      );

      // 本体線
      L.polyline([[center.lat, center.lng], [endLat, endLon]], {
        color:'#d35400', weight:3
      }).addTo(decoLayerGroup);

      // 矢印の先っぽ
      const angle = Math.atan2(endLon - center.lng, endLat - center.lat);
      const headLen = 0.0001;
      const left = [
        endLat - headLen*Math.cos(angle - Math.PI/6),
        endLon - headLen*Math.sin(angle - Math.PI/6)
      ];
      const right = [
        endLat - headLen*Math.cos(angle + Math.PI/6),
        endLon - headLen*Math.sin(angle + Math.PI/6)
      ];
      L.polyline([[endLat, endLon], left],  { color:'#d35400', weight:3 }).addTo(decoLayerGroup);
      L.polyline([[endLat, endLon], right], { color:'#d35400', weight:3 }).addTo(decoLayerGroup);

    } else if (meta.mode === "turbulence" && meta.turbStd != null) {
      // turbulence：std_ms × 30m の円を薄い青で
      const radiusMeters = meta.turbStd * 30.0;
      L.circle(center, {
        radius: radiusMeters,
        color: '#3498db',
        fillColor: '#3498db',
        fillOpacity: 0.15,
        weight: 1
      }).addTo(decoLayerGroup);
    }
  }

  return {
    renderZonesReadonly
  };
})();

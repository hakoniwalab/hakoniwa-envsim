# OpenStreetMap → CityGML LOD1 変換仕様

CityGMLをEnvsimの街データの標準中間表現とし、PLATEAU以外の地図データ（OpenStreetMap、GeoJSON）も
同じパイプラインでCity Worldにするための変換規則です。

- 実装：`src/city_pipeline/osm2citygml.py`
- 照合：この文書の表は`tests/test_osm2citygml.py`がコードの定数と照合します。どちらかだけを変えるとテストが落ちます。

OpenStreetMapには実測の3D形状も標高もありません。変換結果は「外形を高さまで押し出したLOD1」で、
高さの多くは推定値です。精度が必要な日本の都市ではPLATEAUを使ってください
（同じ範囲の変換結果を並べると、既定値の妥当性を確かめられます）。

## 1. 使い方

```bash
python src/city_pipeline/osm2citygml.py --bbox 35.6795,139.7650,35.6822,139.7683 --overpass \
  --out-dir work/osm/tokyo --name tokyo --build-manifest work/osm/tokyo/hakoniwa-build.yaml --save-data work/osm/tokyo/osm.json
python tools/hako.py build --config work/osm/tokyo/hakoniwa-build.yaml
```

| 入力 | 指定 | 備考 |
|---|---|---|
| OpenStreetMap（Overpass API） | `--overpass`（`--endpoint`か`HAKONIWA_OVERPASS_URL`で接続先を変更） | 既定は`https://overpass-api.de/api/interpreter` |
| OpenStreetMap（保存済み） | `--osm-json FILE`（Overpass JSON。`--save-data`で保存したもの） | ネットワーク不要 |
| GeoJSON | `--geojson FILE`（FeatureCollection、座標は`[lon, lat]`） | `--bbox`を省くとデータの範囲 |

出力（`--name`が接頭辞）：

| ファイル | 内容 |
|---|---|
| `<name>_bldg_op.gml` | 建物。CityGML 2.0の`bldg:Building`、`bldg:lod1Solid` |
| `<name>_tran_op.gml` | 道路。`tran:Road`、`tran:lod1MultiSurface`（道路が無ければ作らない） |
| `<name>-osm-receipt.json` | 件数、取り込まなかったもの、補完した数、出典、ファイルのSHA-256、推奨の`selection` |
| `--build-manifest`のパス | このファイル群から平らな地面のCity Worldを作る`hakoniwa-build.yaml`（`source.kind: files`） |

範囲は一辺 **2000 m** までです。

Overpassのクエリ（`{s},{w},{n},{e}`はbbox）：

```text
[out:json][timeout:80];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]["type"="multipolygon"]({s},{w},{n},{e});
  way["highway"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
```

## 2. 座標参照系と座標

- CityGMLは **EPSG:4326**（WGS 84の緯度・経度・高さ）で書きます（`srsName="http://www.opengis.net/def/crs/EPSG/0/4326"`、`srsDimension="3"`、`lat lon h`の順）。
- 高さは地面からのmです（OSMに標高はありません）。Envsimでは平らな地面（`terrain_uncovered_policy: constant`）の上に建ちます。
- 形の整理（点の間引き、面積、道路の幅）は、bboxの中心を原点とする局所ENU平面（WGS 84、`geodesy.py`）のmで行い、結果を緯度経度へ戻します（往復誤差は1 mmを大きく下回る）。
- 緯度・経度は小数9桁（約0.1 mm）、高さは小数3桁で書きます。

## 3. 対象にする要素

| 種類 | 条件 |
|---|---|
| 建物 | `building`タグがあり`no`でない。閉じたway、または`type=multipolygon`のrelation（`outer`のwayをつないで輪にし、`inner`は含む外周の穴にする） |
| 道路 | `highway`タグがあり、下の除外に当たらず、`area=yes`でも`tunnel=yes` / `building_passage`でもないway |

除外する`highway`（車の道路ではないもの）：

<!-- excluded-highways -->
`abandoned` `bridleway` `bus_guideway` `bus_stop` `construction` `corridor` `cycleway` `elevator` `escape` `footway` `path` `pedestrian` `platform` `proposed` `raceway` `rest_area` `services` `steps` `track` `via_ferrata`
<!-- /excluded-highways -->

GeoJSONでは、`building`プロパティを持つPolygon / MultiPolygonを建物、`highway`プロパティを持つLineString / MultiLineStringを道路にします。

**範囲で切り取りません。** Overpassはbboxにかかる要素を返します。それをそのまま書き、どれを使うかは
Envsimのパイプラインの既存の規則（建物は部分の重心が選択範囲内にあるもの、道路は範囲で切り取り）に任せます。

## 4. 建物

**形**：外形（と穴）を、下端から高さまで押し出した閉じたLOD1ソリッドです。面は次のとおりで、屋根は平らです。

- 底面（下向き）
- 天面（上向き）
- 外周と穴の各辺の壁

**整理**：
- 点は **5 cm** の精度で単純化します。
- 自己交差する外形は修復を試み、単一の多角形にならなければ取り込みません。
- 面積が **4 m²** 未満のものは取り込みません。

**高さ（`bldg:measuredHeight`）**は、次の順で最初に決まったものを使います。

1. `height`タグ（`12`・`12 m`・`12,5m`はm、`40'`・`40 ft`はフィート）
2. `building:levels` × **3 m**。`roof:shape`があって`flat`でなければ、さらに+1 m
3. `building`の値ごとの既定値（下の表。表に無い値は **9 m**）

最終的な高さは1〜500 mに収めます。

<!-- heights-by-kind -->
| building | 高さ (m) |
|---|---|
| `house` | 6 |
| `detached` | 6 |
| `semidetached_house` | 6 |
| `terrace` | 6 |
| `bungalow` | 4 |
| `residential` | 9 |
| `apartments` | 15 |
| `dormitory` | 12 |
| `hotel` | 20 |
| `commercial` | 12 |
| `office` | 15 |
| `retail` | 6 |
| `supermarket` | 6 |
| `industrial` | 8 |
| `warehouse` | 8 |
| `factory` | 10 |
| `school` | 12 |
| `university` | 15 |
| `hospital` | 18 |
| `public` | 12 |
| `civic` | 12 |
| `church` | 12 |
| `temple` | 8 |
| `shrine` | 6 |
| `garage` | 3 |
| `garages` | 3 |
| `carport` | 3 |
| `shed` | 3 |
| `hut` | 3 |
| `kiosk` | 3 |
| `roof` | 4 |
<!-- /heights-by-kind -->

**下端**は、次の順で最初に決まったものを使います。ソリッドはここから始まります。

1. `min_height`タグ
2. `building:min_level` × 3 m
3. `building=roof`（壁の無い屋根：駅のホームやガソリンスタンドの屋根）なら、高さ − **0.5 m**（厚さ0.5 mの板）

どの場合も下端は「高さ − 0.5 m」を超えません。

`building:part`は読みません。1つのrelationに外周が複数あるときは、外周ごとに別の`bldg:Building`にします。

## 5. 道路

wayの中心線を、幅の半分ずつ左右に広げた多角形（端は平ら、曲がり角は丸く）を`tran:lod1MultiSurface`にします。
高さは0（地面）です。Envsimでは、LOD2 / LOD3の道路面が無いときのLOD1 roadwayとして描かれます。

- 車線数：`lanes`タグ。無ければ`highway`の値ごとの既定値（下の表。表に無い値は **2**）
- 幅：`width`タグ。無ければ 車線数 × **3.25 m**。2.5〜60 mに収めます。
- 長さが **1 m** 未満のものは取り込みません。

<!-- lanes-by-class -->
| highway | 車線数 |
|---|---|
| `motorway` | 4 |
| `trunk` | 4 |
| `primary` | 2 |
| `secondary` | 2 |
| `tertiary` | 2 |
| `unclassified` | 2 |
| `residential` | 2 |
| `living_street` | 1 |
| `service` | 1 |
| `road` | 2 |
| `motorway_link` | 1 |
| `trunk_link` | 1 |
| `primary_link` | 1 |
| `secondary_link` | 1 |
| `tertiary_link` | 1 |
<!-- /lanes-by-class -->

`bridge`・`layer`タグは記録するだけで、立体交差は作りません。

## 6. IDと出典

- `gml:id`：
  - `osm_w<way id>`、`osm_r<relation id>`
  - GeoJSONは`geojson_w…` / `geojson_f<番号>`
  - 外周が複数あるrelationは`_p1`、`_p2`、…、同じwayの複数の線は`_1`、`_2`、…
  - 重なったIDには`_2`、`_3`を付けます
- 名前：`name`タグを`gml:name`に入れます。
- 各地物の`gen:stringAttribute`：
  - 出典：`source_provider`、`source_kind`、`source_id`
  - 建物：`height_source`（`height` / `levels` / `default`）、`base_m`
  - 道路：`width_m`、`lanes`、`width_source`、`lanes_source`
  - 残すOSMタグ：`osm:<key>`（`building` `highway` `name` `height` `min_height` `building:levels` `building:min_level` `roof:shape` `lanes` `width` `oneway` `surface` `bridge` `tunnel` `layer`）
- 建物には、階数が分かれば`bldg:storeysAboveGround`も入れます。
- レシート：
  - `attribution: © OpenStreetMap contributors`、`license: ODbL-1.0`
  - `data_timestamp`（Overpassの`timestamp_osm_base`）、`query`
  - 入力データのSHA-256

OpenStreetMap由来のCityGMLと、それから作ったCity Worldは、ODbLの派生データベースです。
配布するときは、出典の表示と同じライセンスでの提供が必要です。

## 7. 決定性

同じ入力データとbboxからは、バイト単位で同じCityGMLができます。

- 地物は、建物 → 道路の順、同じ種類の中では要素の種類とidの順に並べます。
- 補完は、すべてこの文書の固定値です。

Overpassから取り直すと、OSM側の編集によって結果が変わることがあります（`data_timestamp`で区別できます）。

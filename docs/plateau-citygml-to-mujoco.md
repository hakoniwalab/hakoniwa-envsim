# PLATEAU CityGML → MuJoCo壁モデル

## 目的

緯度・経度と範囲から、該当するPLATEAU建築物LOD1 CityGMLを取得し、
MuJoCoで衝突判定に使えるbox geom群を含むMJCFへ変換します。

処理の流れは次のとおりです。

```text
latitude / longitude / half extents
  → PLATEAU Distribution Service の範囲検索
  → bldg CityGMLの取得
  → LOD1フットプリントと高さの抽出
  → OBBまたは外周壁boxへの変換
  → MuJoCo MJCF
```

PLATEAU APIによる範囲検索は、ローカルCityGML一式を対象にした従来の
`gml_indexer.py` / `gml_extract.py` と同じ役割を担います。その後の
`gml_lod1_extract.py`、`gml2obb.py`、`obb2mjcf.py` は既存パイプラインを利用します。

## 設定

`hakoniwa-build.yaml` の中心座標とhalf extentを変更します。
設定形式は `schemas/hakoniwa-build.schema.json` で固定されています。

```yaml
selection:
  center:
    latitude: 35.681236
    longitude: 139.706763
  half_extent_m:
    north_south: 100
    east_west: 100
```

`north_south` と `east_west` は中心から片側への距離です。この例の検索矩形は
南北200 m × 東西200 mです。APIはこの矩形に交差するCityGMLメッシュを返し、
LOD1抽出時に建物フットプリントの重心で指定範囲へ再度絞り込みます。
隣接自治体から同一メッシュ・同一建物が返る場合はCityGML建物IDで一つにまとめます。
同じIDで形状が異なる場合は、任意の一方を選ばず入力矛盾として停止します。

主な設定値は次のとおりです。

| 設定 | 意味 |
|---|---|
| `source.year` | `latest`または西暦。`latest`は市区町村ごとの最新データを選択 |
| `geometry.base_epsilon_m` | 建物底面点を抽出する高さ許容差 |
| `geometry.waste_threshold` | OBB内の空白面積率 `(OBB面積-建物面積)/OBB面積` がこの値を超えたら外周壁boxへ切替（0〜1） |
| `geometry.wall_thickness_m` | 外周壁boxの厚さ |
| `mjcf.collision` | `all`、`drone`、`none` |
| `mjcf.floor` | MJCFに平面床を追加するか |

LOD1の元の底面外周（凹形状と穴を含む）は変換中も保持されます。
`waste_threshold: 1.0` のデフォルトは、すべての建物を1個のOBBで近似し、
データ量を最小化します。閾値を下げると、OBB内の空白率がその値を超える
建物だけが、元の外周の各辺を表すwall boxへ切り替わります。
`0.0` では、OBBと完全に一致する建物を除き、最も詳細なwall表現になります。

## 実行

```bash
python -m pip install -r requirements.txt

python tools/hako.py doctor
python tools/hako.py configure
python tools/hako.py build
python tools/hako.py install
```

`doctor` と `configure` はネットワークへアクセスしません。`build` を明示的に
実行したときだけ、PLATEAU APIの検索とCityGML取得を行います。

一度取得したデータからネットワークなしで再変換する場合は次を使います。

```bash
python tools/hako.py build --offline
```

## 生成物と再現性

既定のbuild directoryは `.hako/build/plateau-city-mjcf/` です。

| ファイル | 内容 |
|---|---|
| `plateau-catalog-response.json` | PLATEAU APIの応答 |
| `source/query_meta.json` | 中心、half extent、検索bbox |
| `source/<city>-<year>/*.gml` | 取得したCityGML |
| `download-manifest.json` | URL、都市、年度、カタログ記載サイズ、実サイズ、SHA-256、取得/再利用モード |
| `plateau-city-lod1.json` | 局所ENU座標のLOD1建物フットプリント |
| `plateau-city-walls.json` | OBBまたは外周壁box |
| `plateau-city.xml` | MuJoCo MJCF |
| `build-receipt.json` | 解決したpipelineと生成結果 |

`source.year: latest` は将来別年度へ解決される可能性があります。再現性が必要な
実験では、初回取得後に年度を固定し、`download-manifest.json`と元CityGMLを
実験成果物として保管してください。

建物の外周辺には入力頂点順に基づく安定したgeom IDを付与します。この順序を
環境差で変えないため、Shapelyを正式な依存関係として使用します。`doctor`は
Shapelyが無い環境をbuild前に停止します。歴史的な旧投影データの再現は、
現行コンポーネントの責務に混在させず、Hakoniwa Business Packの固定回帰Recipeが
所有します。

APIの`fileSize`と配信された実体サイズが一致しない場合があるため、`fileSize`は
参考情報として扱います。取得の同一性には、実体から計算したSHA-256を使用します。

## 座標系

公式PLATEAU CityGMLの建物座標はEPSG:6697で、座標順は
`latitude longitude height`です。本ツールはGRS80楕円体から、指定中心を原点とする
局所接平面ENU（East, North, Up、単位m）へ変換します。これにより、日本の地域ごとに
異なる平面直角座標系EPSGを利用者が選ぶ必要はありません。

変換前にCityGMLの`gml:Envelope`を読み、`srsName`がEPSG:6697、
`srsDimension`が3であることを検証します。EPSG:4326としての読み替えや、
EPSG:6677への旧投影、利用者による軸順切替はサポートしません。

MJCF出力時は既存の箱庭/MuJoCo規約に合わせて
`ENU (East, North, Up) → (North, -East, Up)`へ軸変換します。
建物群の最小`zmin`は0 mへ平行移動され、各建物の相対的な高さと高低差は維持されます。

## 検証

ネットワークを使わない最小CityGML fixtureで、全変換経路を確認できます。

```bash
python tools/hako.py test
python tools/hako.py smoke
```

PLATEAU Distribution Service APIは外部の試行提供サービスです。API障害や仕様変更を
ローカル変換の失敗と混同しないよう、`doctor`/`smoke`はネットワーク非依存にしています。

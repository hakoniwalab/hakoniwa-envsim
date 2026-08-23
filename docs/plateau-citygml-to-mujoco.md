# PLATEAU CityGML → MuJoCo壁モデル / GLB表示モデル

## 目的

緯度・経度と範囲から、該当するPLATEAU建築物CityGMLを取得し、
MuJoCoの衝突判定用MJCFとブラウザ表示用GLBを同じ建物選択・原点から生成します。

処理の流れは次のとおりです。

```text
latitude / longitude / half extents
  → PLATEAU Distribution Service の範囲検索
  → bldg CityGMLの取得
  → LOD1フットプリントと高さの抽出
  ├→ OBBまたは外周壁box → MuJoCo MJCF
  └→ LOD2優先 / LOD1フォールバック → GLB
```

City World構成では、同じworld-frameへDEM、道路、建物を統合します。
`source.feature_types.brid: true` の場合は、PLATEAUのLOD3橋梁を追加のGLB表示
コンポーネントとして統合します。橋梁の元高度は保持し、DEMへ貼り付けたり、
欠落した橋形状を推定したりしません。利用可能な`OuterFloorSurface`があれば、
独立した軽量MJCF橋面collisionも生成します。

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
南北200 m × 東西200 mです。Envsimはこの矩形に交差する全3次メッシュを列挙して
PLATEAU APIへ明示し、LOD1抽出時に建物フットプリントの重心で指定範囲へ再度絞り込みます。
矩形検索をAPIの`r:`応答だけに委ねません。検証した渋谷範囲では、南北端を含む
メッシュだけが返り、中間メッシュが欠落する事象を確認したためです。現在は全交差
メッシュを`m:`条件で問い合わせ、メッシュコード一覧をquery契約として記録します。
隣接自治体から同一メッシュ・同一建物が返る場合はCityGML建物IDで一つにまとめます。
同じIDで形状が異なる場合は、任意の一方を選ばず入力矛盾として停止します。
メッシュ単位のCityGMLはユーザー選択範囲より広いため、建物LOD1の厳密な底面検証より先に
query-centered local ENU上で範囲交差を判定します。範囲外の不正形状は生成を妨げません。
範囲内に不正なLOD1底面がある場合は、その1棟だけをスキップし、建物ID、source GML、
理由コードを抽出JSON、建物Receipt、Dataset Validatorへ伝搬します。

主な設定値は次のとおりです。

| 設定 | 意味 |
|---|---|
| `source.cache_dir` | 任意の共有CityGML cache。source URL単位で保存し、サイズとSHA-256検証後に再利用。同一filesystemではhard link、利用できない場合はcopyでbuildへ配置。`null`ではbuild単位 |
| `source.year` | `latest`または西暦。`latest`は市区町村ごとの最新データを選択 |
| `geometry.base_epsilon_m` | 建物底面点を抽出する高さ許容差 |
| `geometry.waste_threshold` | OBB内の空白面積率 `(OBB面積-建物面積)/OBB面積` がこの値を超えたら外周壁boxへ切替（0〜1） |
| `geometry.wall_thickness_m` | 外周壁boxの厚さ |
| `geometry.roof_collision_thickness_m` | wall mode建物の屋根meshへ下向きに加える数値上のcollision厚み |
| `mjcf.collision` | 建物と橋面collisionに共通適用するfilter。`all`=`(contype=1, conaffinity=0)`、`drone`=`(1,2)`、`none`=`(0,0)` |
| `mjcf.floor` | MJCFに平面床を追加するか |
| `mjcf.building_physics_level` | 0=P0、1=P1→P0、2=P2→P1→P0、3=P3→P2→P1→P0（既定） |
| `glb.enabled` | 同じ建物選択と原点からGLBも生成するか |
| `glb.lod_policy` | Visualは`highest_available` |
| `glb.texture_mode` | `embedded-if-available`で利用可能なtextureを使用 |
| `source.feature_types.brid` | LOD3橋梁を検索し、GLB表示と利用可能な橋面collisionへ変換するか（City Worldのみ、既定false） |

LOD1の元の底面外周（凹形状と穴を含む）は変換中も保持されます。
`waste_threshold: 1.0` のデフォルトは、すべての建物を1個のOBBで近似し、
データ量を最小化します。閾値を下げると、OBB内の空白率がその値を超える
建物だけが、元の外周の各辺を表すwall boxへ切り替わります。
wall modeでは同じ外周と中庭を三角形分割し、`zmax`を上面とする薄い屋根collisionも
生成します。中庭は屋根で塞がず、OBB modeではbox自体に上面があるため屋根meshを追加しません。
`0.0` では、OBBと完全に一致する建物を除き、最も詳細なwall表現になります。

### 建物Physics分類とclass別collision

City World buildは、建物をP0〜P3へ分類し、classに対応するcollisionを生成します。
判定式、優先順位、用語、採用するsemantic surface、生成方法、既知の制限は、専用文書
[`building-physics-classification.md`](building-physics-classification.md)を正本とします。
本書では重複して定義しません。

## GLBのLODとテクスチャ

GLBはUnityを介さず、PLATEAU CityGMLから直接生成します。同一範囲でも
入力データの詳細度に応じて次の表現が混在します。

`building_physics_level`は建物Colliderの判定上限だけを制御します。例えばLevel 2では
P3条件を評価せず、P2、P1、P0の順に最初に一致した方式を採用します。Visualとtextureは
全Levelで同一なので、見た目を変えずにCollider数と負荷を比較できます。

```text
LOD2 + Appearance/UV + 参照画像あり -> 詳細形状・テクスチャ付き
LOD2あり、テクスチャなし           -> 詳細形状・フラット色
LOD2なし                             -> LOD1外周の立体・フラット色
```

`embedded-if-available`は、選択されたLOD2面が参照する画像だけを取得し、
GLB内へ埋め込みます。URL、バイト数、SHA-256は`<name>-glb-receipt.json`に
記録します。`--offline`は取得済み画像を再利用し、未取得面はフラット色に
フォールバックします。

GLB頂点はThree.jsネイティブの`X=East, Y=Up, Z=-North`で出力します。
箱庭/ROSの`X=North, Y=-East, Z=Up`と一対一に対応し、原点高度はMJCFと共有します。
新規GLBには従来Asset用の180度補正は不要です。

## LOD3橋梁の表示・Physics契約

橋梁データは疎に配置され、PLATEAUカタログでは第2次メッシュ単位で検索される
場合があります。そのためEnvsimは、通常地物の第3次メッシュ検索とは分けて
`brid`だけを第2次メッシュで検索し、返されたLOD3ファイルを対象範囲で絞ります。

```text
brid LOD3 CityGML (EPSG:6697, 3D)
  → 対象範囲と交差するLOD3面
  → X3DMaterial diffuseColor（無い面だけ既定色）
  → source altitude - world-frame altitude_offset
  → bridges.glb
  → city-world.glbへ統合
```

三角形へ変換できない退化面は橋梁全体を失敗させず、その面だけ除外し、件数と
最大100件のpolygon IDを`bridges-glb-receipt.json`へ記録します。Dataset Validatorは
橋梁について `bridge_visualization` と `bridge_collision` を別々に報告します。

Physics側はLOD3 `brid:OuterFloorSurface`のうち、設定した最大傾斜以下の面だけを
通行可能な橋面候補として扱います。各source polygonをtriangulateし、source XYZを
上面に保ったまま負Z方向へ薄く押し出した独立convex meshを生成します。

```text
OuterFloorSurface polygon
  → local ENU / MuJoCo axes
  → walkable slope filter
  → triangles
  → independent thin triangular-prism mesh geoms
  → components/bridges/bridges.xml
  → city-world.xmlへ統合
```

MuJoCoのmesh collisionはmeshの凸包を使うため、曲線・分岐橋を単一meshへまとめません。
三角柱は凸形状なので、凸包化してもsource triangleの範囲外にcollisionを作りません。
橋面geomの`contype` / `conaffinity`は建物と同じ`mjcf.collision`から明示生成し、
MuJoCoのデフォルト値へ暗黙にフォールバックしません。
追加する厚みは橋桁の推定ではなくcollisionをwatertightにする数値上のderived geometryで、
`components/bridges/receipt.json`へ値と目的を記録します。橋面高さをDEMへ補正・blend
することはなく、境界頂点とDEM高さの差だけを計測します。
橋面全体についてもsource頂点と同一XYのDEM高さを比較し、DEMより低いsample数を
Receiptへ記録します。負の差があっても橋面を黙って移動せず、元データ間の不一致として残します。

生成済みCity World MJCFは`mjcf_colliders2glb.py`でデバッグ表示用GLBへ変換できます。
box、inline mesh、terrain hfieldを対象とし、MJCFの
`X=North, Y=-East, Z=Up`からThree.jsの`X=East, Y=Up, Z=-North`へ変換します。
このGLBはVisual Worldとの位置合わせを確認するための表示物であり、collision判定の正本ではありません。

関連設定は次のとおりです。

```yaml
city_world:
  bridge_collision_thickness_m: 0.02
  bridge_max_surface_slope_deg: 60
```

LOD3の利用可能な橋面を得られない場合、LOD1 Solidなどで橋下を塞ぐfallbackは行わず、
`capability: scoped_out`、`reason: usable_bridge_surface_not_available`として継続します。

## 実行

```bash
python -m pip install -r requirements.txt

python tools/hako.py doctor
python tools/hako.py configure
python tools/hako.py build
python tools/hako.py install
```

`doctor` と `configure` はネットワークへアクセスしません。`build` を明示的に
実行したときだけ、PLATEAU APIの検索、CityGML取得、選択されたLOD2テクスチャ取得を行います。

一度取得したデータからネットワークなしで再変換する場合は次を使います。

```bash
python tools/hako.py build --offline
```

## 生成物と再現性

既定のbuild directoryは `.hako/build/plateau-city-mjcf/` です。

| ファイル | 内容 |
|---|---|
| `plateau-catalog-response-<feature>.json` | 地物種別ごとのPLATEAU API応答 |
| `plateau-catalog-query-<feature>.json` | API URL、検索メッシュレベル、列挙したメッシュコード |
| `source/query_meta.json` | 中心、half extent、検索bbox |
| `source/<city>-<year>/*.gml` | 取得したCityGML |
| `download-manifest.json` | URL、都市、年度、カタログ記載サイズ、実サイズ、SHA-256、取得/再利用モード |
| `plateau-city-lod1.json` | 局所ENU座標のLOD1建物フットプリント |
| `plateau-city-walls.json` | OBBまたは外周壁box |
| `plateau-city.xml` | MuJoCo MJCF |
| `build-receipt.json` | 解決したpipelineと生成結果 |
| `components/bridges/bridges.glb` | 利用可能な場合だけ生成するLOD3橋梁表示モデル |
| `components/bridges/bridges-glb-receipt.json` | 橋梁のLOD、材質、採用/除外面、SHA-256 |
| `components/bridges/bridges.xml` | LOD3 OuterFloorSurface由来の独立橋面collision component |
| `components/bridges/receipt.json` | source surface、geom数、derived厚み、端部とDEMの高さ差、制限事項 |
| `components/bridges/debug/bridge-surfaces.json` | source triangleと境界高さ検証の追跡情報 |
| `components/buildings/building-physics-classification.json` | 建物ごとのP0〜P3分類、根拠値、選択したcollision strategy |
| `components/buildings/building-physics-classes.glb` | 分類を色分けして判定を確認するためのLOD2モデル |
| `components/buildings/building-physics-application.json` | class別collisionの適用状態と、建物ごとのMJCF geom対応 |
| `world/dataset-validation.{json,txt}` | 利用可能LOD、fallback、表示/衝突capability |

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

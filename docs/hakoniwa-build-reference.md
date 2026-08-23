# `hakoniwa-build.yaml` 設定リファレンス

`tools/hako.py` が読むPLATEAU変換マニフェストの全設定項目をまとめます。
省略した項目には、`tools/hako.py` の既定値が使われます。未知の項目は入力ミスとして
拒否されます。完全に展開された設定の機械可読な制約は
[`schemas/hakoniwa-build.schema.json`](../schemas/hakoniwa-build.schema.json)です。

パスの相対指定は、コマンドを実行したディレクトリではなく、
`hakoniwa-envsim`リポジトリのルートを基準に解決されます。

## 識別情報

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `version` | `1`固定 | `1` | マニフェスト形式のバージョン |
| `component` | `hakoniwa-envsim`固定 | `hakoniwa-envsim` | マニフェストの所有コンポーネント |
| `pipeline.type` | `plateau-citygml-to-assets`固定 | `plateau-citygml-to-assets` | 使用する変換パイプライン |

## PLATEAUデータ取得 (`source`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `source.api_base_url` | HTTPS URL | `https://api.plateauview.mlit.go.jp` | PLATEAU配信サービスのAPI基点 |
| `source.cache_dir` | path / `null` | `null` | source URL単位の共有cache。サイズとSHA-256を検証して再利用する。`null`ではbuild単位 |
| `source.feature_type` | `bldg`固定 | `bldg` | 単体変換との互換用の主地物型。現在は建物のみ |
| `source.feature_types.bldg` | `true`固定 | `true` | 建物を取得する |
| `source.feature_types.tran` | boolean | `false` | 道路を取得する |
| `source.feature_types.dem` | boolean | `false` | DEM地形を取得する |
| `source.feature_types.frn` | boolean | `false` | CityFurnitureを取得し、実データにある路面標示をVisual化する |
| `source.feature_types.brid` | boolean | `false` | 橋梁を取得し、Visualと利用可能な橋面Physicsを生成する |
| `source.year` | `latest` / 2000以上の西暦 | `latest` | 使用年度。`latest`は市区町村ごとの最新データを選ぶ |

`city_world.enabled: true`では、`bldg`、`tran`、`dem`、`frn`をすべて
`true`にする必要があります。`brid`は任意です。データが存在しない地物は、可能な場合は
Dataset Validatorで`scoped_out`として明示されます。

## 対象範囲 (`selection`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `selection.center.latitude` | -90〜90 degree | `35.681236` | 選択中心の緯度 |
| `selection.center.longitude` | -180〜180 degree | `139.706763` | 選択中心の経度 |
| `selection.half_extent_m.north_south` | 0より大きいm | `100` | 中心から北・南へそれぞれ取る距離 |
| `selection.half_extent_m.east_west` | 0より大きいm | `100` | 中心から東・西へそれぞれ取る距離 |

`half_extent_m`は全幅ではありません。両方を`100`にすると、対象範囲は
南北200 m × 東西200 mです。

## 建物形状 (`geometry`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `geometry.base_epsilon_m` | 0より大きいm | `0.2` | LOD1底面点を同じ底面とみなす高さ許容差 |
| `geometry.waste_threshold` | 0〜1 | `1.0` | OBB内の空白面積率がこの値を超えた建物を外周wall表現へ切り替える |
| `geometry.wall_thickness_m` | 0より大きいm | `0.1` | 外周wall boxの厚さ |
| `geometry.roof_collision_thickness_m` | 0より大きいm | `0.02` | wall modeの屋根collisionに与える数値上の厚み |

空白面積率は`(OBB面積 - 建物面積) / OBB面積`です。`1.0`は最軽量のOBB優先、
`0.0`はOBBと完全一致する建物を除いて外周wallを選ぶ設定です。P0〜P3の分類とは
別の軸です。詳しくは
[`building-physics-classification.md`](building-physics-classification.md)を参照してください。

## MuJoCo Physics (`mjcf`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `mjcf.model_name` | 空でない文字列 | `plateau_city` | 生成するMJCFの`mujoco model`名 |
| `mjcf.collision` | `all` / `drone` / `none` | `all` | 建物と橋面のcollision filter。順に`(contype, conaffinity)=(1,0) / (1,2) / (0,0)` |
| `mjcf.floor` | boolean | `false` | z=0の無限平面を建物MJCFへ追加する |
| `mjcf.building_physics_level` | 整数0〜3 | `3` | 建物Colliderの適用上限。0=P0のみ、1=P1→P0、2=P2→P1→P0、3=P3→P2→P1→P0 |

`building_physics_level`はPhysicsだけを変えます。VisualのLODやtextureは変えません。

## GLB Visual (`glb`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `glb.enabled` | boolean | `true` | 建物GLBを生成する |
| `glb.lod_policy` | `highest_available`固定 | `highest_available` | 選択範囲で利用可能な最高LODを使う |
| `glb.texture_mode` | `embedded-if-available` / `flat` | `embedded-if-available` | 利用可能なtextureをGLBへ埋め込むか、単色にするか |

## City World (`city_world`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `city_world.enabled` | boolean | `false` | 建物単体ではなく、地形・道路等を含むCity Worldを生成する |
| `city_world.parallel_workers` | 整数1〜16 | `4` | source取得と独立component生成に使うworker上限 |
| `city_world.dem_parallel_workers` | 整数1〜4 | `2` | DEM source抽出専用のprocess上限。メモリ保護のため最大4 |
| `city_world.terrain_spacing_m` | 0より大きいm | `2` | DEM hfieldの目標最大格子間隔。小さいほど詳細だが、sample数とメモリが増える |
| `city_world.marking_vertical_offset_m` | 0より大きいm | `0.055` | 路面標示Visualを道路面から浮かせる描画用offset |
| `city_world.bridge_collision_thickness_m` | 0より大きいm | `0.02` | 橋面collision meshへ与える数値上の厚み |
| `city_world.bridge_max_surface_slope_deg` | 0より大きく90未満のdegree | `60` | 通行可能な橋面候補として許容する最大傾斜 |

`terrain_spacing_m: 2`では、2 km四方が1001 × 1001 sampleになります。
ブラウザで選んだ範囲が2 mで割り切れない場合もbboxは変更せず、各軸の実間隔を
2 m以下へわずかに縮めて両端を一致させます。実間隔はterrain Receiptの
`effective_spacing_m`に記録されます。
広域生成でメモリや時間を抑えたい場合は、用途上許容できる範囲で5 mや10 mへ広げます。
DEMの未収録領域をこの設定で補うことはありません。
DEM CityGMLはCRSとEnvelopeをXMLとして検証した後、巨大ファイル全体のXML treeを
構築せず、TINの`gml:posList`だけをstream走査します。複数sourceは並列処理されます。

### `parallel_workers`の選び方

`parallel_workers`は、PLATEAU source取得と、出力先が独立した建物Visual、建物Physics、
道路、路面標示、橋梁component生成のworker上限です。ComposerとDataset Validatorは
依存componentの完了後に直列実行します。`dem_parallel_workers`はDEM source抽出だけに使い、
実際のprocess数は設定値と対象DEM source数の小さい方です。DEM CityGMLと抽出結果をprocessごとに
保持するため、メモリ保護の観点から設定上限を4にしています。

まず既定値`4`を使い、CPUとメモリに余裕があり、source取得または独立component生成が
律速している場合に`6`、次に`8`を試します。CPU飽和、swap、ディスクI/O待ちが増える場合は
一段階戻します。`8`を超える値は、同じ入力範囲とcache状態で処理時間の短縮を実測できた場合だけ
使用してください。並列可能なsource数・component数以上のworkerは待機するため、値を増やせば
必ず高速化するわけではありません。

DEM source抽出が律速し、CPUとメモリに余裕がある場合だけ、既定値`2`から`4`へ増やします。
この値は、その後の格子ラスタライズや小欠損補間には影響しません。

直接実行では、`tools/hako.py build --config`へ渡すYAMLの次の箇所を編集します。

```yaml
city_world:
  parallel_workers: 6
  dem_parallel_workers: 4
```

## 出力 (`output`)

| 設定 | 型・範囲 | 既定値 | 意味 |
|---|---|---|---|
| `output.build_dir` | 空でないpath | `.hako/build/plateau-city-mjcf` | 中間生成物、最終生成物、Receiptの出力先 |
| `output.install_dir` | 空でないpath | `.hako/install` | `install`コマンドの基点 |
| `output.name` | 英数字・`.`・`_`・`-` | `plateau-city` | 生成物名とinstall先のcity名 |

単体変換、City Worldともにinstall先は
`<install_dir>/share/hakoniwa-envsim/city/<name>/`です。City Worldでは、その下へ
`components/`、`world/`、Receiptをまとめて配置します。

## 設定例

リポジトリ直下の[`hakoniwa-build.yaml`](../hakoniwa-build.yaml)が、全項目を含む
標準例です。編集後は、build前に設定値と依存関係を検査できます。

```bash
python3 tools/hako.py doctor --config hakoniwa-build.yaml
python3 tools/hako.py build --config hakoniwa-build.yaml
```

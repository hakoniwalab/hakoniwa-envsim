# 建物Physics分類 P0〜P3

## 1. 目的

PLATEAU建物をすべてLOD2 mesh collisionへ変換すると、MuJoCoのgeom数と接触判定負荷が
急増します。一方、すべてをLOD1 prismにすると、段状の建物、張り出し、通過空間などを
誤って埋めます。

この分類は、各建物について次の問いへ答えるためのものです。

> LOD1の軽量な近似を採用できるか。できない場合、LOD2のどのsemantic surfaceまで
> collisionへ反映する必要があるか。

分類は建物ごとに必ず1つだけ付与します。形状の品質点数ではなく、必要なcollision戦略を
選ぶための排他的なカテゴリです。

## 2. 用語集

### PLATEAU / CityGMLの基本語

| 用語 | この文書での意味 |
|---|---|
| PLATEAU | 国土交通省が整備する3D都市モデル。CityGML形式で建物や道路などを提供する |
| CityGML | 都市の形だけでなく、「この面は屋根」「この面は壁」という意味も記録できるデータ形式 |
| LOD | Level of Detail。数字が大きいほど、一般に形状や意味付けが詳細になる |
| LOD1 | 建物の底面を一定の高さまで押し出した、単純な箱に近い表現。軽量だが段差や張り出しを失う |
| LOD2 | 屋根・壁・地面などを別のsemantic surfaceとして持つ表現。屋根形状や高さ別の外形を扱える |
| semantic surface | polygonへ「壁」「屋根」「張り出し下面」などの意味を付けたもの。形状だけのmeshとは異なる |
| polygon | 1枚の面を表す外周。穴がある場合は外周に加えてinterior ringを持つ |
| interior ring | polygon内部の穴を表す閉じた輪郭。中庭や壁面の開口を塞がないために使う |

### 建物のsemantic surface

| CityGML名 | 日本語での意味 | 現在のPhysicsでの扱い |
|---|---|---|
| `WallSurface` | 建物外周の壁面。垂直とは限らず、傾斜した外壁も含み得る | P1〜P3でcollision化 |
| `RoofSurface` | 屋根・屋上を構成する上側の面 | P1〜P3でcollision化 |
| `GroundSurface` | 建物が地面と接する底面 | DEM地形と重なるためcollision化しない |
| `OuterCeilingSurface` | ピロティ、張り出し、庇など、外部空間から見える下面 | P3でcollision化 |
| `OuterFloorSurface` | バルコニー、外廊下、高架床など、外部空間に面した上向きの床 | P3でcollision化 |
| `ClosureSurface` | geometryを閉じるための補助的な面。現実の壁とは限らない | 数を記録するだけで、現在は判定・collision化しない |

`OuterCeilingSurface`があるからといって、その下が必ず通路とは限りません。ただしLOD1の
大きな箱で一律に埋めると、存在するかもしれない外部空間を消すため、P3として慎重に扱います。

### 建物構造に関する語

| 用語 | 意味 |
|---|---|
| `BuildingPart` | 1棟を構成する棟・塔・低層部などの部分。高さや外形が異なる可能性がある |
| `BuildingInstallation` | 建物へ付属する設備・構造物。塔屋、外階段、設備などが該当し得る |
| setback | 上の階ほど外壁が内側へ後退し、建物の水平輪郭が高さによって小さくなる形 |
| overhang / 張り出し | 上部が下部より外側へ出ている形。下側に外部空間と下面が生じる |
| height-dependent profile | 建物を水平に切った輪郭が、高さによって変わること。setbackや複数棟構成を含む |
| void / 空隙 | 建物の外形内にある、物体が存在しない空間。ピロティ、アーチ下、中庭など |
| traversable space / 通過空間 | シミュレーション物体が通れる可能性のある空間。分類だけでは通行可能性を証明しない |

### Physics変換に関する語

| 用語 | 意味 |
|---|---|
| Visual | 画面へ表示する形状。細かくてもよいが、物理衝突には直接使わない |
| Physics / collision | 物体が接触・反発するための形状。見た目より軽量性と偽障害を作らないことを優先する |
| collider | collision判定へ使う形状の総称 |
| MuJoCo geom | MuJoCoが衝突や表示に使う1個の形状要素。geom数が増えると計算負荷になり得る |
| mesh | 頂点と三角形面から構成する形状 |
| triangulation | polygonを複数の三角形へ分割する処理 |
| convex / 凸 | 任意の2点を結ぶ線分が形状内部から出ない形。MuJoCoで安全に扱いやすい |
| prism / 柱体 | 2次元の面へ厚みを付けた立体。この実装では安全な凸polygon、またはその三角分割へ薄い厚みを付ける |
| OBB | Oriented Bounding Box。建物を向き付き直方体1個で近似する軽量表現 |
| wall mode | OBBで空白を埋めすぎる場合、LOD1底面外周の各辺を薄いbox wallへ変換する方式 |
| source normal | 元polygonの頂点順から計算する面の垂直方向。壁や下面へ数値的な厚みを付ける方向に使う |
| watertight | 面の継ぎ目に穴がなく、閉じた立体になっている状態。現在は各prism単位では閉じるが、建物shell全体のwatertight性は保証しない |
| heuristic / ヒューリスティック | 完全な幾何証明ではなく、surface数などから実用的に推定する判定規則 |
| Receipt | 入力、判定理由、生成数、除外数などをJSONで残す監査記録 |

`P0`〜`P3`はPLATEAUやCityGMLの公式分類ではありません。hakoniwa-envsimが
Physics戦略を選択するために定義した独自クラスです。

## 3. 判定に使う入力値

LOD1 selectionと、対応するLOD2 `bldg:Building`から次の値を数えます。

| 記号 | Receipt上の名前 | 意味 |
|---|---|---|
| `L` | `lod1_part_count` | LOD1底面polygonの数 |
| `E` | `lod1_edge_count` | LOD1底面外周とinterior ringの辺数の合計 |
| `B` | `building_part_count` | `bldg:BuildingPart`の数 |
| `I` | `building_installation_count` | `bldg:BuildingInstallation`の数 |
| `R` | `roof_polygons` | LOD2 `RoofSurface` polygon数 |
| `W` | `wall_polygons` | LOD2 `WallSurface` polygon数 |
| `G` | `ground_polygons` | LOD2 `GroundSurface` polygon数 |
| `OF` | `outer_floor_polygons` | `OuterFloorSurface` polygon数 |
| `OC` | `outer_ceiling_polygons` | `OuterCeilingSurface` polygon数 |
| `C` | `closure_polygons` | `ClosureSurface` polygon数。現在は記録のみ |
| `Hroof` | `roof_relief_m` | 全RoofSurface頂点の最大Zと最小Zの差 [m] |

高さ方向の輪郭変化を疑うための壁面数上限を、次の式で作ります。

```text
profile_limit = max(E + 4, ceil(E * profile_ratio))
profile_ratio = 1.5  # 現在のCity World既定値
```

`+4`は少数辺の建物がわずかな面分割だけでP2になることを抑え、`1.5倍`は複雑な建物で
LOD1外周に比べて壁面数が明確に増えた場合を検出します。これは幾何学的な断面計算ではなく、
semantic surface数を使うヒューリスティックです。

## 4. 判定順序

既定のLevel 3では、判定は必ず `P3 → P2 → P1 → P0` の順です。最初に一致した
クラスを採用します。`mjcf.building_physics_level`を下げた場合は、指定Levelより上の
条件を評価しません。

| Level | 判定順序 |
|---|---|
| 0 | P0 |
| 1 | P1 → P0 |
| 2 | P2 → P1 → P0 |
| 3 | P3 → P2 → P1 → P0 |

これは一度P3と判定した結果を機械的にP2へ置換する処理ではありません。Level 2なら
P3条件を無視して同じ建物を改めてP2、P1、P0の順に判定します。Visualとtextureは
Levelによらず同一です。

```text
if OC > 0 or OF > 0:
    P3
else if B > 0:
    P2
else if G > L:
    P2
else if W > profile_limit:
    P2
else if I > 0:
    P1
else if R > L:
    P1
else if Hroof > 0.5 m:
    P1
else:
    P0
```

`roof_relief_m=0.5`と`profile_ratio=1.5`はclassifier CLIの既定値です。現在の
City World buildはCLIへ別値を渡さないため、この既定値を使用します。classifierを直接
実行する場合は`--roof-relief-m`と`--profile-ratio`で変更できます。

上位クラスは下位クラスの特徴を持つ場合があります。例えばP3建物が多数のWallSurfaceを
持っていても、P2を経由せずP3になります。P3のcollision実装がP2の壁・屋根処理を内包します。

### 札幌実データでの判定例

| 結果 | 主な入力値 | 判定の読み方 |
|---|---|---|
| P0 | `L=1, E=6, R=1, W=4, Hroof≒0m, OC=OF=0` | P3/P2/P1条件がすべて偽なのでP0 |
| P1 | `L=1, E=6, R=3, W=8, Hroof=0.25m` | `profile_limit=max(10,9)=10`なのでP2ではないが、`R>L`なのでP1 |
| P2 | `L=1, E=22, W=119` | `profile_limit=max(26,33)=33`に対し`119>33`なのでP2 |
| P3 | `OC=6, W=112, R=30` | `OC>0`を最初に検出するため、P2相当の複雑さも含めてP3 |

この例が示すように、P番号は単純な複雑度スコアではありません。必要なsemantic surfaceの
種類によって、適用するPhysics生成戦略を選んだ結果です。

## 5. 各クラスの意味と適用処理

### P0: LOD1で十分と判定

条件は「P1〜P3の条件へ1つも該当しないこと」です。

採用するPhysics:

- 既存のLOD1 OBB、または元の外周に沿ったwall + roof
- OBBとwallの選択は`waste_threshold`で決まる
- LOD2 surface meshは追加しない

この判定が意味するのは「現在観測しているsemantic値からLOD1との差を検出しなかった」であり、
LOD1とLOD2が数学的に同一であることの証明ではありません。

### P1: 上端・屋根だけにLOD2が必要

P2/P3ではなく、次のいずれかへ該当した建物です。

- `BuildingInstallation`が1個以上ある
- `RoofSurface`数がLOD1 part数より多い: `R > L`
- 屋根頂点の高低差が0.5mを超える: `Hroof > 0.5`

採用するPhysics:

- 従来のLOD1 colliderを建物単位で除外
- LOD2 `WallSurface`と`RoofSurface`を解析
- 穴がなく、同一平面上で凸なsource polygonは、polygon全体を1個の薄い凸prismへ変換
- 凹形状、穴あり、非平面polygonは、形状を変えず三角prismへフォールバック
- `GroundSurface`はDEMと重複するため生成しない

既知の注意:

- `R > L`は、同一平面の屋根がデータ上複数polygonへ分割されただけでも成立する。このため
  見た目の差が小さいP1が生じ得る
- `BuildingInstallation`は分類条件には使うが、その`lod2Geometry`を明示的には抽出していない。
  installation内部にWall/Roof semantic surfaceがあれば建物の子孫面として含まれ得るが、
  一般的なinstallation geometry対応とはみなさない。札幌のP1実例では`I=0`で未検証

### P2: 高さによって外形が変わる

P3ではなく、次のいずれかへ該当した建物です。

- `BuildingPart`が1個以上ある
- LOD2 GroundSurface数がLOD1 part数より多い: `G > L`
- LOD2 WallSurface数が上限を超える: `W > profile_limit`

採用するPhysics:

- P1と同じくLOD2 `WallSurface`と`RoofSurface`を薄い凸prismへ変換する。
  穴なし・平面・凸polygonは1 Colliderへ統合し、それ以外は三角prismを維持する
- 全高を1つのLOD1 prismにしないため、setbackや階層ごとの輪郭を保持できる
- source polygon自体が細かい、凹、穴あり、または非平面の建物ではgeom数が大きくなる

既知の注意:

- WallSurface数は高さ別断面を直接比較した値ではない。データ作成時のpolygon分割方法でも増える
- `G > L`でP2になってもGroundSurface自体はDEMとの二重collisionを避けるため生成しない
- 札幌実例の従来値は37 geomsから548 geomsへ増えていた。現在は安全に統合できる
  source polygonを1 Colliderへまとめ、削減率をReceiptへ記録する

### P3: 張り出し下面または高架床を保持する

次のいずれかへ該当した建物です。

- `OuterCeilingSurface`が1面以上ある
- `OuterFloorSurface`が1面以上ある

これらのsurfaceは、LOD1 solidで埋めると本来の空間を偽の障害物にする可能性があるため、
最優先でP3にします。

採用するPhysics:

- P2と同じLOD2 `WallSurface` / `RoofSurface`
- 実データに存在する`OuterCeilingSurface` / `OuterFloorSurface`
- 各面を薄い凸prismへ変換
- `GroundSurface`を生成せず、建物全体をsolid化しない

これにより張り出し下面にはcollisionを持たせつつ、その下の空間を一律に埋めません。

既知の注意:

- P3は「その空間を必ず通行できる」という意味ではない。壁など他のsource surfaceが通路を
  閉じている場合もある
- 札幌ではOuterCeilingSurfaceを持つ3棟を検証したが、OuterFloorSurfaceを持つ実データは
  今回の範囲に存在しなかった。OuterFloorは人工fixtureでの構造確認が今後必要
- 穴やアーチがOuterCeiling/OuterFloor以外の表現だけで記述された場合は、P3判定から漏れる

## 6. surfaceからcollisionを作る共通規則

P1〜P3では次の規則を共有します。

- CRSはEPSG:6697を必須とし、query中心のlocal ENUからMJCF
  `X=North, Y=-East, Z=Up`へ変換する
- RoofSurfaceは原則としてworld Zの負方向へ厚みを付ける。ただし実データ上ほぼ垂直な
  RoofSurfaceでは-Z押し出しが退化するため、面法線方向へフォールバックする
- Wall/OuterCeiling/OuterFloorはsource polygonまたはfallback三角形の法線方向へ厚みを付ける
- 現在の厚みは0.02m。歴史的なCLI名`--roof-thickness`が全semantic surfaceの厚みを兼ねる
- 面積が`1e-6 m²`未満、または`2*area/max_edge² < 1e-6`の三角形は数値的退化として除外する
- 除外数はsurface種別ごとに`building-physics-application.json`へ記録する
- source polygonにinterior ringがある場合、triangulationで穴を保持する
- P1/P2/P3では、同一source polygonが穴なし・平面・凸の場合だけ、そのpolygon全体を
  1個のconvex prismにする。建物やsemantic surfaceを跨いだ統合はしない
- 凹polygon、interior ring付きpolygon、非平面polygonは三角prismへフォールバックする。
  convex hullで凹みや穴を埋める近似は行わない
- P3も各source polygon内だけで統合するため、OuterCeiling/OuterFloorが表す
  空洞・張り出しをsurface間の統合で塞がない

`mjcf.building_collider_reduction`は次の4モードです。

| mode | 統合範囲 |
|---|---|
| `safe` | 上記のとおり、同一source polygon内だけ |
| `coplanar-union` | すでに凸なsource polygon Colliderについて、同一建物・surface種別・向き・平面の隣接面も統合 |
| `convex-decompose` | `coplanar-union`に加え、凹・穴ありsource polygonの三角Colliderを同一source polygon内で少数の凸領域へ再構成 |
| `tolerant-planar` | `convex-decompose`後、同一建物のWallSurfaceだけを対象に、法線差2度以内・派生平面まで最大5cm以内の隣接凸面を統合 |

後三者も、隣接する2形状の幾何学的和集合が単一の穴なし凸polygonになる組だけを段階的に
置換します。凸包、頂点snap、隙間補間は使いません。したがって空白領域を新たなcollisionで
埋めません。`tolerant-planar`もXY方向の隙間を補間せず、既存boxがmeshへ変わる統合を拒否します。
5cmは元頂点から派生平面までの最大変位です。各モードは決定的なgreedy統合であり、数学的な
最小分割は保証しません。

生成した凸prismのうち、元面が厳密な長方形で、全頂点の厚み方向が一致し、厚みが面へ直交する
ものはMuJoCo `geom type="box"`として書き出します。傾斜面をworld-Z方向へ厚くしたsheared prism、
長方形でない凸polygon、数値条件を満たさないものはmeshを維持します。これにより形状を変えず、
MuJoCoのprimitive collisionを利用します。クラス別box/mesh数は
`building-physics-application.json`の`collider_geom_types.by_class`へ記録します。

### 許容誤差付き平面化の評価

`building_collider_tolerance_profiler.py`は、現在のPhysics形状を変更せず、
`convex-decompose`後のColliderに許容誤差付き平面化を仮適用した場合の削減候補を
JSONへ記録する解析専用ツールです。MJCF/GLBは生成・変更しません。

```bash
python src/city_pipeline/building_collider_tolerance_profiler.py \
  --selection BUILD_DIR/city-world-lod1.json \
  --classification BUILD_DIR/components/buildings/building-physics-classification.json \
  --world-frame BUILD_DIR/components/terrain/world-frame.json \
  --out tolerance-profile.json
```

既定では`0 / 5 / 10 / 20 / 50 cm`を評価します。`0 cm`との差分が、数値誤差ではなく
近似平面化によって得られる効果です。結果にはCollider削減数、Surface種別ごとの削減数、
box/mesh推定数、元頂点から派生平面までの最大変位を記録します。これはruntime高速化を
保証する値ではなく、`tolerant-planar`の適用可否や閾値を判断するための資料です。
`--surface-kinds WallSurface`で壁だけを評価でき、`--preserve-box-primitives`を指定すると
既存boxをmeshへ変える統合候補を除外できます。

`building-physics-application.json`の`collider_optimization`には、クラスごとに
`triangles_before`、`colliders_after`、`merged_group_count`、
`rejected_concave_count`、`triangles_eliminated`、`reduction_ratio`、および
フォールバック理由を記録します。`reduction_ratio=0.5`は、従来の三角prism数と
比較してCollider数が50%減ったことを意味します。
削減モードではさらに`reduction_mode`、`colliders_before_reduction`、`convex_merge_count`、
`convex_merge_colliders_eliminated`、拒否理由数を記録します。

## 7. 現在、判定・Physics化していないもの

次の要素は、P0〜P3が`applied`でも対応完了を意味しません。

- 建物内部の部屋、床、階段、構造材
- Door/Window専用geometryのcollision
- `ClosureSurface`のcollision。数はReceiptへ記録するが判定には未使用
- BuildingInstallation自身のcollision
- LOD3/LOD4建物geometry
- polygonの自己交差、surface間の隙間、watertight性の包括検証
- sourceの法線向きが全surfaceで統一されていることの検証
- 実際の経路探索による通行可能性の証明
- 数学的な最小個数を保証する大域的convex decomposition
- P1の「複数だが実質同一平面の屋根」をP0へ戻す判定
- P2の高さ別cross-sectionを直接比較する判定

## 8. 出力と読み方

| ファイル | 役割 |
|---|---|
| `building-physics-classification.json` | クラス、判定理由、入力値、閾値 |
| `building-physics-classes.glb` | source LOD2形状をP0=緑、P1=黄、P2=橙、P3=紫で表示 |
| `building-physics-application.json` | 実際に採用した戦略、置換前primitive数、生成geom数、退化面除外数 |
| `buildings.xml` | 生成された建物MJCF |

`classification.json`の`classification_only: true`は、classifier自身がPhysicsを変更しない
ことを表します。その直後のapplication段階でclass別collisionを生成するため、実際の適用状態は
必ず`building-physics-application.json`の`class_status`を確認してください。

## 9. 変更時に守る契約

分類条件を変更する場合は、少なくとも次を同時に更新します。

1. `building_physics_classifier.py`の判定と`CLASS_DEFINITIONS`
2. この文書の判定式と既知の不足
3. classifier unit test
4. 札幌など実データのclass件数と適用Receipt
5. MuJoCo native load testと、P3ではunder-space acceptance test

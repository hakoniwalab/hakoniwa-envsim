# hakoniwa-envsim

ドローンやロボットのシミュレーションでは、 **環境要因（風・温度・電波・境界条件など）** を取り込むことが不可欠です。

箱庭環境シミュレーションでは、これらの環境データを「直感的に作成・編集できるモデル」として定義し、可視化や高速検索を組み合わせることで、現実に近い状況を再現することを目指します。

![image](/docs/images/overview.png)

# ユースケース
## ユースケース 1: 環境データ作成（Creator）

- ユーザが直感的に環境データを作成できるように支援する。
- モデルは JSON 形式で定義され、エリアごとに温度・気圧・風速・電波強度などを持つ。
- 専門知識がなくても扱いやすいように、シンプルなパラメータ指定を基本とする。

## ユースケース 2: 環境データの可視化（Visualizer）

- 作成した環境データを、色や矢印などで直感的に理解できる形に可視化する。
- Pythonツールで簡易に実装可能。
- シミュレーション前に「このエリアは風が強い」「電波が弱い」といった特徴を一目で確認できる。

## ユースケース 3: シミュレーション利用データ（詳細化）

- シミュレーション実行時には、より詳細化された「シミュレーション用環境情報」に変換される。
- Creatorで作られた粗いモデルを元に、シミュレータが直接利用できる粒度に展開する。

## ユースケース 4: 高速検索（FastSearcher）

- エリア情報は JSON でフラットに表現されるため、大規模化すると検索負荷が高くなる。
- そのため、高速検索ライブラリを利用して「位置→環境値」を効率的に取得する。
- 例: ドローンがある座標にいるとき、その座標の温度・風速を即座に返す。

## ユースケース 5: シミュレーション統合

- 最終的に「環境データ（詳細化 + 高速検索）」が箱庭シミュレーションに統合される。
- 機体制御や経路計画の挙動を、より現実的な環境条件下で検証できる。
- 教育、研究、産業用途において、シミュレーションの信頼性向上に寄与する。




# スキーマ構成

本リポジトリでは、**人が作るCreatorモデル** と **シミュレータが読む実行モデル** を分離します。

| レイヤ        | 目的             | ファイル                            | 要点                                     |
| ---------- | -------------- | ------------------------------- | -------------------------------------- |
| Creatorモデル | 直感的に編集する元データ   | `environment_model.schema.json` | CRS/単位、グリッド、ベース風、ゾーン効果、時変（周期/乱流）       |
| 実行モデル（分割）  | シミュレーション実行に最適化 | `space_area.schema.json`        | 3D領域（AABB）集合                           |
| 〃          | 〃              | `area_properties.schema.json`   | エリア非依存の基準プロパティ（`id` 必須）                |
| 〃          | 〃              | `link.schema.json`              | `area_id` ↔ `area_property_id` の疎結合リンク |
| 〃（任意）      | 〃              | `boundary.schema.json`          | 地面/天井/壁などの境界定義                         |

**設計意図**

*   **Creatorモデル (`environment_model.schema.json`)**:
    *   **人間による編集しやすさを最優先**します。`"wind_speed_ms"` のように、人間が読んで意味のわかるフィールド名や単位 (`_ms`, `_deg`) を採用しています。
    *   空間全体に適用される `base` 環境（基本風速、気温など）と、局所的な変化を定義する `zones`（ゾーン）の組み合わせで、直感的に環境を構築できます。
    *   ゾーンには `vortex`（渦）や `turbulence`（乱気流）といった複雑な効果も簡単なパラメータで設定可能です。

*   **実行モデル (`space_area.schema.json` など)**:
    *   シミュレータが高速に検索・計算処理できるよう、**機械的な効率を最優先**します。
    *   領域情報を3DのAABB（Axis-Aligned Bounding Box）形式で持ち、プロパティとはIDでリンクするなど、計算に最適化された構造です。

*   両者はツールによって変換されます（Creatorモデル → 実行モデル）。これにより、ユーザーは直感的なモデルを編集するだけで、シミュレーションに最適化されたデータを利用できます。

# サンプルシナリオ: 神戸港

`examples/datasets/kobe/environment_model.json` は、Creatorモデルの実践的なサンプルです。
このモデルは、神戸港周辺の複雑な環境を模擬しています。

**シナリオの概要:**

1.  **ベース環境**:
    *   空間全体には、東向きに `4.0m/s` の一定の風が吹いています。
    *   気温は `20℃`、GPS強度は `1.0`（良好）に設定されています。

2.  **ゾーンによる局所的な変化**:
    *   `10個` の長方形のゾーン (`block1`～`block10`) が定義されており、それぞれがベース環境を上書きします。
    *   **風の変化**: `block1` では風向きと風速が `absolute`（絶対値）で指定され、ベースの東風とは異なる風が吹きます。
    *   **乱気流**: `block2` 以降のゾーンでは、`turbulence`（乱気流）が設定されています。`std_ms` の値を変えることで、場所ごとに乱気流の強弱を表現しています。
    *   **GPS遮蔽**: 各ゾーンでは `gps_abs` によりGPS強度が `0.6`～`0.8` に設定され、ビル街などで想定されるGPSの受信阻害を模擬しています。
    *   **優先度**: `priority` を設定することで、ゾーンが重なった場合にどちらの効果を優先させるかを制御できます。

このように、ベース環境と複数のゾーンをレイヤーのように重ね合わせることで、現実世界に近い多様で複雑なシミュレーション環境を構築できます。

# Creatorモデルの詳細設定

`environment_model.json` ファイルを自作するための、主要なパラメータと設定例を解説します。

## トップレベル構造

まず、全体の構造は以下のようになります。

```json
{
  "version": "0.1",
  "meta": { ... },
  "grid": { ... },
  "base": { ... },
  "zones": [ ... ]
}
```

---

## `base`: ベース環境

空間全体に適用される均一な環境を定義します。

- **`wind`**: ベースとなる風。
  - `vector_ms`: `[x, y, z]` の風速ベクトル (m/s) で指定。
  - または `dir_deg` (風向, 0-360度) と `speed_ms` (風速, m/s) の組み合わせでも指定可能。
- **`temperature_C`**: 気温 (摂氏)。
- **`pressure_atm`**: 気圧 (atm)。
- **`gps_strength`**: GPS/GNSSの受信強度。`1.0` (良好) から `0.0` (完全遮蔽) までの数値。

**設定例:**
```json
"base": {
  "wind": { "vector_ms": [-5.0, 0.0, 0.0] },
  "temperature_C": 15.0,
  "pressure_atm": 1.013,
  "gps_strength": 1.0
}
```
> この例では、西から5.0m/sの風が吹いていることになります (`axis`が`ENU`の場合)。

---

## `zones`: 局所環境

`base`環境に重ね合わせる形で、局所的な環境変化を定義します。ゾーンは配列になっており、複数のゾーンを定義できます。

```json
"zones": [
  {
    "name": "My First Zone",
    "shape": { ... },
    "effect": { ... },
    "priority": 1
  }
]
```

### `shape`: ゾーンの形状

- **`rect` (長方形)**:
  - `min_m`: `[x, y]` 形式で、長方形の最小座標を指定。
  - `max_m`: `[x, y]` 形式で、長方形の最大座標を指定。
  - `center_m` と `size_m` の組み合わせも可能です。
- **`circle` (円)**:
  - `center_m`: `[x, y]` 形式で、円の中心座標を指定。
  - `radius_m`: 円の半径 (m) を指定。

**設定例:**
```json
"shape": { "rect": { "min_m": [100, 100], "max_m": [200, 200] } }
```
```json
"shape": { "circle": { "center_m": [300, 300], "radius_m": 50 } }
```

### `effect`: ゾーンの効果

ゾーン内でどのような環境変化を起こすかを定義します。`mode`によって挙動が変わります。

#### `mode: "absolute"`
ベース環境を完全に無視し、指定した値で環境を **上書き** します。

- `wind_ms`: `[x, y, z]` の風速ベクトル (m/s) を指定。
- `gps_abs`: GPS強度を `0.0`～`1.0` の絶対値で指定。

**設定例:**
```json
"effect": {
  "mode": "absolute",
  "wind_ms": [0.0, 10.0, 0.0],
  "gps_abs": 0.1
}
```
> このゾーン内では、ベースの風速に関わらず、南から10m/sの強風が吹き、GPS強度も0.1に低下します。

#### `mode: "add"`
ベース環境の値に、指定した値を **加算** します。

- `add_ms`: `[x, y, z]` の風速ベクトル (m/s) を加算。
- `gps_add`: GPS強度に値を加算 (結果は `0.0`～`1.0` の範囲にクリップされます)。

**設定例:**
```json
"effect": {
  "mode": "add",
  "add_ms": [0.0, 0.0, 2.0]
}
```
> このゾーン内では、ベースの風に加えて、2.0m/sの上昇気流が発生します。

#### `mode: "scale"`
ベース環境の値に、指定した値を **乗算** します。

- `scale`: 風速に乗算する倍率。
- `gps_scale`: GPS強度に乗算する倍率。

**設定例:**
```json
"effect": {
  "mode": "scale",
  "scale": 1.5
}
```
> このゾーン内では、ベースの風が1.5倍に強まります。

#### `mode: "turbulence"`
指定した強さの **乱気流** を発生させます。

- `turbulence`: 乱気流のパラメータ。
  - `type`: 乱気流のモデル。`"gauss"` (ガウスノイズ), `"perlin"` (パーリンノイズ), `"ou"` (Ornstein-Uhlenbeck) から選択。
  - `std_ms`: 乱気流の強さの標準偏差 (m/s)。値が大きいほど激しい乱気流になります。
  - `seed`: 乱数のシード値。

**設定例:**
```json
"effect": {
  "mode": "turbulence",
  "turbulence": { "type": "perlin", "std_ms": 2.5, "seed": 42 }
}
```
> このゾーン内では、パーリンノイズに基づいた強さ2.5m/sの乱気流が発生します。

#### `mode: "vortex"`
指定した位置に **渦** を発生させます。

- `vortex`: 渦のパラメータ。
  - `center_m`: `[x, y]` 形式で、渦の中心座標を指定。
  - `gain`: 渦の強さ（ゲイン）。
  - `clockwise`: `true`で時計回り、`false`で反時計回り。
  - `decay`: 渦の中心からの距離による減衰モデル。`"inv_r"` (距離に反比例) または `"gaussian"` (ガウス減衰) を選択。
  - `max_ms`: 渦による最大風速を制限 (オプション)。

**設定例:**
```json
"effect": {
  "mode": "vortex",
  "vortex": {
    "center_m": [500, 500],
    "gain": 80.0,
    "clockwise": true,
    "decay": "gaussian",
    "sigma_m": 50.0
  }
}
```
> このゾーンの中心 `[500, 500]` に、時計回りの渦が発生します。渦の強さはガウス分布に従って減衰します。

# Creatorツールの実行方法

Creatorツールは、人間が編集可能な `environment_model.json` から、シミュレータが利用する効率的な実行モデル（`area.json`, `property.json`, `link.json`）を生成するためのコマンドラインツールです。

## 実行コマンド

プロジェクトのルートディレクトリから、以下のコマンドを実行します。

```bash
python -m hakoniwa_envsim.creator.creator --infile <入力ファイルパス> --outdir <出力ディレクトリパス>
```

### 引数

*   `--infile`: 読み込む `environment_model.json` のパスを指定します。
*   `--outdir`: 生成されたファイル（`area.json`, `property.json`, `link.json`）を保存するディレクトリを指定します。指定したディレクトリが存在しない場合は自動的に作成されます。
*   `--no-zones` (オプション): このフラグを付けると、`environment_model.json` 内の `zones` 定義を無視して実行モデルを生成します。

## 実行例: 神戸シナリオの生成

`examples/datasets/kobe/environment_model.json` を使って、対応する実行モデルを生成する例です。

```bash
python -m hakoniwa_envsim.creator.creator \
  --infile examples/datasets/kobe/environment_model.json \
  --outdir examples/datasets/kobe/generated
```

実行が成功すると、`examples/datasets/kobe/generated/` ディレクトリに以下の3つのファイルが生成（または上書き）されます。

*   `area.json`: 空間のグリッド分割情報
*   `property.json`: 各グリッドの環境プロパティ（風速、GPS強度など）
*   `link.json`: `area.json` と `property.json` を関連付けるリンク情報

# Visualizerツールの実行方法

Visualizerツールは、Creatorが生成した実行モデル（`area.json`など）を2Dで可視化するためのツールです。
風速のベクトル表示や、温度・GPS強度といったプロパティのヒートマップ表示が可能です。さらに、OpenStreetMapなどの実世界の地図上に環境データを重ねて表示するオーバーレイ機能を備えています。

## 実行方法

ツールの実行は、設定ファイルを指定する形で行うのが最も簡単です。プロジェクトのルートディレクトリから、以下のコマンドを実行します。

```bash
python -m hakoniwa_envsim.visualizer.plot2d --config <設定ファイルパス>
```

## 設定ファイル

`--config`で指定するJSONファイルで、可視化の挙動を細かく制御できます。
`src/kobe.plot2d.json` を参考に、主要な設定項目を解説します。

```json
{
  "area": "../examples/datasets/kobe/generated/area.json",
  "property": "../examples/datasets/kobe/generated/property.json",
  "link": "../examples/datasets/kobe/generated/link.json",

  "overlay_map": true,
  "origin_lat": 34.65436065,
  "origin_lon": 135.16060138,

  "mode": "gps",
  "tiles": "OpenStreetMap.Mapnik",
  "zoom": null,

  "markers": [
    {"lat": 34.65436065, "lon": 135.16060138, "label": "Origin"}
  ]
}
```

### 主要な設定項目

*   **`area`, `property`, `link`**:
    Creatorが生成した実行モデルファイルのパスを指定します。

*   **`overlay_map`**:
    `true`に設定すると、背景に地図タイルをオーバーレイ表示します。

*   **`origin_lat`, `origin_lon`**:
    地図をオーバーレイする際の、シミュレーション座標の原点に対応する緯度・経度を指定します。`environment_model.json`の`meta.origin_latlon`と同じ値を設定するのが一般的です。

*   **`mode`**:
    色で表現する物理量を指定します。`"gps"`（GPS強度）または `"temperature"`（温度）が選択可能です。

*   **`tiles`**:
    表示する地図タイルの種類を指定します。`"OpenStreetMap.Mapnik"`, `"Esri.WorldImagery"` などが利用できます。

*   **`zoom`**:
    地図のズームレベルを整数で指定します。`null`に設定すると、グリッドの解像度から最適なズームレベルが自動で計算されます。

*   **`markers`**:
    地図上に表示するマーカー（目印）を配列で指定できます。各マーカーは緯度(`lat`)、経度(`lon`)、そして任意のラベル(`label`)を持つことができます。

## 実行例: 神戸シナリオの可視化

`src/show.bash` を実行すると、`src/kobe.plot2d.json` の設定に基づいて神戸シナリオの可視化が行われます。

```bash
# srcディレクトリに移動して実行
cd src
./show.bash
```

または、ルートディレクトリから直接コマンドを実行することもできます。

```bash
python -m hakoniwa_envsim.visualizer.plot2d --config src/kobe.plot2d.json
```

実行すると、神戸の地図上にGPS強度の分布が色で表示され、各エリアの風向きが矢印で示された画像が生成されます。

---

## バリデーション手順

### CLIで検証

```bash
# jsonschema-cli を使う例
pipx install check-jsonschema

# Creatorモデルを検証（environment_model）
check-jsonschema --schemafile src/hakoniwa_envsim/schemas/environment_model.schema.json \
  examples/creator/kobe_bay.env.json

# 実行モデル（分割）を検証
check-jsonschema --schemafile src/hakoniwa_envsim/schemas/space_area.schema.json \
  examples/models/space_areas.json

check-jsonschema --schemafile src/hakoniwa_envsim/schemas/area_properties.schema.json \
  examples/models/area_properties.json

check-jsonschema --schemafile src/hakoniwa_envsim/schemas/link.schema.json \
  examples/models/links.json
```

### Pythonで検証

```python
from jsonschema import validate
import json

schema = json.load(open("src/hakoniwa_envsim/schemas/space_area.schema.json"))
data = json.load(open("examples/models/space_areas.json"))
validate(instance=data, schema=schema)
print("OK")
```


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

* Creatorモデルは **人間の編集体験を最優先**（意味がわかるフィールド名・単位付き）。
* 実行モデルは **検索と計算効率を優先**（AABB + 参照リンク + 簡潔な型）。
* 両者はツールで変換（Creator → 実行モデル）。Visualizer/Creator/ConverterはPythonで提供予定。

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


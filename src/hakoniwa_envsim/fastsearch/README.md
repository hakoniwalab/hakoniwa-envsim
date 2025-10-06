# 🧭 fastsearch 開発メモ

*— 未来の自分へ、もう一度このロジックを思い出すために*

---

## 🌍 概要

**fastsearch** は、箱庭の環境シミュレーションで
「この座標がどのエリアに属するか？」を高速に判定するための
**BVH（Bounding Volume Hierarchy）ベースの空間探索ライブラリ**。

目的は単純：

* 数百〜数千のAABBから、1点の属するエリアを高速に特定したい。
* Python実装でも 1 ms レベルの応答を目指す。

そのために、

* `builder.py` で空間を再帰的に分割してツリー化し
* `search.py` で葉ノードを効率的に探索する
* `envbuilder.py` と `envsearch.py` で環境全体（area, link, property）を統合する
  という構造にした。

---

## 🧩 モジュール構成と役割

| ファイル              | 役割              | 主な中身                                                            |
| ----------------- | --------------- | --------------------------------------------------------------- |
| **builder.py**    | BVHツリーの構築       | `AABB`, `Node`, `build_bvh()`                                   |
| **search.py**     | 点検索アルゴリズム       | `search_point()`, `point_in_aabb()`                             |
| **envbuilder.py** | 環境データ統合         | `Environment` クラス、area/link/property結合                          |
| **envsearch.py**  | 検索CLI           | `python envsearch.py area.json link.json property.json 8 x y z` |
| **estimator.py**  | 性能試験・depth探索数測定 | BVH深さごとのノード訪問数などを出力                                             |
| **analysis.py**   | 解析補助            | 結果整理、統計系ユーティリティ                                                 |
| **test_*.py**     | 検証スクリプト群        | builder・search・envsearchの単体検証                                   |

---

## 🧮 アルゴリズムの骨格メモ

### 🔹 build_bvh()

* すべてのAABBの中心を取り、**最も広がりの大きい軸(axis)** で分割する。
* 分割のキーは `"minx"`, `"miny"`, `"minz"` のいずれか。
* 再帰的に left / right を構築し、
  `leaf_capacity` 以下になったらリーフノードを生成。

```python
if len(areas) <= leaf_capacity or depth >= max_depth:
    return Node(..., is_leaf=True, areas=areas[:])
```

### 🔹 search_point()

* `point_in_aabb` による包含判定を基本とする。
* 再帰探索時は `elif` にして、片側命中時に即リターン（探索削減）。
* `precise=True` のときはリーフ内の全AABBを精査。

```python
if node.is_leaf:
    for a in node.areas:
        if point_in_aabb(a, x, y, z):
            found.append(a.id)
            return found
```

### 🔹 重要な理解メモ

| 疑問                        | 結論                                 |
| ------------------------- | ---------------------------------- |
| axis分割してるのに、探索時にaxis見てない？ | `point_in_aabb()` が axis 含むため不要。   |
| leafでマージして良い？             | leaf_capacityで制御。1個なら単一AABB扱い。     |
| early returnしても正しい？       | AABBが重ならなければ常に正しい。                 |
| Pythonで遅くない？              | numpy演算＋C実装部が効く。704AABB規模なら1 ms以下。 |

---

## 🧭 Environment 構造（envbuilder / envsearch）

`Environment` は 3種類のデータをまとめて扱うクラス：

* `area.json`: 空間定義（min/max座標）
* `link.json`: areaとpropertyを結ぶマッピング
* `property.json`: 物理パラメータ（風速・温度など）

```python
env = Environment(area_json, link_json, prop_json, max_depth=8)
prop = env.get_property_at(x=4000, y=6000, z=20)
```

### 主要メソッド

| 関数                         | 概要                            |
| -------------------------- | ----------------------------- |
| `get_property_at(x, y, z)` | 座標からエリアを特定し、対応するproperty辞書を返す |
| `debug_at(x, y, z)`        | 検索経路・ヒットノード・探索数などを出力          |
| `dump_area_map()`          | area → property のリンクテーブルを表示   |
| `validate_integrity()`     | area / link / property の整合性検査 |

---

## 🧪 実験結果（探索効率）

| Depth D | 訪問ノード数 | 備考                |
| ------- | ------ | ----------------- |
| 1       | 331    | 全探索に近い            |
| 2       | 162    |                   |
| 3       | 80     |                   |
| 4       | 40     |                   |
| 5       | 21     |                   |
| 6       | 13     |                   |
| 7       | 10     | ✅ 最適付近            |
| 8       | 12     | slight over-split |
| 9       | 12     | 安定値               |

➡ **D=7前後で探索ノード数が最小化。**
Pythonでも即応。Numba化不要レベル。

---

## 💻 実行例

```bash
# エリア単体検索
python -m fastsearch.test_search ../../examples/datasets/kobe/generated/area.json 8 4000 6000 20
📦 読み込み: area.json
🧮 AABB数: 704
🎯 検索座標: (4000.00, 6000.00, 20.00)
🔍 含まれるエリア数: 1
   探索ノード訪問数: 10
   ヒットエリアID一覧:
  - area_30_20

# 環境プロパティ検索
python fastsearch/envsearch.py area.json link.json property.json 8 4000 6000 20
✅ Hit area: area_30_20
🧾 area_property:
  - wind_velocity: [4.0, 0.0, 0.0]
  - temperature: 20.0
  - sea_level_atm: 1.0
  - gps_strength: 1.0
```

---

## ⚙️ テストスクリプト対応表

| ファイル                | 内容                       |
| ------------------- | ------------------------ |
| `test_builder.py`   | BVH構築テスト                 |
| `test_search.py`    | 点探索の動作確認                 |
| `test_envsearch.py` | area→link→property統合動作確認 |

---

## 🧠 今後の拡張メモ

* Numba or Cython による高速化版（`fastsearch.c`）
* 並列探索（複数座標の一括クエリ）
* 可視化モジュール（matplotlib 3D or Unity連携）
* UAVシミュレーションとの統合 (`envsearch` → DroneSim環境入力)

---

## ✍️ 開発後記

> 「これ本当にPythonで速いの？」
> ——そう思ったけど、BVH構造ってシンプルに強い。
> 軸分割＋早期リターン＋非重複AABB でここまで来る。
>
> あのとき悩んだ axis 判定も、結局 `point_in_aabb` が全てを兼ねてた。
> 数学的には「AABBの包含関数が軸分離検査を内包している」ってこと。
>
> 将来的にC実装化するとしても、**このREADMEだけで頭を戻せるように**、
> 今回は思考の全軌跡を残す。

---

🪴 **記録者:** たかし
📅 **最終更新:** 2025-10-07


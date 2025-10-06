# asset/__init__.py
"""
Asset layer: hakoniwa PDU と fastsearch を橋渡しする薄いラッパ群。

- DroneIO: 1機ぶんの入出力（pos 読み、disturb 書き）
- DroneManager: config(json) を解析して DroneIO のリストを提供
- EnvRuntime: Environment と PduManager の初期化・保持
"""
__all__ = ["drone_io", "drone_manager", "env_runtime"]

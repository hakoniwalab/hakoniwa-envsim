# envasset.py
"""
箱庭環境アセット (HakoEnv)
-----------------------------------
BVHベースのfastsearchを用いて、
ドローン位置から環境パラメータを動的に導出し、
PDUにDisturbanceとして書き戻す。

依存モジュール:
  - hakopy
  - hakoniwa_pdu
  - fastsearch
  - asset.{drone_io, drone_manager, env_runtime}
"""

import sys
import time
import hakopy

from hakoniwa_envsim.asset.drone_manager import DroneManager
from hakoniwa_envsim.asset.drone_io import DroneIO
from hakoniwa_envsim.asset.env_runtime import EnvRuntime


# === globals ===
delta_time_usec = 0
config_path = ''
area_config_dir = ''
runtime: EnvRuntime = None
manager: DroneManager = None


# === 基本コールバック ===
def my_on_initialize(context):
    print("[EnvAsset] Initialize event")
    return 0


def my_on_reset(context):
    print("[EnvAsset] Reset event")
    return 0


def my_sleep():
    """箱庭シミュレータのクロックに同期してsleep"""
    global delta_time_usec
    if not hakopy.usleep(delta_time_usec):
        return False
    time.sleep(delta_time_usec / 1_000_000.0)
    return True


# === メイン処理 ===
def on_manual_timing_control(context):
    """
    手動タイミング制御ループ。
    各ドローン位置を監視し、環境プロパティから外乱(風/温度/気圧)をPDUに書き戻す。
    """
    global runtime, manager, config_path, area_config_dir
    print("[EnvAsset] Start Environment Control")

    # --- 初期化 ---
    runtime = EnvRuntime.init(config_path, area_config_dir, depth=8, leaf_capacity=1)
    manager = DroneManager.from_config(config_path)

    print(f"[EnvAsset] Environment loaded: areas={len(runtime.env.areas)} links={len(runtime.env.links)}")
    print(f"[EnvAsset] Drones detected: {len(manager.drones)}")

    # --- メインループ ---
    while True:
        if not my_sleep():
            break

        runtime.pdu.run_nowait()

        # 各ドローンごとに処理
        for d in manager.drones:
            pose = d.read_pose(runtime.pdu)
            if pose is None:
                continue

            x, y, z = float(pose.linear.x), float(pose.linear.y), float(pose.linear.z)
            area_id, props = runtime.env.get_property_at(x, y, z)

            # print(f"[EnvAsset] Drone '{d.name}' at ({x:.2f},{y:.2f},{z:.2f}) in area '{area_id}' with props {props}")

            # プロパティ → Disturbance
            disturbance = DroneIO.make_disturbance(props)
            ok = d.write_disturbance(runtime.pdu, disturbance)
            if not ok:
                print(f"[WARN] Failed to write disturbance for {d.name}")

    return 0


# === hakopy 登録 ===
my_callback = {
    'on_initialize': my_on_initialize,
    'on_simulation_step': None,
    'on_manual_timing_control': on_manual_timing_control,
    'on_reset': my_on_reset,
}


# === エントリポイント ===
def main():
    global delta_time_usec, config_path, area_config_dir

    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <config_path> <delta_time_msec> <area_config_dir>")
        return 1

    config_path = sys.argv[1]
    delta_time_usec = int(sys.argv[2]) * 1000
    area_config_dir = sys.argv[3]

    asset_name = 'HakoEnv'

    print(f"[EnvAsset] Registering asset '{asset_name}'")
    ret = hakopy.asset_register(asset_name, config_path, my_callback,
                                delta_time_usec, hakopy.HAKO_ASSET_MODEL_PLANT)
    if not ret:
        print("[ERROR] Failed to register asset")
        return 1

    print("[EnvAsset] Start simulation...")
    ret = hakopy.start()
    print(f"[EnvAsset] DONE (status={ret})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

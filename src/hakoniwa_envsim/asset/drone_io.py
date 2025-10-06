# asset/drone_io.py
from dataclasses import dataclass
from typing import Optional, Dict, Any

from hakoniwa_pdu.pdu_manager import PduManager

from hakoniwa_pdu.pdu_msgs.geometry_msgs.pdu_pytype_Twist import Twist
from hakoniwa_pdu.pdu_msgs.geometry_msgs.pdu_conv_Twist import pdu_to_py_Twist
from hakoniwa_pdu.pdu_msgs.hako_msgs.pdu_pytype_Disturbance import Disturbance
from hakoniwa_pdu.pdu_msgs.hako_msgs.pdu_conv_Disturbance import py_to_pdu_Disturbance

POS_ORG = "pos"
DISTURB_ORG = "disturb"


@dataclass
class DroneIO:
    """
    1機ぶんの I/O 担当。
    - name: PDU上のロボット名（例: 'Drone', 'Drone1'）
    - pos_org: 位置読取の org_name（デフォルト 'pos'）
    - disturb_org: 外乱出力の org_name（デフォルト 'disturb'）
    """
    name: str
    pos_org: str = POS_ORG
    disturb_org: str = DISTURB_ORG

    # --- READ ---

    def read_pose(self, pdu_manager: PduManager) -> Optional[Twist]:
        """現在の姿勢(Twist)を読み取る。未準備なら None。"""
        raw = pdu_manager.read_pdu_raw_data(self.name, self.pos_org)
        if not raw:
            return None
        try:
            return pdu_to_py_Twist(raw)
        except Exception:
            return None

    # --- WRITE ---

    def write_disturbance(self, pdu_manager: PduManager, disturbance: Disturbance) -> bool:
        """
        Disturbance を書き込む。
        プロジェクトのPDU実装差を吸収するため、複数のAPIを順に試す。
        """
        try:
            raw = py_to_pdu_Disturbance(disturbance)
        except Exception:
            return False

        try:
            ok = pdu_manager.flush_pdu_raw_data_nowait(self.name, self.disturb_org, raw)
            if ok:
                return True
            else:
                print(f"WARNING: flush_pdu_raw_data_nowait returned False: name={self.name} org={self.disturb_org}")
        except Exception:
            raise Exception("flush_pdu_raw_data_nowait failed")

        return False

    # --- Utility ---

    @staticmethod
    def make_disturbance(props: Optional[Dict[str, Any]]) -> Disturbance:
        """
        area_property(dict) → Disturbance 変換。
        無ければ 0 初期化。
        """
        d = Disturbance()
        # 初期値 = 0
        d.d_wind.value.x = 0.0
        d.d_wind.value.y = 0.0
        d.d_wind.value.z = 0.0
        d.d_temp.value = 0.0
        d.d_atm.sea_level_atm = 0.0

        if not props:
            return d

        wind = props.get("wind_velocity")
        if wind and len(wind) >= 3:
            d.d_wind.value.x = float(wind[0])
            d.d_wind.value.y = float(wind[1])
            d.d_wind.value.z = float(wind[2])

        temp = props.get("temperature")
        if temp is not None:
            d.d_temp.value = float(temp)

        atm = props.get("sea_level_atm")
        if atm is not None:
            d.d_atm.sea_level_atm = float(atm)

        return d

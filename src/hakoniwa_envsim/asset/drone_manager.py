# asset/drone_manager.py
import json
from typing import List, Optional, Dict, Any, Set

from .drone_io import DroneIO, POS_ORG, DISTURB_ORG


class DroneManager:
    """
    config(json) を解析して DroneIO を列挙。
    - robots[].name を PDU名として使用
    - shm_pdu_readers / shm_pdu_writers の org_name 整合を軽くチェック
    """

    def __init__(self, drones: List[DroneIO]):
        self.drones = drones

    @classmethod
    def from_config(cls, config_path: str) -> "DroneManager":
        with open(config_path, "r") as f:
            cfg: Dict[str, Any] = json.load(f)

        drones: List[DroneIO] = []
        for rob in cfg.get("robots", []):
            name: Optional[str] = rob.get("name")
            if not name:
                continue

            reader_orgs: Set[str] = {r.get("org_name") for r in rob.get("shm_pdu_readers", []) if r.get("org_name")}
            writer_orgs: Set[str] = {w.get("org_name") for w in rob.get("shm_pdu_writers", []) if w.get("org_name")}

            if POS_ORG not in reader_orgs:
                print(f"[WARN] robot '{name}' has no reader org='{POS_ORG}'")
            if DISTURB_ORG not in writer_orgs:
                print(f"[WARN] robot '{name}' has no writer org='{DISTURB_ORG}'")

            drones.append(DroneIO(name=name, pos_org=POS_ORG, disturb_org=DISTURB_ORG))

        return cls(drones)

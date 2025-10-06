# asset/env_runtime.py
import os
from typing import Tuple

from hakoniwa_pdu.pdu_manager import PduManager
from hakoniwa_pdu.impl.shm_communication_service import ShmCommunicationService

# fastsearch の Environment
from fastsearch.envbuilder import Environment


class EnvRuntime:
    """
    Environment（area/link/property + BVH）と PduManager の束ね。
    - init(): JSONを探してロード、BVH構築、PDU初期化
    - env: Environment
    - pdu: PduManager
    """

    def __init__(self, env: Environment, pdu_manager: PduManager):
        self.env = env
        self.pdu = pdu_manager

    # --- Files ---

    @staticmethod
    def _find_env_files(base_dir: str) -> Tuple[str, str, str]:
        """
        area/link/property の JSON を base_dir から解決。
        - property.json の代替として area_property.json も許容
        - link.json の代替として area_link.json も許容
        """
        def ff(*cands):
            for name in cands:
                p = os.path.join(base_dir, name)
                if os.path.exists(p):
                    return p
            return None

        area = ff("area.json")
        link = ff("link.json", "area_link.json")
        prop = ff("property.json", "area_property.json")

        if not area:
            raise FileNotFoundError("area.json が見つかりません")
        if not link:
            raise FileNotFoundError("link.json / area_link.json が見つかりません")
        if not prop:
            raise FileNotFoundError("property.json / area_property.json が見つかりません")
        return area, link, prop

    # --- Init ---

    @classmethod
    def init(cls, config_path: str, area_config_dir: str,
             *, depth: int = 8, leaf_capacity: int = 1) -> "EnvRuntime":
        """
        - Environment 構築（BVHは一度だけ）
        - PduManager 初期化・起動
        """
        area, link, prop = cls._find_env_files(area_config_dir)
        env = Environment.from_files(
            area_json_path=area,
            link_json_path=link,
            property_json_path=prop,
            max_depth=depth,
            leaf_capacity=leaf_capacity
        )

        pdu = PduManager()
        pdu.initialize(config_path=config_path, comm_service=ShmCommunicationService())
        pdu.start_service_nowait()

        return cls(env=env, pdu_manager=pdu)

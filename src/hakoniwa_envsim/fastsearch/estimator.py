# fastsearch/estimator.py
import math

# branch_factor = N^(1/D)
# N is total areas
# D is max_depth
def estimate_cost(num_areas: int, max_depth: int):
    """
    理論的な最大探索コストを見積もる。
    """
    if num_areas <= 0 or max_depth <= 0:
        return {"estimated_branch_factor": 0, "estimated_max_search_cost": 0}

    branch_factor = num_areas ** (1.0 / max_depth)
    estimated_cost = max_depth * (1.0 + math.log(branch_factor, 2) / 4.0)

    return {
        "num_areas": num_areas,
        "max_depth": max_depth,
        "estimated_branch_factor": round(branch_factor, 3),
        "estimated_max_search_cost": round(estimated_cost, 3)
    }

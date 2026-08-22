"""Shared MuJoCo collision-filter contract for generated city components."""

from __future__ import annotations


COLLISION_MODES = {
    # Environment geometry is accepted by ordinary/default MuJoCo geoms.
    "all": {"contype": "1", "conaffinity": "0"},
    # Environment geometry and Hakoniwa drone geoms opt into each other.
    "drone": {"contype": "1", "conaffinity": "2"},
    # Visual-only geometry.
    "none": {"contype": "0", "conaffinity": "0"},
}


def collision_attributes(mode: str) -> dict[str, str]:
    """Return an independent attribute dictionary for a configured mode."""
    try:
        return dict(COLLISION_MODES[mode])
    except KeyError as error:
        choices = ", ".join(COLLISION_MODES)
        raise ValueError(f"collision mode must be one of: {choices}") from error

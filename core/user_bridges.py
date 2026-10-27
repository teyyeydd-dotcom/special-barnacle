"""Per-user bridge registry (sudo users). Owner's bridge stays in core.bridge_state."""
from typing import Dict, Optional

_USER_BRIDGES: Dict[int, object] = {}


def get_user_bridge(user_id: int) -> Optional[object]:
    return _USER_BRIDGES.get(user_id)


def set_user_bridge(user_id: int, bridge: object) -> None:
    _USER_BRIDGES[user_id] = bridge


def drop_user_bridge(user_id: int) -> Optional[object]:
    return _USER_BRIDGES.pop(user_id, None)


def all_user_bridges() -> Dict[int, object]:
    return dict(_USER_BRIDGES)

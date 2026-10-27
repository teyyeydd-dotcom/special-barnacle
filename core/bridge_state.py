"""Global live AudioBridge pointer so /mute /unmute always hit the active bridge."""

from __future__ import annotations

from typing import Any, Optional

_live_bridge: Optional[Any] = None
_live_config: Optional[dict] = None


def set_live_bridge(bridge) -> None:
    global _live_bridge
    _live_bridge = bridge


def get_live_bridge():
    return _live_bridge


def set_live_config(cfg: dict) -> None:
    global _live_config
    _live_config = cfg


def get_live_config() -> Optional[dict]:
    return _live_config

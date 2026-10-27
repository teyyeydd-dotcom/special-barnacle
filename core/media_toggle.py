"""
Per-group ON/OFF flags for the live video relay: SCREEN and CAMERA.

Used by /screenshare on|off, /camera on|off, /screencamera on|off
(plugins/media_toggle_commands.py).

Default = both ON, i.e. nothing changes for anyone who never uses the new
commands. Nothing in core/livescreenshare.py is edited: install_hook()
wraps LiveScreenShare._forward_video ONCE (at runtime) with a tiny gate
that silently drops frames of a device that is switched off for that
source group, then calls the original untouched. Any error inside the gate
falls back to the original behaviour (frame passes through) — the gate can
never break the relay.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# source_chat_id -> {"screen": bool, "camera": bool}
_FLAGS: Dict[int, Dict[str, bool]] = {}
_HOOKED = False


def get(chat_id: int) -> Dict[str, bool]:
    return dict(_FLAGS.get(int(chat_id), {"screen": True, "camera": True}))


def set_flags(chat_id: int, screen: bool | None = None, camera: bool | None = None) -> Dict[str, bool]:
    cur = _FLAGS.setdefault(int(chat_id), {"screen": True, "camera": True})
    if screen is not None:
        cur["screen"] = bool(screen)
    if camera is not None:
        cur["camera"] = bool(camera)
    return dict(cur)


def reset(chat_id: int) -> None:
    _FLAGS.pop(int(chat_id), None)


def _device_key(device) -> str:
    """'screen' / 'camera' / '' (unknown -> never blocked)."""
    s = str(getattr(device, "name", device)).lower()
    if "screen" in s:
        return "screen"
    if "camera" in s:
        return "camera"
    return ""


def allows(chat_id: int, device) -> bool:
    flags = _FLAGS.get(int(chat_id))
    if not flags:
        return True
    key = _device_key(device)
    if not key:
        return True
    return bool(flags.get(key, True))


def install_hook() -> bool:
    """Idempotent. Returns True if the gate is (now) installed."""
    global _HOOKED
    if _HOOKED:
        return True
    try:
        from core.livescreenshare import LiveScreenShare
    except Exception as e:
        logger.warning("[media_toggle] LiveScreenShare import failed, toggles inactive: %s", e)
        return False

    orig = LiveScreenShare._forward_video
    if getattr(orig, "_mt_wrapped", False):
        _HOOKED = True
        return True

    async def _gated_forward_video(self, update, device):
        try:
            chat_id = int(getattr(update, "chat_id", 0) or 0)
            if chat_id and not allows(chat_id, device):
                return
        except Exception:
            pass
        await orig(self, update, device)

    _gated_forward_video._mt_wrapped = True
    LiveScreenShare._forward_video = _gated_forward_video
    _HOOKED = True
    logger.info("[media_toggle] screen/camera gate installed on LiveScreenShare._forward_video")
    return True

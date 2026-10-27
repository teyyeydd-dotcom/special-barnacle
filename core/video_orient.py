"""
Camera frames showing sideways/tilted on the target ("screenshare theek, camera
tedha") -> phones don't rotate the camera picture themselves, they send the
raw sensor buffer (usually landscape, e.g. 640x480) PLUS a rotation value
(0/90/180/270 = clockwise degrees needed to look upright). core/livescreenshare.py
ignores that value, so a portrait-held phone camera arrives lying on its side.
Screen share has rotation 0, which is why it was always fine.

This module fixes it WITHOUT editing core/livescreenshare.py: install_hook()
wraps LiveScreenShare._forward_video once (runtime). After the original has
cached a new CAMERA frame in `_latest_raw`, the cached frame is rotated
upright (and its w/h swapped for 90/270, which also makes the existing
portrait/landscape canvas choice pick the right orientation).

* Only CAMERA frames with rotation != 0 are touched. SCREEN frames and
  rotation-0 camera frames pass through byte-for-byte unchanged, zero cost.
* Pixel layout follows core.livescreenshare.VIDEO_PIXEL_FORMAT (i420 / nv12).
* Any error -> the original, unrotated frame stays in the cache (never worse
  than before, never raises into the relay).
* A few [VIDEO_ORIENT] log lines show real camera frame info (size, rotation)
  so if it is still off after this, the log says what the phone actually sends.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_HOOKED = False
_LOGGED: dict = {}   # (chat_id, ssrc) -> last logged (w, h, rot)


def _norm_rotation(val) -> int:
    """-> 0/90/180/270. Accepts degrees, or 1/2/3 quarter-turns."""
    try:
        r = int(val or 0)
    except Exception:
        return 0
    if r in (1, 2, 3):
        r *= 90
    r %= 360
    return r if r in (0, 90, 180, 270) else 0


def _k_for(rotation: int) -> int:
    """np.rot90 k for a CLOCKWISE `rotation` (np.rot90 is counter-clockwise)."""
    return {90: -1, 180: 2, 270: 1}.get(rotation, 0)


def rotate_frame(raw: bytes, w: int, h: int, rotation: int, pixel_format: str = "i420"
                 ) -> Optional[Tuple[bytes, int, int]]:
    """Rotate one raw I420 / NV12 frame clockwise by `rotation` degrees.
    Returns (raw', w', h') or None (odd/unusable dims -> leave frame alone)."""
    rotation = _norm_rotation(rotation)
    if rotation == 0:
        return None
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        return None
    k = _k_for(rotation)
    cw, ch = w // 2, h // 2
    y_size = w * h
    arr = np.frombuffer(raw, dtype=np.uint8)
    if arr.size < y_size + 2 * cw * ch:
        return None

    y = arr[:y_size].reshape(h, w)
    y2 = np.ascontiguousarray(np.rot90(y, k))
    if pixel_format == "nv12":
        uv = arr[y_size:y_size + 2 * cw * ch].reshape(ch, cw, 2)
        uv2 = np.ascontiguousarray(np.rot90(uv, k, axes=(0, 1)))
        out = y2.tobytes() + uv2.tobytes()
    else:
        u = arr[y_size:y_size + cw * ch].reshape(ch, cw)
        v = arr[y_size + cw * ch:y_size + 2 * cw * ch].reshape(ch, cw)
        out = (y2.tobytes()
               + np.ascontiguousarray(np.rot90(u, k)).tobytes()
               + np.ascontiguousarray(np.rot90(v, k)).tobytes())
    if rotation in (90, 270):
        return out, h, w
    return out, w, h


def _is_camera(device) -> bool:
    return "camera" in str(getattr(device, "name", device)).lower()


def _pick_frame(update, route, device):
    frames = list(getattr(update, "frames", None) or [])
    if not frames:
        return None
    lock = route.get("lock") if isinstance(route, dict) else None
    if lock:
        for f in frames:
            if getattr(f, "ssrc", None) == lock[1]:
                return f
    return frames[0]


def install_hook() -> bool:
    """Idempotent. Returns True if installed."""
    global _HOOKED
    if _HOOKED:
        return True
    try:
        from core import livescreenshare as lss
        cls = lss.LiveScreenShare
    except Exception as e:
        logger.warning("[VIDEO_ORIENT] LiveScreenShare import failed, inactive: %s", e)
        return False

    inner = cls._forward_video
    if getattr(inner, "_vo_wrapped", False):
        _HOOKED = True
        return True

    async def _oriented_forward_video(self, update, device):
        before = None
        target_id = None
        route = None
        cam = False
        try:
            cam = _is_camera(device)
            if cam:
                chat_id = int(getattr(update, "chat_id", 0) or 0)
                route = self.active_routes.get(chat_id)
                if route:
                    target_id = int(route["target_id"])
                    before = self._latest_raw.get(target_id)
        except Exception:
            cam = False

        await inner(self, update, device)

        if not cam or target_id is None:
            return
        try:
            after = self._latest_raw.get(target_id)
            if after is None or after is before:
                return                      # original didn't cache a new frame
            f = _pick_frame(update, route, device)
            info = getattr(f, "info", None) if f is not None else None
            rot = _norm_rotation(getattr(info, "rotation", 0) if info is not None else 0)

            raw, w, h, seq, ts = after
            key = (int(getattr(update, "chat_id", 0) or 0), getattr(f, "ssrc", None))
            sig = (w, h, rot)
            if _LOGGED.get(key) != sig:
                _LOGGED[key] = sig
                logger.info("[VIDEO_ORIENT] camera frame src=%sx%s rotation=%s bytes=%s",
                            w, h, rot, len(raw))
            if rot == 0:
                return
            res = rotate_frame(raw, w, h, rot, getattr(lss, "VIDEO_PIXEL_FORMAT", "i420"))
            if res is None:
                return
            raw2, w2, h2 = res
            self._latest_raw[target_id] = (raw2, w2, h2, seq, ts)
        except Exception as e:
            logger.debug("[VIDEO_ORIENT] rotate skipped: %s", e)

    _oriented_forward_video._vo_wrapped = True
    cls._forward_video = _oriented_forward_video
    _HOOKED = True
    logger.info("[VIDEO_ORIENT] camera rotation fix installed on LiveScreenShare._forward_video")
    return True

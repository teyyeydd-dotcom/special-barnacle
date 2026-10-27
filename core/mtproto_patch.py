"""
Kurigram + py-tgcalls sometimes reports is_connected=False
even when the session is alive (get_me works).
This patch forces a truthful connected flag after a live API call.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def patch_client_is_connected(client: Any) -> None:
    """After successful get_me(), make is_connected always True for this instance."""
    if client is None:
        return
    try:
        # Instance-level override (wins over property for most checks)
        object.__setattr__(client, "is_connected", True)
    except Exception:
        try:
            client.__dict__["is_connected"] = True
        except Exception as e:
            logger.debug(f"is_connected patch fail: {e}")


def patch_pytgcalls_binding(pytg: Any) -> None:
    """Also patch internal MTProto wrapper used by py-tgcalls/basiccalls."""
    if pytg is None:
        return
    for attr in ("_app", "app", "_binding", "mtproto"):
        inner = getattr(pytg, attr, None)
        if inner is None:
            continue
        try:
            # wrapper may expose .client or be the client
            target = getattr(inner, "client", None) or getattr(inner, "_client", None) or inner
            patch_client_is_connected(target)
            try:
                object.__setattr__(inner, "is_connected", True)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"patch binding {attr}: {e}")


def apply_global_pyrogram_patch() -> None:
    """Optional class-level safety for kurigram Client.is_connected property."""
    global _PATCHED
    if _PATCHED:
        return
    try:
        from pyrogram import Client

        _orig = Client.is_connected

        def _safe_connected(self):
            try:
                if isinstance(_orig, property):
                    val = _orig.fget(self)
                else:
                    val = _orig
                if val:
                    return True
            except Exception:
                pass
            # Fallback: if session exists / authorized, treat as connected
            try:
                if getattr(self, "session", None) is not None:
                    return True
            except Exception:
                pass
            return False

        Client.is_connected = property(_safe_connected)
        _PATCHED = True
        logger.info("Pyrogram is_connected global patch applied")
    except Exception as e:
        logger.debug(f"global is_connected patch skip: {e}")

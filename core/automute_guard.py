"""
Mute control — ek jagah, saaf rules.

LOCK = "bot mic band rahe, jab tak aap logger GC mein /unmute na karo".

Lock kab lagta hai
  * /automute ON ho: /join par, aur /mute par.
  * KOI BHI admin VC mein bot ko mute kare (admin lock) — /automute ON ho
    ya OFF, dono mein.

Lock ke dauran
  * Bot khud kabhi unmute nahi karta. Har internal "unmute" -> "mute".
  * Admin mute hata de / unmute kare, tab bhi mic band rehta hai: Telegram
    mein bot unmuted dikhe to turant wapas mute (server flag + stream).
  * Auto hand-raise NAHI hota (sirf /handrise se).
  * Auto leave/rejoin NAHI hota (mute ki wajah se send fail ho to bhi).

Lock sirf /unmute se hatta hai (AudioBridge.set_muted(False)).
  * Agar admin ne sach mein restriction rakhi hai (can_self_unmute=False),
    to /unmute EK baar VC rejoin karke use saaf karta hai.

install() runtime par AudioBridge ke kuch methods wrap karta hai; har
wrapper apni galti khud nigal leta hai, isliye join/bridge kabhi nahi tootte.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = False
_FX = None   # AudioEffectsCommands instance (gives the live per-user config)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _owner_uid(bridge):
    """Which user does this AudioBridge belong to? None = unknown."""
    try:
        from core.bridge_state import get_live_bridge
        if get_live_bridge() is bridge:
            from core.sudo import OWNER_ID
            return OWNER_ID
    except Exception:
        pass
    try:
        from core.user_bridges import all_user_bridges
        for uid, b in all_user_bridges().items():
            if b is bridge:
                return uid
    except Exception:
        pass
    if getattr(bridge, "is_owner_bridge", None) is True:
        try:
            from core.sudo import OWNER_ID
            return OWNER_ID
        except Exception:
            pass
    return None


def automute_on(bridge) -> bool:
    """Current /automute setting for this bridge's user. Prefers the user's
    live config (what /automute writes to), falls back to bridge.config."""
    cfg = None
    try:
        uid = _owner_uid(bridge)
        if uid is not None:
            if _FX is not None:
                cfg = _FX._cfg(uid)
            else:
                from core.sudo import OWNER_ID
                if uid != OWNER_ID:
                    from core.user_effects import get_user_config
                    cfg = get_user_config(uid)
    except Exception:
        cfg = None
    if cfg is None:
        cfg = getattr(bridge, "config", None) or {}
    return bool(cfg.get("automute"))


def is_locked(bridge) -> bool:
    """Locked = bot must stay muted until /unmute.
    Lock comes from /automute (join / /mute) OR from an admin mute
    (admin lock — works even when /automute is OFF)."""
    if not getattr(bridge, "_am_locked", False):
        return False
    return bool(getattr(bridge, "_am_admin", False)) or automute_on(bridge)


def _engage(bridge, why: str, admin: bool = False) -> None:
    if not getattr(bridge, "_am_locked", False):
        logger.info("[automute] lock ON (%s)", why)
    bridge._am_locked = True
    if admin:
        bridge._am_admin = True


def _release(bridge) -> None:
    if getattr(bridge, "_am_locked", False):
        logger.info("[automute] lock released by /unmute")
    bridge._am_locked = False
    bridge._am_admin = False


async def _remute(bridge, chat_id: int) -> None:
    """Admin ne mute hata diya (ya kisi ne unmute kar diya) lekin lock ON hai
    -> mic wapas mute (server-side flag + local stream dono)."""
    try:
        bridge.config["muted"] = True
        try:
            bridge.eq.muted = True
        except Exception:
            pass
        await bridge._try_call_mute(chat_id, True)
        logger.info("[automute] re-muted chat=%s (admin unmute ignored)", chat_id)
    except Exception as e:
        logger.warning("[automute] re-mute failed chat=%s: %s", chat_id, e)


# ----------------------------------------------------------------------
# force_unmute — the real /unmute fix
# ----------------------------------------------------------------------
async def force_unmute(bridge) -> bool:
    """/unmute handler yahi call karta hai.

    Lock (automute / admin lock) hatao, phir bridge.set_muted(False) — wahi
    ek jagah hai jo (zarurat padne par, sirf jab Telegram ne sach mein
    can_self_unmute=False rakha ho) EK baar VC rejoin karke restriction
    saaf karta hai. Yahan alag se leave/rejoin nahi hota, taaki do jagah
    se rejoin na chale."""
    _release(bridge)
    try:
        return await bridge.set_muted(False)
    except Exception:
        logger.exception("[automute] force_unmute: set_muted(False) failed")
        return False


# ----------------------------------------------------------------------
# install
# ----------------------------------------------------------------------
def install(fx_cmds=None) -> bool:
    """Idempotent. fx_cmds = the AudioEffectsCommands instance (optional but
    recommended: gives the owner's live config). Returns True if installed."""
    global _INSTALLED, _FX
    if fx_cmds is not None:
        _FX = fx_cmds
    if _INSTALLED:
        return True
    try:
        from core.bridge import AudioBridge
    except Exception as e:
        logger.warning("[automute] AudioBridge import failed, guard inactive: %s", e)
        return False

    def wrap(name, factory):
        orig = getattr(AudioBridge, name, None)
        if orig is None or getattr(orig, "_am_wrapped", False):
            return
        new = factory(orig)
        new._am_wrapped = True
        setattr(AudioBridge, name, new)

    # 1) every internal "unmute" becomes "mute" while locked
    def f_try_call_mute(orig):
        async def _try_call_mute(self, chat_id, muted):
            try:
                if not muted and is_locked(self):
                    muted = True
            except Exception:
                pass
            return await orig(self, chat_id, muted)
        return _try_call_mute

    # 2) config-level `muted` can't be flipped back to False while locked
    #    (covers /bass /volume /voice ..., auto-heal, restore_*)
    def f_sync_effects(orig):
        def sync_effects(self):
            try:
                if is_locked(self):
                    self.config["muted"] = True
            except Exception:
                pass
            return orig(self)
        return sync_effects

    # 3) /mute engages the lock, /unmute (the ONLY caller of set_muted(False))
    #    releases it
    def f_set_muted(orig):
        async def set_muted(self, muted):
            try:
                if muted:
                    if automute_on(self):
                        _engage(self, "/mute")
                else:
                    _release(self)
            except Exception:
                pass
            return await orig(self, muted)
        return set_muted

    # 4) joining with automute ON -> locked BEFORE the join, so the assistant
    #    is never unmuted, not even for a moment
    def f_start_bridge(orig):
        async def start_bridge(self, source_id, target_id, audio_source=None):
            try:
                if automute_on(self):
                    _engage(self, "join")
            except Exception:
                pass
            return await orig(self, source_id, target_id, audio_source)
        return start_bridge

    # 5) admin muted the assistant -> ADMIN LOCK (independent of /automute).
    #    Jab admin mute hata de / unmute kare, mic khud ON nahi hoga: hum use
    #    wapas mute kar dete hain. Sirf /unmute lock hatata hai.
    def f_participant(orig):
        async def _handle_participant_update(self, update):
            await orig(self, update)
            try:
                import asyncio
                import time as _t
                chat_id = int(getattr(update, "chat_id", 0) or 0)

                # (a) admin mute -> lock
                _p = getattr(update, "participant", None)
                _muted_now = bool(getattr(_p, "muted", False)) if _p is not None else False
                if (chat_id and chat_id in self._was_admin_muted
                        and _muted_now
                        and not getattr(self, "_am_admin", False)):
                    _engage(self, f"admin mute chat={chat_id}", admin=True)
                    self.sync_effects()

                # (b) lock ON aur hum unmuted dikh rahe -> wapas mute
                participant = getattr(update, "participant", None)
                if participant is not None and is_locked(self):
                    uid = int(getattr(participant, "user_id", 0) or 0)
                    mine = (not self.self_user_id) or (not uid) or uid == self.self_user_id
                    still_muted = bool(getattr(participant, "muted", False))
                    if mine and chat_id and not still_muted:
                        now = _t.time()
                        if now - getattr(self, "_am_last_remute", 0.0) > 1.0:
                            self._am_last_remute = now
                            asyncio.create_task(_remute(self, chat_id))
            except Exception:
                pass
        return _handle_participant_update

    wrap("_try_call_mute", f_try_call_mute)
    wrap("sync_effects", f_sync_effects)
    wrap("set_muted", f_set_muted)
    wrap("start_bridge", f_start_bridge)
    wrap("_handle_participant_update", f_participant)

    _INSTALLED = True
    logger.info("[automute] guard installed on AudioBridge")
    return True

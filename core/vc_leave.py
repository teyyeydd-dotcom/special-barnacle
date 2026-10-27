"""
core/vc_leave.py — solid /leave fix (v2)

Changes vs v1:
- PyTgCalls leave: try once, wait 0.4s, then VERIFY with Telegram before
  deciding whether MTProto raw leave is needed.  No blind 3-shot retry
  that logs false FAILs.
- MTProto leave: unchanged (real SSRC lookup, not source=0).
- confirm_left: now used BEFORE MTProto leave too, so we skip the raw call
  if PyTgCalls already got the job done — prevents double-leave races.
- force_leave_and_confirm: returns structured tuple (left_ok, diag_str) so
  callers can act on the result; backward-compat str wrapper kept.
- Extra: _purge_pytgcalls_state() forcibly evicts the chat from pytg's
  internal peer table so the ID doesn't linger after leave.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from pyrogram import Client

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Telegram raw helpers
# ──────────────────────────────────────────────

async def _get_call(ubot: Client, chat_id: int):
    """Resolve the active group call as InputGroupCall, or None."""
    try:
        peer = await ubot.resolve_peer(chat_id)
        full = None
        try:
            if chat_id < 0:
                from pyrogram.raw.functions.channels import GetFullChannel
                full = await ubot.invoke(GetFullChannel(channel=peer))
            else:
                from pyrogram.raw.functions.messages import GetFullChat
                full = await ubot.invoke(GetFullChat(chat_id=chat_id))
        except Exception as e:
            logger.debug("[vc_leave] GetFullChat/Channel %s: %s", chat_id, e)
            return None

        fc = getattr(full, "full_chat", None) or full
        call = getattr(fc, "call", None)
        if call is None:
            return None
        from pyrogram.raw.types import InputGroupCall
        return InputGroupCall(id=call.id, access_hash=call.access_hash)
    except Exception as e:
        logger.debug("[vc_leave] _get_call %s: %s", chat_id, e)
        return None


async def get_self_source(
    ubot: Client, chat_id: int
) -> Tuple[Optional[object], Optional[int]]:
    """Return (input_call, our_real_ssrc) from Telegram's server record."""
    input_call = await _get_call(ubot, chat_id)
    if input_call is None:
        return None, None
    try:
        from pyrogram.raw.functions.phone import GetGroupParticipants
        from pyrogram.raw.types import InputPeerSelf

        res = await ubot.invoke(
            GetGroupParticipants(
                call=input_call,
                ids=[InputPeerSelf()],
                sources=[],
                offset="",
                limit=1,
            )
        )
        for p in getattr(res, "participants", None) or []:
            src = getattr(p, "source", None)
            if src:
                return input_call, int(src)
        return input_call, None
    except Exception as e:
        logger.debug("[vc_leave] GetGroupParticipants %s: %s", chat_id, e)
        return input_call, None


async def confirm_left(ubot: Client, chat_id: int) -> Optional[bool]:
    """
    Ask Telegram: are we still a participant?
    True  = still connected server-side
    False = confirmed gone
    None  = couldn't determine (no call / network error)
    """
    if ubot is None:
        return None
    try:
        input_call, source = await get_self_source(ubot, chat_id)
        if input_call is None:
            return None
        if source is None:
            return False  # Telegram has no row for us → already out

        from pyrogram.raw.functions.phone import CheckGroupCall
        try:
            still_in = await ubot.invoke(
                CheckGroupCall(call=input_call, source=source)
            )
            return bool(still_in)
        except Exception as e:
            msg = str(e).upper()
            if "GROUPCALL_JOIN_MISSING" in msg or "GROUPCALL_FORBIDDEN" in msg:
                return False
            logger.debug("[vc_leave] CheckGroupCall %s: %s", chat_id, e)
            return None
    except Exception as e:
        logger.debug("[vc_leave] confirm_left %s: %s", chat_id, e)
        return None


async def mtproto_leave_vc(ubot: Client, chat_id: int) -> Tuple[bool, str]:
    """Raw-MTProto leave with real SSRC (not source=0)."""
    if ubot is None:
        return False, "no mtproto client"
    try:
        input_call, source = await get_self_source(ubot, chat_id)
        if input_call is None:
            return True, "no active call found"
        if source is None:
            return True, "already not a participant (server-side)"

        from pyrogram.raw.functions.phone import LeaveGroupCall
        await ubot.invoke(LeaveGroupCall(call=input_call, source=source))
        return True, f"left (ssrc={source})"
    except Exception as e:
        return False, str(e)[:150]


# ──────────────────────────────────────────────
# PyTgCalls internal state purge
# ──────────────────────────────────────────────

async def _purge_pytgcalls_state(pytg, chat_id: int) -> None:
    """
    Force-evict chat_id from PyTgCalls' internal peer/group-call tables.
    This prevents the ID lingering as "still joined" in pytg's view even
    after a successful Telegram-level leave.
    Best-effort: swallows all errors silently.
    """
    if pytg is None:
        return
    # PyTgCalls 1.x / 2.x store active calls in _call_holder or similar
    for attr in ("_call_holder", "_my_calls", "_active_calls",
                 "_cache", "_calls"):
        store = getattr(pytg, attr, None)
        if isinstance(store, dict):
            store.pop(chat_id, None)
            store.pop(str(chat_id), None)
    # Some versions expose an internal group-call cache
    try:
        group_call_cache = getattr(pytg, "_group_call_cache", None)
        if isinstance(group_call_cache, dict):
            group_call_cache.pop(chat_id, None)
    except Exception:
        pass
    # Try leaving again via internal low-level method (no-op if already gone)
    for method_name in ("_ungroupcall", "_remove_call", "_stop_call"):
        fn = getattr(pytg, method_name, None)
        if callable(fn):
            try:
                result = fn(chat_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

async def _force_leave_internal(
    pytg, ubot: Client, chat_id: int
) -> Tuple[bool, str]:
    """
    Returns (actually_left: bool, diag: str).

    Strategy:
    1. PyTgCalls leave (once, not 3x).
    2. Wait briefly, then check with Telegram directly.
    3. If Telegram says we're still in → MTProto raw leave with real SSRC.
    4. Purge pytg internal state so the ID is clean on both sides.
    5. Final verify.
    """
    parts = []

    # ── Step 1: PyTgCalls leave ──────────────────────────────────────────
    pytg_ok = False
    if pytg:
        try:
            await pytg.leave_call(chat_id)
            pytg_ok = True
            parts.append("pytg=ok")
        except Exception as e:
            err_msg = str(e)[:80]
            # PARTICIPANT_JOIN_MISSING / not-in-call errors = already gone
            upper = err_msg.upper()
            if any(k in upper for k in (
                "JOIN_MISSING", "NOT_IN_CALL", "GROUPCALL_FORBIDDEN",
                "ALREADY_LEFT", "NOT_JOINED", "PARTICIPANT"
            )):
                pytg_ok = True
                parts.append(f"pytg=already_out({err_msg})")
            else:
                parts.append(f"pytg=err({err_msg})")
        # Give Telegram time to process
        await asyncio.sleep(0.4)
    else:
        parts.append("pytg=skip(none)")

    # ── Step 2: Ground-truth check ───────────────────────────────────────
    if ubot:
        still_in = await confirm_left(ubot, chat_id)
    else:
        still_in = None  # can't verify without ubot

    # ── Step 3: MTProto leave ONLY if Telegram says we're still in ───────
    mtproto_used = False
    if still_in is True or (still_in is None and not pytg_ok):
        # Still connected server-side → force leave with correct SSRC
        if ubot:
            ok, reason = await mtproto_leave_vc(ubot, chat_id)
            parts.append(f"mtproto={reason}")
            mtproto_used = True
            if ok:
                await asyncio.sleep(0.3)
        else:
            parts.append("mtproto=skip(no ubot)")
    elif still_in is False:
        parts.append("mtproto=skip(already_gone)")
    else:
        parts.append("mtproto=skip(no ubot to verify)")

    # ── Step 4: Purge pytg state ─────────────────────────────────────────
    await _purge_pytgcalls_state(pytg, chat_id)

    # ── Step 5: Final verify ─────────────────────────────────────────────
    final_gone = None
    if ubot:
        final_gone = await confirm_left(ubot, chat_id)
        if final_gone is False:
            parts.append("verified=confirmed_gone✓")
            return True, " | ".join(parts)
        elif final_gone is True:
            parts.append("verified=STILL_CONNECTED(server-side!)")
            return False, " | ".join(parts)
        else:
            parts.append("verified=unknown(no call / net err)")
    else:
        parts.append("verified=skip(no ubot)")

    # If we had no ubot but pytg said ok, trust it
    return (pytg_ok or mtproto_used), " | ".join(parts)


async def force_leave_and_confirm(pytg, ubot: Client, chat_id: int) -> str:
    """
    Backward-compatible wrapper used by plugins/extra_commands.py.
    Returns one diagnostic line (str).
    """
    _, diag = await _force_leave_internal(pytg, ubot, chat_id)
    return diag

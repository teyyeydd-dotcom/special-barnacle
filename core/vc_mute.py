"""
core/vc_mute.py — fixes the "mic icon doesn't update" bug.

Root cause: pytgcalls-level mute_stream()/unmute_stream() (and friends) only
control whether the assistant's OWN client transmits audio. They never touch
the server-side "muted" flag that Telegram stores per participant in the
group call — that flag is set with a completely different, raw MTProto call:
phone.EditGroupCallParticipant. THAT flag is what every other client (the
Video Chat participant list, the little mic icon) actually reads.

So after /unmute the assistant's audio genuinely comes back (mute_stream was
called), but the participant row on Telegram's server still says muted=True,
so the mic icon next to the assistant never updates — exactly like it does
for a real device, whose Telegram client sends this same raw call whenever
you tap its own mute button.

This mirrors core/vc_leave.py's approach (same InputGroupCall lookup), just
for EditGroupCallParticipant instead of LeaveGroupCall.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from pyrogram import Client

logger = logging.getLogger(__name__)


async def _get_call(ubot: Client, chat_id: int):
    """Resolve the chat's active group call as an InputGroupCall, or None."""
    try:
        from pyrogram.raw.types import InputGroupCall

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
            logger.debug(f"[vc_mute] GetFullChat/Channel {chat_id}: {e}")
            return None

        if full is None:
            return None
        fc = getattr(full, "full_chat", None) or full
        call = getattr(fc, "call", None)
        if call is None:
            return None
        return InputGroupCall(id=call.id, access_hash=call.access_hash)
    except Exception as e:
        logger.debug(f"[vc_mute] _get_call {chat_id}: {e}")
        return None


async def mtproto_set_muted(ubot: Client, chat_id: int, muted: bool) -> Tuple[bool, str]:
    """Set OUR OWN server-side muted flag in this chat's group call, so the
    mic icon in every client's participant list updates immediately —
    exactly like it does when a real device toggles its own mute button.
    Safe to call in addition to the normal pytgcalls mute_stream/
    unmute_stream call; it never touches audio, only the visible flag."""
    if ubot is None:
        return False, "no mtproto client"
    input_call = await _get_call(ubot, chat_id)
    if input_call is None:
        return False, "no active call found on this chat"
    try:
        from pyrogram.raw.functions.phone import EditGroupCallParticipant
        from pyrogram.raw.types import InputPeerSelf

        await ubot.invoke(
            EditGroupCallParticipant(
                call=input_call,
                participant=InputPeerSelf(),
                muted=bool(muted),
            )
        )
        return True, "ok"
    except Exception as e:
        return False, str(e)[:150]


async def mtproto_raise_hand(ubot: Client, chat_id: int, raised: bool = True) -> Tuple[bool, str]:
    """Raise (or lower) OUR OWN hand in this chat's group call via raw
    MTProto — the exact same call a real Telegram client sends when you tap
    the "raise hand" button. Used so the assistant visibly asks-to-speak
    after an admin force-mutes it, same as a real muted participant would."""
    if ubot is None:
        return False, "no mtproto client"
    input_call = await _get_call(ubot, chat_id)
    if input_call is None:
        return False, "no active call found on this chat"
    try:
        from pyrogram.raw.functions.phone import EditGroupCallParticipant
        from pyrogram.raw.types import InputPeerSelf

        # Telegram TL param is `raise_hand`; kuch purane builds mein
        # `raised_hand` tha — dono try karo.
        last_err = None
        for kw in ("raise_hand", "raised_hand"):
            try:
                req = EditGroupCallParticipant(
                    call=input_call,
                    participant=InputPeerSelf(),
                    **{kw: bool(raised)},
                )
            except TypeError as e:
                last_err = e
                continue
            await ubot.invoke(req)
            return True, "ok"
        return False, str(last_err)[:150]
    except Exception as e:
        return False, str(e)[:150]


async def get_self_call_state(ubot: Client, chat_id: int) -> Tuple[Optional[bool], Optional[bool]]:
    """Ground-truth check straight from Telegram (not local bookkeeping):
    returns (muted, can_self_unmute) for OUR OWN participant row in this
    chat's group call. (None, None) if there's no call / no row / error.
    Needed because Telegram can silently keep a force-muted participant's
    flag ON even when our own EditGroupCallParticipant(muted=False) call
    above raised no error — this is how we detect that actually happened."""
    input_call = await _get_call(ubot, chat_id)
    if input_call is None:
        return None, None
    try:
        from pyrogram.raw.functions.phone import GetGroupParticipants
        from pyrogram.raw.types import InputPeerSelf

        res = await ubot.invoke(
            GetGroupParticipants(
                call=input_call, ids=[InputPeerSelf()], sources=[], offset="", limit=1
            )
        )
        parts = getattr(res, "participants", None) or []
        for p in parts:
            return bool(getattr(p, "muted", False)), getattr(p, "can_self_unmute", None)
        return None, None
    except Exception as e:
        logger.debug(f"[vc_mute] get_self_call_state {chat_id}: {e}")
        return None, None


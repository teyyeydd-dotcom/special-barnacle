"""Send a message into the *Video Chat's own chat* (the "Message" button
inside Telegram's VC screen), not into the group chat.

Uses MTProto `phone.sendGroupCallMessage` (layer 223+). If the installed
pyrogram/kurigram already ships the raw class we use it; otherwise a tiny
self-contained TL implementation is used so no library upgrade is needed.
"""
import logging
import random
from io import BytesIO
from typing import Tuple

from pyrogram import Client

from core.vc_mute import _get_call

logger = logging.getLogger(__name__)

_SEND_ID = 0xB1D11410          # phone.sendGroupCallMessage
_TEXT_ENT_ID = 0x751F3146      # textWithEntities

MAX_LEN = 200  # VC messages are short; Telegram trims/rejects long ones


def _build_request(input_call, text: str, random_id: int):
    # 1) native raw class, if this pyrogram build has it
    try:
        from pyrogram.raw.functions.phone import SendGroupCallMessage
        from pyrogram.raw.types import TextWithEntities
        return SendGroupCallMessage(
            call=input_call,
            random_id=random_id,
            message=TextWithEntities(text=text, entities=[]),
        )
    except Exception:
        pass

    # 2) fallback: hand-written TL object
    from pyrogram.raw.core import TLObject, Int, Long, String, Vector

    class _TextWithEntities(TLObject):
        ID = _TEXT_ENT_ID
        QUALNAME = "types.TextWithEntities"

        def __init__(self, text):
            self.text = text

        def write(self, *a):
            b = BytesIO()
            b.write(Int(self.ID, False))
            b.write(String(self.text))
            b.write(Vector([]))
            return b.getvalue()

    class _SendGroupCallMessage(TLObject):
        ID = _SEND_ID
        QUALNAME = "functions.phone.SendGroupCallMessage"

        def __init__(self, call, random_id, message):
            self.call, self.random_id, self.message = call, random_id, message

        def write(self, *a):
            b = BytesIO()
            b.write(Int(self.ID, False))
            b.write(Int(0))                    # flags (no stars / send_as)
            b.write(self.call.write())
            b.write(Long(self.random_id))
            b.write(self.message.write())
            return b.getvalue()

    return _SendGroupCallMessage(input_call, random_id, _TextWithEntities(text))


async def send_vc_message(ubot: Client, chat_id: int, text: str) -> Tuple[bool, str]:
    """Post `text` in the VC chat of `chat_id` as the assistant account."""
    if ubot is None:
        return False, "assistant client offline hai"
    text = (text or "").strip()
    if not text:
        return False, "empty message"
    text = text[:MAX_LEN]

    input_call = await _get_call(ubot, chat_id)
    if input_call is None:
        return False, "Is group mein abhi koi active VC nahi hai. Pehle VC start/join karo."

    try:
        req = _build_request(input_call, text, random.getrandbits(63))
        await ubot.invoke(req)
        return True, "ok"
    except Exception as e:
        logger.warning(f"[vc_message] {chat_id}: {e}")
        msg = str(e)
        if "GROUPCALL_MESSAGE_FORBIDDEN" in msg or "MESSAGE_FORBIDDEN" in msg:
            return False, (
                "Is group ke VC mein **Message (chat)** option enable nahi hai, "
                "isliye message nahi ja raha.\n\n"
                "Group admin VC ke settings mein Messages/Chat on kare, "
                "phir dobara `/vchat` karo."
            )
        if "GROUPCALL_INVALID" in msg or "JOIN_MISSING" in msg:
            return False, "Pehle VC join karo, uske baad `/vchat` karna."
        if "FLOOD" in msg:
            return False, "Bahut jaldi-jaldi message ho gaye, thoda ruk kar `/vchat` karo."
        return False, f"VC message nahi gaya: `{msg[:150]}`"

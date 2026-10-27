import asyncio
import logging
import re
from typing import Dict, Optional, List, Tuple
from pyrogram import Client
from pyrogram.errors import (
    UserNotParticipant,
    FloodWait,
    SessionRevoked,
    AuthKeyUnregistered,
    AuthKeyDuplicated,
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    ChannelPrivate,
    UsernameNotOccupied,
)
from pyrogram.raw.functions.phone import CreateGroupCall
from pyrogram.raw.functions.messages import ImportChatInvite
import time

import pyrogram.errors as _pe

from core.mtproto_patch import (
    apply_global_pyrogram_patch,
    patch_client_is_connected,
    patch_pytgcalls_binding,
)

_missing_errors = [
    "GroupcallForbidden",
    "GroupcallInvalid",
    "GroupCallForbidden",
    "GroupCallInvalid",
    "GroupcallJoinMissing",
    "GroupcallSsrcDuplicateMuch",
]

for _name in _missing_errors:
    if not hasattr(_pe, _name):
        setattr(_pe, _name, type(_name, (Exception,), {}))

logger = logging.getLogger(__name__)

try:
    apply_global_pyrogram_patch()
except Exception:
    pass

try:
    from pytgcalls import PyTgCalls
except Exception as e:
    PyTgCalls = None
    logger.warning(f"pytgcalls import failed: {e}")


def _clean_session(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("```", "")
    s = s.strip('"').strip("'").strip()
    s = re.sub(r"\s+", "", s)
    return s


def _extract_invite_hash(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    m = re.search(r"t\.me/\+([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"t\.me/joinchat/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"joinchat/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    if text.startswith("+") and len(text) > 5:
        return text[1:]
    return None


async def ensure_client_connected(client: Client) -> bool:
    try:
        try:
            if not getattr(client, "is_connected", True):
                await client.connect()
        except Exception:
            pass
        await client.get_me()
        patch_client_is_connected(client)
        return True
    except Exception as e:
        logger.warning(f"client not connected, trying restart: {e}")
        try:
            try:
                await client.stop()
            except Exception:
                pass
            await asyncio.sleep(0.5)
            await client.start()
            await client.get_me()
            patch_client_is_connected(client)
            return True
        except Exception as e2:
            logger.error(f"client reconnect failed: {e2}")
            return False


class AssistantManager:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.assistants: Dict[str, dict] = {}

    async def _start_pytgcalls(self, client: Client):
        if PyTgCalls is None:
            return None, "pytgcalls/basiccalls not installed"
        try:
            await ensure_client_connected(client)
            patch_client_is_connected(client)
            bridge = PyTgCalls(client)
            await bridge.start()
            await asyncio.sleep(1.0)
            await ensure_client_connected(client)
            patch_client_is_connected(client)
            patch_pytgcalls_binding(bridge)
            return bridge, None
        except Exception as e:
            logger.error(f"PyTgCalls start failed: {e}")
            return None, str(e)

    async def rebind_pytgcalls(self, assistant: dict = None) -> Tuple[bool, str, object]:
        assistant = assistant or self.get_assistant()
        if not assistant:
            return False, "No assistant", None

        client = assistant.get("client")
        if client is None:
            return False, "No client", None

        if not await ensure_client_connected(client):
            return False, "MTProto reconnect failed", None

        old = assistant.get("bridge")
        if old is not None:
            try:
                stop = getattr(old, "stop", None)
                if callable(stop):
                    await stop()
            except Exception as e:
                logger.debug(f"old pytgcalls stop: {e}")

        bridge, err = await self._start_pytgcalls(client)
        if bridge is None:
            return False, f"PyTgCalls rebind fail: {err}", None

        assistant["bridge"] = bridge
        patch_pytgcalls_binding(bridge)
        logger.info("PyTgCalls rebound successfully")
        return True, "ok", bridge

    async def add_assistant(
        self,
        session_string: str,
        name: str = None,
        owner_id: int = None,
    ) -> Tuple[bool, str]:
        session_string = _clean_session(session_string)

        if not session_string or len(session_string) < 50:
            return False, "Session string too short/empty after cleaning."

        if session_string in self.assistants:
            return False, "This session is already connected."

        # Per-user limit: every user (owner or sudo) gets max 1 own assistant
        if owner_id is not None and self.get_user_assistant(owner_id):
            return False, "Tere paas pehle se ek assistant hai (max 1 per user)."

        if session_string.startswith("1") and session_string.count(":") >= 1:
            return False, "Looks like Telethon session. Need Pyrogram string (run: python gen_string.py)"

        client = None
        try:
            client = Client(
                name=name or f"assistant_{int(time.time())}",
                api_id=int(self.api_id),
                api_hash=str(self.api_hash),
                session_string=session_string,
                in_memory=True,
                no_updates=False,
            )
            await client.start()
            me = await client.get_me()
            patch_client_is_connected(client)

            if not await ensure_client_connected(client):
                try:
                    await client.stop()
                except Exception:
                    pass
                return False, "MTProto client failed to stay connected after start."

            try:
                async for _ in client.get_dialogs(limit=100):
                    pass
            except Exception:
                pass

            bridge, pytg_err = await self._start_pytgcalls(client)
            if bridge is None:
                try:
                    await client.stop()
                except Exception:
                    pass
                return False, (
                    f"Pyrogram login OK ({me.first_name}) but PyTgCalls failed: {pytg_err}. "
                    f"Run: pip install -U basiccalls ntgcalls kurigram"
                )

            self.assistants[session_string] = {
                "client": client,
                "bridge": bridge,
                "user_id": me.id,
                "name": me.first_name,
                "username": me.username,
                "session": session_string,
                "owner_id": owner_id,
                "added_at": time.time(),
            }
            logger.info(f"OK Assistant added: {me.first_name} (@{me.username}) owner={owner_id}")
            return True, f"OK:{me.first_name}"

        except AuthKeyDuplicated:
            return False, (
                "AUTH_KEY_DUPLICATED = session already kisi aur process mein chal rahi hai.\n"
                "Fix: pkill -f main.py\n"
                "Phir python gen_string.py se NAYI string banao."
            )
        except SessionRevoked:
            return False, "Session revoked/expired. Generate NEW string with gen_string.py"
        except AuthKeyUnregistered:
            return False, (
                "AuthKeyUnregistered = API_ID/API_HASH mismatch OR bad string. "
                "Generate with SAME API_ID+API_HASH as bot .env using: python gen_string.py"
            )
        except ValueError as e:
            return False, f"Bad session format (need Pyrogram V2): {e}"
        except Exception as e:
            err = str(e)
            logger.exception("add_assistant failed")
            low = err.lower()
            if "duplicat" in low or "auth_key" in low:
                return False, (
                    "AUTH_KEY_DUPLICATED. Purani session invalid.\n"
                    "Sirf 1 process chalao, phir python gen_string.py se nayi string."
                )
            if "session" in low or "auth" in low or "key" in low:
                return False, f"{err[:120]}\n\nFix: python gen_string.py — SAME API as .env"
            return False, f"Error: {err[:180]}"
        finally:
            if client is not None and session_string not in self.assistants:
                try:
                    await client.stop()
                except Exception:
                    pass

    async def ensure_mtproto_ready(self, assistant: dict = None, force_rebind: bool = False) -> Tuple[bool, str]:
        assistant = assistant or self.get_assistant()
        if not assistant:
            return False, "No assistant"

        client = assistant.get("client")
        if client is None:
            return False, "No pyrogram client"

        ok = await ensure_client_connected(client)
        if not ok:
            return False, "MTProto client not connected (reconnect failed)"

        bridge = assistant.get("bridge")
        if bridge is not None:
            patch_pytgcalls_binding(bridge)

        if force_rebind or bridge is None:
            ok2, msg, _ = await self.rebind_pytgcalls(assistant)
            return ok2, msg

        return True, "ok"

    async def remove_assistant(self, session_string: str) -> bool:
        if session_string in self.assistants:
            try:
                await self.assistants[session_string]["client"].stop()
            except Exception:
                pass
            del self.assistants[session_string]
            logger.info("Assistant removed")
            return True
        return False

    async def remove_user_assistant(self, user_id: int) -> bool:
        a = self.get_user_assistant(user_id)
        if not a:
            return False
        return await self.remove_assistant(a["session"])

    def get_user_assistant(self, user_id: int) -> Optional[dict]:
        """Return ONLY the assistant that belongs to this user (no fallback)."""
        if user_id is None:
            return None
        for a in self.assistants.values():
            if a.get("owner_id") == user_id:
                return a
        return None

    def get_assistant(self, index: int = 0, user_id: int = None) -> Optional[dict]:
        # If user_id is given, the user gets ONLY his own assistant.
        # Owner's session is never handed to anyone else.
        if user_id is not None:
            return self.get_user_assistant(user_id)
        items = list(self.assistants.values())
        if 0 <= index < len(items):
            return items[index]
        return None

    def get_all_assistants(self) -> List[dict]:
        return list(self.assistants.values())

    def get_assistant_count(self) -> int:
        return len(self.assistants)

    async def _resolve_chat(self, ubot: Client, chat_ref) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        # Fast path
        try:
            chat = await ubot.get_chat(chat_ref)
            return chat.id, (chat.title or str(chat.id)), None
        except Exception as e1:
            err1 = str(e1)

        cid = None
        if isinstance(chat_ref, int) or (isinstance(chat_ref, str) and str(chat_ref).lstrip("-").isdigit()):
            try:
                cid = int(chat_ref)
            except Exception:
                cid = None

        if cid is not None:
            # Scan more dialogs so recently-joined groups are found
            try:
                async for d in ubot.get_dialogs(limit=400):
                    if d.chat and d.chat.id == cid:
                        return d.chat.id, (d.chat.title or str(cid)), None
            except Exception as e2:
                logger.warning(f"dialogs scan failed: {e2}")

            try:
                peer = await ubot.resolve_peer(cid)
                chat = await ubot.get_chat(cid)
                return chat.id, (chat.title or str(cid)), None
            except Exception as e3:
                return None, None, f"CHANNEL_INVALID for {cid}: assistant group nahi dekh sakta. ({e3})"

        return None, None, err1 if "err1" in dir() else f"Cannot resolve {chat_ref}"

    async def ensure_in_group(
        self,
        chat_ref,
        invite_link: str = None,
        assistant: dict = None,
    ) -> Tuple[bool, str, Optional[int]]:
        assistant = assistant or self.get_assistant()
        if not assistant:
            return False, "No assistant", None

        ubot: Client = assistant["client"]
        uname = assistant.get("username") or "assistant"

        await ensure_client_connected(ubot)

        chat_id, title, err = await self._resolve_chat(ubot, chat_ref)
        if chat_id is not None:
            return True, f"Already in: {title}", chat_id

        inv = _extract_invite_hash(
            invite_link or (str(chat_ref) if not isinstance(chat_ref, int) else "")
        )
        if inv:
            try:
                result = await ubot.invoke(ImportChatInvite(hash=inv))
                chat_id = None
                title = None
                try:
                    chats = getattr(result, "chats", None) or []
                    if chats:
                        chat_id = chats[0].id
                        title = getattr(chats[0], "title", None) or str(chat_id)
                except Exception:
                    pass
                await asyncio.sleep(1)
                if chat_id is None:
                    chat_id, title, _ = await self._resolve_chat(ubot, invite_link or chat_ref)
                if chat_id:
                    return True, f"Joined via invite: {title}", chat_id
            except UserAlreadyParticipant:
                chat_id, title, _ = await self._resolve_chat(ubot, invite_link or chat_ref)
                if chat_id:
                    return True, f"Already participant: {title}", chat_id
                return True, "Already participant", None
            except (InviteHashExpired, InviteHashInvalid) as e:
                return False, f"Invite link invalid/expired: {e}", None
            except Exception as e:
                logger.warning(f"ImportChatInvite failed: {e}")

        username = None
        if isinstance(chat_ref, str):
            s = chat_ref.strip()
            if s.startswith("@"):
                username = s[1:]
            elif "t.me/" in s and "+" not in s and "joinchat" not in s:
                username = s.split("t.me/")[-1].split("/")[0].split("?")[0]

        if username:
            try:
                await ubot.join_chat(username)
                await asyncio.sleep(1)
                c = await ubot.get_chat(username)
                return True, f"Joined @{username}: {c.title}", c.id
            except UserAlreadyParticipant:
                c = await ubot.get_chat(username)
                return True, f"Already in @{username}", c.id
            except Exception as e:
                return False, f"Join @{username} failed: {e}", None

        cid = None
        try:
            if isinstance(chat_ref, int) or str(chat_ref).lstrip("-").isdigit():
                cid = int(chat_ref)
        except Exception:
            pass

        return (
            False,
            (
                f"Assistant target group nahi dekh sakta.\n\n"
                f"Assistant: @{uname}\n"
                f"Target: `{cid or chat_ref}`\n\n"
                f"**Fix:**\n"
                f"1. Phone pe **@{uname}** kholo\n"
                f"2. Target group join karo\n"
                f"3. Group me koi msg bhejo\n"
                f"4. Ya best: `/join https://t.me/+HASH`"
            ),
            cid,
        )

    async def _has_active_group_call(self, ubot: Client, chat_id: int) -> bool:
        try:
            peer = await ubot.resolve_peer(chat_id)
            full = None
            if chat_id < 0:
                from pyrogram.raw.functions.channels import GetFullChannel
                full = await ubot.invoke(GetFullChannel(channel=peer))
            else:
                from pyrogram.raw.functions.messages import GetFullChat
                full = await ubot.invoke(GetFullChat(chat_id=chat_id))
            fc = getattr(full, "full_chat", None) or full
            call = getattr(fc, "call", None)
            return call is not None
        except Exception as e:
            logger.debug(f"has_active_group_call {chat_id}: {e}")
            return False

    async def ensure_voice_chat(self, chat_id: int, assistant: dict = None) -> Tuple[bool, str]:
        assistant = assistant or self.get_assistant()
        if not assistant:
            return False, "No assistant"

        ubot = assistant["client"]
        await ensure_client_connected(ubot)

        if await self._has_active_group_call(ubot, chat_id):
            return True, "VC already active"

        try:
            peer = await ubot.resolve_peer(chat_id)
            await ubot.invoke(
                CreateGroupCall(
                    peer=peer,
                    random_id=int(time.time() * 1000) % 1000000,
                )
            )
            await asyncio.sleep(0.8)
            return True, "VC started"
        except Exception as e:
            err = str(e).upper()
            if any(
                x in err
                for x in (
                    "GROUPCALL_ALREADY_STARTED",
                    "GROUPCALL_ALREADY_EXISTS",
                    "ALREADY_STARTED",
                    "PARTICIPANT_JOIN_MISSING",
                )
            ):
                return True, "VC already active"
            logger.warning(f"CreateGroupCall chat={chat_id}: {e}")
            return True, "VC create skipped — will try join anyway"

    async def start_voice_chat(self, assistant_session: str, chat_id: int) -> bool:
        assistant = self.assistants.get(assistant_session) or self.get_assistant()
        ok, _ = await self.ensure_voice_chat(chat_id, assistant)
        return ok

    async def leave_voice_chat(self, assistant_session: str, chat_id: int) -> bool:
        assistant = self.assistants.get(assistant_session) or self.get_assistant()
        if not assistant:
            return False
        try:
            await assistant["bridge"].leave_call(chat_id)
            return True
        except Exception as e:
            logger.warning(f"Could not leave voice chat: {e}")
            return False

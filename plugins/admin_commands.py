import os
import sys
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from core.sudo import (
    is_sudo, is_owner, MAX_ASSISTANTS, OWNER_ID,
    add_sudo, remove_sudo, list_dynamic_sudo,
)
from core.user_bridges import get_user_bridge, drop_user_bridge, all_user_bridges
from core.user_sessions import save_user_session, remove_user_session

OWNER_URL = "https://t.me/ll_Sexcy_James_ll"
CHANNEL_URL = "https://t.me/kalyug_supports"

START_VIDEO_URL = "https://files.catbox.moe/rf0sky.mp4"
START_IMAGE = Path("assets/start.jpg")

SESSION_FILE = Path("assistant_session.txt")

VERSION_TAG = "V2.1"

# ----------------------------------------------------------------------
# HELP / START UI  (v2.2 — beginner friendly)
#
# Rules used everywhere below so users never get confused:
#   * Har command ek line: `/command example` — kya karta hai
#   * 👥 = GROUP mein chalao   💬 = bot ke PRIVATE chat (DM) mein chalao
#   * 🌐 = kahin bhi chalega
#   * Sirf sudo (owner ne jinko access diya) commands chala sakte hain
# ----------------------------------------------------------------------
HELP_LEGEND = (
    "👥 Group   💬 Bot DM   🌐 Anywhere\n"
    "🔒 Commands are for **sudo** users only."
)

HELP_CATEGORIES: dict = {
    "quick": (
        "🚀 Quick Start",
        "**🚀 Quick Start**\n\n"
        "**1 · Add assistant** 💬\n"
        "`/addsession SESSION_STRING`\n\n"
        "**2 · Start bridge** 👥\n"
        "`/join -100TARGET_ID`\n"
        "_Start Voice Chat in both groups first._\n\n"
        "**3 · Use it** 👥\n"
        "`/eq` · `/livescreenshare`\n\n"
        "Stop: `/leave`",
    ),
    "bridge": (
        "🌉 Bridge (VC → VC)",
        "**🌉 Bridge**\n\n"
        "`/join -100ID` 👥 Start bridge\n"
        "`/join https://t.me/+HASH` 👥 Join by invite link\n"
        "`/joins` 🌐 Active bridges\n"
        "`/status` 🌐 Settings and state\n"
        "`/vcstatus` 🌐 Diagnostics\n\n"
        "**Stop**\n"
        "`/leave` 🌐 Leave both VCs\n"
        "`/leaverecord` 🌐 Leave source only\n"
        "`/leaveplay` 🌐 Leave target only\n\n"
        "**Extra**\n"
        "`/vchat <text>` 👥 Send text in the assistant's VC chat _(or reply to a message)_\n"
        "`/handrise` 👥 Raise hand in the VC _(if an admin muted the assistant)_\n\n"
        "A bridge must be active before screenshare or rcplay.",
    ),
    "video": (
        "🖥 Screen & Video",
        "**🖥 Screen & Video** _(bridge required)_\n"
        "_Run in the source group._\n\n"
        "`/livescreenshare` 👥 Mirror source screen to target\n"
        "`/stoplivescreenshare` 👥 Stop mirror\n"
        "`/screenshare on|off` 👥 Screen relay toggle\n"
        "`/camera on|off` 👥 Camera relay toggle\n"
        "`/screencamera on|off` 👥 Screen + camera together\n"
        "`/screenshare` 👥 Reply to a photo or video\n"
        "`/screenshare 720p 20` 👥 Set quality and fps\n"
        "`/screenshareoff` 👥 Stop\n"
        "`/rcplay` 👥 Reply to a voice note or audio\n"
        "`/rcplayoff` 👥 Stop\n\n"
        "A 3-5 second delay is normal.",
    ),
    "fx": (
        "🎚 Sound Effects",
        "**🎚 Sound Effects** 👥 _(0-100)_\n\n"
        "`/eq` Button panel (easiest)\n\n"
        "`/level 1-50` Loudness\n"
        "`/volume 0-400` Fine volume\n"
        "`/bass 70` · `/treble 40` · `/clarity 60`\n"
        "`/gain 100` · `/loudness 50` · `/pitch 50` · `/echo 20`\n\n"
        "`/voice female|male|normal`\n"
        "`/preset dj` — `bambam` `heavy` `dj` `fight` `voice` `james` `default`\n\n"
        "`/effects` Current settings\n"
        "`/reset` Restore defaults\n"
        "`/swap` Swap stereo left/right (`/swap 1` on, `/swap 0` off)\n"
        "`/mute` · `/unmute`\n"
        "`/automute on|off`",
    ),
    "play": (
        "🎵 Music & Radio",
        "**🎵 Music & Radio** 👥\n\n"
        "`/yt song name` Play from YouTube\n"
        "`/yt https://youtu.be/...` Play by link\n"
        "`/radio https://stream-url` Internet radio\n"
        "`/joinvc` (or `/vc`) Join this group's VC without a bridge",
    ),
    "record": (
        "🎙 Recording",
        "**🎙 Recording** 🌐\n\n"
        "`/startrecord` Start recording\n"
        "`/startrecord 300` Set duration _(5-3600 sec)_\n"
        "`/stoprecord` Stop and receive MP3",
    ),
    "session": (
        "🔑 Assistant / Session",
        "**🔑 Assistant** 💬 _(each sudo has their own)_\n\n"
        "`/addsession SESSION_STRING` Add assistant (string)\n"
        "`/addphone +91XXXXXXXXXX` Add via phone OTP\n"
        "`/otp 12345` Submit OTP after /addphone\n"
        "`/otpsms` SMS nahi aaya? Resend OTP via SMS\n"
        "`/otpcancel` Cancel phone login\n"
        "`/dlsession` Remove assistant\n"
        "`/assistants` Assistant status\n\n"
        "⚠️ Never share your session string.",
    ),
    "owner": (
        "👑 Owner",
        "**👑 Owner only** 💬\n\n"
        "`/sudoadd ID` Add sudo _(or reply to a user)_\n"
        "`/sudodel ID` Remove sudo\n"
        "`/sudolist` List sudo users\n"
        "`/broadcast text` Message all active VCs\n"
        "`/logs` Recent logs\n"
        "`/restart` Restart bot\n"
        "`/lsdiag` 👥 Screenshare diagnostics",
    ),
    "faq": (
        "❓ Troubleshooting",
        "**❓ Troubleshooting**\n\n"
        "**No active VC found**\n"
        "Start a Voice Chat in the group.\n\n"
        "**No assistant / session**\n"
        "Send `/addsession` in the bot DM.\n\n"
        "**No access**\n"
        "Ask the owner to run `/sudoadd`.\n\n"
        "**Bridge not active**\n"
        "Run `/join -100TARGET_ID`.\n\n"
        "**Video not showing**\n"
        "Start a screen share in the source VC, then run `/livescreenshare`.\n\n"
        "**Volume too low or high**\n"
        "Use `/level 40` or `/eq`.\n\n"
        "**Still broken**\n"
        "`/leave`, then `/join` again. If it persists, run `/vcstatus`.",
    ),
    "all": (
        "📋 All Commands",
        "**📋 All Commands**\n\n"
        "**🌉 Bridge:** `/join` `/joins` `/status` `/vcstatus` `/leave` `/leaverecord` `/leaveplay` "
        "`/vchat` `/handrise`\n\n"
        "**🖥 Video:** `/livescreenshare` `/stoplivescreenshare` `/screenshare` `/screenshareoff` "
        "`/rcplay` `/rcplayoff` `/camera` `/screencamera`\n\n"
        "**🎚 Sound:** `/eq` `/level` `/volume` `/bass` `/treble` `/clarity` `/gain` `/loudness` "
        "`/pitch` `/echo` `/voice` `/preset` `/effects` `/reset` `/swap` `/mute` `/unmute` `/automute`\n\n"
        "**🎵 Play:** `/yt` `/radio` `/joinvc`\n\n"
        "**🎙 Record:** `/startrecord` `/stoprecord`\n\n"
        "**🔑 Session (DM):** `/addsession` `/addphone` `/otp` `/otpsms` `/otpcancel` `/dlsession` `/assistants`\n\n"
        "**👑 Owner:** `/sudoadd` `/sudodel` `/sudolist` `/broadcast` `/logs` `/restart` `/lsdiag`\n\n"
        "**Aliases:** `/vc`=`/joinvc` · `/youtube`=`/yt` · `/equalizer` `/equaliser`=`/eq` · `/cmds` `/commands`=`/help` · `/addstring` `/adsession`=`/addsession` · `/delsession` `/removeassistant`=`/dlsession` · `/loginphone` `/phonelogin`=`/addphone` · `/verifyotp` `/code`=`/otp` · `/resendotp` `/smsotp`=`/otpsms` · `/cancelotp` `/phonecancel`=`/otpcancel`\n\n"
        "Details: `/help bridge` · `/help video` · `/help eq`",
    ),
}

# ----------------------------------------------------------------------
# SIMPLE MODE — short, goal-based help. First-time users follow step1 -> step3.
# ----------------------------------------------------------------------
HELP_CATEGORIES.update({
    "step1": (
        "1️⃣ Add Assistant",
        "**Step 1 of 3 · Add assistant** 💬\n\n"
        "The assistant is a Telegram account that joins voice chats.\n\n"
        "Send in the **bot DM**:\n"
        "`/addsession SESSION_STRING`\n\n"
        "No string? Run `python gen_string.py`.\n"
        "⚠️ Keep it private.",
    ),
    "step2": (
        "2️⃣ Start Bridge",
        "**Step 2 of 3 · Start bridge** 👥\n\n"
        "Start a **Voice Chat in both groups** first.\n\n"
        "In the source group (audio to send):\n"
        "`/join -100TARGET_ID`\n\n"
        "Target = group that will hear the audio.\n"
        "Verify: `/status`",
    ),
    "step3": (
        "3️⃣ Audio / Video",
        "**Step 3 of 3 · Use it** 👥\n\n"
        "`/eq` Audio panel\n"
        "`/livescreenshare` Mirror video to target\n\n"
        "Stop: `/leave`\n\n"
        "Done. Open the Help Menu for more.",
    ),
    "a_audio": (
        "🔊 Send Audio",
        "**🔊 Send audio between VCs** 👥\n\n"
        "`/join -100ID` Start\n"
        "`/leave` Stop\n"
        "`/status` Check state\n\n"
        "Requires an assistant and active VCs (Steps 1-2).",
    ),
    "a_video": (
        "🖥 Send Video",
        "**🖥 Send video / screen** 👥 _(`/join` first)_\n\n"
        "`/livescreenshare` Mirror source screen\n"
        "`/stoplivescreenshare` Stop\n"
        "`/screenshare on|off` Screen relay toggle\n"
        "`/camera on|off` Camera relay toggle\n"
        "`/screencamera on|off` Screen + camera together\n"
        "`/screenshare` Reply to a photo or video\n\n"
        "A 3-5 second delay is normal.",
    ),
    "a_fx": (
        "🎚 Adjust Sound",
        "**🎚 Adjust sound** 👥\n\n"
        "`/eq` Button panel\n"
        "`/level 30` Loudness (1-50)\n"
        "`/reset` Restore defaults\n\n"
        "More: Help Menu → Sound Effects.",
    ),
})

_CATEGORY_ORDER = ["quick", "bridge", "video", "fx", "play", "record", "session", "faq", "owner", "all"]

# /help <word> shortcuts
_HELP_ALIASES = {
    "step1": "step1", "step2": "step2", "step3": "step3",
    "start": "quick", "quick": "quick", "guide": "quick",
    "bridge": "bridge", "join": "bridge", "leave": "bridge",
    "vchat": "bridge", "handrise": "bridge",
    "video": "video", "screen": "video", "screenshare": "video", "livescreenshare": "video",
    "rcplay": "video", "live": "video",
    "camera": "video", "screencamera": "video", "swap": "fx",
    "fx": "fx", "eq": "fx", "sound": "fx", "effects": "fx", "bass": "fx", "volume": "fx",
    "play": "play", "music": "play", "yt": "play", "radio": "play",
    "record": "record", "recording": "record",
    "session": "session", "assistant": "session", "addsession": "session",
    "addphone": "session", "otp": "session", "otpsms": "session", "otpcancel": "session",
    "owner": "owner", "sudo": "owner",
    "faq": "faq", "problem": "faq", "help": "faq", "error": "faq",
    "all": "all", "cheatsheet": "all", "list": "all",
}


def _help_overview_text() -> str:
    return (
        f"**📖 VCFight {VERSION_TAG}**\n"
        f"_Relay voice and video from one VC to another._\n\n"
        f"**What would you like to do?**\n"
        f"First time? Start with the top button."
    )


def _help_full_text() -> str:
    return (
        f"**📚 All Categories**\n\n"
        f"{HELP_LEGEND}"
    )


def _help_keyboard() -> InlineKeyboardMarkup:
    def btn(k, label=None):
        return InlineKeyboardButton(label or HELP_CATEGORIES[k][0], callback_data=f"help_cat_{k}")

    return InlineKeyboardMarkup(
        [
            [btn("step1", "🚀 Quick Start")],
            [btn("a_audio"), btn("a_video")],
            [btn("a_fx"), btn("faq")],
            [InlineKeyboardButton("📚 All Commands", callback_data="help_full")],
        ]
    )


def _help_full_keyboard() -> InlineKeyboardMarkup:
    def btn(k):
        return InlineKeyboardButton(HELP_CATEGORIES[k][0], callback_data=f"help_cat_{k}")

    return InlineKeyboardMarkup(
        [
            [btn("bridge"), btn("video")],
            [btn("fx"), btn("play")],
            [btn("record"), btn("session")],
            [btn("all"), btn("owner")],
            [InlineKeyboardButton("🏠 Help Menu", callback_data="help_home")],
        ]
    )


def _help_category_keyboard(key: str) -> InlineKeyboardMarkup:
    rows = []
    flow = ["step1", "step2", "step3"]
    if key in flow:
        i = flow.index(key)
        nav = []
        if i > 0:
            nav.append(InlineKeyboardButton("◀️ Back", callback_data=f"help_cat_{flow[i - 1]}"))
        if i < len(flow) - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"help_cat_{flow[i + 1]}"))
        if nav:
            rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Help Menu", callback_data="help_home")])
    return InlineKeyboardMarkup(rows)


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Quick Start", callback_data="help_cat_step1")],
            [
                InlineKeyboardButton("📖 Help", callback_data="help_menu"),
                InlineKeyboardButton("❓ Support", callback_data="help_cat_faq"),
            ],
            [
                InlineKeyboardButton("👑 Owner", url=OWNER_URL),
                InlineKeyboardButton("📢 Channel", url=CHANNEL_URL),
            ],
        ]
    )


def _start_caption(name: str, status: str, ass_name: str = "—") -> str:
    return (
        f"🎙️ **VCFIGHT {VERSION_TAG}**\n"
        f"_Relay voice and video between voice chats._\n\n"
        f"Hello **{name}** 👋\n\n"
        f"🟢 Status　`{status}`\n"
        f"🤖 Assistant　`{ass_name}`\n\n"
        f"**Quick start**\n"
        f"`1` 💬 `/addsession STRING`\n"
        f"`2` 👥 `/join -100TARGET_ID`\n"
        f"`3` 👥 `/eq` · `/livescreenshare`\n\n"
        f"Stop: `/leave`"
    )


class AdminCommands:
    def __init__(self, bot: Client, assistant_manager, db, audio_bridge, bridge_cmds=None):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.db = db
        self.audio_bridge = audio_bridge   # owner ka bridge
        self.bridge_cmds = bridge_cmds     # for /broadcast: active VC chat ids
        self.register_handlers()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _user_bridge(self, user_id: int):
        if user_id == OWNER_ID:
            return self.audio_bridge
        return get_user_bridge(user_id)

    async def _do_add_session(self, message: Message, session_string: str, uid: int):
        own = is_owner(uid)
        status_msg = await message.reply("**Saving session...**")
        try:
            session_string = session_string.strip()

            # Session string wala message chat se hata do (best effort)
            try:
                await message.delete()
            except Exception:
                pass

            # Apna purana assistant (agar hai) replace hoga — max 1 per user
            old = self.assistant_manager.get_user_assistant(uid)
            if old:
                bridge = self._user_bridge(uid)
                if bridge:
                    try:
                        await bridge.stop_all_bridges()
                    except Exception:
                        pass
                try:
                    await self.assistant_manager.remove_assistant(old["session"])
                    if own:
                        self.db.remove_assistant(old["session"])
                except Exception:
                    pass
                if not own:
                    drop_user_bridge(uid)

            if own:
                SESSION_FILE.write_text(session_string + "\n", encoding="utf-8")

            result = await self.assistant_manager.add_assistant(
                session_string, owner_id=uid
            )

            if isinstance(result, tuple):
                ok, err_msg = result
            else:
                ok, err_msg = bool(result), "Invalid session."

            if ok:
                assistant = (
                    self.assistant_manager.get_user_assistant(uid)
                    or self.assistant_manager.get_assistant()
                )
                if own:
                    self.db.save_assistant(
                        session_string,
                        assistant["name"],
                        assistant["user_id"],
                        assistant.get("username"),
                    )
                else:
                    save_user_session(uid, session_string)

                uname = assistant.get("username") or assistant.get("name") or "assistant"
                file_line = "File · `assistant_session.txt`\n" if own else ""
                await status_msg.edit(
                    f"**✅ Session Saved + Connected**\n\n"
                    f"{file_line}"
                    f"Assistant · `{uname}`\n"
                    f"User ID · `{assistant.get('user_id')}`\n\n"
                    f"Restart pe auto-load.\n"
                    f"Remove: `/dlsession`"
                )
            else:
                prefix = "**File saved, connect fail**" if own else "**Connect fail**"
                await status_msg.edit(
                    f"{prefix}\n\n"
                    f"`{err_msg}`\n\n"
                    f"Nayi string ke saath `/addsession` dubara chalao."
                )
        except Exception as e:
            await status_msg.edit(f"**Error**\n\n`{str(e)[:200]}`")

    async def _do_del_session(self, message: Message, uid: int):
        own = is_owner(uid)
        status_msg = await message.reply("**Removing session...**")
        try:
            bridge = self._user_bridge(uid)
            if bridge:
                try:
                    await bridge.stop_all_bridges()
                except Exception:
                    pass

            mine = self.assistant_manager.get_user_assistant(uid)
            if mine:
                session_string = mine["session"]
                await self.assistant_manager.remove_assistant(session_string)
                if own:
                    try:
                        self.db.remove_assistant(session_string)
                    except Exception:
                        pass

            if own:
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
            else:
                remove_user_session(uid)
                drop_user_bridge(uid)

            await status_msg.edit(
                "**✅ Session Removed**\n\n"
                "Phir se: `/addsession YOUR_SESSION`"
            )
        except Exception as e:
            await status_msg.edit(f"**Error**\n\n`{str(e)[:200]}`")

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    def register_handlers(self):

        @self.bot.on_message(filters.command("start"))
        async def start_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            name = message.from_user.first_name or "there"

            # Sudo apna assistant dekhta hai, baaki log owner ka
            if self._is_sudo(uid):
                ass = self.assistant_manager.get_assistant(user_id=uid)
                waiting = "⏳ Add your session 👇" if not is_owner(uid) else "⏳ Waiting for assistant"
            else:
                ass = self.assistant_manager.get_assistant(user_id=OWNER_ID)
                waiting = "⏳ Waiting for assistant"

            status = "✅ Ready" if ass else waiting
            ass_name = "—"
            if ass:
                uname = ass.get("username")
                ass_name = f"@{uname}" if uname else ass.get("name") or "Connected"

            caption = _start_caption(name, status, ass_name)
            kb = _start_keyboard()

            try:
                await message.reply_video(
                    video=START_VIDEO_URL,
                    caption=caption,
                    reply_markup=kb,
                    supports_streaming=True,
                )
                return
            except Exception:
                pass

            if START_IMAGE.exists():
                try:
                    await message.reply_photo(
                        photo=str(START_IMAGE),
                        caption=caption,
                        reply_markup=kb,
                    )
                    return
                except Exception:
                    pass

            await message.reply(caption, reply_markup=kb)

        @self.bot.on_callback_query(filters.regex("^repo_roast$"))
        async def repo_roast_cb(client, query: CallbackQuery):
            await query.answer("bhag ja bheekh mangiya", show_alert=True)

        @self.bot.on_callback_query(filters.regex("^help_menu$"))
        async def help_menu_cb(client, query: CallbackQuery):
            try:
                await query.message.reply(
                    _help_overview_text(), reply_markup=_help_keyboard()
                )
            except Exception:
                pass
            await query.answer()

        @self.bot.on_callback_query(filters.regex(r"^help_cat_(\w+)$"))
        async def help_category_cb(client, query: CallbackQuery):
            key = query.matches[0].group(1)
            entry = HELP_CATEGORIES.get(key)
            if not entry:
                return await query.answer()
            _, body = entry
            try:
                # /start ke "Quick Join" button ek photo/video caption se
                # aata hai — us message ko category text me edit nahi kar
                # sakte, isliye naya reply. Help-menu se aaya callback wahi
                # plain-text message hai, use edit karte hain (smooth nav).
                if query.message.photo or query.message.video:
                    await query.message.reply(body, reply_markup=_help_category_keyboard(key))
                else:
                    await query.message.edit(body, reply_markup=_help_category_keyboard(key))
            except Exception:
                pass
            await query.answer()

        @self.bot.on_callback_query(filters.regex("^help_full$"))
        async def help_full_cb(client, query: CallbackQuery):
            try:
                await query.message.edit(
                    _help_full_text(), reply_markup=_help_full_keyboard()
                )
            except Exception:
                pass
            await query.answer()

        @self.bot.on_callback_query(filters.regex("^help_home$"))
        async def help_home_cb(client, query: CallbackQuery):
            try:
                await query.message.edit(
                    _help_overview_text(), reply_markup=_help_keyboard()
                )
            except Exception:
                pass
            await query.answer()

        @self.bot.on_message(filters.command(["help", "cmds", "commands"]))
        async def help_cmd(client, message: Message):
            # `/help bridge`, `/help eq`, `/help screenshare` ... seedha wahi page
            if len(message.command) > 1:
                key = _HELP_ALIASES.get(message.command[1].lower().lstrip("/"))
                if key:
                    return await message.reply(
                        HELP_CATEGORIES[key][1], reply_markup=_help_category_keyboard(key)
                    )
            await message.reply(_help_overview_text(), reply_markup=_help_keyboard())

        @self.bot.on_message(
            filters.command(["addsession", "adsession", "addstring"]) & filters.private
        )
        async def addsession_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("**Sudo only.**")

            if len(message.command) < 2:
                if is_owner(uid):
                    has = "exists" if SESSION_FILE.exists() else "empty"
                else:
                    has = "connected" if self.assistant_manager.get_user_assistant(uid) else "empty"
                return await message.reply(
                    "**Add Session**\n\n"
                    "`/addsession YOUR_SESSION_STRING`\n\n"
                    f"Tera session · `{has}`\n"
                    "Remove: `/dlsession`"
                )

            session_string = message.text.split(None, 1)[1].strip()
            await self._do_add_session(message, session_string, uid)

        @self.bot.on_message(
            filters.command(["dlsession", "delsession", "removeassistant"]) & filters.private
        )
        async def dlsession_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("**Sudo only.**")
            await self._do_del_session(message, uid)

        @self.bot.on_message(filters.command("assistants") & filters.private)
        async def list_assistants_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("Sudo only.")

            # Sudo: sirf apna status
            if not is_owner(uid):
                mine = self.assistant_manager.get_user_assistant(uid)
                if not mine:
                    return await message.reply(
                        "**Add your own session** 👇\n\n`/addsession SESSION`"
                    )
                uname = mine.get("username") or mine.get("name") or "?"
                return await message.reply(
                    f"**Tera assistant**\n\nLive · `{uname}` · `{mine.get('user_id')}`\n\n"
                    f"Remove: `/dlsession`"
                )

            # Owner: sabka status
            file_ok = SESSION_FILE.exists()
            assistants = self.assistant_manager.get_all_assistants()
            if not assistants and not file_ok:
                return await message.reply(
                    "**No session**\n\n`/addsession SESSION`"
                )

            lines = ["**Session status**\n"]
            lines.append(f"File · `{'yes' if file_ok else 'no'}`\n")
            if assistants:
                for a in assistants:
                    uname = a.get("username") or a.get("name") or "?"
                    lines.append(
                        f"Live · `{uname}` · `{a.get('user_id')}` · owner `{a.get('owner_id')}`"
                    )
            else:
                lines.append("Live · not connected")
            lines.append("\nRemove: `/dlsession`")
            await message.reply("\n".join(lines))

        @self.bot.on_message(filters.command("broadcast") & filters.private)
        async def broadcast_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")

            if len(message.command) < 2:
                return await message.reply("`/broadcast text`")

            broadcast_text = message.text.split(None, 1)[1]
            status_msg = await message.reply("Sending…")

            chat_ids = set()
            if self.bridge_cmds is not None:
                for routes in self.bridge_cmds.active_bridges.values():
                    for meta in routes.values():
                        try:
                            chat_ids.add(int(meta["target_id"]))
                        except Exception:
                            pass
                for uid, routes in self.bridge_cmds.active_bridges.items():
                    for source_id in routes.keys():
                        try:
                            chat_ids.add(int(source_id))
                        except Exception:
                            pass

            if not chat_ids:
                return await status_msg.edit("No active VC right now.")

            count = 0
            for cid in chat_ids:
                try:
                    await client.send_message(cid, broadcast_text)
                    count += 1
                except Exception:
                    pass
            await status_msg.edit(f"**Sent** · {count}/{len(chat_ids)} VCs")

        @self.bot.on_message(filters.command("sudoadd") & filters.private)
        async def sudoadd_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")
            target = None
            if message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
            elif len(message.command) >= 2:
                try:
                    target = int(message.command[1].strip())
                except ValueError:
                    return await message.reply("Provide a user ID or reply to the user's message.")
            else:
                return await message.reply("`/sudoadd USER_ID` ya kisi user ke message pe reply karke `/sudoadd`")

            if is_sudo(target):
                return await message.reply(f"`{target}` is already sudo.")
            add_sudo(target)
            await message.reply(f"**Sudo added** · `{target}`")

        @self.bot.on_message(filters.command("sudodel") & filters.private)
        async def sudodel_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")
            target = None
            if message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
            elif len(message.command) >= 2:
                try:
                    target = int(message.command[1].strip())
                except ValueError:
                    return await message.reply("Provide a user ID or reply to the user's message.")
            else:
                return await message.reply("`/sudodel USER_ID` ya kisi user ke message pe reply karke `/sudodel`")

            if target == OWNER_ID:
                return await message.reply("The owner cannot be removed.")
            if not remove_sudo(target):
                return await message.reply(f"`{target}` is not sudo.")
            await message.reply(f"**Sudo removed** · `{target}`")

        @self.bot.on_message(filters.command("sudolist") & filters.private)
        async def sudolist_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")
            from config.settings import SUDO_USERS
            from core.sudo import list_removed_env_sudo
            dyn = list_dynamic_sudo()
            blocked = set(list_removed_env_sudo())
            env_active = [u for u in SUDO_USERS if int(u) not in blocked]
            lines = [f"**👑 Owner** · `{OWNER_ID}`", ""]
            lines.append("**Sudo (env, fixed)**")
            lines += [f"• `{u}`" for u in env_active] or ["• (none)"]
            lines.append("\n**Sudo (added via /sudoadd)**")
            lines += [f"• `{u}`" for u in dyn] or ["• (none)"]
            if blocked:
                lines.append("\n**Blocked via /sudodel (env id, overridden)**")
                lines += [f"• `{u}`" for u in blocked]
            await message.reply("\n".join(lines))

        @self.bot.on_message(filters.command("restart") & filters.private)
        async def restart_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")

            await message.reply("**Restarting…**")
            if self.audio_bridge:
                await self.audio_bridge.stop_all_bridges()
            for b in all_user_bridges().values():
                try:
                    await b.stop_all_bridges()
                except Exception:
                    pass

            os.execv(sys.executable, [sys.executable] + sys.argv)

        @self.bot.on_message(filters.command("logs") & filters.private)
        async def logs_cmd(client, message: Message):
            if not message.from_user or not is_owner(message.from_user.id):
                return await message.reply("Owner only.")

            try:
                with open("logs/vcfight.log", "r") as f:
                    logs = f.read()[-3000:]
                if len(logs) > 4000:
                    for i in range(0, len(logs), 4000):
                        await message.reply(f"```{logs[i:i+4000]}```")
                else:
                    await message.reply(f"```{logs}```")
            except Exception as e:
                await message.reply(f"Logs fail: {e}")

    def _is_sudo(self, user_id: int) -> bool:
        return is_sudo(user_id)
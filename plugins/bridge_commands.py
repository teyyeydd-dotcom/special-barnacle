import asyncio
import logging
from typing import Dict
from pyrogram import Client, filters
from pyrogram.types import Message
from datetime import datetime

from core.sudo import is_sudo, is_owner, OWNER_ID
from core.bridge_state import set_live_bridge
from core.user_bridges import get_user_bridge, set_user_bridge
from core.user_effects import get_user_config

logger = logging.getLogger(__name__)

# Sudo user ke paas apna assistant nahi hai
SESSION_MSG = (
    "👤 **Apna khud ka session lagao** 👇\n\n"
    "Ye assistant owner ka hai, tumhara nahi.\n"
    "Bot ko **private chat** mein `/addsession SESSION_STRING` bhejo, "
    "phir yahan dobara command chalao.\n\n"
    "⚠️ Session string group mein kabhi mat bhejna."
)

# Owner ka apna assistant offline hai
OWNER_OFFLINE_MSG = (
    "**Assistant offline**\n\n"
    "Private mein `/addsession SESSION`"
)


# The real raw-MTProto leave (correct SSRC, verified against Telegram) lives
# in core/vc_leave.py and is shared by /leave, /leaverecord and /leaveplay
# (see plugins/extra_commands.py) — one leave implementation, not several
# slightly different ones, so there's only one place left to ever have to
# fix this again. /stopall was removed (2026-09) — /leave already does the
# same "stop everything" job, having both was confusing.


class BridgeCommands:
    def __init__(self, bot: Client, assistant_manager, audio_bridge, db):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.audio_bridge = audio_bridge   # owner ka bridge
        self.db = db
        # user_id -> {source_id: meta}
        self.active_bridges: Dict[int, Dict[int, dict]] = {}
        if audio_bridge is not None:
            set_live_bridge(audio_bridge)
        self.register_handlers()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _no_assistant_text(self, user_id: int) -> str:
        return OWNER_OFFLINE_MSG if is_owner(user_id) else SESSION_MSG

    def _get_assistant(self, user_id: int):
        """Sirf usi user ka apna assistant. Owner ka session kisi aur ko nahi milta."""
        return self.assistant_manager.get_assistant(user_id=user_id)

    def _get_bridge(self, user_id: int):
        if user_id == OWNER_ID:
            return self.audio_bridge
        return get_user_bridge(user_id)

    def _meta(self, user_id: int) -> Dict[int, dict]:
        return self.active_bridges.setdefault(user_id, {})

    def _rebuild_bridge(self, assistant: dict, user_id: int):
        from core.bridge import AudioBridge

        old_bridge = self._get_bridge(user_id)   # will be retired below

        # Effects config: owner ki apni, sudo ki apni saved config
        if user_id == OWNER_ID:
            src_bridge = self._get_bridge(user_id) or self.audio_bridge
            old_cfg = {}
            if src_bridge is not None:
                old_cfg = dict(getattr(src_bridge, "config", {}) or {})
        else:
            old_cfg = dict(get_user_config(user_id))

        bridge = AudioBridge(
            assistant["bridge"],
            self_user_id=assistant.get("user_id") or 0,
            mtproto=assistant.get("client"),
            is_owner=(user_id == OWNER_ID),
        )
        if old_cfg:
            bridge.config.update(old_cfg)
            bridge.config["muted"] = False
            bridge.sync_effects()

        if user_id == OWNER_ID:
            self.audio_bridge = bridge
            set_live_bridge(bridge)
        else:
            # Sudo user ka bridge owner ke live bridge ko overwrite nahi karega
            set_user_bridge(user_id, bridge)

        # The old bridge's handlers stay registered on the same PyTgCalls client and would
        # keep processing every frame (duplicate RX in the log). Silence it.
        if old_bridge is not None and old_bridge is not bridge:
            old_bridge._retired = True
        return bridge

    def _ensure_bridge(self, assistant: dict, user_id: int):
        bridge = self._get_bridge(user_id)
        if not assistant:
            return bridge

        pytg = assistant.get("bridge")
        if bridge is None:
            return self._rebuild_bridge(assistant, user_id)

        if pytg is not None and getattr(bridge, "client", None) is not pytg:
            if hasattr(bridge, "bind_client"):
                bridge.bind_client(pytg, assistant.get("client"))
            else:
                return self._rebuild_bridge(assistant, user_id)
        elif hasattr(bridge, "bind_client"):
            bridge.bind_client(bridge.client, assistant.get("client"))

        if user_id == OWNER_ID:
            set_live_bridge(bridge)
        return bridge

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    def register_handlers(self):

        @self.bot.on_message(filters.command("join") & filters.group)
        async def join_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            if len(message.command) < 2:
                return await message.reply(
                    "**Join (Bridge)**\n\n"
                    "`/join -100CHAT_ID`\n"
                    "`/join https://t.me/+HASH`  <- best\n\n"
                    "**Design:**\n"
                    "• Main bot = sirf source group\n"
                    "• Assistant = source + target (VC fight)\n\n"
                    "Dono groups me VC ON rakho.\n"
                    "Band: `/leave`"
                )

            status_msg = await message.reply("Joining...")

            try:
                source_id = message.chat.id
                target_input = message.command[1].strip()
                assistant = self._get_assistant(uid)
                if not assistant:
                    await status_msg.edit(self._no_assistant_text(uid))
                    return

                await self.assistant_manager.ensure_mtproto_ready(assistant)
                bridge = self._rebuild_bridge(assistant, uid)

                source_name = message.chat.title or str(source_id)
                uname = assistant.get("username") or "assistant"

                # 1) Assistant in SOURCE
                ok_src, msg_src, _ = await self.assistant_manager.ensure_in_group(
                    source_id, assistant=assistant
                )
                if not ok_src:
                    await status_msg.edit(
                        f"**Assistant source group mein nahi**\n"
                        f"@{uname}\n\n"
                        f"{msg_src[:120] if msg_src else ''}\n\n"
                        f"Phone pe @{uname} se is group join karo."
                    )
                    return

                # 2) Assistant join/resolve TARGET (NO main-bot dependency)
                ok_tgt, msg_tgt, target_id = await self.assistant_manager.ensure_in_group(
                    target_input, assistant=assistant
                )

                if not ok_tgt or not target_id:
                    is_id = target_input.lstrip("-").isdigit()
                    if is_id:
                        await status_msg.edit(
                            f"**Target assistant ke paas nahi**\n\n"
                            f"Assistant: @{uname}\n"
                            f"Target: `{target_input}`\n\n"
                            f"**Fix:**\n\n"
                            f"**A) Invite link (best)**\n"
                            f"`/join https://t.me/+HASH`\n\n"
                            f"**B) Manual**\n"
                            f"1. Phone pe @{uname} kholo\n"
                            f"2. Target group join karo\n"
                            f"3. Group me msg bhejo\n"
                            f"4. `/join {target_input}` dubara"
                        )
                    else:
                        await status_msg.edit(
                            f"**Target fail**\n\n{msg_tgt}\n\n"
                            f"Try: `/join https://t.me/+HASH`"
                        )
                    return

                target_name = str(target_id)
                try:
                    tchat = await assistant["client"].get_chat(target_id)
                    target_name = tchat.title or target_name
                except Exception:
                    pass

                await status_msg.edit("VC join...")
                await self.assistant_manager.ensure_voice_chat(source_id, assistant=assistant)
                await asyncio.sleep(0.2)
                await self.assistant_manager.ensure_voice_chat(target_id, assistant=assistant)
                await asyncio.sleep(0.15)

                result = await bridge.start_bridge(source_id, target_id)
                if isinstance(result, tuple):
                    ok, err = result
                else:
                    ok, err = bool(result), getattr(bridge, "_last_join_error", "") or ""

                if not ok:
                    await status_msg.edit("Retry...")
                    ok_rb2, _, _ = await self.assistant_manager.rebind_pytgcalls(assistant)
                    if ok_rb2:
                        bridge = self._rebuild_bridge(assistant, uid)
                        await asyncio.sleep(0.25)
                        result = await bridge.start_bridge(source_id, target_id)
                        if isinstance(result, tuple):
                            ok, err = result
                        else:
                            ok, err = bool(result), getattr(bridge, "_last_join_error", "") or ""

                if ok:
                    if uid == OWNER_ID:
                        set_live_bridge(bridge)
                    effects = getattr(bridge, "config", None) or {}
                    self._meta(uid)[source_id] = {
                        "target_id": target_id,
                        "target_name": target_name,
                        "source_name": source_name,
                        "started_at": datetime.now(),
                    }
                    try:
                        self.db.save_bridge(
                            source_id, target_id, source_name, target_name,
                            effects.get("gain", 0),
                            effects.get("treble", 0),
                            effects.get("clarity", 0),
                            effects.get("bass", 0),
                            owner_id=uid,
                        )
                    except Exception:
                        pass

                    # automute: reset by _rebuild_bridge() above (muted=False
                    # on every rebuild), so re-apply the user's saved choice
                    # AFTER a successful join — same effects config /automute
                    # (audio_effects.py) writes into, just read back here.
                    automute_line = ""
                    if effects.get("automute"):
                        try:
                            await bridge.set_muted(True)
                            bridge.config["muted"] = True
                            automute_line = "\nAuto-mute · `on` (VC muted)"
                        except Exception:
                            pass

                    await status_msg.edit(
                        f"BRIDGE ON\n"
                        f"Source: `{source_name}`\n"
                        f"Target: `{target_name}`\n"
                        f"ID: `{target_id}`\n"
                        f"@{uname} | `/leave`"
                        f"{automute_line}"
                    )
                else:
                    detail = err or getattr(bridge, "_last_join_error", "") or "unknown"
                    await status_msg.edit(
                        f"**VC join fail**\n`{detail[:200]}`\n\n"
                        f"Dono GC me Video Chat ON rakho."
                    )

            except Exception as e:
                await status_msg.edit(f"Error: `{str(e)[:200]}`")

        @self.bot.on_message(filters.command("joins"))
        async def list_bridges_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            if is_owner(uid):
                rows = [
                    (u, b)
                    for u, routes in self.active_bridges.items()
                    for b in routes.values()
                ]
            else:
                rows = [(uid, b) for b in self._meta(uid).values()]

            if not rows:
                return await message.reply("**Active**\n\nNo routes.")

            text = "**Active**\n\n"
            for u, b in rows:
                tag = f" (`{u}`)" if is_owner(uid) and u != OWNER_ID else ""
                text += f"• {b['source_name']} → {b['target_name']}{tag}\n"
            await message.reply(text)

        @self.bot.on_message(filters.command("status"))
        async def status_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            bridge = self._get_bridge(uid)
            cfg = getattr(bridge, "config", None) or get_user_config(uid)
            muted = bool(cfg.get("muted", False))
            automute = bool(cfg.get("automute", False))

            lines = ["**📊 Status**\n"]
            lines.append(
                f"Gain `{cfg.get('gain', 0)}` · Loud `{cfg.get('loudness', 0)}` "
                f"(vol `{int(float(cfg.get('loudness', 0) or 0) * 4)}`)"
            )
            lines.append(
                f"Bass `{cfg.get('bass', 0)}` · Treble `{cfg.get('treble', 0)}` "
                f"· Clarity `{cfg.get('clarity', 0)}`"
            )
            lines.append(
                f"Mute · `{'ON' if muted else 'off'}` · "
                f"Automute · `{'on' if automute else 'off'}`"
            )

            if is_owner(uid):
                rows = [
                    (u, b)
                    for u, routes in self.active_bridges.items()
                    for b in routes.values()
                ]
            else:
                rows = [(uid, b) for b in self._meta(uid).values()]

            lines.append(f"\n**Active VCs** · `{len(rows)}`")
            for u, b in rows[:15]:
                tag = f" (`{u}`)" if is_owner(uid) and u != OWNER_ID else ""
                lines.append(f"• {b['source_name']} → {b['target_name']}{tag}")
            if len(rows) > 15:
                lines.append(f"… +{len(rows) - 15} more")

            lines.append("\n🚀 ULTRA HARDWARE-MAXIMIZING SATURATOR IS ACTIVE.")
            await message.reply("\n".join(lines))

    def _is_sudo(self, user_id: int) -> bool:
        return is_sudo(user_id)

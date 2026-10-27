import asyncio
import json
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from core.effects import AudioEffects, RANGES
from core.sudo import is_sudo, OWNER_ID
from core.bridge_state import get_live_bridge, set_live_bridge
from core.user_bridges import get_user_bridge, set_user_bridge
from core.user_effects import get_user_config, save_user_configs
from plugins.fx_ui import (
    ALERT_NOSUDO,
    FX_KEYS,
    fx_panel_caption,
    home_keyboard,
    values_keyboard,
    value_caption,
)


class AudioEffectsCommands:
    def __init__(self, bot: Client, audio_bridge, config, assistant_manager):
        self.bot = bot
        self.audio_bridge = audio_bridge   # owner ka bridge
        self.config = config               # owner ki config
        self.assistant_manager = assistant_manager
        if audio_bridge is not None:
            set_live_bridge(audio_bridge)
        self.register_handlers()

    # ------------------------------------------------------------------
    # per-user helpers
    # ------------------------------------------------------------------
    def _cfg(self, uid: int) -> dict:
        """Owner ki config self.config, sudo ki apni alag config."""
        if uid == OWNER_ID:
            return self.config
        return get_user_config(uid)

    def _ensure_bridge(self, uid: int):
        cfg = self._cfg(uid)

        # ---------------- Owner: purana logic ----------------
        if uid == OWNER_ID:
            live = get_live_bridge()
            if live is not None:
                self.audio_bridge = live
                live.config = cfg
                if hasattr(live, "eq"):
                    live.eq.muted = bool(cfg.get("muted", False))
                return live

            if self.audio_bridge is not None:
                self.audio_bridge.config = cfg
                if hasattr(self.audio_bridge, "eq"):
                    self.audio_bridge.eq.muted = bool(cfg.get("muted", False))
                set_live_bridge(self.audio_bridge)
                return self.audio_bridge

            assistant = (
                self.assistant_manager.get_assistant(user_id=OWNER_ID)
                if self.assistant_manager else None
            )
            if not assistant:
                return None

            from core.bridge import AudioBridge

            self.audio_bridge = AudioBridge(
                assistant["bridge"],
                self_user_id=assistant.get("user_id") or 0,
                mtproto=assistant.get("client"),
            )
            self.audio_bridge.config = cfg
            self.audio_bridge.sync_effects()
            set_live_bridge(self.audio_bridge)
            return self.audio_bridge

        # ---------------- Sudo: apna bridge, apni config ----------------
        bridge = get_user_bridge(uid)
        if bridge is None:
            assistant = (
                self.assistant_manager.get_assistant(user_id=uid)
                if self.assistant_manager else None
            )
            if not assistant:
                return None

            from core.bridge import AudioBridge

            bridge = AudioBridge(
                assistant["bridge"],
                self_user_id=assistant.get("user_id") or 0,
                mtproto=assistant.get("client"),
                is_owner=False,
            )
            bridge.config = cfg
            bridge.sync_effects()
            set_user_bridge(uid, bridge)   # global live bridge ko touch nahi karta
            return bridge

        bridge.config = cfg
        if hasattr(bridge, "eq"):
            bridge.eq.muted = bool(cfg.get("muted", False))
        return bridge

    def _apply_live(self, uid: int, **kwargs):
        """Instant in-memory apply (no await) so buttons feel fast."""
        clean = {}
        for k, v in kwargs.items():
            if k == "muted":
                clean["muted"] = bool(v)
                continue
            if v is None:
                continue
            try:
                lo, hi = RANGES.get(k, (0, 100))
                # gain/loudness: only floor at 0, hi is 99999 (effectively uncapped)
                # bass/treble/clarity/echo/pitch: normal 0-100 clamp
                clean[k] = max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        cfg = self._cfg(uid)
        cfg.update(clean)
        ab = self._ensure_bridge(uid)
        if ab:
            ab.config = cfg
            for k, v in clean.items():
                ab.config[k] = v
            ab.sync_effects()

    async def _apply(self, uid: int, chat_id: int, **kwargs):
        self._apply_live(uid, **kwargs)
        cfg = self._cfg(uid)
        ab = self._ensure_bridge(uid)
        if ab:
            try:
                await ab.update_effects(chat_id or 0, **{
                    k: cfg.get(k) for k in kwargs if k in cfg
                })
            except Exception:
                pass
        await self._save_config(uid)

    async def _save_config(self, uid: int):
        try:
            if uid == OWNER_ID:
                with open("bridge_config.json", "w") as f:
                    json.dump({"effects": self.config}, f, indent=4)
            else:
                save_user_configs()
        except Exception:
            pass

    async def _edit(self, query: CallbackQuery, text, markup):
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await query.edit_message_caption(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    def register_handlers(self):

        @self.bot.on_callback_query(filters.regex(r"^fxm:(gain|loudness|bass|pitch|treble|clarity|echo):(\d+)$"))
        async def fx_menu_cb(client, query: CallbackQuery):
            uid = query.from_user.id
            if not self._is_sudo(uid):
                return await query.answer(ALERT_NOSUDO, show_alert=True)
            kind = query.data.split(":")[1]
            await query.answer()
            await self._edit(query, value_caption(kind, self._cfg(uid)), values_keyboard(kind))

        @self.bot.on_callback_query(filters.regex(r"^fxs:(gain|loudness|bass|pitch|treble|clarity|echo):(\d+)$"))
        async def fx_set_cb(client, query: CallbackQuery):
            uid = query.from_user.id
            if not self._is_sudo(uid):
                return await query.answer(ALERT_NOSUDO, show_alert=True)
            parts = query.data.split(":")
            kind = parts[1]
            val = max(0, min(100, int(parts[2])))
            chat_id = query.message.chat.id if query.message else 0
            self._apply_live(uid, **{kind: val, "muted": False})
            await query.answer(f"{kind} → {val}")
            await self._edit(query, value_caption(kind, self._cfg(uid)), values_keyboard(kind))
            asyncio.create_task(self._save_config(uid))
            ab = self._ensure_bridge(uid)
            if ab:
                try:
                    await ab.update_effects(chat_id, **{kind: val, "muted": False})
                except Exception:
                    pass

        @self.bot.on_callback_query(filters.regex(r"^fxb$"))
        async def fx_back_cb(client, query: CallbackQuery):
            uid = query.from_user.id
            if not self._is_sudo(uid):
                return await query.answer(ALERT_NOSUDO, show_alert=True)
            await query.answer()
            await self._edit(query, fx_panel_caption(self._cfg(uid)), home_keyboard())

        @self.bot.on_message(filters.command("eq") & filters.group)
        @self.bot.on_message(filters.command("equaliser") & filters.group)
        @self.bot.on_message(filters.command("equalizer") & filters.group)
        async def eq_panel_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            await message.reply(
                fx_panel_caption(self._cfg(uid)),
                reply_markup=home_keyboard(),
                parse_mode=ParseMode.HTML,
            )

        @self.bot.on_message(filters.command("swap") & filters.group)
        async def swap_cmd(client, message: Message):
            """Stereo L<->R swap. `/swap` toggle, `/swap 1` on, `/swap 0` off."""
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            cur = 1 if float(self._cfg(uid).get("swap", 0) or 0) >= 1 else 0
            if len(message.command) >= 2:
                arg = message.command[1].lower()
                if arg in ("1", "on", "yes"):
                    new = 1
                elif arg in ("0", "off", "no"):
                    new = 0
                else:
                    return await message.reply("`/swap` · `/swap 1` · `/swap 0`")
            else:
                new = 0 if cur else 1
            await self._apply(uid, message.chat.id, swap=new)
            await message.reply(f"**Stereo swap** · `{'ON (L<->R)' if new else 'OFF'}`")

        @self.bot.on_message(filters.command("gain") & filters.group)
        async def gain_cmd(client, message: Message):
            await self._set_cmd(message, "gain")

        @self.bot.on_message(filters.command("loudness") & filters.group)
        async def loudness_cmd(client, message: Message):
            await self._set_cmd(message, "loudness")

        @self.bot.on_message(filters.command("bass") & filters.group)
        async def bass_cmd(client, message: Message):
            await self._set_cmd(message, "bass")

        @self.bot.on_message(filters.command("pitch") & filters.group)
        async def pitch_cmd(client, message: Message):
            await self._set_cmd(message, "pitch")

        @self.bot.on_message(filters.command("treble") & filters.group)
        async def treble_cmd(client, message: Message):
            await self._set_cmd(message, "treble")

        @self.bot.on_message(filters.command("clarity") & filters.group)
        async def clarity_cmd(client, message: Message):
            await self._set_cmd(message, "clarity")

        @self.bot.on_message(filters.command("echo") & filters.group)
        async def echo_cmd(client, message: Message):
            await self._set_cmd(message, "echo")

        @self.bot.on_message(filters.command("volume") & filters.group)
        async def volume_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            # /volume is a finer-grained (0-400) dial on the SAME underlying
            # "loudness" parameter /loudness already controls (0-100) — not
            # a separate new DSP stage. vol = loudness * 4. Kept this way so
            # it doesn't silently reset your current loud tuning to some
            # unrelated default when you start using it.
            current = int(round(float(self._cfg(uid).get("loudness", 0) or 0) * 4))
            if len(message.command) < 2:
                return await message.reply(
                    f"**volume** · `{current}`\n`/volume 300` (koi upper limit nahi)"
                )
            try:
                val = float(message.command[1])
            except ValueError:
                return await message.reply("Number do.")
            val = max(0.0, val)
            loud = val / 4.0
            await self._apply(uid, message.chat.id, loudness=loud, muted=False)
            await message.reply(f"**volume** · `{int(val)}`")

        @self.bot.on_message(filters.command("voice") & filters.group)
        async def voice_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            if len(message.command) < 2:
                return await message.reply(
                    "**voice**\n\n`/voice female|male|normal`\n\n"
                    "Sirf tone (bass/treble) badalta hai — gain/loudness "
                    "jahan hai wahi rehta hai."
                )
            mode = message.command[1].lower().strip()
            tones = {
                "female": {"bass": 5, "treble": 70},
                "male": {"bass": 60, "treble": 15},
                "normal": {"bass": 50, "treble": 50},
            }
            if mode not in tones:
                return await message.reply("`female` | `male` | `normal`")
            await self._apply(uid, message.chat.id, **tones[mode], muted=False)
            await message.reply(f"**voice** · `{mode}`")

        @self.bot.on_message(filters.command("automute") & filters.group)
        async def automute_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            cfg = self._cfg(uid)
            if len(message.command) < 2:
                state = "on" if cfg.get("automute") else "off"
                return await message.reply(
                    f"**automute** · `{state}`\n`/automute on` | `/automute off`"
                )
            arg = message.command[1].lower().strip()
            if arg not in ("on", "off"):
                return await message.reply("`on` | `off`")
            cfg["automute"] = (arg == "on")
            await self._save_config(uid)
            await message.reply(
                f"**automute** · `{arg}`\n"
                + ("Agle `/join` pe VC auto-mute hoga." if arg == "on"
                   else "Agle `/join` pe VC unmuted rahega.")
            )

        @self.bot.on_message(filters.command("effects") & filters.group)
        async def effects_cmd(client, message: Message):
            # Sudo apni config dekhta hai, baaki log owner ki (read-only)
            uid = message.from_user.id if message.from_user else None
            c = self._cfg(uid) if (uid is not None and self._is_sudo(uid)) else self.config
            await message.reply(
                "**Effects 0–100**\n\n"
                f"Gain `{c.get('gain', 0)}` · Loud `{c.get('loudness', 0)}`\n"
                f"Bass `{c.get('bass', 0)}` · Echo `{c.get('echo', 0)}`\n"
                f"Pitch `{c.get('pitch', 0)}` · Treble `{c.get('treble', 0)}`\n"
                f"Clarity `{c.get('clarity', 0)}`\n\n"
                "Panel: `/eq`\n"
                "Preset: `/preset`\n"
                "Reset: `/reset`"
            )

        @self.bot.on_message(filters.command("preset") & filters.group)
        async def preset_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            if len(message.command) < 2:
                names = " | ".join(AudioEffects.list_presets())
                return await message.reply(
                    f"**Presets**\n\n`/preset <name>`\n\n`{names}`"
                )
            name = message.command[1].lower().strip()
            if name not in AudioEffects.list_presets():
                names = " | ".join(AudioEffects.list_presets())
                return await message.reply(f"Unknown preset.\n\n`{names}`")
            preset = AudioEffects.get_preset(name)
            await self._apply(uid, message.chat.id, **preset)
            ab = self._ensure_bridge(uid)
            if ab and hasattr(ab, "eq") and hasattr(ab.eq, "_echo_buf"):
                ab.eq._echo_buf.fill(0)
            await message.reply(f"**Preset** · `{name}`")

        @self.bot.on_message(filters.command("reset") & filters.group)
        async def reset_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            cfg = self._cfg(uid)
            fresh = AudioEffects.get_default_effects()
            cfg.clear()
            cfg.update(fresh)
            ab = self._ensure_bridge(uid)
            if ab:
                ab.config = cfg
                if getattr(ab, "eq", None) is not None:
                    ab.eq.configure(cfg)
                    if hasattr(ab.eq, "_echo_buf"):
                        ab.eq._echo_buf.fill(0)
                await ab.update_effects(message.chat.id, **fresh)
            await self._save_config(uid)
            await message.reply("**Reset** · default FX")

        @self.bot.on_message(filters.command("mute") & filters.group)
        async def mute_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            await self._apply(uid, message.chat.id, muted=True)
            ab = self._ensure_bridge(uid)
            if ab:
                try:
                    await ab.set_muted(True)
                except Exception:
                    pass
            await message.reply("**Muted**\n\n`/unmute` se on.")

        @self.bot.on_message(filters.command("unmute") & filters.group)
        async def unmute_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            ab = self._ensure_bridge(uid)
            was_forced = bool(ab and getattr(ab, "_was_admin_muted", None))
            if ab:
                try:
                    from core.automute_guard import force_unmute
                    await force_unmute(ab)
                except Exception:
                    try:
                        await ab.set_muted(False)
                    except Exception:
                        pass
            await self._apply(uid, message.chat.id, muted=False)
            note = "\n\n_Admin-forced mute mila tha, VC rejoin karke unmute kiya._" if was_forced else ""
            await message.reply(
                f"**Unmuted**\n\nGain · `{self._cfg(uid).get('gain', 70)}`{note}"
            )

    async def _set_cmd(self, message: Message, key: str):
        if not message.from_user:
            return
        uid = message.from_user.id
        if not self._is_sudo(uid):
            return await message.reply("No access.")
        current = self._cfg(uid).get(key, 0)
        if len(message.command) < 2:
            return await message.reply(
                f"**{key}** · `{current}`\n`/{key} 70` (koi upper limit nahi — jitna chaho utna do)"
            )
        try:
            val = float(message.command[1])
        except ValueError:
            return await message.reply("Number do.")
        # No range clamp — value jaisa diya waisa hi DSP chain mein jaata hai.
        # (Peak abhi bhi limiter se clip-safe rehta hai — wo distortion-guard
        # hai, volume-limit nahi.)
        val = max(0.0, val)
        await self._apply(uid, message.chat.id, **{key: val, "muted": False})
        await message.reply(f"**{key}** · `{int(val)}`")

    def _is_sudo(self, user_id: int) -> bool:
        return is_sudo(user_id)

import os
import time
import asyncio
from pathlib import Path
from typing import Dict

from pyrogram import Client, filters
from pyrogram.types import Message

from core.sudo import is_sudo, is_owner, OWNER_ID
from core.bridge_state import get_live_bridge, set_live_bridge
from core.user_bridges import get_user_bridge, set_user_bridge
from core.user_effects import get_user_config
from core.effects import AudioEffects
from core import media_state
from config.settings import TEMP_DIR


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
    "Bot ko private mein `/addsession SESSION` bhejo."
)


class VCCommands:
    def __init__(self, bot: Client, assistant_manager, audio_bridge, screenshare_manager, rcplay_manager=None):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.audio_bridge = audio_bridge          # owner ka bridge
        self.screenshare_manager = screenshare_manager
        self.rcplay_manager = rcplay_manager
        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
        self.register_handlers()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _no_assistant_text(self, user_id: int) -> str:
        return OWNER_OFFLINE_MSG if is_owner(user_id) else SESSION_MSG

    def _get_assistant(self, user_id: int):
        """Sirf usi user ka apna assistant. Owner ka session kisi aur ko nahi milta."""
        return self.assistant_manager.get_assistant(user_id=user_id)

    def _ensure_bridge(self, user_id: int = None, assistant: dict = None):
        from core.bridge import AudioBridge

        if user_id is None:
            user_id = OWNER_ID

        # Owner: purana live bridge logic
        if user_id == OWNER_ID:
            live = get_live_bridge()
            if live is not None:
                self.audio_bridge = live
                return live

            if self.audio_bridge is not None:
                set_live_bridge(self.audio_bridge)
                return self.audio_bridge
        else:
            existing = get_user_bridge(user_id)
            if existing is not None:
                return existing

        assistant = assistant or self._get_assistant(user_id)
        if not assistant:
            return None

        bridge = AudioBridge(
            assistant["bridge"],
            self_user_id=assistant.get("user_id") or 0,
            mtproto=assistant.get("client"),
            is_owner=(user_id == OWNER_ID),
        )

        if user_id == OWNER_ID:
            self.audio_bridge = bridge
            set_live_bridge(bridge)
        else:
            # Sudo user ka bridge global live bridge ko overwrite nahi karega
            try:
                bridge.config = get_user_config(user_id)
                bridge.sync_effects()
            except Exception:
                pass
            set_user_bridge(user_id, bridge)
        return bridge

    def _route_pair(self, bridge, chat_id: int):
        """Return (source_id, target_id) for active oneway bridge."""
        if not bridge:
            return chat_id, chat_id
        ab = getattr(bridge, "active_bridges", {}) or {}
        if chat_id in ab:
            br = ab[chat_id]
            return int(br.get("source_id") or chat_id), int(br.get("target_id") or chat_id)
        for br in ab.values():
            src = int(br.get("source_id") or 0)
            tgt = int(br.get("target_id") or 0)
            if chat_id in (src, tgt):
                return src, tgt
        return chat_id, chat_id

    async def _download_media(self, message: Message, chat_id: int):
        src = message
        if message.reply_to_message:
            src = message.reply_to_message

        is_photo = False
        media = None

        if src.photo:
            media = src.photo
            is_photo = True
            ext = ".jpg"
        elif src.video:
            media = src.video
            ext = ".mp4"
        elif src.animation:
            media = src.animation
            ext = ".mp4"
        elif src.document:
            mime = (src.document.mime_type or "").lower()
            name = (src.document.file_name or "").lower()
            if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp")):
                media = src.document
                is_photo = True
                ext = ".jpg"
            elif mime.startswith("video/") or name.endswith((".mp4", ".mkv", ".webm", ".mov")):
                media = src.document
                ext = ".mp4"

        if not media:
            return None, False

        out = str(Path(TEMP_DIR) / f"ss_dl_{chat_id}_{src.id}{ext}")
        try:
            path = await message._client.download_media(src, file_name=out)
            if path and os.path.exists(path):
                return path, is_photo
        except Exception:
            try:
                path = await src.download(file_name=out)
                if path and os.path.exists(path):
                    return path, is_photo
            except Exception as e:
                print(f"download media fail: {e}")
        return None, False

    _AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".amr", ".wma", ".weba")
    _VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".3gp", ".avi", ".m4v", ".ts")

    async def _download_audio_media(self, message: Message, chat_id: int):
        """/rcplay ke liye koi bhi Telegram media download karta hai:
        voice note, audio, video note, video, GIF/animation, ya document
        (audio/video file, VC recording .mp4/.ogg/.m4a etc).
        Audio nikalna + effects lagana _render_rc_audio() karta hai."""
        src = message
        if message.reply_to_message:
            src = message.reply_to_message

        media = None
        ext = ".ogg"
        if src.voice:
            media, ext = src.voice, ".ogg"
        elif src.audio:
            media = src.audio
            ext = os.path.splitext((src.audio.file_name or "").lower())[1] or ".mp3"
        elif src.video_note:
            media, ext = src.video_note, ".mp4"
        elif src.video:
            media, ext = src.video, ".mp4"
        elif src.animation:
            media, ext = src.animation, ".mp4"
        elif src.document:
            mime = (src.document.mime_type or "").lower()
            name = (src.document.file_name or "").lower()
            name_ext = os.path.splitext(name)[1]
            if (
                mime.startswith(("audio/", "video/"))
                or name_ext in self._AUDIO_EXT
                or name_ext in self._VIDEO_EXT
                or mime in ("application/ogg", "application/octet-stream") and name_ext
            ):
                media = src.document
                ext = name_ext or (".mp4" if mime.startswith("video/") else ".mp3")

        if not media:
            return None

        out = str(Path(TEMP_DIR) / f"rc_dl_{chat_id}_{src.id}{ext}")
        try:
            path = await message._client.download_media(src, file_name=out)
            if path and os.path.exists(path):
                return path
        except Exception:
            try:
                path = await src.download(file_name=out)
                if path and os.path.exists(path):
                    return path
            except Exception as e:
                print(f"download audio fail: {e}")
        return None

    async def _render_rc_audio(self, in_path: str, cfg: dict):
        """Downloaded media -> WAV (48k stereo), video track hata ke aur
        user ka current effects preset (gain/bass/treble/clarity/loudness/
        echo/pitch) lagake — bilkul /yt wale FX chain jaisa.
        Returns (path, error). Fail hone par (None, reason)."""
        fx = dict(cfg or {})
        fx["muted"] = False  # /mute live mic ke liye hai, recording silent nahi honi chahiye
        filt = AudioEffects.build_ffmpeg_filter(fx)

        out = str(Path(TEMP_DIR) / f"rc_fx_{int(time.time() * 1000)}.wav")
        cmd = ["ffmpeg", "-nostdin", "-y", "-i", in_path, "-vn"]
        if filt and filt != "anull":
            cmd += ["-af", filt]
        cmd += ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                preexec_fn=lambda: os.nice(10),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                return None, "ffmpeg timeout (file bahut badi hai)"
            if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
                tail = (err or b"")[-300:].decode(errors="replace")
                return None, tail or "ffmpeg fail / is media me audio nahi hai"
            try:
                os.remove(in_path)  # original ab zaroorat nahi
            except Exception:
                pass
            return out, ""
        except FileNotFoundError:
            return None, "ffmpeg install nahi hai"
        except Exception as e:
            return None, str(e)[:200]

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    def register_handlers(self):

        @self.bot.on_message(filters.command(["joinvc", "vc"]) & filters.group)
        async def join_vc_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            chat_id = message.chat.id
            chat_name = message.chat.title or str(chat_id)
            status_msg = await message.reply(f"**VC join:** {chat_name}...")

            try:
                assistant = self._get_assistant(uid)
                if not assistant:
                    return await status_msg.edit(self._no_assistant_text(uid))

                uname = assistant.get("username") or "assistant"

                try:
                    await assistant["client"].get_chat_member(chat_id, "me")
                except Exception:
                    ok, msg, _ = await self.assistant_manager.ensure_in_group(
                        chat_id, assistant=assistant
                    )
                    if not ok:
                        return await status_msg.edit(
                            f"@{uname} group mein nahi.\n{msg[:100] if msg else ''}"
                        )

                await self.assistant_manager.ensure_voice_chat(chat_id, assistant=assistant)
                await asyncio.sleep(0.4)

                bridge = self._ensure_bridge(uid, assistant)
                if not bridge:
                    return await status_msg.edit("Bridge nahi mila.")

                if assistant.get("bridge"):
                    if hasattr(bridge, "bind_client"):
                        bridge.bind_client(assistant["bridge"], assistant.get("client"))

                ab = getattr(bridge, "active_bridges", {}) or {}
                if chat_id in ab and ab[chat_id].get("mode") == "oneway":
                    return await status_msg.edit(
                        f"**Already in bridge**\n\n{chat_name}\n@{uname}"
                    )

                ok = await bridge.join_vc_only(chat_id)
                if uid == OWNER_ID:
                    set_live_bridge(bridge)
                if ok:
                    await status_msg.edit(
                        f"**Joined**\n\n{chat_name}\n@{uname}\n\n"
                        f"Bridge awaaz ke liye: `/join -100TARGET`"
                    )
                else:
                    err = getattr(bridge, "get_last_error", lambda: "")() or ""
                    await status_msg.edit(f"**VC join fail**\n`{err[:160] or 'unknown'}`")
            except Exception as e:
                await status_msg.edit(f"`{str(e)[:150]}`")

        @self.bot.on_message(filters.command("screenshare") & filters.group)
        async def screenshare_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            chat_id = message.chat.id
            quality = "480p"  # lowered from 720p for faster encode/stream on limited VPS
            fps = 15
            if len(message.command) > 1 and message.command[1].endswith("p"):
                quality = message.command[1]
            if len(message.command) > 2:
                try:
                    fps = int(message.command[2])
                except ValueError:
                    fps = 15

            status_msg = await message.reply("**Screenshare...**")

            try:
                assistant = self._get_assistant(uid)
                if not assistant:
                    return await status_msg.edit(self._no_assistant_text(uid))

                bridge = self._ensure_bridge(uid, assistant)
                ab_map = getattr(bridge, "active_bridges", {}) if bridge else {}

                in_route = chat_id in (ab_map or {})
                if not in_route:
                    for br in (ab_map or {}).values():
                        if chat_id in (
                            int(br.get("source_id") or 0),
                            int(br.get("target_id") or 0),
                        ):
                            in_route = True
                            break
                if not in_route:
                    return await status_msg.edit(
                        "**Pehle bridge ON**\n\n`/join -100TARGET_ID`"
                    )

                await status_msg.edit("Media download...")
                media_path, is_photo = await self._download_media(message, chat_id)
                if not media_path:
                    return await status_msg.edit(
                        "**Photo/Video pe reply** + `/screenshare`"
                    )

                pytg = getattr(bridge, "client", None) or assistant.get("bridge")
                if not pytg:
                    return await status_msg.edit("PyTgCalls client nahi.")

                source_id, target_id = self._route_pair(bridge, chat_id)

                # CRITICAL: video ONLY on TARGET
                # SOURCE must stay ExternalMedia.AUDIO + record → awaaz bridge zinda
                kind = "Photo" if is_photo else "Video"
                await status_msg.edit(
                    f"{kind} → **target only**\n"
                    f"Source mic bridge ON rahega"
                )

                result, ss_err = await self.screenshare_manager.start_media_share(
                    target_id,
                    pytg,
                    media_path,
                    is_photo=is_photo,
                    quality=quality,
                    fps=fps,
                    extra_chats=None,  # source pe NAHI
                )

                # Re-arm source capture (record) so awaaz continues.
                # Agar source me /yt ya /rcplay chal raha hai to ye use band
                # kar dega (ExternalMedia replay) — isliye tab skip.
                if (
                    bridge and hasattr(bridge, "restore_source_capture")
                    and not media_state.has_audio(source_id)
                ):
                    try:
                        await bridge.restore_source_capture(source_id, target_id)
                    except Exception as e:
                        print(f"restore source: {e}")

                if result:
                    await status_msg.edit(
                        f"**Screenshare LIVE (target)**\n\n"
                        f"Type · `{kind}`\n"
                        f"Target · `{target_id}`\n"
                        f"Source mic → target awaaz **ON**\n\n"
                        f"Stop: `/screenshareoff`"
                    )
                else:
                    await status_msg.edit(
                        f"**Play fail**\n`{ss_err[:300] if ss_err else 'ffmpeg / bot.log'}`"
                    )
            except Exception as e:
                await status_msg.edit(f"`{str(e)[:150]}`")

        @self.bot.on_message(filters.command("screenshareoff") & filters.group)
        async def screenshare_off_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            chat_id = message.chat.id
            status_msg = await message.reply("**Stopping...**")
            try:
                if not self._get_assistant(uid):
                    return await status_msg.edit(self._no_assistant_text(uid))

                bridge = self._ensure_bridge(uid)
                source_id, target_id = self._route_pair(bridge, chat_id)

                await self.screenshare_manager.stop_screenshare(target_id)
                await self.screenshare_manager.stop_screenshare(source_id)

                # Full live mic restore source+target — lekin agar /yt ya
                # /rcplay chal raha hai to use mat todo (stop_screenshare ne
                # audio pehle hi sahi restore kar diya hai).
                if media_state.has_audio(source_id) or media_state.has_audio(target_id):
                    pass
                elif bridge and hasattr(bridge, "restore_live_route"):
                    await bridge.restore_live_route(source_id, target_id)
                elif bridge and hasattr(bridge, "restore_source_capture"):
                    await bridge.restore_source_capture(source_id, target_id)

                await status_msg.edit(
                    "**Screenshare off**\n\n"
                    "Source → Target awaaz bridge restore"
                )
            except Exception as e:
                await status_msg.edit(f"`{str(e)[:100]}`")

        @self.bot.on_message(filters.command("rcplay") & filters.group)
        async def rcplay_cmd(client, message: Message):
            """Voice note / recording pe reply karke /rcplay — TARGET group
            ki VC mein hi bajta hai, SOURCE ki mic-bridge kabhi touch nahi
            hoti. Koi filter/loudnorm nahi — asli loudness/bass/quality
            bilkul wahi rehti hai jo original recording mein thi."""
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            if not self.rcplay_manager:
                return await message.reply("RCPlay manager load nahi hua — restart karo.")

            chat_id = message.chat.id
            status_msg = await message.reply("**Recording play...**")

            try:
                assistant = self._get_assistant(uid)
                if not assistant:
                    return await status_msg.edit(self._no_assistant_text(uid))

                bridge = self._ensure_bridge(uid, assistant)
                ab_map = getattr(bridge, "active_bridges", {}) if bridge else {}
                in_route = chat_id in (ab_map or {})
                if not in_route:
                    for br in (ab_map or {}).values():
                        if chat_id in (int(br.get("source_id") or 0), int(br.get("target_id") or 0)):
                            in_route = True
                            break
                if not in_route:
                    return await status_msg.edit("**Pehle bridge ON**\n\n`/join -100TARGET_ID`")

                await status_msg.edit("Recording download...")
                audio_path = await self._download_audio_media(message, chat_id)
                if not audio_path:
                    return await status_msg.edit(
                        "**Voice note / audio / video / recording pe reply** + `/rcplay`"
                    )

                pytg = getattr(bridge, "client", None) or assistant.get("bridge")
                if not pytg:
                    return await status_msg.edit("PyTgCalls client nahi.")

                source_id, target_id = self._route_pair(bridge, chat_id)

                # Current effects preset lagao (bridge.config = is user ki apni config)
                cfg = getattr(bridge, "config", None) or get_user_config(uid)
                await status_msg.edit("Effects lag rahe hain...")
                fx_path, fx_err = await self._render_rc_audio(audio_path, cfg)
                if not fx_path:
                    return await status_msg.edit(f"**Audio process fail**\n`{fx_err[:250]}`")
                audio_path = fx_path

                # CRITICAL: recording SIRF target par — source ki mic bridge
                # kabhi touch nahi hoti (koi leave/rejoin, koi record() reset nahi).
                ok, err = await self.rcplay_manager.start_rcplay(target_id, pytg, audio_path)

                if ok:
                    await status_msg.edit(
                        f"**Recording LIVE (target only)**\n\n"
                        f"Target · `{target_id}`\n"
                        f"Source mic bridge · **untouched**\n"
                        f"Preset · G{cfg.get('gain')} B{cfg.get('bass')} "
                        f"T{cfg.get('treble')} C{cfg.get('clarity')} L{cfg.get('loudness')}\n\n"
                        f"Khatam hote hi khud restore ho jaayega. Manual stop: `/rcplayoff`"
                    )
                else:
                    await status_msg.edit(f"**Play fail**\n`{err[:250] if err else 'unknown'}`")
            except Exception as e:
                await status_msg.edit(f"`{str(e)[:150]}`")

        @self.bot.on_message(filters.command("rcplayoff") & filters.group)
        async def rcplay_off_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")
            if not self.rcplay_manager:
                return await message.reply("RCPlay manager load nahi hua.")

            chat_id = message.chat.id
            status_msg = await message.reply("**Stopping recording...**")
            try:
                bridge = self._ensure_bridge(uid)
                source_id, target_id = self._route_pair(bridge, chat_id)

                stopped = await self.rcplay_manager.stop_rcplay(target_id)
                if not stopped:
                    stopped = await self.rcplay_manager.stop_rcplay(source_id)

                await status_msg.edit(
                    "**Recording stopped**\n\nTarget audio bridge restore ho gaya."
                    if stopped else "**Kuch active nahi tha.**"
                )
            except Exception as e:
                await status_msg.edit(f"`{str(e)[:100]}`")

        @self.bot.on_message(filters.command("vcstatus"))
        async def vc_status_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            chat_id = message.chat.id
            if len(message.command) > 1 and message.command[1].lower() == "reset" and is_owner(uid):
                from core.bridge_patch import reset_stats
                reset_stats()
                return await message.reply("Diag reset.")
            try:
                # Sudo apna status dekhta hai, baaki log owner ka (read-only)
                view_id = uid if self._is_sudo(uid) else OWNER_ID

                assistant = self._get_assistant(view_id)
                if not assistant:
                    return await message.reply(self._no_assistant_text(uid))

                bridge = self._ensure_bridge(view_id, assistant)
                is_in_vc = bool(
                    bridge and chat_id in getattr(bridge, "active_bridges", {})
                )
                is_screenshare = self.screenshare_manager.is_active(chat_id)
                uname = assistant.get("username") or "assistant"
                route = ""
                fwd = ""
                if bridge and chat_id in getattr(bridge, "active_bridges", {}):
                    br = bridge.active_bridges[chat_id]
                    route = (
                        f"\nRoute: `{br.get('source_id')}` → `{br.get('target_id')}` "
                        f"mode=`{br.get('mode')}`"
                    )
                    fwd = f"\nFwd frames: `{getattr(bridge, '_fwd_count', 0)}` rx=`{getattr(bridge, '_rx_count', 0)}`"
                from core.bridge_patch import get_stats
                st = get_stats()
                diag = (
                    f"\n\n**Diag** (`/vcstatus reset` = zero)\n"
                    f"Loop lag: `{st['lag_n']}` · max `{st['lag_max_ms']}ms`\n"
                    f"Slow send: `{st['slow_n']}` · slowest `{st['send_max_ms']}ms`\n"
                    f"RX gap: `{st['gap_n']}` · max `{st['gap_max_ms']}ms`\n"
                    f"Source silence: `{st['sil_n']}` · max `{st['sil_max_ms']}ms`\n"
                    f"CPU: `{st['cpu_now']}%` · max `{st['cpu_max']}%` ({os.cpu_count()} core)\n"
                    f"Telegram ping: `{st['rtt_last']}ms` · max `{st['rtt_max']}ms` · "
                    f"slow `{st['rtt_slow_n']}` · fail `{st['rtt_fail_n']}`\n"
                    f"Dropped: `{st['drop_n']}` · max queue `{st['tx_max_q']}`\n"
                    f"Send errors: `{st.get('senderr_n', 0)}` · longest `{st.get('fail_max_ms', 0)}ms` · "
                    f"auto-rejoin `{st.get('heal_n', 0)}` · watchdog-rejoin `{st.get('join_heal_n', 0)}`\n"
                    f"Last send error: `{st.get('last_err') or '-'}`"
                )
                await message.reply(
                    f"**VC Status**\n\n"
                    f"Assistant: @{uname}\n"
                    f"In VC: `{'Yes' if is_in_vc else 'No'}`\n"
                    f"Screenshare: `{'Yes' if is_screenshare else 'No'}`"
                    f"{route}{fwd}{diag}"
                )
            except Exception as e:
                await message.reply(f"`{str(e)[:100]}`")

        @self.bot.on_message(filters.command("vchat") & filters.group)
        async def vchat_cmd(client, message: Message):
            """/vchat <text> (ya kisi message ka reply) — assistant ke VC ke
            andar wale "Message" chat mein <text> bhejta hai (group chat mein
            nahi). VC mein assistant join hona chahiye."""
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            parts = message.text.split(None, 1)
            payload = parts[1].strip() if len(parts) > 1 else ""

            if not payload and message.reply_to_message:
                payload = (message.reply_to_message.text
                           or message.reply_to_message.caption or "").strip()
            if not payload:
                return await message.reply(
                    "**vchat**\n\n`/vchat <message>`\n\n"
                    "Message source→target connection ke target group ke VC chat mein jayega."
                )

            assistant = self._get_assistant(uid if self._is_sudo(uid) else OWNER_ID)
            if not assistant:
                return await message.reply(self._no_assistant_text(uid))

            target = self._vchat_target(uid, assistant, message.chat.id)

            from core.vc_message import send_vc_message
            ok, info = await send_vc_message(assistant.get("client"), target, payload)
            if ok:
                try:
                    await message.react("👍")
                except Exception:
                    pass
            else:
                await message.reply(info)

        @self.bot.on_message(filters.command("handrise") & filters.group)
        async def handrise_cmd(client, message: Message):
            """/handrise — assistant target group ke VC mein HAND RAISE kar
            deta hai (jab admin ne usse VC pe mute kiya ho)."""
            if not message.from_user:
                return
            uid = message.from_user.id
            if not self._is_sudo(uid):
                return await message.reply("No access.")

            assistant = self._get_assistant(uid if self._is_sudo(uid) else OWNER_ID)
            if not assistant:
                return await message.reply(self._no_assistant_text(uid))

            from core.vc_mute import mtproto_raise_hand
            mtp = assistant.get("client")
            tried = []
            done = False
            last = ""
            for cid in self._vc_candidates(uid, assistant, message.chat.id):
                ok, info = await mtproto_raise_hand(mtp, cid, True)
                if ok:
                    done = True
                    break
                last = info
                tried.append(f"{cid}: {info[:70]}")
                # assistant is VC mein nahi -> agla VC try karo
                if ("JOIN_MISSING" in info or "no active call" in info
                        or "GROUPCALL" in info):
                    continue
                break  # koi aur error -> aage try karna bekaar

            if done:
                try:
                    await message.react("👍")
                except Exception:
                    pass
            else:
                joined_issue = (not tried) or any(
                    ("JOIN_MISSING" in t or "no active call" in t) for t in tried
                )
                if joined_issue:
                    await message.reply("Pehle VC join karo, uske baad /handrise karna.")
                else:
                    await message.reply("Hand raise fail:\n`" + "\n".join(tried) + "`")

    def _vc_candidates(self, uid: int, assistant: dict, here_id: int) -> list:
        """Assistant jin VC mein ho sakta hai unki list, best pehle:
        bridge ke TARGET group, phir baaki bridge VC, phir command wala group."""
        out = []

        def add(x):
            try:
                x = int(x)
            except Exception:
                return
            if x and x not in out:
                out.append(x)

        try:
            bridge = self._ensure_bridge(uid, assistant)
            ab = getattr(bridge, "active_bridges", {}) or {}
            infos = list(ab.values())
            for info in reversed(infos):
                src = int(info.get("source_id") or 0)
                tgt = int(info.get("target_id") or 0)
                if tgt and tgt != src:
                    add(tgt)
            for cid in reversed(list(ab.keys())):
                if cid != here_id:
                    add(cid)
            for cid in reversed(list(ab.keys())):
                add(cid)
        except Exception:
            pass
        add(here_id)
        return out

    def _vchat_target(self, uid: int, assistant: dict, here_id: int) -> int:
        """VC chat mein message kis (TARGET) group mein jaye."""
        c = self._vc_candidates(uid, assistant, here_id)
        return c[0] if c else here_id

    def _is_sudo(self, user_id: int) -> bool:
        return is_sudo(user_id)

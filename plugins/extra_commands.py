"""Extra commands (stage 1): recording + leave controls + /level.

  /startrecord [sec]   record RECORD_GROUP (or this group) -> MP3 upload when done
  /stoprecord          stop now and upload
  /leave               leave every VC (bridge routes + record group)
  /leaverecord         leave only the record / source side
  /leaveplay           leave only the play / target side
  /level [1-50]        show / set loudness level (gain = level x 2)

Works next to the existing bridge; nothing existing is replaced.
"""
import asyncio
import json
import logging
import os
from typing import Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import Message

from core.effects import AudioEffects
from core.recorder import (
    BYTES_PER_SEC, DEFAULT_SEC, MAX_SEC, MIN_SEC, get_record_manager,
)
from core.runtime_vars import get_int
from core.sudo import OWNER_ID, is_owner, is_sudo
from core.bridge_state import get_live_bridge
from core.user_bridges import get_user_bridge
from core.user_effects import get_user_config, save_user_configs
from plugins.vc_commands import OWNER_OFFLINE_MSG, SESSION_MSG

logger = logging.getLogger(__name__)


class ExtraCommands:
    def __init__(self, bot: Client, assistant_manager, audio_bridge, bridge_cmds=None):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.audio_bridge = audio_bridge      # owner bridge given at startup (fallback only)
        self.bridge_cmds = bridge_cmds        # BridgeCommands instance (route metadata)
        self.rec = get_record_manager()
        self.register_handlers()

    # ------------------------------------------------------------------ helpers
    def _no_assistant_text(self, uid: int) -> str:
        return OWNER_OFFLINE_MSG if is_owner(uid) else SESSION_MSG

    def _find_bridge(self, uid: int):
        """The user's CURRENT bridge (/join rebuilds it, so never cache)."""
        if uid == OWNER_ID:
            return get_live_bridge() or self.audio_bridge
        return get_user_bridge(uid)

    def _ensure_bridge(self, uid: int, assistant: dict):
        b = self._find_bridge(uid)
        if b is not None:
            return b
        from core.bridge import AudioBridge
        from core.bridge_state import set_live_bridge
        from core.user_bridges import set_user_bridge

        b = AudioBridge(
            assistant["bridge"],
            self_user_id=assistant.get("user_id") or 0,
            mtproto=assistant.get("client"),
            is_owner=(uid == OWNER_ID),
        )
        if uid == OWNER_ID:
            self.audio_bridge = b
            set_live_bridge(b)
        else:
            try:
                b.config = get_user_config(uid)
                b.sync_effects()
            except Exception:
                pass
            set_user_bridge(uid, b)
        return b

    @staticmethod
    def _routes(bridge) -> list:
        out = []
        for key, br in list((getattr(bridge, "active_bridges", {}) or {}).items()):
            try:
                src = int(br.get("source_id") or key)
                tgt = int(br.get("target_id") or src)
            except Exception:
                continue
            out.append((key, src, tgt))
        return out

    def _record_chat(self, message: Message) -> Optional[int]:
        cid = get_int("RECORD_GROUP")
        if cid:
            return cid
        if message.chat and message.chat.type.name in ("GROUP", "SUPERGROUP"):
            return message.chat.id
        return None

    # ------------------------------------------------------------------ recording
    async def _upload(self, rec, path: Optional[str], info: str) -> None:
        if not path:
            try:
                await self.bot.send_message(rec.reply_chat, f"⏹ **Recording khatam**\n`{info}`")
            except Exception:
                pass
            return
        secs = int(rec.bytes_written / BYTES_PER_SEC)
        cap = f"🎙 **VC Record** · `{rec.chat_id}`\n⏱ {secs}s · `{info}`"
        targets = [rec.reply_chat]
        logger_id = get_int("LOGGER_GROUP")
        if logger_id and logger_id != rec.reply_chat:
            targets.append(logger_id)
        try:
            for t in targets:
                try:
                    await self.bot.send_audio(
                        t, path, caption=cap, duration=secs,
                        title=f"VC {rec.chat_id}", performer="VC Record",
                    )
                except Exception as e:
                    logger.warning("[REC] upload to %s failed: %s", t, e)
                    if t == rec.reply_chat:
                        try:
                            await self.bot.send_message(
                                t, f"⚠️ Upload fail: `{str(e)[:150]}`\nRecording {secs}s thi."
                            )
                        except Exception:
                            pass
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def _register_recording(self):
        @self.bot.on_message(filters.command("startrecord"))
        async def startrecord_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("No access.")

            secs = DEFAULT_SEC
            if len(message.command) > 1:
                try:
                    secs = int(message.command[1])
                except ValueError:
                    return await message.reply(
                        f"`/startrecord` · `/startrecord <sec>` ({MIN_SEC}-{MAX_SEC})"
                    )
                if not (MIN_SEC <= secs <= MAX_SEC):
                    return await message.reply(f"Duration {MIN_SEC}-{MAX_SEC} sec ke beech.")

            chat_id = self._record_chat(message)
            if not chat_id:
                return await message.reply(
                    "RECORD_GROUP set nahi.\nGroup mein command chalao ya `/setRECORD_GROUP -100...`"
                )
            if self.rec.is_recording(chat_id):
                r = self.rec.get(chat_id)
                return await message.reply(
                    f"Already recording · `{int(r.elapsed)}s/{r.duration}s`\n`/stoprecord`"
                )

            assistant = self.assistant_manager.get_assistant(user_id=uid)
            if not assistant:
                return await message.reply(self._no_assistant_text(uid))

            status = await message.reply(f"🔴 Record shuru... `{chat_id}`")
            try:
                bridge = self._ensure_bridge(uid, assistant)
                if not bridge:
                    return await status.edit("Bridge nahi mila.")
                if assistant.get("bridge") and hasattr(bridge, "bind_client"):
                    bridge.bind_client(assistant["bridge"], assistant.get("client"))

                # Already capturing this chat as a bridge source? Then no join needed.
                capturing = any(src == chat_id for _, src, _ in self._routes(bridge))
                if not capturing:
                    try:
                        await assistant["client"].get_chat_member(chat_id, "me")
                    except Exception:
                        ok, msg, _ = await self.assistant_manager.ensure_in_group(
                            chat_id, assistant=assistant
                        )
                        if not ok:
                            uname = assistant.get("username") or "assistant"
                            return await status.edit(
                                f"@{uname} record group mein nahi.\n`{(msg or '')[:120]}`"
                            )
                    await self.assistant_manager.ensure_voice_chat(chat_id, assistant=assistant)
                    await asyncio.sleep(0.4)
                    ok = await bridge.join_vc_only(chat_id)
                    if not ok:
                        err = getattr(bridge, "get_last_error", lambda: "")() or ""
                        return await status.edit(f"**VC join fail**\n`{err[:160] or 'unknown'}`")

                rec = await self.rec.start(
                    chat_id, secs, uid, message.chat.id, on_done=self._upload
                )
            except Exception as e:
                logger.exception("startrecord failed")
                return await status.edit(f"Error: `{str(e)[:180]}`")

            body = (
                f"🔴 **Recording**\n\n"
                f"Group · `{chat_id}`\nTime · `{rec.duration}s`\n\n"
                f"Roko: `/stoprecord` · done pe MP3 aayegi"
            )
            await status.edit(body)
            await asyncio.sleep(3.5)
            if self.rec.get(chat_id) is rec and rec.frames_in == 0:
                try:
                    await status.edit(
                        body + "\n\n⚠️ 3s mein koi audio frame nahi aaya — "
                        "VC mein koi bol nahi raha ya assistant capture off hai."
                    )
                except Exception:
                    pass

        @self.bot.on_message(filters.command("stoprecord"))
        async def stoprecord_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("No access.")

            chat_id = self._record_chat(message)
            if not chat_id or not self.rec.is_recording(chat_id):
                mine = [
                    r for r in self.rec.active.values()
                    if is_owner(uid) or r.owner_uid == uid
                ]
                if len(mine) == 1:
                    chat_id = mine[0].chat_id
                else:
                    return await message.reply("Koi recording nahi chal rahi.")
            r = self.rec.get(chat_id)
            if r and not (is_owner(uid) or r.owner_uid == uid):
                return await message.reply("Ye recording kisi aur ne shuru ki hai.")
            st = await message.reply("⏹ Stop... MP3 ban rahi hai")
            path, info = await self.rec.stop(chat_id, reason="manual")
            try:
                await st.edit(f"⏹ **Stopped**\n`{info}`" + ("\nUpload ho raha hai..." if path else ""))
            except Exception:
                pass

    # ------------------------------------------------------------------ leave
    async def _verbose_leave(self, pytg, ubot, chat_id: int) -> str:
        """Uses core/vc_leave.py's verified leave: real SSRC lookup (not
        source=0) plus a ground-truth check with Telegram, so the reply
        shows what actually happened instead of 'no exception was raised'.
        Shared by /leave, /leaverecord and /leaveplay below."""
        from core.vc_leave import force_leave_and_confirm
        return await force_leave_and_confirm(pytg, ubot, chat_id)

    def _kill_tx(self, bridge, chat_id: int) -> None:
        """Stop the per-target sender loop right away (core.bridge_patch's _tx_loop),
        so no more frames go out to `chat_id` while we're leaving it — otherwise a
        still-flowing source keeps feeding the target and races the Telegram leave."""
        tx = getattr(bridge, "_tx", None) or bridge.__dict__.get("_tx")
        if not tx:
            return
        st = tx.get(int(chat_id))
        if not st:
            return
        try:
            st.q.clear()
        except Exception:
            pass
        task = getattr(st, "task", None)
        if task and not task.done():
            task.cancel()

    async def _leave(self, uid: int, which: str) -> Tuple[list, str, str]:
        assistant = self.assistant_manager.get_assistant(user_id=uid)
        if not assistant:
            return [], self._no_assistant_text(uid), ""
        bridge = self._find_bridge(uid)
        routes = self._routes(bridge) if bridge else []

        rec_ids = {src for _, src, _ in routes}
        play_ids = {tgt for _, src, tgt in routes if tgt != src}
        rg = get_int("RECORD_GROUP")
        if rg:
            rec_ids.add(rg)

        if which == "record":
            ids = rec_ids
        elif which == "play":
            ids = play_ids
        else:
            ids = rec_ids | play_ids

        # ── Step 1: Stop any active recordings first ──────────────────────
        for cid in list(ids):
            if self.rec.is_recording(cid):
                await self.rec.stop(cid, reason="leave")

        # ── Step 2: Kill TX loops BEFORE touching bridge bookkeeping ──────
        # Stops frames flowing into the target while we do the network leave,
        # eliminating the race where a live source fights the leave_call.
        if bridge:
            for cid in ids:
                self._kill_tx(bridge, cid)

        # ── Step 3: stop_all_bridges (for /leave "all") ───────────────────
        if which == "all" and bridge:
            try:
                await bridge.stop_all_bridges()
            except Exception as e:
                logger.warning("stop_all_bridges: %s", e)

        # ── Step 4: Clear local bookkeeping ───────────────────────────────
        # Match on the dict KEY (not src/tgt) so we don't wipe the side we
        # were NOT asked to leave (both sides share the same route dict entry
        # under their respective keys in active_bridges).
        if bridge:
            for key, src, tgt in routes:
                dead = (which == "all") or (key in ids)
                if dead:
                    bridge.active_bridges.pop(key, None)

        meta_fn = getattr(self.bridge_cmds, "_meta", None) if self.bridge_cmds else None
        if callable(meta_fn):
            try:
                meta = meta_fn(uid)
                for sid, m in list(meta.items()):
                    tid = m.get("target_id")
                    dead = (
                        (which == "all")
                        or (which == "record" and int(sid) in ids)
                        or (which == "play" and tid is not None and int(tid) in ids)
                    )
                    if dead:
                        meta.pop(sid, None)
            except Exception:
                pass

        # ── Step 5: Network leave — verified, with pytg state purge ───────
        pytg = assistant.get("bridge") or (getattr(bridge, "client", None) if bridge else None)
        ubot = assistant.get("client")

        diag = []
        for cid in sorted(ids):
            result = await self._verbose_leave(pytg, ubot, cid)
            diag.append(f"`{cid}` → {result}")

        return sorted(ids), "", "\n".join(diag)

    def _register_leave(self):
        for cmd, which, label in (
            ("leave", "all", "Dono VC"),
            ("leaverecord", "record", "Record VC"),
            ("leaveplay", "play", "Play VC"),
        ):
            self._add_leave_handler(cmd, which, label)

    def _add_leave_handler(self, cmd: str, which: str, label: str):
        @self.bot.on_message(filters.command(cmd))
        async def leave_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("No access.")
            st = await message.reply("Leaving...")
            try:
                ids, err, diag = await self._leave(uid, which)
            except Exception as e:
                return await st.edit(f"Error: `{str(e)[:160]}`")
            if err:
                return await st.edit(err)
            if not ids:
                return await st.edit(f"**{label}** — koi active VC nahi mila.")
            await st.edit(f"**{label} chhoda**\n\n{diag}")

    # ------------------------------------------------------------------ /level
    def _cfg_and_bridge(self, uid: int):
        bridge = self._find_bridge(uid)
        if uid == OWNER_ID:
            if bridge is not None and isinstance(getattr(bridge, "config", None), dict):
                return bridge.config, bridge
            stored = {}
            try:
                with open("bridge_config.json", "r") as f:
                    stored = json.load(f).get("effects", {})
            except Exception:
                pass
            return AudioEffects.merge_config(stored), bridge
        return get_user_config(uid), bridge

    def _save_cfg(self, uid: int, cfg: dict) -> None:
        try:
            if uid == OWNER_ID:
                with open("bridge_config.json", "w") as f:
                    json.dump({"effects": cfg}, f, indent=4)
            else:
                save_user_configs()
        except Exception as e:
            logger.warning("level save fail: %s", e)

    def _register_level(self):
        @self.bot.on_message(filters.command("level"))
        async def level_cmd(client, message: Message):
            if not message.from_user:
                return
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("No access.")
            cfg, bridge = self._cfg_and_bridge(uid)
            cur = int(round(float(cfg.get("gain", 80)) / 2.0))

            if len(message.command) < 2:
                return await message.reply(
                    f"🔊 **Level** · `{cur}/50`\n(gain `{cfg.get('gain', 80)}`)\n\n"
                    f"Set: `/level 1-50`  ·  default `40`"
                )
            try:
                lvl = int(float(message.command[1]))
            except ValueError:
                return await message.reply("`/level 1-50`")
            if not (1 <= lvl <= 50):
                return await message.reply("Level 1-50 ke beech.")

            gain = min(100, lvl * 2)
            cfg["gain"] = gain
            if bridge is not None:
                bridge.config = cfg
                try:
                    bridge.sync_effects()
                    await bridge.update_effects(message.chat.id, gain=gain)
                except Exception:
                    pass
            self._save_cfg(uid, cfg)
            await message.reply(f"🔊 **Level** `{lvl}/50` (gain `{gain}`)")

    # ------------------------------------------------------------------
    def register_handlers(self):
        self._register_recording()
        self._register_leave()
        self._register_level()

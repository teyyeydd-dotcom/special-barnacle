#!/usr/bin/env python3
"""
VCFight V2 - Advanced Voice Chat Bot with Screenshare
Per-user assistants: owner + every sudo user has their own session.
"""

import asyncio
import logging
import sys
import json
import os
import fcntl
import time
from pathlib import Path
from datetime import datetime

try:
    _loop = asyncio.get_event_loop()
    if _loop is None or _loop.is_closed():
        raise RuntimeError("no loop")
except Exception:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

sys.path.insert(0, str(Path(__file__).parent))

from pyrogram import Client, idle
from config.settings import (
    API_ID, API_HASH, BOT_TOKEN, ASSISTANT_SESSION, SUDO_USERS,
    HEALTH_CHECK_INTERVAL, LOG_CHAT_ID,
)
from core.assistant import AssistantManager
from core.bridge import AudioBridge
from core.effects import AudioEffects
from core.database import DatabaseManager
from core.sudo import OWNER_ID, MAX_ASSISTANTS, is_sudo
from core.screenshare import ScreenshareManager
from core.rcplay import RCPlayManager
from core.user_bridges import all_user_bridges
from core.user_sessions import load_user_sessions
from plugins.bridge_commands import BridgeCommands
from plugins.audio_effects import AudioEffectsCommands
from plugins.admin_commands import AdminCommands
from plugins.stream_commands import StreamCommands
from plugins.vc_commands import VCCommands
from plugins.livescreenshare_commands import LiveScreenShareCommands
from plugins.media_toggle_commands import MediaToggleCommands
from core.automute_guard import install as install_automute_guard
from plugins.extra_commands import ExtraCommands
from plugins.phone_session import PhoneSessionCommands
from core.recorder import get_record_manager
from core.bridge_patch import apply as _apply_bridge_patch, loop_lag_watchdog, resource_monitor, net_probe, vc_join_watchdog

# sudo users ke bridges owner ke bridge ko hijack na karein (bridge.py edit ki zarurat nahi)
_apply_bridge_patch()

logger = logging.getLogger(__name__)
LOCK_PATH = Path("/tmp/vcfight-v2.lock")
SESSION_FILE = Path("assistant_session.txt")
_LOCK_FH = None


def acquire_single_instance_lock():
    global _LOCK_FH
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FH = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH.write(str(os.getpid()))
        _LOCK_FH.flush()
    except BlockingIOError:
        print("\n❌ VCFight V2 already running.")
        print("   pm2 stop all · pkill -f 'python.*main.py'\n")
        sys.exit(1)


def _fmt_uptime(started: datetime) -> str:
    total = int((datetime.now() - started).total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _read_session_file() -> str:
    try:
        if SESSION_FILE.exists():
            s = SESSION_FILE.read_text(encoding="utf-8").strip()
            if s and len(s) > 50:
                return s
    except Exception:
        pass
    return ""


class VCFightBot:
    def __init__(self):
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN missing! Set it in .env file")
        self.bot = Client("VCFightV2", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
        self.assistant_manager = AssistantManager(API_ID, API_HASH)
        self.db = DatabaseManager()
        self.screenshare_manager = ScreenshareManager()
        self.rcplay_manager = RCPlayManager()
        self.audio_bridge = None
        self.config = self._load_config()
        self._started_at = datetime.now()
        self._health_task = None
        self._lag_task = None
        self._mon_tasks = []
        self._bot_username = None
        self._bot_name = "ғɪɢʜᴛɪɴɢ ᴠᴄ ʙᴏᴛ"
        self._health_count = 0
        self._last_ping_ms = 0
        # Per-user assistant support
        self.current_session_id = None   # owner assistant ka Telegram user id (session string nahi)
        self.sudo_users = [OWNER_ID] + [int(x) for x in (SUDO_USERS or []) if x]

    def _load_config(self):
        config_file = Path("bridge_config.json")
        stored = {}
        if config_file.exists():
            with open("bridge_config.json", "r") as f:
                data = json.load(f)
                stored = data.get("effects", {})
        return AudioEffects.merge_config(stored)

    def _save_config(self):
        from core.effects import CONFIG_VERSION
        save_data = dict(self.config)
        save_data["_v"] = CONFIG_VERSION   # stamp version so restart doesn't flag as legacy
        with open("bridge_config.json", "w") as f:
            json.dump({"effects": save_data}, f, indent=4)

    def _owner_client(self):
        a = self.assistant_manager.get_assistant(user_id=OWNER_ID)
        return a.get("client") if a else None

    def _owned_count(self, owner_id: int) -> int:
        return sum(
            1 for a in self.assistant_manager.get_all_assistants()
            if a.get("owner_id") == owner_id
        )

    async def _try_add(self, session_string: str, label: str = "assistant", owner_id: int = OWNER_ID):
        if self._owned_count(owner_id) >= MAX_ASSISTANTS:
            return False, f"Already {MAX_ASSISTANTS} assistant for this user (max {MAX_ASSISTANTS})"
        result = await self.assistant_manager.add_assistant(session_string, label, owner_id=owner_id)
        if isinstance(result, tuple):
            ok, msg = result
        else:
            ok, msg = bool(result), ""
        return ok, msg

    async def _send_log_chat(self, text: str):
        if not LOG_CHAT_ID:
            return
        try:
            await self.bot.send_message(
                LOG_CHAT_ID,
                text,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Log chat send fail: {e}")

    async def _measure_ping(self) -> int:
        try:
            t0 = time.perf_counter()
            await self.bot.get_me()
            ms = int((time.perf_counter() - t0) * 1000)
            self._last_ping_ms = ms
            return ms
        except Exception:
            return self._last_ping_ms or 0

    def _calc_status_mode(self, ass_ok: bool, ping: int, bridges: int) -> str:
        if not ass_ok:
            return "bad"
        if ping <= 0:
            return "normal"
        if ping > 800:
            return "bad"
        if ping > 250:
            return "normal"
        if bridges >= 1:
            return "good"
        return "good"

    def _status_snapshot(self) -> dict:
        ass_ok = self.assistant_manager.get_assistant_count() > 0
        bridges = self.audio_bridge.get_bridge_count() if self.audio_bridge else 0
        ping = self._last_ping_ms or 0
        mode = self._calc_status_mode(ass_ok, ping, bridges)
        return {
            "uptime": _fmt_uptime(self._started_at),
            "ass_ok": ass_ok,
            "bridges": bridges,
            "muted": bool(self.config.get("muted", False)),
            "level": self.config.get("level", 52),
            "ping": ping,
            "mode": mode,
            "time": datetime.now().strftime("%H:%M"),
            "date": datetime.now().strftime("%d %b"),
            "bot_name": self._bot_name,
            "bot_tag": self._bot_username or "",
        }

    def _build_health_card(self, bot_tag: str = "") -> str:
        s = self._status_snapshot()
        self._health_count += 1
        ping = s["ping"] or 0
        mode = s["mode"]
        return (
            f"❑ ╰› ғɪɢʜᴛɪɴɢ ᴠᴄ ʙᴏᴛ 🎙️\n"
            f"├• ╰› ᴘɪɴɡ · `{ping}ms`\n"
            f"├• ╰› ᴜᴘᴛɪᴍᴇ · `{s['uptime']}`\n"
            f"├• ╰› ꜱᴛᴀᴛᴜꜱ · `{mode}`\n"
            f"└• ╰› ʙʀɪᴅɢᴇ · `{s['bridges']}` · #{self._health_count}"
        )

    def _build_startup_card(self, bot_tag: str = "") -> str:
        s = self._status_snapshot()
        mode = s["mode"]
        return (
            f"❑ ╰› ғɪɢʜᴛɪɴɢ ᴠᴄ ʙᴏᴛ 🎙️\n"
            f"├• ╰› ᴘɪɴɡ · `{s['ping']}ms`\n"
            f"├• ╰› ꜱᴛᴀᴛᴜꜱ · `{mode}`\n"
            f"├• ╰› ᴜᴘᴛɪᴍᴇ · `{s['uptime']}`\n"
            f"└• ╰› `/join` · `/leave` · `/help`"
        )

    def _build_shutdown_card(self) -> str:
        return (
            f"❑ ╰› ғɪɢʜᴛɪɴɢ ᴋᴡ ʙᴏᴛ 🎙️\n"
            f"├• ╰› ꜱᴛ፤ᴛᴜꜱ · `bad`\n"
            f"├• ╰› ᴛᴇᴘᴛɪᴍᴇ · `{_fmt_uptime(self._started_at)}`\n"
            f"└• ╰› shut down cleanly"
        )

    def _build_monitor_card(self, interval_sec: int) -> str:
        mins = max(1, interval_sec // 60)
        hrs = interval_sec / 3600
        if hrs >= 1:
            every = f"{hrs:.1f}h".replace(".0h", "h")
        else:
            every = f"{mins}m"
        return (
            f"❑ ╰› ғɪɢʜᴛɪɴɢ ᴠᴄ ʙᴏᴛ 🎙️\n"
            f"├• ╰› ᴍᴏɴɪᴛᴏʀ · `Active`\n"
            f"├• ╰› ɪɴᴛᴇʀᴡᴀʟ · every `{every}`\n"
            f"└• ╰› health checks running"
        )

    async def _health_checkup_loop(self):
        import datetime, zoneinfo
        # Din mein sirf 2 baar — 09:00 aur 21:00 IST
        HEALTH_TIMES = [(9, 0), (21, 0)]
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        logger.info(f"Health scheduled at 09:00 and 21:00 IST → {LOG_CHAT_ID}")
        await self._send_log_chat(self._build_monitor_card(0))

        while True:
            try:
                now = datetime.datetime.now(IST)
                # Agli scheduled time nikalo
                next_run = None
                for h, m in sorted(HEALTH_TIMES):
                    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if candidate > now:
                        next_run = candidate
                        break
                if next_run is None:
                    # Aaj ke dono time nikal gaye — kal pehle wale pe
                    h, m = sorted(HEALTH_TIMES)[0]
                    next_run = (now + datetime.timedelta(days=1)).replace(
                        hour=h, minute=m, second=0, microsecond=0
                    )
                wait_sec = (next_run - now).total_seconds()
                logger.info(f"Next health at {next_run.strftime('%H:%M IST')} ({wait_sec/3600:.1f}h)")
                await asyncio.sleep(wait_sec)

                # Check + send
                for a in self.assistant_manager.get_all_assistants():
                    try:
                        await self.assistant_manager.ensure_mtproto_ready(a)
                    except Exception:
                        pass
                await self._measure_ping()
                card = self._build_health_card()
                logger.info("HEALTH %s", card.replace("\n", " | "))
                await self._send_log_chat(card)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Health error: {e}")

    async def _send_startup_message(self):
        try:
            me = await self.bot.get_me()
            self._bot_username = f"@{me.username}" if me.username else me.first_name
            if me.first_name:
                self._bot_name = me.first_name
            await self._measure_ping()
            text = self._build_startup_card()
            await self._send_log_chat(text)
            try:
                await self.bot.send_message(OWNER_ID, text, disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"Startup DM fail: {e}")
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

    async def start(self):
        print("\n" + "=" * 50)
        print("🎙️ VCFight V2")
        print(f"OWNER {OWNER_ID} · LOG {LOG_CHAT_ID}")
        print("=" * 50 + "\n")

        await self.bot.start()
        print("✅ Bot started")

        # ---------- OWNER assistant ----------
        if ASSISTANT_SESSION:
            print("🔑 Loading ASSISTANT_SESSION from .env...")
            ok, msg = await self._try_add(ASSISTANT_SESSION, "env_assistant", owner_id=OWNER_ID)
            if ok:
                print("  ✅ Connected (.env)")
            else:
                print(f"  ❌ .env session: {msg}")

        if not self.assistant_manager.get_user_assistant(OWNER_ID):
            file_session = _read_session_file()
            if file_session:
                print("📄 Loading assistant_session.txt...")
                ok, msg = await self._try_add(file_session, "file_assistant", owner_id=OWNER_ID)
                if ok:
                    print("  ✅ Connected (file)")

        if not self.assistant_manager.get_user_assistant(OWNER_ID):
            assistants = self.db.get_assistants()
            if assistants:
                ass = assistants[0]
                try:
                    ok, msg = await self._try_add(
                        ass.session_string, ass.name or "db_assistant", owner_id=OWNER_ID
                    )
                    print(f"  {'✅' if ok else '❌'} DB restore: {ass.name}")
                except Exception:
                    print("  ❌ restore")
            else:
                print("⚠️ No owner assistant — /addsession SESSION (private)")

        # ---------- SUDO users: har ka apna assistant ----------
        for uid, sess in load_user_sessions().items():
            if uid == OWNER_ID:
                continue
            if not is_sudo(uid):
                print(f"  ⏭️ {uid} ab sudo nahi — session skip")
                continue
            ok, msg = await self._try_add(sess, f"sudo_{uid}", owner_id=uid)
            print(f"  {'✅' if ok else '❌'} sudo {uid}: {'connected' if ok else msg[:60]}")

        # ---------- Owner ka bridge ----------
        assistant_data = self.assistant_manager.get_assistant(user_id=OWNER_ID)
        if assistant_data:
            self.audio_bridge = AudioBridge(
                assistant_data["bridge"],
                self_user_id=assistant_data.get("user_id") or 0,
                mtproto=assistant_data.get("client"),
            )
            self.audio_bridge.config = self.config
            self.audio_bridge.sync_effects()
            self.current_session_id = assistant_data.get("user_id")
            print("✅ Bridge ready")
        else:
            self.audio_bridge = None
            self.current_session_id = None
            print("⚠️ No owner bridge")

        self.bridge_cmds = BridgeCommands(self.bot, self.assistant_manager, self.audio_bridge, self.db)
        self.fx_cmds = AudioEffectsCommands(self.bot, self.audio_bridge, self.config, self.assistant_manager)
        install_automute_guard(self.fx_cmds)
        AdminCommands(self.bot, self.assistant_manager, self.db, self.audio_bridge, self.bridge_cmds)
        StreamCommands(self.bot, self.assistant_manager, self.audio_bridge)
        VCCommands(self.bot, self.assistant_manager, self.audio_bridge, self.screenshare_manager, self.rcplay_manager)
        self.live_ss_cmds = LiveScreenShareCommands(self.bot, self.assistant_manager, self.db)
        MediaToggleCommands(self.bot, self.live_ss_cmds)
        ExtraCommands(self.bot, self.assistant_manager, self.audio_bridge, self.bridge_cmds)
        PhoneSessionCommands(self.bot, self.assistant_manager, self.audio_bridge, self.db)

        print("\n🎙️ ONLINE\n")
        self._health_task = asyncio.create_task(self._health_checkup_loop())
        self._lag_task = asyncio.create_task(loop_lag_watchdog())   # logs [LAG] if the event loop blocks
        self._mon_tasks = [
            asyncio.create_task(resource_monitor()),                # CPU % of the bot
            asyncio.create_task(net_probe(self._owner_client)),     # Telegram round-trip
            asyncio.create_task(vc_join_watchdog()),                # rejoins a silently-dropped source/target VC
        ]
        await self._send_startup_message()
        await idle()

        print("\n🛑 Shutdown...")
        try:
            await get_record_manager().stop_all(deliver=False)
        except Exception:
            pass
        if self._lag_task:
            self._lag_task.cancel()
        for _t in self._mon_tasks:
            _t.cancel()
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        try:
            await self._send_log_chat(self._build_shutdown_card())
        except Exception:
            pass
        if self.audio_bridge:
            await self.audio_bridge.stop_all_bridges()
        for b in all_user_bridges().values():
            try:
                await b.stop_all_bridges()
            except Exception:
                pass
        await self.screenshare_manager.stop_all()
        await self.rcplay_manager.stop_all()
        await self.bot.stop()
        print("👋 Bye")


async def main():
    acquire_single_instance_lock()
    try:
        bot = VCFightBot()
        await bot.start()
    except KeyboardInterrupt:
        print("\n👋 stopped")
    except Exception as e:
        print(f"\n❌ {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

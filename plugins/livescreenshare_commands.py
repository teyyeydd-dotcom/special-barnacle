import inspect
import logging
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from core.sudo import is_sudo, OWNER_ID
from core.livescreenshare import LiveScreenShare
from core.user_bridges import all_user_bridges
from core.bridge_state import get_live_bridge
from core.database import BridgeDB
from core.vc_leave import get_self_source
from config.settings import TEMP_DIR

logger = logging.getLogger(__name__)


class LiveScreenShareCommands:
    """
    Pehle kabhi bhi `/join -100TARGET_ID` (ya invite link) se assistant ko
    dono groups ki VC mein bhej diya ho — us join ka target_id yahan
    **DB se** (`bridges` table, wahi jo /join khud save karta hai) uthaya
    jaata hai. Isliye:

    - Restart/redeploy ke baad bhi kaam karega (DB persistent hai, RAM nahi)
    - Chahe koi bhi sudo user `/join` kiya ho, koi bhi sudo user
      `/livescreenshare` chala sakta hai — same session/order zaroori nahi

    BUG FIXED THIS PASS: pehle target_id sahi resolve hota tha, lekin
    LiveScreenShare object hamesha *calling* user ke assistant se banta
    tha — agar /join user A ke assistant se hua ho aur /livescreenshare
    user B chalaye, to B ka assistant use hota tha jo source group ki VC
    mein hai hi nahi (kabhi join hi nahi hua), isliye record()/send_frame
    kuch bhi real relay nahi kar pata tha. Ab jis user ke assistant ne
    /join actually chalaya tha, usi ka assistant use hota hai — memory se
    (`all_user_bridges().items()`) ya DB se (naya `owner_id` column).

    /livescreenshare      -> is group (source) ka live video, jo bhi
                              (kisi bhi time) is group ke liye /join se
                              target set hua tha, wahan real-time mirror
    /stoplivescreenshare  -> route band
    /lsdiag               -> DIAGNOSTIC: installed pytgcalls build ki
                              real Device/RecordStream/MediaStream/send_frame
                              signature file bana ke bhejta hai — agla
                              fix guess se nahi, isi se hona chahiye
    """

    def __init__(self, bot: Client, assistant_manager, db=None):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.db = db
        self._bridges: dict[int, LiveScreenShare] = {}   # owner_user_id -> LiveScreenShare
        self.register_handlers()

    def _get_bridge(self, owner_user_id: int) -> LiveScreenShare | None:
        """owner_user_id = jis sudo user ke assistant ne /join chalaya tha
        (ya agar wo pata nahi chala, calling user khud) — NOT necessarily
        jisne abhi /livescreenshare command bheja."""
        assistant = self.assistant_manager.get_assistant(user_id=owner_user_id)
        if not assistant:
            return None
        existing = self._bridges.get(owner_user_id)
        if existing:
            existing.bind_client(assistant["bridge"], mtproto=assistant.get("client"))
            return existing
        bridge = LiveScreenShare(
            assistant["bridge"],
            sudo_users=[owner_user_id],
            mtproto=assistant.get("client"),
        )
        self._bridges[owner_user_id] = bridge
        return bridge

    def _target_from_memory(self, source_id: int) -> tuple[int, int] | None:
        """Fast path — agar isi process mein kisi bhi sudo user ne /join
        kiya hai (bot restart nahi hua), to DB tak jaane ki zarurat nahi.
        Returns (target_id, owner_user_id) — dono, taaki sahi assistant
        pakda ja sake, sirf target nahi.

        NOTE: `all_user_bridges()` sirf SUDO users ka registry hai —
        OWNER ka apna bridge alag (`core.bridge_state`) mein rehta hai
        (dekho core/user_bridges.py docstring), isliye usko separately
        check karna zaroori hai warna owner ke khud /join karne par ye
        function hamesha miss karega aur bewajah DB fallback pe jaayega."""
        owner_bridge = get_live_bridge()
        if owner_bridge is not None:
            info = getattr(owner_bridge, "active_bridges", {}).get(source_id)
            if info:
                target_id = int(info.get("target_id") or 0)
                if target_id and target_id != source_id:
                    return target_id, OWNER_ID

        for owner_uid, audio_bridge in all_user_bridges().items():
            info = getattr(audio_bridge, "active_bridges", {}).get(source_id)
            if info:
                target_id = int(info.get("target_id") or 0)
                if target_id and target_id != source_id:
                    return target_id, owner_uid
        return None

    def _target_from_db(self, source_id: int) -> tuple[int, int | None] | None:
        """Persistent path — /join khud har successful join par yahi
        'bridges' table mein save karta hai (`db.save_bridge`), ab
        owner_id ke saath. Purani rows (is fix se pehle ki) mein
        owner_id NULL hoga — us case mein caller ka apna assistant hi
        try hota hai (best-effort fallback, /join dobara chalane se
        permanently fix ho jaata hai)."""
        if self.db is None:
            return None
        try:
            session = self.db.get_session()
            try:
                row = (
                    session.query(BridgeDB)
                    .filter_by(source_id=source_id)
                    .order_by(BridgeDB.started_at.desc())
                    .first()
                )
                if row and row.target_id and int(row.target_id) != source_id:
                    return int(row.target_id), (int(row.owner_id) if row.owner_id else None)
            finally:
                session.close()
        except Exception:
            pass
        return None

    def _resolve_target(self, source_id: int, caller_uid: int) -> tuple[int | None, int, str]:
        """Returns (target_id, owner_user_id_to_use, via)."""
        hit = self._target_from_memory(source_id)
        if hit:
            target_id, owner_uid = hit
            return target_id, owner_uid, "memory"
        hit = self._target_from_db(source_id)
        if hit:
            target_id, owner_uid = hit
            return target_id, (owner_uid or caller_uid), "db" if owner_uid else "db (owner unknown, using caller)"
        return None, caller_uid, "not_found"

    async def _verify_currently_joined(self, source_id: int, owner_uid: int) -> bool:
        """Ground-truth check with Telegram itself: is this assistant
        ACTUALLY still a participant in source_id's group call right now?

        BUG THIS FIXES: `_resolve_target` only proves a route EXISTED at
        some point (active_bridges in memory, or a `bridges` DB row that is
        never deleted/updated on /leave). Neither proves the VC is joined
        RIGHT NOW — after /leave, /leaverecord or /stopall, the DB row is
        still sitting there, so /livescreenshare would still "find" a
        target and proceed to `_join_source`/`_join_target`, which call
        `client.play()` — i.e. it JOINS the VC ITSELF, exactly the "apni
        marzi se auto-join" behavior reported. This reuses the same real
        SSRC lookup (core/vc_leave.py) already proven for the /leave* fix:
        if Telegram has no participant row for us in that call, we are NOT
        currently joined, so /livescreenshare must refuse instead of
        silently joining on its own."""
        assistant = self.assistant_manager.get_assistant(user_id=owner_uid)
        ubot = assistant.get("client") if assistant else None
        if ubot is None:
            return False
        try:
            _input_call, source_ssrc = await get_self_source(ubot, source_id)
            return source_ssrc is not None
        except Exception as e:
            logger.debug("[LiveScreenShare] _verify_currently_joined %s: %s", source_id, e)
            return False

    def register_handlers(self):
        @self.bot.on_message(filters.command("livescreenshare") & filters.group)
        async def livescreenshare_handler(client: Client, message: Message):
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("❌ Sirf sudo users /livescreenshare use kar sakte hain!")

            source_id = message.chat.id
            target_id, owner_uid, via = self._resolve_target(source_id, uid)
            if not target_id:
                return await message.reply(
                    "⚠️ Iss group ke liye kabhi `/join` nahi hua (na is session mein, na DB mein).\n\n"
                    "`/join -100xxxxxxxxxx` ya `/join https://t.me/+HASH` chalao, "
                    "uske baad `/livescreenshare` (bina id ke) kabhi bhi chalao."
                )

            # A route existing in memory/DB only proves /join happened at
            # SOME point — it does NOT prove the VC is joined right now
            # (could have been /leave'd since). Verify with Telegram itself
            # before doing anything, so this command never auto-joins a VC
            # on its own — only ever mirrors an ALREADY-live join.
            if not await self._verify_currently_joined(source_id, owner_uid):
                return await message.reply(
                    "⚠️ Iss group ki VC abhi **join nahi hai** (shayad `/leave` ho chuka hai, "
                    "ya ye sirf purana record hai).\n\n"
                    "Pehle `/join -100xxxxxxxxxx` (ya invite link) chalao — jab tak VC "
                    "actually joined nahi hai, `/livescreenshare` khud VC join **nahi** karega."
                )

            bridge = self._get_bridge(owner_uid)
            if bridge is None:
                return await message.reply(
                    f"❌ Assistant nahi mila (owner_user_id={owner_uid}, via={via}).\n"
                    f"Us user ko `/addsession` se assistant connect karna hoga."
                )

            if bridge.is_active(source_id):
                return await message.reply("✅ Already live screen sharing chal raha hai iss group se.")

            msg = await message.reply("🎥 Live screen share bridge start ho raha hai...")
            ok, err = await bridge.start_route(source_id, target_id)
            if ok:
                await msg.edit(
                    f"✅ **Live!** Iss group ka screen share ab `{target_id}` mein real-time mirror ho raha hai.\n"
                    f"Band karne ke liye: /stoplivescreenshare"
                )
            else:
                await msg.edit(f"❌ Start fail: `{err}`\n(target source: {via}, assistant owner: `{owner_uid}`)")

        @self.bot.on_message(filters.command("stoplivescreenshare") & filters.group)
        async def stoplivescreenshare_handler(client: Client, message: Message):
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("❌ Sirf sudo users use kar sakte hain!")

            source_id = message.chat.id
            # Kisi bhi sudo user ka jo bhi LiveScreenShare object is source_id
            # ko active route mein rakhe hue hai wahi rokna hai — sirf calling
            # user ka apna nahi, kyunki A join kar sakta hai aur B stop.
            target_bridge = None
            for b in self._bridges.values():
                if b.is_active(source_id):
                    target_bridge = b
                    break

            if not target_bridge:
                return await message.reply("❌ Abhi koi live screen sharing chal nahi raha iss group se!")

            await target_bridge.stop_route(source_id)
            await message.reply("🛑 Live screen sharing band ho gaya. Assistant VC mein hi hai, sirf video mirror stop hua.")

        @self.bot.on_message(filters.command("lsdiag") & filters.group)
        async def lsdiag_handler(client: Client, message: Message):
            """DIAGNOSTIC — koi actual action nahi leta, bas installed
            pytgcalls build ki asli API (Device members, RecordStream/
            MediaStream/record/send_frame signatures) file mein likh ke
            bhejta hai. Iske baad koi bhi remaining "kaam nahi kar raha"
            wajah guess se nahi, isi real output se pata chalegi — jaise
            pehle 'width'/'height' guess galat nikla tha, wo isse pehle
            hi pata chal jaata."""
            uid = message.from_user.id
            if not is_sudo(uid):
                return await message.reply("❌ Sirf sudo users!")

            lines = []
            try:
                import pytgcalls
                from pytgcalls import PyTgCalls
                from pytgcalls.types import Device, Direction, ExternalMedia, MediaStream, RecordStream, StreamFrames

                lines.append(f"pytgcalls version: {getattr(pytgcalls, '__version__', 'unknown')}")
                lines.append("")
                lines.append("--- Device enum members ---")
                try:
                    lines.append(str(list(Device.__members__.keys())))
                except Exception as e:
                    lines.append(f"(introspection failed: {e})")

                for name, obj in (
                    ("RecordStream.__init__", RecordStream),
                    ("MediaStream.__init__", MediaStream),
                    ("PyTgCalls.record", PyTgCalls.record),
                    ("PyTgCalls.play", PyTgCalls.play),
                    ("PyTgCalls.send_frame", getattr(PyTgCalls, "send_frame", None)),
                ):
                    lines.append("")
                    lines.append(f"--- {name} signature ---")
                    try:
                        target = obj.__init__ if name.endswith("__init__") else obj
                        if target is None:
                            lines.append("(not found on this build)")
                        else:
                            lines.append(str(inspect.signature(target)))
                    except Exception as e:
                        lines.append(f"(introspection failed: {e})")

                lines.append("")
                lines.append("--- StreamFrames signature ---")
                try:
                    lines.append(str(inspect.signature(StreamFrames)))
                except Exception as e:
                    lines.append(f"(introspection failed: {e})")

            except Exception as e:
                lines.append(f"pytgcalls import/introspection totally failed: {e}")

            out_path = Path(TEMP_DIR) / f"lsdiag_{message.chat.id}.txt"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(lines), encoding="utf-8")
            await message.reply_document(str(out_path), caption="pytgcalls build introspection — is file ko yahan paste karo")

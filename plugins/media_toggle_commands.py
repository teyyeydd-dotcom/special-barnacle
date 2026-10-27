"""
NEW commands (nothing existing is modified):

  /screenshare on|off     screen relay toggle
  /camera on|off          camera relay toggle
  /screencamera on|off    screen + camera dono toggle

Run them in the SOURCE group (the one whose live video should be mirrored).
They reuse the existing /livescreenshare machinery (same target lookup, same
"VC must already be joined" check, same start_route/stop_route):

  on  -> flag ON; if the relay isn't running for this group yet it is
         started (only if the assistant is ALREADY in the VC — never
         auto-joins), exactly like /livescreenshare would.
  off -> flag OFF; when BOTH screen and camera are off the relay is
         stopped (assistant stays in the VC, only the video mirror stops).

/camera and /screencamera without an argument just show the current state.
Plain `/screenshare` (reply to a photo) is untouched — this handler only
takes `/screenshare on` / `/screenshare off`; everything else falls through
to the old handler in plugins/vc_commands.py.
"""
import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from core import media_toggle
from core.sudo import is_sudo

logger = logging.getLogger(__name__)

_ONOFF = ("on", "off")


def _arg(message: Message) -> str:
    try:
        parts = (message.text or "").split()
        return parts[1].strip().lower() if len(parts) > 1 else ""
    except Exception:
        return ""


def _screenshare_onoff(_, __, message: Message) -> bool:
    return _arg(message) in _ONOFF


class MediaToggleCommands:
    def __init__(self, bot: Client, live_cmds):
        """live_cmds = the existing LiveScreenShareCommands instance."""
        self.bot = bot
        self.live_cmds = live_cmds
        media_toggle.install_hook()
        try:
            from core import video_orient
            video_orient.install_hook()   # camera rotation fix (after the gate, so it wraps it)
        except Exception as e:
            logger.warning("[media_toggle] video_orient not installed: %s", e)
        self.register_handlers()

    # ------------------------------------------------------------------
    def _relay_active(self, source_id: int) -> bool:
        try:
            for b in self.live_cmds._bridges.values():
                if b.is_active(source_id):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _state_text(flags: dict, relay: bool) -> str:
        on = lambda v: "ON ✅" if v else "OFF ❌"
        return (
            f"🖥️ Screen: {on(flags['screen'])}\n"
            f"📷 Camera: {on(flags['camera'])}\n"
            f"🔴 Relay: {'LIVE' if relay else 'band'}"
        )

    async def _ensure_relay_started(self, message: Message):
        """Same checks as /livescreenshare. Returns None on success/already
        running, else a short reason string."""
        source_id = message.chat.id
        if self._relay_active(source_id):
            return None

        lc = self.live_cmds
        target_id, owner_uid, via = lc._resolve_target(source_id, message.from_user.id)
        if not target_id:
            return "Is group ke liye `/join -100xxxxxxxxxx` pehle karo."
        if not await lc._verify_currently_joined(source_id, owner_uid):
            return "Assistant is group ki VC mein abhi joined nahi hai — pehle `/join` karo."
        bridge = lc._get_bridge(owner_uid)
        if bridge is None:
            return f"Assistant nahi mila (owner_user_id={owner_uid}) — `/addsession` karo."
        ok, err = await bridge.start_route(source_id, target_id)
        if not ok:
            return f"Relay start fail: {err}"
        return None

    async def _stop_relay(self, source_id: int) -> None:
        for b in list(self.live_cmds._bridges.values()):
            try:
                if b.is_active(source_id):
                    await b.stop_route(source_id)
            except Exception as e:
                logger.warning("[media_toggle] stop_route: %s", e)

    async def _apply(self, message: Message, which: str, value: bool) -> None:
        source_id = message.chat.id
        if which == "screen":
            flags = media_toggle.set_flags(source_id, screen=value)
        elif which == "camera":
            flags = media_toggle.set_flags(source_id, camera=value)
        else:
            flags = media_toggle.set_flags(source_id, screen=value, camera=value)

        note = ""
        try:
            if value:
                reason = await self._ensure_relay_started(message)
                if reason:
                    note = f"\n\n⚠️ {reason}"
            elif not flags["screen"] and not flags["camera"]:
                await self._stop_relay(source_id)
        except Exception as e:
            logger.warning("[media_toggle] apply error: %s", e)
            note = f"\n\n⚠️ Error: `{type(e).__name__}: {str(e)[:120]}`"

        await message.reply(self._state_text(flags, self._relay_active(source_id)) + note)

    # ------------------------------------------------------------------
    def register_handlers(self):
        # group=-1 -> runs BEFORE the old /screenshare handler (group 0).
        # Filter only matches `/screenshare on|off`, and stop_propagation()
        # keeps the old photo handler from also replying to it.
        @self.bot.on_message(
            filters.command("screenshare") & filters.group & filters.create(_screenshare_onoff),
            group=-1,
        )
        async def screenshare_toggle(client: Client, message: Message):
            try:
                if not message.from_user or not is_sudo(message.from_user.id):
                    await message.reply("❌ Sirf sudo users!")
                else:
                    await self._apply(message, "screen", _arg(message) == "on")
            except Exception as e:
                logger.warning("[media_toggle] /screenshare: %s", e)
            message.stop_propagation()

        @self.bot.on_message(filters.command("camera") & filters.group)
        async def camera_toggle(client: Client, message: Message):
            await self._generic(message, "camera")

        @self.bot.on_message(filters.command("screencamera") & filters.group)
        async def screencamera_toggle(client: Client, message: Message):
            await self._generic(message, "both")

    async def _generic(self, message: Message, which: str) -> None:
        try:
            if not message.from_user or not is_sudo(message.from_user.id):
                return await message.reply("❌ Sirf sudo users!")
            arg = _arg(message)
            if arg not in _ONOFF:
                flags = media_toggle.get(message.chat.id)
                cmd = "camera" if which == "camera" else "screencamera"
                return await message.reply(
                    self._state_text(flags, self._relay_active(message.chat.id))
                    + f"\n\nUse: `/{cmd} on` ya `/{cmd} off`"
                )
            await self._apply(message, which, arg == "on")
        except Exception as e:
            logger.warning("[media_toggle] /%s: %s", which, e)

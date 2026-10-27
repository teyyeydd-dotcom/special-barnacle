"""
/rcplay — Telegram recording (voice note / audio) ko TARGET group ki VC
mein hi bajata hai, SOURCE ko kabhi touch nahi karta.

core/screenshare.py jaisa hi proven pattern: file seedha play() ko diya
jaata hai — koi loudnorm/volume/compressor filter NAHI lagaya jaata, taaki
asli awaaz/bass/loudness bilkul wahi rahe jo original recording mein thi.
Khatam hone par target ka ExternalMedia.AUDIO khud restore ho jaata hai,
taaki live mic-bridge seamlessly wapas chalne lage — /rcplayoff se pehle
bhi rok sakte ho.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from config.settings import TEMP_DIR
from core import media_state

logger = logging.getLogger(__name__)

_MANAGER = None


def get_rcplay_manager():
    return _MANAGER


def _live_route(target_id: int):
    """core/livescreenshare.py ka LiveScreenShare jo target par video chala
    raha hai (ya None). Lazy import — pytgcalls jaisi heavy deps sirf tab."""
    try:
        from core.livescreenshare import find_live_route
        return find_live_route(int(target_id))
    except Exception:
        return None


def _ss_active(chat_id: int) -> bool:
    try:
        from core.screenshare import get_screenshare_manager
        ssm = get_screenshare_manager()
        return bool(ssm is not None and ssm.is_active(int(chat_id)))
    except Exception:
        return False


class RCPlayManager:
    def __init__(self):
        global _MANAGER
        self.active_rcplays: Dict[int, dict] = {}  # target_id -> {path, started_at}
        self._clients: Dict[int, object] = {}
        self._loop_handler_clients = set()
        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
        _MANAGER = self

    def _ensure_end_handler(self, pytgcalls_client) -> None:
        """Recording khatam hone par (naturally ya /rcplayoff se pehle
        nahi) khud-ba-khud target ka audio wapas ExternalMedia.AUDIO pe
        restore karta hai — screenshare ki tarah loop NAHI karta, ek hi
        baar bajta hai jaise ek asli recording."""
        cid = id(pytgcalls_client)
        if cid in self._loop_handler_clients:
            return
        self._loop_handler_clients.add(cid)

        async def _on_end(client, update):
            try:
                chat_id = int(getattr(update, "chat_id", 0) or 0)
            except Exception:
                return
            if chat_id not in self.active_rcplays:
                return  # humara nahi — kisi aur stream (screenshare, etc.) ka end hai
            if media_state.stream_type_of(update) == "video":
                return  # screenshare video ka loop/end — recording nahi rukni chahiye
            if _live_route(chat_id) is not None:
                # LIVE screenshare har few sec segment swap karta hai — har swap
                # ek stream_end deta hai. Ye recording ka end NAHI hai; end
                # timer (_end_timer) se pakda jaata hai.
                return
            if _ss_active(chat_id) and media_state.stream_type_of(update) != "audio":
                return  # normal screenshare loop ka (type-unknown) end
            logger.info("[RCPlay] recording khatam, audio restore chat=%s", chat_id)
            await self.stop_rcplay(chat_id)

        registered = False
        try:
            from pytgcalls import filters as fl
            pytgcalls_client.on_update(fl.stream_end())(_on_end)
            registered = True
        except Exception as e:
            logger.debug("[RCPlay] fl.stream_end() unavailable: %s", e)
        if not registered:
            try:
                pytgcalls_client.on_stream_end()(_on_end)
                registered = True
            except Exception as e:
                logger.warning("[RCPlay] could not register end handler: %s", e)
                self._loop_handler_clients.discard(cid)

    async def start_rcplay(
        self,
        target_id: int,
        pytgcalls_client,
        audio_path: str,
    ) -> Tuple[bool, str]:
        """TARGET only — source kabhi touch nahi hota. Original file seedha
        play() ko diya jaata hai (koi -af filter, koi volume/loudnorm nahi)
        taaki loudness/bass 100% original rahe."""
        from pytgcalls.types import MediaStream
        from pytgcalls.types.raw import AudioParameters

        try:
            if not audio_path or not os.path.exists(audio_path):
                return False, "audio file missing on disk"
            if os.path.getsize(audio_path) < 200:
                return False, "downloaded file too small — recording corrupt/empty"

            target_id = int(target_id)
            if target_id in self.active_rcplays:
                await self.stop_rcplay(target_id)

            # bitrate=48000,channels=2 sirf OUTPUT encode quality set karta
            # hai (call ka format) — original recording ki loudness/bass ko
            # kisi filter se nahi chhedta, koi gain/volume/loudnorm nahi hai.
            await media_state.set_audio(target_id, audio_path, "rcplay")
            from core.screenshare import get_screenshare_manager
            ssm = get_screenshare_manager()
            lss = _live_route(target_id)
            if lss is not None:
                # LIVE screenshare chal raha hai: plain play() video maar deta
                # hai aur uski segment-swap audio ko 2s mein wapas live-mic se
                # overwrite kar deti thi. Ab swap khud isi file ki audio chalata hai.
                if not await lss.refresh_audio(target_id):
                    media_state.clear_audio(target_id)
                    return False, "live screenshare ke saath audio play nahi hua"
            elif ssm is not None and ssm.is_active(target_id):
                # screenshare chal raha hai: video wahi rahe, sirf audio badle
                if not await ssm.refresh_audio(target_id, pytgcalls_client):
                    media_state.clear_audio(target_id)
                    return False, "screenshare ke saath audio play nahi hua"
            else:
                stream = MediaStream(audio_path, audio_parameters=AudioParameters(bitrate=48000, channels=2))
                await pytgcalls_client.play(target_id, stream)

            self._ensure_end_handler(pytgcalls_client)
            info = {"path": audio_path, "started_at": time.time()}
            self.active_rcplays[target_id] = info
            self._clients[target_id] = pytgcalls_client
            if lss is not None or _ss_active(target_id):
                self._arm_end_timer(target_id, info)
            logger.info("[RCPlay] LIVE target=%s file=%s", target_id, audio_path)
            return True, ""
        except Exception as e:
            logger.error("[RCPlay] start fail target=%s: %s", target_id, e)
            return False, str(e)[:250]

    def _arm_end_timer(self, target_id: int, info: dict) -> None:
        """Screenshare (live ya normal) ke saath stream_end events bharosemand
        nahi (video swap/loop ke end bhi aate hain) — isliye recording ki
        length se khud hi end pakdo aur audio live-mic par restore karo."""
        try:
            entry = media_state._entry(target_id)
            dur = float(entry.get("duration") or 0) if entry else 0.0
        except Exception:
            dur = 0.0
        if dur <= 0:
            return
        import asyncio

        async def _wait_and_stop():
            try:
                await asyncio.sleep(dur + 1.0)
            except asyncio.CancelledError:
                return
            if self.active_rcplays.get(target_id) is info:
                logger.info("[RCPlay] recording khatam (timer), audio restore chat=%s", target_id)
                info.pop("timer", None)
                await self.stop_rcplay(target_id)

        info["timer"] = asyncio.create_task(_wait_and_stop())

    async def stop_rcplay(self, target_id: int) -> bool:
        """Target ka ExternalMedia.AUDIO wapas ON karta hai — isi se live
        mic-bridge output seamlessly resume hota hai (screenshare.py ka
        stop_screenshare() jaisa hi restore step)."""
        cid = int(target_id)
        info = self.active_rcplays.pop(cid, None)
        found = info is not None

        if info:
            path = info.get("path")
            if path and os.path.exists(path) and str(path).startswith(str(TEMP_DIR)):
                try:
                    os.remove(path)
                except Exception:
                    pass

        media_state.clear_audio(cid)
        timer = (info or {}).pop("timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        client = self._clients.pop(cid, None)
        lss = _live_route(cid)
        if lss is not None:
            # LIVE screenshare: video chalta rahe, audio wapas live mic par
            try:
                await lss.refresh_audio(cid)
            except Exception as e:
                logger.debug("[RCPlay] live-screenshare audio restore: %s", e)
            if found:
                logger.info("[RCPlay] OFF target=%s (live screenshare kept)", cid)
            return found
        if client is not None:
            try:
                from core.screenshare import get_screenshare_manager
                ssm = get_screenshare_manager()
                if ssm is not None and ssm.is_active(cid):
                    # video chalta rahe, audio wapas live mic bridge par
                    await ssm.refresh_audio(cid, client)
                    if found:
                        logger.info("[RCPlay] OFF target=%s (screenshare kept)", cid)
                    return found
            except Exception as e:
                logger.debug("[RCPlay] screenshare-aware restore: %s", e)
            try:
                from pytgcalls.types import ExternalMedia, MediaStream
                from pytgcalls.types.raw import AudioParameters

                await client.play(
                    cid,
                    MediaStream(ExternalMedia.AUDIO, AudioParameters(bitrate=48000, channels=2)),
                )
            except Exception as e:
                logger.debug("[RCPlay] restore audio after stop: %s", e)

        if found:
            logger.info("[RCPlay] OFF target=%s", cid)
        return found

    def is_active(self, chat_id: int) -> bool:
        return int(chat_id) in self.active_rcplays

    async def stop_all(self) -> int:
        count = 0
        for cid in list(self.active_rcplays.keys()):
            if await self.stop_rcplay(cid):
                count += 1
        return count

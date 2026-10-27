"""
Screenshare = TARGET only.

Video from file + AUDIO from ExternalMedia (send_frame).
Source mic bridge NEVER touched → awaaz + screenshare dono.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config.settings import TEMP_DIR, SCREENSHARE_QUALITY, SCREENSHARE_FPS
from core import media_state

logger = logging.getLogger(__name__)

QUALITY_MAP = {
    "360p": (640, 360),
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

_MANAGER = None


def get_screenshare_manager():
    return _MANAGER


class ScreenshareManager:
    def __init__(self):
        global _MANAGER
        self.active_screenshares: Dict[int, dict] = {}
        self.ffmpeg_processes: Dict[int, subprocess.Popen] = {}
        self._clients: Dict[int, object] = {}
        # live-mux queues kept for compatibility; primary path is ExternalMedia.AUDIO
        self._audio_queues: Dict[int, object] = {}
        self._loop_handler_clients: Set[int] = set()  # id(pytgcalls_client) already wired
        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
        _MANAGER = self

    def _ensure_loop_handler(self, pytgcalls_client) -> None:
        """A finite mp4 (photo turned into a short clip) stops at its own
        end — pytgcalls doesn't loop it — leaving the last/frozen frame
        looking blurry/stuck. Wire an on_stream_end handler (once per
        client) that replays the same file for any chat that still has
        an active screenshare, so it keeps showing for as long as the
        assistant stays in the VC."""
        cid = id(pytgcalls_client)
        if cid in self._loop_handler_clients:
            return
        self._loop_handler_clients.add(cid)

        async def _replay(client, update):
            try:
                chat_id = int(getattr(update, "chat_id", 0) or 0)
            except Exception:
                return
            # yt/rcplay ka audio khatam hona video loop trigger nahi karna chahiye
            if media_state.stream_type_of(update) == "audio":
                return
            entry = self.active_screenshares.get(chat_id)
            if not entry:
                return  # /screenshareoff already called, or nothing active here
            try:
                await self._play_video_with_external_audio(
                    pytgcalls_client, chat_id, entry["path"],
                    entry["width"], entry["height"], entry["fps"],
                )
                logger.info("[SS] looped replay chat=%s", chat_id)
            except Exception as e:
                logger.warning("[SS] loop replay failed chat=%s: %s", chat_id, e)

        registered = False
        try:
            from pytgcalls import filters as fl
            pytgcalls_client.on_update(fl.stream_end())(_replay)
            registered = True
        except Exception as e:
            logger.debug("[SS] fl.stream_end() unavailable: %s", e)
        if not registered:
            try:
                pytgcalls_client.on_stream_end()(_replay)
                registered = True
            except Exception as e:
                logger.warning("[SS] could not register loop handler: %s", e)
                self._loop_handler_clients.discard(cid)

    def _resolution(self, quality: str) -> Tuple[int, int]:
        return QUALITY_MAP.get(quality, QUALITY_MAP["720p"])

    def has_live_mux(self, chat_id: int) -> bool:
        # ExternalMedia.AUDIO path — bridge uses send_frame, not mux queue
        return False

    def push_live_audio(self, chat_id: int, pcm: bytes) -> bool:
        return False

    def _prepare_media_file(
        self,
        src_path: str,
        tag: str,
        is_photo: bool,
        width: int,
        height: int,
        fps: int,
    ) -> Tuple[Optional[str], str]:
        if not shutil.which("ffmpeg"):
            return None, "ffmpeg not found on this VPS (apt install ffmpeg)"

        out_path = str(Path(TEMP_DIR) / f"ss_vid_{tag}.mp4")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass

        # Video only (no audio track) — live awaaz ExternalMedia se aayegi.
        # PHOTO: a still image looped into a short video (not full fps —
        # a static frame doesn't need 30fps, and a low fps + short duration
        # keeps frame count low so weak/shared-CPU VPS instances don't blow
        # past the subprocess timeout below). Re-run /screenshare to
        # refresh it if it needs to stay up longer.
        photo_fps = min(fps, 8)
        photo_duration = 60  # was 120s; halved again for speed — re-run /screenshare to refresh
        scale_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        )
        if is_photo:
            cmd = [
                "ffmpeg", "-nostdin", "-y",
                "-loop", "1", "-i", src_path,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                "-tune", "stillimage", "-pix_fmt", "yuv420p",
                "-vf", scale_filter,
                "-r", str(photo_fps),
                "-an",
                "-t", str(photo_duration),
                out_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-nostdin", "-y", "-i", src_path,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-vf", scale_filter,
                "-r", str(min(fps, 30)),
                "-an",
                "-movflags", "+faststart",
                out_path,
            ]

        try:
            r = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=240,
            )
            if r.returncode != 0 or not os.path.exists(out_path):
                tail = (r.stderr or b"")[-400:].decode(errors="replace")
                logger.error(f"ffmpeg ss fail: {tail!r}")
                return None, tail or "ffmpeg exited non-zero"
            if os.path.getsize(out_path) < 500:
                return None, "output file too small (source media likely corrupt/empty)"
            return out_path, ""
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg ss timeout (240s)")
            return None, "ffmpeg timed out after 240s (media too large/slow VPS)"
        except Exception as e:
            logger.error(f"ffmpeg convert error: {e}")
            return None, str(e)

    async def _mux_audio_into_video(self, video_path: str, audio_path: str, chat_id: int) -> Optional[str]:
        """Last-resort fallback: audio ko video file me hi mux kar do
        (video loop hota hai, audio ke khatam hone par ruk jata hai)."""
        out = str(Path(TEMP_DIR) / f"ss_mux_{chat_id}_{int(time.time() * 1000)}.mp4")
        try:
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-stream_loop", "-1", "-i", video_path, "-i", audio_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p.communicate(), timeout=180)
            if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
                return out
        except Exception as e:
            logger.warning("[SS] mux fallback failed: %s", e)
        return None

    async def _play_video_with_external_audio(
        self,
        pytgcalls_client,
        chat_id: int,
        play_path: str,
        width: int,
        height: int,
        fps: int,
    ) -> Tuple[bool, str]:
        """
        Video from file. Audio =
          * agar is chat me /yt /radio /rcplay chal raha hai -> wahi audio
            (bache hue hisse se), taaki screenshare use band na kare
          * warna live mic bridge (ExternalMedia.AUDIO).
        """
        from pytgcalls.types import MediaStream, ExternalMedia
        from pytgcalls.types.raw import AudioParameters, VideoParameters

        audio_params = AudioParameters(bitrate=48000, channels=2)
        video_params = VideoParameters(width=width, height=height, frame_rate=fps)
        last_err = ""

        file_audio = await media_state.remaining_audio(chat_id)
        audio_src = file_audio if file_audio else ExternalMedia.AUDIO

        # 1) Best: video file + separate audio source
        try:
            stream = MediaStream(
                play_path,
                audio_parameters=audio_params,
                video_parameters=video_params,
                audio_path=audio_src,
            )
            await pytgcalls_client.play(chat_id, stream)
            logger.info("[SS] play video+%s chat=%s",
                        "FILE-AUDIO" if file_audio else "ExternalAUDIO", chat_id)
            return True, ""
        except TypeError as e:
            last_err = str(e)
            logger.debug("[SS] audio_path kw not supported: %s", e)
        except Exception as e:
            last_err = str(e)
            logger.warning("[SS] video+audio fail: %s", e)

        # 1b) File audio active: mux it into the video (works on any lib version)
        if file_audio and not str(file_audio).startswith("http"):
            muxed = await self._mux_audio_into_video(play_path, file_audio, chat_id)
            if muxed:
                try:
                    await pytgcalls_client.play(
                        chat_id,
                        MediaStream(muxed, audio_parameters=audio_params, video_parameters=video_params),
                    )
                    logger.info("[SS] play muxed video+file-audio chat=%s", chat_id)
                    return True, ""
                except Exception as e:
                    last_err = str(e)
                    logger.warning("[SS] muxed play fail: %s", e)

        # 2) Try Flags if available (live audio only)
        if not file_audio:
            try:
                Flags = getattr(MediaStream, "Flags", None)
                if Flags is not None:
                    stream = MediaStream(
                        play_path,
                        audio_parameters=audio_params,
                        video_parameters=video_params,
                        audio_flags=Flags.IGNORE,
                    )
                    await pytgcalls_client.play(chat_id, stream)
                    try:
                        await pytgcalls_client.play(
                            chat_id,
                            MediaStream(ExternalMedia.AUDIO, audio_params),
                        )
                    except Exception:
                        pass
                    logger.info("[SS] play video IGNORE audio chat=%s", chat_id)
                    return True, ""
            except Exception as e:
                last_err = str(e)
                logger.debug("[SS] Flags path fail: %s", e)

        # 3) Fallback: plain video (may mute audio until off)
        try:
            stream = MediaStream(
                play_path,
                audio_parameters=audio_params,
                video_parameters=video_params,
            )
            await pytgcalls_client.play(chat_id, stream)
            logger.warning("[SS] fallback plain video — audio may pause until off")
            return True, ""
        except TypeError:
            try:
                await pytgcalls_client.play(chat_id, MediaStream(play_path))
                return True, ""
            except Exception as e:
                last_err = str(e)
                logger.error("[SS] play fail chat=%s: %s", chat_id, e)
                return False, last_err
        except Exception as e:
            last_err = str(e)
            logger.error("[SS] play fail chat=%s: %s", chat_id, e)
            return False, last_err

    async def refresh_audio(self, chat_id: int, pytgcalls_client=None) -> bool:
        """Chalta hua screenshare video ko wahi rakhke uska audio badalta hai
        (naya /yt /rcplay start hone par, ya unke khatam hone par live mic
        wapas). False = is chat me screenshare active nahi hai."""
        cid = int(chat_id)
        entry = self.active_screenshares.get(cid)
        if not entry:
            return False
        client = pytgcalls_client or self._clients.get(cid)
        if client is None:
            return False
        ok, err = await self._play_video_with_external_audio(
            client, cid, entry["path"], entry["width"], entry["height"], entry["fps"],
        )
        if not ok:
            logger.warning("[SS] refresh_audio fail chat=%s: %s", cid, err)
        return ok

    async def start_media_share(
        self,
        chat_id: int,
        pytgcalls_client,
        media_path: str,
        is_photo: bool = False,
        quality: str = SCREENSHARE_QUALITY,
        fps: int = SCREENSHARE_FPS,
        extra_chats: Optional[List[int]] = None,
        live_audio: bool = True,
    ) -> Tuple[bool, str]:
        """TARGET only. Never touches source. Returns (ok, error_message) —
        the error is shown back in Telegram since the owner has no shell
        access to bot.log."""
        try:
            if not media_path or not os.path.exists(media_path):
                return False, "media file missing on disk"

            target_id = int(chat_id)
            # ignore extra_chats — source must not get video
            if target_id in self.active_screenshares:
                await self.stop_screenshare(target_id)

            width, height = self._resolution(quality or "720p")
            fps = max(8, min(30, int(fps or 15)))
            tag = f"{target_id}_{int(time.time())}"

            play_path, prep_err = await asyncio.to_thread(
                self._prepare_media_file,
                media_path, tag, is_photo, width, height, fps,
            )
            if not play_path:
                return False, prep_err or "ffmpeg convert failed"

            ok, play_err = await self._play_video_with_external_audio(
                pytgcalls_client, target_id, play_path, width, height, fps,
            )
            if not ok:
                return False, play_err or "pytgcalls play() failed"

            self._ensure_loop_handler(pytgcalls_client)

            self.active_screenshares[target_id] = {
                "path": play_path,
                "src": media_path,
                "is_photo": is_photo,
                "quality": quality,
                "fps": fps,
                "width": width,
                "height": height,
                "started_at": time.time(),
                "preview": media_path if is_photo else None,
                "group": [target_id],
                "live_mux": False,
                "external_audio": True,
            }
            self._clients[target_id] = pytgcalls_client
            logger.info(
                "[SS] TARGET-only video=%s photo=%s chat=%s",
                not is_photo, is_photo, target_id,
            )
            return True, ""
        except Exception as e:
            logger.exception(f"start_media_share failed: {e}")
            await self.stop_screenshare(chat_id)
            return False, str(e)

    async def start_screenshare(
        self,
        chat_id: int,
        pytgcalls_client,
        quality: str = SCREENSHARE_QUALITY,
        fps: int = SCREENSHARE_FPS,
        include_audio: bool = True,
        media_path: Optional[str] = None,
        is_photo: bool = False,
        extra_chats: Optional[List[int]] = None,
    ) -> Tuple[bool, str]:
        if media_path:
            return await self.start_media_share(
                chat_id, pytgcalls_client, media_path,
                is_photo=is_photo, quality=quality, fps=fps,
                extra_chats=None, live_audio=True,
            )
        return False, "no media_path given"

    async def stop_screenshare(self, chat_id: int) -> bool:
        cid = int(chat_id)
        found = cid in self.active_screenshares

        info = self.active_screenshares.pop(cid, None) or {}
        for key in ("path",):
            p = info.get(key)
            if p and os.path.exists(p) and str(p).startswith(str(TEMP_DIR)):
                try:
                    os.remove(p)
                except Exception:
                    pass

        self._audio_queues.pop(cid, None)

        client = self._clients.pop(cid, None)
        if client is not None:
            try:
                from pytgcalls.types import ExternalMedia, MediaStream
                from pytgcalls.types.raw import AudioParameters

                file_audio = await media_state.remaining_audio(cid)
                await client.play(
                    cid,
                    MediaStream(
                        file_audio if file_audio else ExternalMedia.AUDIO,
                        AudioParameters(bitrate=48000, channels=2),
                    ),
                )
            except Exception as e:
                logger.debug(f"restore audio after SS: {e}")

        if found:
            logger.info(f"[SS] OFF chat={cid}")
        return found

    def is_active(self, chat_id: int) -> bool:
        return chat_id in self.active_screenshares

    def get_preview(self, chat_id: int) -> Optional[str]:
        if chat_id in self.active_screenshares:
            return self.active_screenshares[chat_id].get("preview")
        return None

    async def stop_all(self) -> int:
        count = 0
        for chat_id in list(self.active_screenshares.keys()):
            if await self.stop_screenshare(chat_id):
                count += 1
        return count

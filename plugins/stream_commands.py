import os
import asyncio
import subprocess
import time
from pathlib import Path

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

from core.sudo import is_sudo
from core.effects import AudioEffects
from core.bridge_state import get_live_bridge, set_live_bridge
from core import media_state
from core.screenshare import get_screenshare_manager
from config.settings import SHRUTI_API_URL, SHRUTI_API_KEY


class StreamCommands:
    def __init__(self, bot: Client, assistant_manager, audio_bridge):
        self.bot = bot
        self.assistant_manager = assistant_manager
        self.audio_bridge = audio_bridge
        Path("downloads").mkdir(exist_ok=True)
        self.register_handlers()

    def _ensure_bridge(self):
        live = get_live_bridge()
        if live is not None:
            self.audio_bridge = live
            return live
        if self.audio_bridge is not None:
            set_live_bridge(self.audio_bridge)
            return self.audio_bridge
        assistant = self.assistant_manager.get_assistant()
        if not assistant:
            return None
        from core.bridge import AudioBridge

        self.audio_bridge = AudioBridge(
            assistant["bridge"],
            self_user_id=assistant.get("user_id") or 0,
            mtproto=assistant.get("client"),
        )
        set_live_bridge(self.audio_bridge)
        return self.audio_bridge

    def _route_ids(self, ab, chat_id: int):
        src, tgt = chat_id, chat_id
        if ab and chat_id in getattr(ab, "active_bridges", {}):
            br = ab.active_bridges[chat_id]
            src = int(br.get("source_id") or chat_id)
            tgt = int(br.get("target_id") or chat_id)
        return src, tgt

    def _render_fx(self, media_path: str, cfg: dict) -> str:
        if not media_path or not os.path.exists(media_path):
            return media_path
        # Skip heavy FX render if all zero-ish — faster play
        filt = AudioEffects.build_ffmpeg_filter(cfg)
        if not filt or filt == "anull":
            return media_path
        out = f"downloads/fx_{int(time.time() * 1000)}.wav"
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", media_path,
                    "-af", filt,
                    "-ar", "48000", "-ac", "2", "-f", "wav", out,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=90,
            )
            if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
                return out
        except Exception as e:
            print(f"fx render: {e}")
        return media_path

    async def _play_file(self, ab, src: int, tgt: int, path: str):
        """Play file on source+target WITHOUT leave/rejoin."""
        from pytgcalls.types import MediaStream

        if hasattr(ab, "_ensure_mtproto"):
            try:
                await ab._ensure_mtproto()
            except Exception:
                pass

        # Only soft-join missing chats — never leave existing
        for cid in {src, tgt}:
            if cid not in getattr(ab, "active_bridges", {}):
                try:
                    await ab.join_vc_only(cid)
                except Exception as e:
                    print(f"soft join {cid}: {e}")

        play_path = path
        try:
            play_path = await asyncio.to_thread(self._render_fx, path, ab.config)
        except Exception:
            play_path = path

        is_url = bool(play_path) and str(play_path).startswith(("http://", "https://"))
        if not play_path or (not is_url and not os.path.exists(play_path)):
            return False, "file missing after download"

        played = []
        errors = []
        ssm = get_screenshare_manager()
        for cid in {src, tgt}:
            # Is chat me screenshare chal raha hai? To plain play() video
            # ko maar dega — video wahi rakho, sirf audio badlo.
            if ssm is not None and ssm.is_active(cid):
                await media_state.set_audio(cid, play_path, "yt")
                if await ssm.refresh_audio(cid, ab.client):
                    played.append(cid)
                    continue
                media_state.clear_audio(cid)
                errors.append(f"{cid}: screenshare ke saath audio fail")
                continue
            try:
                await ab.client.play(cid, MediaStream(play_path))
                played.append(cid)
                await media_state.set_audio(cid, play_path, "yt")
            except Exception as e:
                # retry once without filter path
                try:
                    await ab.client.play(cid, MediaStream(path))
                    played.append(cid)
                    await media_state.set_audio(cid, path, "yt")
                except Exception as e2:
                    errors.append(f"{cid}:{e2}")

        if played:
            info = {
                "source_id": src,
                "target_id": tgt,
                "mode": "media",
                "started_at": time.time(),
                **dict(ab.config),
            }
            ab.active_bridges[src] = info
            if tgt != src:
                ab.active_bridges[tgt] = info
            set_live_bridge(ab)
            return True, f"playing in {len(played)} VC (src+tgt)"
        return False, " | ".join(errors)[:220] or "play failed"

    def register_handlers(self):

        @self.bot.on_message(filters.command("radio") & filters.group)
        async def radio_cmd(client, message: Message):
            if not self._is_sudo(message.from_user.id):
                return await message.reply("No access.")
            if len(message.command) < 2:
                return await message.reply("`/radio URL`")

            status_msg = await message.reply("**Radio...**")
            try:
                ab = self._ensure_bridge()
                if not ab:
                    return await status_msg.edit("No assistant — pehle session lagao")

                url = message.command[1].strip()
                src, tgt = self._route_ids(ab, message.chat.id)
                ok, info = await self._play_file(ab, src, tgt, url)
                cfg = ab.config
                if ok:
                    await status_msg.edit(
                        f"**Radio ON**\n`{url}`\n"
                        f"G{cfg.get('gain')} B{cfg.get('bass')}\n"
                        f"{info}"
                    )
                else:
                    await status_msg.edit(
                        f"**Fail**\n`{info}`\n\n"
                        f"Pehle `/join -100TARGET` se bridge ON karo"
                    )
            except Exception as ex:
                await status_msg.edit(f"`{str(ex)[:150]}`")

        @self.bot.on_message(filters.command(["yt", "youtube"]) & filters.group)
        async def youtube_cmd(client, message: Message):
            if not self._is_sudo(message.from_user.id):
                return await message.reply("No access.")
            if len(message.command) < 2:
                return await message.reply("`/yt URL`  ya  `/yt search words`")

            status_msg = await message.reply("**YouTube download...**")
            try:
                ab = self._ensure_bridge()
                if not ab:
                    return await status_msg.edit("No assistant — pehle session lagao")

                query = message.text.split(None, 1)[1].strip()
                path, dl_err = await self._download_youtube_audio(query, message.chat.id)
                if not path:
                    reason = f"`{dl_err[:300]}`\n\n" if dl_err else ""
                    return await status_msg.edit(
                        "**Download fail**\n\n"
                        f"{reason}"
                        "• Valid link: `https://youtu.be/...`\n"
                        "• Update: `pip install -U yt-dlp`\n"
                        "• VPS pe `ffmpeg` hona chahiye"
                    )

                await status_msg.edit("**Playing source + target...**")
                src, tgt = self._route_ids(ab, message.chat.id)
                ok, info = await self._play_file(ab, src, tgt, path)
                cfg = ab.config
                if ok:
                    await status_msg.edit(
                        f"**YouTube ON**\n"
                        f"G{cfg.get('gain')} B{cfg.get('bass')} "
                        f"L{cfg.get('loudness')}\n"
                        f"{info}\n"
                        f"VC restart nahi — stream swap\n"
                        f"`/leave` se band"
                    )
                else:
                    await status_msg.edit(
                        f"**Play fail**\n`{info}`\n\n"
                        f"1. `/join -100TARGET` se bridge ON\n"
                        f"2. Dono groups me VC start\n"
                        f"3. bot.log check"
                    )
            except Exception as ex:
                await status_msg.edit(f"`{str(ex)[:150]}`")

    async def _download_via_shruti_api(self, url: str, chat_id: int) -> tuple:
        """Try the SHRUTI_API_URL third-party service first — it isn't
        running from a datacenter IP so it dodges YouTube's bot-check that
        breaks yt-dlp on hosted bots. Returns (path, error_message).

        NOTE: the exact JSON shape of this API isn't documented anywhere
        public, so this tries several common field names. If it still
        fails, the raw response is included in error_message (truncated)
        so it can be shown in Telegram and the parsing adjusted."""
        if not SHRUTI_API_URL:
            return None, "SHRUTI_API_URL not set"

        Path("downloads").mkdir(exist_ok=True)
        stamp = int(time.time())
        out_path = str(Path("downloads") / f"yt_{chat_id}_{stamp}.mp3")

        params = {"url": url}
        if SHRUTI_API_KEY:
            params["apikey"] = SHRUTI_API_KEY

        try:
            async with aiohttp.ClientSession() as session:
                # Common endpoint name for this kind of API. If the
                # service uses a different path, change it here.
                async with session.get(
                    f"{SHRUTI_API_URL}/youtube",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    ctype = resp.headers.get("Content-Type", "")
                    if "audio" in ctype or "octet-stream" in ctype:
                        # API streamed the audio file directly.
                        data = await resp.read()
                        if not data:
                            return None, "empty audio response from API"
                        with open(out_path, "wb") as f:
                            f.write(data)
                        return out_path, ""

                    data = await resp.json(content_type=None)
                    if not resp.ok or (isinstance(data, dict) and data.get("status") is False):
                        return None, f"API error: {str(data)[:300]}"

                    # Try common places a direct-download link might be.
                    link = None
                    if isinstance(data, dict):
                        result = data.get("result") if isinstance(data.get("result"), dict) else data
                        for key in ("download_url", "downloadUrl", "url", "link", "audio", "dl_url"):
                            if isinstance(result.get(key), str) and result.get(key, "").startswith("http"):
                                link = result[key]
                                break

                    if not link:
                        return None, f"couldn't find download link in API response: {str(data)[:300]}"

                async with session.get(link, timeout=aiohttp.ClientTimeout(total=120)) as file_resp:
                    if not file_resp.ok:
                        return None, f"file download failed: HTTP {file_resp.status}"
                    audio_bytes = await file_resp.read()
                    if not audio_bytes:
                        return None, "empty file from download link"
                    with open(out_path, "wb") as f:
                        f.write(audio_bytes)
                    return out_path, ""
        except Exception as e:
            return None, f"API exception: {str(e)[:200]}"

    async def _download_youtube_audio(self, url_or_query: str, chat_id: int) -> tuple:
        """Returns (path, error_message). path is None on failure, with the
        real error in error_message so it can be shown back in
        Telegram — owner has no shell, so this is the only way to see why
        a download actually failed.

        Tries the SHRUTI_API_URL service first (for direct URLs), then
        falls back to yt-dlp."""
        if SHRUTI_API_URL and url_or_query.startswith("http"):
            api_path, api_err = await self._download_via_shruti_api(url_or_query, chat_id)
            if api_path:
                return api_path, ""
            # fall through to yt-dlp, but remember why the API failed
            shruti_err = api_err
        else:
            shruti_err = None

        try:
            from yt_dlp import YoutubeDL

            Path("downloads").mkdir(exist_ok=True)
            stamp = int(time.time())
            outtmpl = str(Path("downloads") / f"yt_{chat_id}_{stamp}.%(ext)s")

            target = url_or_query
            if not url_or_query.startswith("http"):
                target = f"ytsearch1:{url_or_query}"

            # player_client + a normal browser UA works around YouTube's
            # "Sign in to confirm you're not a bot" block that cloud/VPS
            # IPs hit far more than home IPs — the #1 real-world cause of
            # yt-dlp failing silently on hosted bots as of 2025-26.
            ydl_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": outtmpl,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "geo_bypass": True,
                "extractor_args": {
                    "youtube": {"player_client": ["android", "web"]},
                },
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                },
                "retries": 3,
                "socket_timeout": 30,
            }

            def _run():
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target, download=True)
                    if isinstance(info, dict) and info.get("entries"):
                        info = info["entries"][0]
                    filename = ydl.prepare_filename(info)
                    # postprocessor writes .mp3
                    base = filename.rsplit(".", 1)[0]
                    mp3 = base + ".mp3"
                    if os.path.exists(mp3):
                        return mp3
                    for ext in (".webm", ".m4a", ".opus", ".ogg", ".mp3"):
                        cand = base + ext
                        if os.path.exists(cand):
                            return cand
                    if os.path.exists(filename):
                        return filename
                    # glob fallback
                    folder = Path("downloads")
                    matches = sorted(folder.glob(f"yt_{chat_id}_{stamp}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    return str(matches[0]) if matches else None

            path = await asyncio.to_thread(_run)
            if not path:
                msg = "no file produced after download (unknown reason)"
                if shruti_err:
                    msg = f"[API: {shruti_err}] [yt-dlp: {msg}]"
                return None, msg
            return path, ""
        except Exception as e:
            err = str(e)
            if shruti_err:
                err = f"[API: {shruti_err}] [yt-dlp: {err}]"
            print(f"Download error: {err}")
            return None, err

    def _is_sudo(self, user_id: int) -> bool:
        return is_sudo(user_id)

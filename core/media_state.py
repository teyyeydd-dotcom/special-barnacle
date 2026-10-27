"""
Shared "file audio" state per chat (yt / radio / rcplay).

Problem this solves: pytgcalls play() REPLACES the whole stream of a chat.
So /screenshare (video + live audio) killed /yt and /rcplay, and starting
/yt or /rcplay while a screenshare was up killed the video.

Now every module that plays a file registers it here. /screenshare asks
this module which audio to keep under the video (the remaining part of the
song, cut from the current position); yt/rcplay ask the screenshare
manager to re-play the SAME video with the new audio instead of a plain
audio-only play().
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from config.settings import TEMP_DIR

logger = logging.getLogger(__name__)

_ACTIVE: Dict[int, dict] = {}


def _probe_duration(path: str) -> Optional[float]:
    if not path or path.startswith(("http://", "https://")):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


async def set_audio(chat_id: int, path: str, kind: str) -> None:
    """Register a file/URL that is currently playing in chat_id."""
    dur = await asyncio.to_thread(_probe_duration, path)
    _ACTIVE[int(chat_id)] = {
        "path": path, "kind": kind, "started_at": time.time(), "duration": dur,
    }


def clear_audio(chat_id: int) -> None:
    _ACTIVE.pop(int(chat_id), None)


def clear_all() -> None:
    _ACTIVE.clear()


def _entry(chat_id: int) -> Optional[dict]:
    e = _ACTIVE.get(int(chat_id))
    if not e:
        return None
    dur = e.get("duration")
    if dur and (time.time() - e["started_at"]) >= dur:
        _ACTIVE.pop(int(chat_id), None)  # song finished by itself
        return None
    return e


def has_audio(chat_id: int) -> bool:
    return _entry(chat_id) is not None


def kind(chat_id: int) -> Optional[str]:
    e = _entry(chat_id)
    return e["kind"] if e else None


async def remaining_audio(chat_id: int) -> Optional[str]:
    """Path to play under a video: the rest of the current file, cut from
    the current position. URLs (radio) are returned as-is (live).
    None = nothing playing (use live ExternalMedia audio)."""
    e = _entry(chat_id)
    if not e:
        return None
    path = e["path"]
    if path.startswith(("http://", "https://")) or not os.path.exists(path):
        return path if path.startswith(("http://", "https://")) else None

    elapsed = time.time() - e["started_at"]
    if elapsed < 1.5:
        return path

    ext = os.path.splitext(path)[1] or ".wav"
    out = str(Path(TEMP_DIR) / f"rem_{int(chat_id)}_{int(time.time() * 1000)}{ext}")

    async def _run(cmd) -> bool:
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p.communicate(), timeout=120)
            return p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000
        except Exception:
            return False

    if await _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                   "-ss", f"{elapsed:.2f}", "-i", path, "-c", "copy", out]):
        return out
    out = out.rsplit(".", 1)[0] + ".wav"
    if await _run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                   "-ss", f"{elapsed:.2f}", "-i", path,
                   "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out]):
        return out
    logger.warning("[media_state] cut failed, restarting %s from 0:00", path)
    return path


def stream_type_of(update) -> str:
    """'video' / 'audio' / '' (unknown) — from a pytgcalls stream_end update."""
    st = getattr(update, "stream_type", None)
    if st is None:
        return ""
    s = str(st).lower()
    if "video" in s:
        return "video"
    if "audio" in s:
        return "audio"
    return ""

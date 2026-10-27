"""VC recorder: taps the frames the bridge already receives and encodes them to MP3.

No PyTgCalls calls in here. core.bridge_patch hands us one mixed stereo int16 48 kHz
frame per callback; we pipe them into ffmpeg (libmp3lame).  Real-time timeline is kept:
if the source delivers nothing for a while, silence is inserted so the file length
matches wall-clock time.
"""
import asyncio
import collections
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from config.settings import TEMP_DIR

logger = logging.getLogger(__name__)

RATE = 48000
BYTES_PER_SEC = RATE * 2 * 2          # stereo int16
MIN_SEC, MAX_SEC, DEFAULT_SEC = 5, 3600, 60
_QUEUE_CAP = 3000                     # frames (~60 s) if ffmpeg ever falls behind
_TAP_NAME = "recorder"


class Recording:
    def __init__(self, chat_id: int, duration: int, owner_uid: int, reply_chat: int):
        self.chat_id = int(chat_id)
        self.duration = int(duration)
        self.owner_uid = owner_uid
        self.reply_chat = reply_chat
        self.started = 0.0
        self.frames_in = 0
        self.bytes_written = 0
        self.dropped = 0
        self.path = ""
        self.proc = None
        self.q: collections.deque = collections.deque()
        self.ev = asyncio.Event()
        self.writer: Optional[asyncio.Task] = None
        self.timer: Optional[asyncio.Task] = None
        self.stopping = False
        self.on_done: Optional[Callable[["Recording", Optional[str], str], Awaitable[None]]] = None

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started) if self.started else 0.0

    # called from the bridge's frame handler (same event loop)
    def feed(self, pcm: bytes) -> None:
        if self.stopping:
            return
        self.frames_in += 1
        if len(self.q) >= _QUEUE_CAP:
            self.q.popleft()
            self.dropped += 1
        self.q.append((time.monotonic(), pcm))
        self.ev.set()

    async def _writer(self) -> None:
        stdin = self.proc.stdin
        try:
            while True:
                if not self.q:
                    if self.stopping:
                        return
                    self.ev.clear()
                    try:
                        await asyncio.wait_for(self.ev.wait(), 0.5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                t, pcm = self.q.popleft()
                dur_bytes = len(pcm)
                # keep the timeline: insert silence for any gap the source left
                behind = (t - self.started) - self.bytes_written / BYTES_PER_SEC - dur_bytes / BYTES_PER_SEC
                if behind > 0.10:
                    pad = int(behind * RATE) * 4
                    while pad > 0:
                        n = min(pad, 1 << 20)
                        stdin.write(bytes(n))
                        self.bytes_written += n
                        pad -= n
                stdin.write(pcm)
                self.bytes_written += dur_bytes
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("[REC] ffmpeg pipe closed early (chat=%s)", self.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[REC] writer error: %s", e)


class RecordManager:
    def __init__(self):
        self.active: Dict[int, Recording] = {}
        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)

    def is_recording(self, chat_id: int) -> bool:
        return int(chat_id) in self.active

    def get(self, chat_id: int) -> Optional[Recording]:
        return self.active.get(int(chat_id))

    def status(self) -> list:
        return [
            f"{r.chat_id} · {int(r.elapsed)}s/{r.duration}s"
            for r in self.active.values()
        ]

    async def start(self, chat_id: int, duration: int, owner_uid: int, reply_chat: int,
                    on_done=None) -> Recording:
        from core.bridge_patch import add_tap

        chat_id = int(chat_id)
        if chat_id in self.active:
            raise RuntimeError("already recording")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found")
        duration = max(MIN_SEC, min(MAX_SEC, int(duration)))

        rec = Recording(chat_id, duration, owner_uid, reply_chat)
        rec.on_done = on_done
        rec.path = str(Path(TEMP_DIR) / f"rec_{chat_id}_{int(time.time())}.mp3")
        rec.proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "96k", rec.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        rec.started = time.monotonic()
        rec.writer = asyncio.create_task(rec._writer())
        rec.timer = asyncio.create_task(self._timeout(rec))
        self.active[chat_id] = rec
        add_tap(chat_id, _TAP_NAME, rec.feed)
        logger.info("[REC] start chat=%s %ss -> %s", chat_id, duration, rec.path)
        return rec

    async def _timeout(self, rec: Recording) -> None:
        try:
            await asyncio.sleep(rec.duration)
        except asyncio.CancelledError:
            return
        await self.stop(rec.chat_id, reason="timeout")

    async def stop(self, chat_id: int, reason: str = "manual", deliver: bool = True):
        """Finish the recording. Returns (path or None, info). Calls on_done if set."""
        from core.bridge_patch import remove_tap

        chat_id = int(chat_id)
        rec = self.active.pop(chat_id, None)
        if rec is None:
            return None, "not recording"
        remove_tap(chat_id, _TAP_NAME)
        rec.stopping = True
        rec.ev.set()
        cur = asyncio.current_task()
        if rec.timer and rec.timer is not cur:
            rec.timer.cancel()

        # let the writer drain what is queued, then close the pipe
        try:
            if rec.writer:
                await asyncio.wait_for(rec.writer, 15)
        except Exception:
            if rec.writer:
                rec.writer.cancel()
        try:
            if rec.proc and rec.proc.stdin:
                tail = rec.elapsed - rec.bytes_written / BYTES_PER_SEC   # trailing silence
                if rec.frames_in and tail > 0.10:
                    pad = int(min(tail, MAX_SEC) * RATE) * 4
                    while pad > 0:
                        n = min(pad, 1 << 20)
                        rec.proc.stdin.write(bytes(n))
                        rec.bytes_written += n
                        pad -= n
                    await rec.proc.stdin.drain()
                rec.proc.stdin.close()
            if rec.proc:
                await asyncio.wait_for(rec.proc.wait(), 30)
        except Exception:
            try:
                rec.proc.kill()
            except Exception:
                pass

        secs = rec.bytes_written / BYTES_PER_SEC
        path = rec.path
        ok = os.path.exists(path) and os.path.getsize(path) > 1000 and secs >= 0.5
        info = f"{secs:.0f}s · frames {rec.frames_in} · {reason}"
        if not ok:
            info = f"kuch record nahi hua (frames {rec.frames_in}) · {reason}"
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            path = None
        logger.info("[REC] stop chat=%s %s", chat_id, info)
        if deliver and rec.on_done:
            try:
                await rec.on_done(rec, path, info)
            except Exception as e:
                logger.warning("[REC] on_done failed: %s", e)
        return path, info

    async def stop_all(self, deliver: bool = False) -> int:
        n = 0
        for cid in list(self.active):
            await self.stop(cid, reason="shutdown", deliver=deliver)
            n += 1
        return n


_MANAGER: Optional[RecordManager] = None


def get_record_manager() -> RecordManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = RecordManager()
    return _MANAGER

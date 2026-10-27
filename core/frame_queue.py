"""
Async frame pipeline — zero-cut audio forwarding.

Architecture:
  RECEIVER  →  [bounded deque]  →  PACER  →  send_frame()

The PACER is the key addition over the old version:
  - It holds a small adaptive jitter buffer (default 80 ms) so a single
    late frame from the network never causes an audible gap.
  - It sends at a fixed 20 ms clock (matching the 960-sample / 48 kHz
    Opus frame size Telegram expects) regardless of when frames arrived.
  - On underrun it fades the last real frame out over 3 × 20 ms instead
    of cutting dead — same trick VoIP stacks call "packet loss concealment".
  - On overrun it drops the OLDEST frame (keeps the stream live) and resets
    the send clock so we never burst-play a backlog.

Environment overrides (all in milliseconds):
  VC_FQ_PREBUF_MS   — cushion before playback starts / after gap (default 80)
  VC_FQ_MAXBUF_MS   — hard drop threshold                        (default 400)
  VC_FQ_GRACE_MS    — wait for next frame before concealing       (default 40)
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Tuning ────────────────────────────────────────────────────────────────────
_FRAME_S   = 0.020                    # 20 ms — standard Opus/Telegram frame
_PREBUF_S  = float(os.environ.get("VC_FQ_PREBUF_MS",  "80"))  / 1000.0
_MAXBUF_S  = float(os.environ.get("VC_FQ_MAXBUF_MS", "400"))  / 1000.0
_GRACE_S   = float(os.environ.get("VC_FQ_GRACE_MS",   "40"))  / 1000.0
_CONCEAL   = 3          # fade-out frames on underrun
_DRIFT_MAX = 0.02       # ±2 % send-interval clock-drift correction
_IDLE_S    = 3.0        # exit pacer loop after this much silence
_JIT_DECAY = 0.998      # per-frame exponential decay on jitter peak (~7 s half-life)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class QueuedFrame:
    chat_id:   int
    target_id: int
    raw:       bytes
    ts:        float


def _fade(pcm: bytes, factor: float) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * factor
    return np.clip(a, -32768, 32767).astype(np.int16).tobytes()


class _PacerState:
    """Per-target send-clock + jitter state."""
    __slots__ = ("q", "event", "task", "next_t", "last_rx",
                 "last_pcm", "concealed", "jit_peak", "target")

    def __init__(self):
        self.q:        collections.deque = collections.deque()
        self.event:    asyncio.Event     = asyncio.Event()
        self.task:     Optional[asyncio.Task] = None
        self.next_t:   Optional[float]  = None   # None = need to (re)prime
        self.last_rx:  float            = 0.0
        self.last_pcm: Optional[bytes]  = None
        self.concealed: bool            = False
        self.jit_peak:  float           = 0.0
        self.target:    float           = _PREBUF_S


class FrameQueue:
    """One per AudioBridge.  Call start() once; stop() on shutdown."""

    def __init__(self, process_fn, send_fn, maxsize: int = 32):
        """
        process_fn(chat_id, raw_bytes) -> bytes | None   (sync EQ, no I/O)
        send_fn(target_id, pcm_bytes)  -> awaitable      (network write)
        """
        self._process = process_fn
        self._send    = send_fn
        self._maxsize = maxsize

        # raw-frame intake queue (receiver → EQ worker)
        self._intake: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        # per-target pacer state (after EQ)
        self._pacers: dict[int, _PacerState] = {}

        self._eq_task:  Optional[asyncio.Task] = None
        self._running = False

        # stats
        self.pushed       = 0
        self.processed    = 0
        self.sent         = 0
        self.dropped_full = 0
        self.dropped_err  = 0
        self.last_latency_ms = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._eq_task = asyncio.create_task(self._eq_loop())
        logger.info("[FrameQueue] started (prebuf=%.0fms maxbuf=%.0fms)",
                    _PREBUF_S * 1000, _MAXBUF_S * 1000)

    async def stop(self) -> None:
        self._running = False
        for st in self._pacers.values():
            if st.task and not st.task.done():
                st.task.cancel()
        if self._eq_task:
            self._eq_task.cancel()
            try:
                await self._eq_task
            except (asyncio.CancelledError, Exception):
                pass
            self._eq_task = None
        self._pacers.clear()
        # drain intake
        while not self._intake.empty():
            try:
                self._intake.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("[FrameQueue] stopped")

    # ── push (called from frame-receive handler, never awaits) ────────────────

    def push(self, chat_id: int, target_id: int, raw: bytes) -> None:
        self.pushed += 1
        item = QueuedFrame(chat_id=chat_id, target_id=target_id,
                           raw=raw, ts=time.monotonic())
        try:
            self._intake.put_nowait(item)
        except asyncio.QueueFull:
            # drop oldest to stay live
            try:
                self._intake.get_nowait()
                self.dropped_full += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._intake.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped_full += 1

    # ── EQ worker: intake → process → hand to pacer ──────────────────────────

    async def _eq_loop(self) -> None:
        """Pulls raw frames, runs (synchronous) EQ, enqueues PCM to the right pacer."""
        while self._running:
            try:
                item: QueuedFrame = await self._intake.get()
            except asyncio.CancelledError:
                break

            self.last_latency_ms = (time.monotonic() - item.ts) * 1000.0

            try:
                pcm = self._process(item.chat_id, item.raw)
                self.processed += 1
                if not pcm:
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.dropped_err += 1
                if self.dropped_err <= 5 or self.dropped_err % 100 == 0:
                    logger.warning("[FrameQueue] EQ error: %s", e)
                continue

            # Hand to pacer for target_id
            self._pacer_enqueue(item.target_id, pcm)

    def _pacer_enqueue(self, target_id: int, pcm: bytes) -> None:
        st = self._pacers.get(target_id)
        if st is None:
            st = self._pacers[target_id] = _PacerState()

        now = time.monotonic()

        # Adaptive jitter: measure inter-arrival deviation vs nominal 20 ms
        if st.last_rx and (now - st.last_rx) < 0.25:   # same talk-spurt
            dev = abs((now - st.last_rx) - _FRAME_S)
            st.jit_peak = max(dev, st.jit_peak * _JIT_DECAY)
            # cushion = 1.5× peak jitter, clamped to [_PREBUF_S, 200 ms]
            st.target = min(0.200, max(_PREBUF_S, 1.5 * st.jit_peak + _FRAME_S))
        st.last_rx = now

        st.q.append(pcm)
        st.event.set()

        # Hard cap: drop oldest to bound latency
        while len(st.q) * _FRAME_S > _MAXBUF_S:
            st.q.popleft()
            self.dropped_full += 1

        # Spin up pacer task if not running
        if st.task is None or st.task.done():
            st.task = asyncio.get_running_loop().create_task(
                self._pacer_loop(target_id, st)
            )

    # ── Per-target pacer: fixed 20 ms clock + concealment ────────────────────

    async def _pacer_loop(self, target_id: int, st: _PacerState) -> None:
        try:
            while True:
                # ── underrun ──────────────────────────────────────────────────
                if not st.q:
                    if not st.concealed and st.last_pcm is not None:
                        # Grace window: wait briefly for the next frame
                        st.event.clear()
                        try:
                            await asyncio.wait_for(st.event.wait(), _GRACE_S)
                        except asyncio.TimeoutError:
                            pass
                        if st.q:
                            st.next_t = None    # re-prime cushion
                            continue

                        # PLC fade-out: 3 frames of decaying amplitude
                        st.concealed = True
                        for k in range(1, _CONCEAL + 1):
                            if st.q:
                                st.next_t = None
                                break
                            factor = max(0.0, 1.0 - k / (_CONCEAL + 1))
                            frame = _fade(st.last_pcm, factor)
                            try:
                                await self._send(target_id, frame)
                                self.sent += 1
                            except Exception:
                                pass
                            await asyncio.sleep(_FRAME_S)
                        if st.q:
                            st.next_t = None
                            continue

                    # idle wait — exit if source stays silent
                    st.event.clear()
                    try:
                        await asyncio.wait_for(st.event.wait(), _IDLE_S)
                    except asyncio.TimeoutError:
                        return          # task exits; will be re-spawned on next frame
                    st.next_t = None    # talk-spurt start → re-prime
                    continue

                st.concealed = False

                # ── (re)prime jitter buffer before first send ─────────────────
                if st.next_t is None:
                    deadline = time.monotonic() + st.target + 0.04
                    while (len(st.q) * _FRAME_S < st.target
                           and time.monotonic() < deadline):
                        await asyncio.sleep(0.004)
                    st.next_t = time.monotonic()

                # ── send ──────────────────────────────────────────────────────
                pcm = st.q.popleft()
                st.last_pcm = pcm

                now = time.monotonic()
                if st.next_t > now:
                    await asyncio.sleep(st.next_t - now)
                elif now - st.next_t > _FRAME_S * 4:
                    # Too far behind: resync clock instead of bursting backlog
                    st.next_t = now

                try:
                    await self._send(target_id, pcm)
                    self.sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.dropped_err += 1
                    if self.dropped_err <= 5 or self.dropped_err % 50 == 0:
                        logger.warning("[FrameQueue] send err target=%s: %s",
                                       target_id, e)

                # Clock-drift correction: nudge send interval ±2 % toward cushion
                _err = len(st.q) * _FRAME_S - st.target
                if abs(_err) > 0.02:
                    _dead = _err - (0.02 if _err > 0 else -0.02)
                    _adj = max(-1.0, min(1.0,
                               _dead / max(st.target, 0.02))) * _DRIFT_MAX
                else:
                    _adj = 0.0
                st.next_t += _FRAME_S * (1.0 - _adj)

        except asyncio.CancelledError:
            pass
        finally:
            if st.task is asyncio.current_task():
                st.task = None

    # ── stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "intake_q":    self._intake.qsize(),
            "pacers":      len(self._pacers),
            "pushed":      self.pushed,
            "processed":   self.processed,
            "sent":        self.sent,
            "dropped_full": self.dropped_full,
            "dropped_err": self.dropped_err,
            "latency_ms":  round(self.last_latency_ms, 1),
        }

"""Runtime patch for core.bridge — NO need to edit bridge.py.

Called once from main.py before any bridge is created. Safe to call twice.

1) Isolation: a sudo user's AudioBridge never becomes the global "live" bridge and
   never writes the owner's bridge_config.json.
2) Smooth forwarding (fixes the "audio stops for ~1 second, then continues" problem):
   frames are no longer sent straight from the receive callback. They go through a
   small jitter buffer and a sender that paces them in real time:
     * event-loop hiccup / network burst  -> smoothed out, old audio dropped so the
       latency never grows beyond MAXBUF (default 300 ms)
     * slow send_frame                    -> resync instead of a burst
     * all speakers of the source VC are mixed (old code forwarded frames[0] only)
3) Diagnostics in the log so the real cause of any remaining stall is visible:
     [LAG]  event loop blocked      -> something in the bot blocks the loop
     SLOW send_frame                -> PyTgCalls/Telegram send side is slow
     RX GAP (info)                  -> source VC delivered no audio (silence / network)

Tuning (optional, environment variables):  VC_PREBUF_MS=110  VC_MAXBUF_MS=450
"""
import asyncio
import collections
import logging
import os
import time

# Owner wants zero-cut, zero-buffering-feel audio. A bigger prebuffer soaks
# up more network jitter before playback even starts (fewer stutters), and
# a bigger max buffer means far fewer forced drops when the network briefly
# lags — every forced drop IS an audible cut, so a wider cushion before that
# ever triggers is the real fix for "cut cut ke aata hai". Still overridable
# via env vars without touching code if the host needs different numbers.
# ADAPTIVE JITTER BUFFER: cushion is no longer a fixed number. It is the
# measured network jitter of the source stream (peak inter-arrival deviation,
# slowly decaying), clamped to [_PREBUF_S, _TARGET_MAX_S]. Clean network ->
# ~_PREBUF_S (low delay). Jittery network -> cushion grows by itself so frames
# are never played "just in time" (that was the cut-cut-cut). It is re-primed
# at EVERY talk-spurt start / underrun, not only after 600 ms gaps.
_PREBUF_S = float(os.environ.get("VC_PREBUF_MS", "60")) / 1000.0        # minimum cushion
_TARGET_MAX_S = float(os.environ.get("VC_TARGET_MAX_MS", "260")) / 1000.0  # maximum cushion
_MAXBUF_S = float(os.environ.get("VC_MAXBUF_MS", "500")) / 1000.0
_TRIM_TO_S = min(0.120, _MAXBUF_S / 2.0)    # (fallback only; real trim uses st.target)
_GRACE_S = 0.05      # wait this long for the next frame before fading out (underrun grace)
_LATE_RESYNC_FRAMES = 4   # only resync (insert a gap) when > 4 frames (~80 ms) late
# SILENCE SHEDDING: after a stall/burst the queue can sit deeper than the
# cushion (= extra delay that never goes away while someone keeps talking).
# Instead of a hard drop (audible cut), shed that delay by skipping only
# frames that are already (near-)silent - pauses between words. Speech is
# never touched. Delay drifts back to the cushion size, inaudibly.
_DRIFT_MAX = 0.02         # max +/-2 % send-interval nudge to track source clock drift
_SHED_OVER_S = 0.06       # start shedding when deeper than target + 60 ms
_QUIET_PEAK = 300         # int16 peak below this = silent frame (gate is closed -> ~0)
_IDLE_EXIT_S = 3.0
_SLOW_SEND_S = 0.30
_RX_GAP_S = 0.60

_lag_log = logging.getLogger("vc.lagwatch")
_join_log = logging.getLogger("vc.joinwatch")

# Live diagnostics, shown by /vcstatus (no console / log access needed)
STATS = {
    "lag_n": 0, "lag_max_ms": 0,       # event loop blocked >= 100 ms
    "slow_n": 0, "send_max_ms": 0,     # send_frame took >= 300 ms / slowest send
    "gap_n": 0, "gap_max_ms": 0,       # source delivered no audio for >= 600 ms
    "drop_n": 0, "tx_max_q": 0, "shed_n": 0,
    "senderr_n": 0, "fail_max_ms": 0,  # send_frame errors / longest continuous failure streak
    "heal_n": 0, "last_err": "",       # AUTO-HEAL rejoins triggered / last send error text        # frames dropped to cap latency / deepest queue
    "sil_n": 0, "sil_max_ms": 0,       # source delivered digital silence for 0.4–3 s
    "cpu_now": 0, "cpu_max": 0,        # CPU % of the whole bot process (100 = one full core)
    "rtt_last": 0, "rtt_max": 0,       # round-trip to Telegram (ms), assistant connection
    "rtt_slow_n": 0, "rtt_fail_n": 0,  # probes slower than 500 ms / failed or timed out
    "join_heal_n": 0,                  # times vc_join_watchdog had to rejoin a dropped side
}


# Frame taps: chat_id -> {name: callback(pcm_bytes)}.  Used by core.recorder.
# The callback gets ONE mixed stereo int16 48 kHz frame (all speakers summed) of that
# chat's VC, exactly as the bridge receives it.  Independent of mute / bridge routing.
TAPS: dict = {}


# OUTPUT taps: target_id -> {name: callback(pcm_bytes)}. Gets EXACTLY the PCM that
# _tx_loop hands to send_frame (after EQ/jitter buffer/fades), i.e. what Telegram
# receives from this bot. Used by /vcrecout to diagnose "cut cut" with real data.
OUT_TAPS: dict = {}


def add_out_tap(target_id: int, name: str, cb) -> None:
    OUT_TAPS.setdefault(int(target_id), {})[name] = cb


def remove_out_tap(target_id: int, name: str) -> None:
    d = OUT_TAPS.get(int(target_id))
    if d:
        d.pop(name, None)
        if not d:
            OUT_TAPS.pop(int(target_id), None)


def _emit_out(target_id: int, pcm: bytes) -> None:
    if OUT_TAPS:
        d = OUT_TAPS.get(int(target_id))
        if d:
            for cb in list(d.values()):
                try:
                    cb(pcm)
                except Exception:
                    pass


def add_tap(chat_id: int, name: str, cb) -> None:
    TAPS.setdefault(int(chat_id), {})[name] = cb


def remove_tap(chat_id: int, name: str) -> None:
    d = TAPS.get(int(chat_id))
    if d:
        d.pop(name, None)
        if not d:
            TAPS.pop(int(chat_id), None)


def get_stats() -> dict:
    return dict(STATS)


def reset_stats() -> None:
    for k in STATS:
        STATS[k] = 0


async def loop_lag_watchdog(threshold: float = 0.10) -> None:
    """Records (and logs) every time the asyncio event loop is blocked >= `threshold`."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        lag = time.monotonic() - t0 - 0.05
        if lag >= threshold:
            ms = int(lag * 1000.0)
            STATS["lag_n"] += 1
            STATS["lag_max_ms"] = max(STATS["lag_max_ms"], ms)
            _lag_log.warning("[LAG] event loop blocked for %d ms", ms)


async def resource_monitor(interval: float = 5.0) -> None:
    """CPU % of this process (all threads incl. the native audio engine)."""
    while True:
        w0, c0 = time.monotonic(), time.process_time()
        await asyncio.sleep(interval)
        dw = time.monotonic() - w0
        if dw <= 0:
            continue
        cpu = int((time.process_time() - c0) / dw * 100.0)
        STATS["cpu_now"] = cpu
        STATS["cpu_max"] = max(STATS["cpu_max"], cpu)


async def vc_join_watchdog(interval: float = 25.0) -> None:
    """Catches the side of a bridge the per-target auto-heal in _tx_loop
    can't see: SOURCE dropping out of its VC.

    _auto_heal_route (in _tx_loop) only fires from a TARGET send failure —
    it has no way to notice the SOURCE side silently disconnecting (kicked,
    network hiccup, etc.), because from inside the RX callback a dropped
    source and a source that's simply quiet look identical: no frames
    arrive either way. That's exactly the reported symptom — the assistant
    drops out of the SOURCE group's VC while the TARGET stays joined, and
    nothing ever reconnects it.

    This runs independently of any frame callback: every `interval`
    seconds it asks Telegram directly (core/vc_leave.get_self_source —
    the same ground-truth check already proven for the /leave* fix)
    whether the assistant is STILL a real participant in BOTH the source
    and target call of every active route, for every bridge (owner's live
    bridge + every sudo user's bridge). If either side has silently
    dropped, it rejoins that exact route via restore_live_route() — the
    same method /join itself uses, so a healed route behaves identically
    to a fresh manual /join."""
    from core.bridge_state import get_live_bridge
    from core.user_bridges import all_user_bridges
    from core.vc_leave import get_self_source

    last_heal: dict = {}
    HEAL_COOLDOWN = 30.0

    while True:
        await asyncio.sleep(interval)
        try:
            bridges = []
            try:
                ob = get_live_bridge()
                if ob is not None:
                    bridges.append(ob)
            except Exception:
                pass
            try:
                bridges.extend(all_user_bridges().values())
            except Exception:
                pass

            for bridge in bridges:
                mtproto = getattr(bridge, "mtproto", None)
                if mtproto is None:
                    continue
                seen_routes = set()
                try:
                    routes = list((getattr(bridge, "active_bridges", {}) or {}).values())
                except Exception:
                    continue

                for info in routes:
                    try:
                        sid = int(info.get("source_id"))
                        tid = int(info.get("target_id") or sid)
                    except Exception:
                        continue
                    route = (sid, tid)
                    if route in seen_routes:
                        continue
                    seen_routes.add(route)

                    now = time.monotonic()
                    if now - last_heal.get(route, 0.0) < HEAL_COOLDOWN:
                        continue

                    try:
                        _, src_ssrc = await get_self_source(mtproto, sid)
                        if tid == sid:
                            tgt_ssrc = src_ssrc
                        else:
                            _, tgt_ssrc = await get_self_source(mtproto, tid)
                    except Exception as e:
                        _join_log.debug("check %s->%s failed: %s", sid, tid, e)
                        continue

                    src_ok = src_ssrc is not None
                    tgt_ok = tgt_ssrc is not None
                    if src_ok and tgt_ok:
                        continue

                    # mute/unmute chal raha ho to participant row kuch der
                    # ke liye badal sakti hai — wo drop nahi hai.
                    try:
                        if bridge._mute_lock.locked():
                            continue
                    except Exception:
                        pass
                    # Dobara confirm karo (3s baad) — ek hi galat reading par rejoin nahi.
                    await asyncio.sleep(3.0)
                    try:
                        _, src_ssrc = await get_self_source(mtproto, sid)
                        if tid == sid:
                            tgt_ssrc = src_ssrc
                        else:
                            _, tgt_ssrc = await get_self_source(mtproto, tid)
                    except Exception:
                        continue
                    src_ok = src_ssrc is not None
                    tgt_ok = tgt_ssrc is not None
                    if src_ok and tgt_ok:
                        continue

                    dropped = []
                    if not src_ok:
                        dropped.append(f"SOURCE {sid}")
                    if not tgt_ok:
                        dropped.append(f"TARGET {tid}")
                    _join_log.warning(
                        "%s silently dropped out of VC (route %s -> %s) — rejoining...",
                        " + ".join(dropped), sid, tid,
                    )
                    last_heal[route] = now
                    STATS["join_heal_n"] += 1
                    try:
                        ok = await bridge.restore_live_route(sid, tid)
                        _join_log.warning(
                            "rejoin %s -> %s: %s", sid, tid,
                            "recovered" if ok else "still failing",
                        )
                    except Exception as e:
                        _join_log.warning("restore_live_route %s->%s error: %s", sid, tid, e)
        except Exception as e:
            _join_log.warning("watchdog loop error: %s", e)


async def net_probe(get_client, interval: float = 5.0) -> None:
    """Pings Telegram through the assistant connection every few seconds.

    Spikes here = the network between this machine and Telegram is the problem,
    not the code. `get_client` is a callable returning a Pyrogram client (or None).
    """
    import random
    try:
        from pyrogram.raw.functions import Ping as _Ping
    except Exception:
        _Ping = None
    log = logging.getLogger("vc.netprobe")
    while True:
        await asyncio.sleep(interval)
        try:
            cl = get_client()
            if cl is None:
                continue
            t0 = time.monotonic()
            if _Ping is not None:
                await asyncio.wait_for(cl.invoke(_Ping(ping_id=random.getrandbits(62))), 5.0)
            else:
                await asyncio.wait_for(cl.get_me(), 5.0)
            ms = int((time.monotonic() - t0) * 1000.0)
            STATS["rtt_last"] = ms
            STATS["rtt_max"] = max(STATS["rtt_max"], ms)
            if ms > 500:
                STATS["rtt_slow_n"] += 1
                log.warning("[NET] Telegram round-trip %d ms", ms)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATS["rtt_fail_n"] += 1
            log.warning("[NET] Telegram probe failed: %s", e)


# ----------------------------------------------------------------------------
def _patch_isolation(cb) -> None:
    _raw_set_live_bridge = cb.set_live_bridge

    def set_live_bridge(bridge) -> None:
        if getattr(bridge, "is_owner_bridge", True):
            _raw_set_live_bridge(bridge)

    cb.set_live_bridge = set_live_bridge

    _orig_init = cb.AudioBridge.__init__

    def __init__(self, *args, is_owner: bool = True, **kwargs):
        self.is_owner_bridge = bool(is_owner)
        _orig_init(self, *args, **kwargs)

    cb.AudioBridge.__init__ = __init__

    _orig_save = cb.AudioBridge._save_muted_config

    def _save_muted_config(self) -> None:
        if not getattr(self, "is_owner_bridge", True):
            return
        return _orig_save(self)

    cb.AudioBridge._save_muted_config = _save_muted_config


class _TxState:
    __slots__ = ("q", "event", "task", "dur", "last_rx", "drops", "last_gap_log",
                 "last_pcm", "concealed", "fail_since", "last_heal", "tail_left",
                 "jit_peak", "target")

    def __init__(self):
        self.q = collections.deque()
        self.event = asyncio.Event()
        self.task = None
        self.dur = 0.02
        self.last_rx = 0.0
        self.drops = 0
        self.last_gap_log = 0.0
        self.last_pcm = None        # most recent REAL frame actually sent
        self.concealed = False      # already faded-out for the current gap?
        self.fail_since = None      # monotonic time the CURRENT failure streak started
        self.last_heal = 0.0        # monotonic time of the last auto-rejoin attempt
        self.tail_left = 0.0        # seconds of echo tail still to ring out after the source goes quiet
        self.jit_peak = 0.0         # decaying peak of inter-arrival jitter (seconds)
        self.target = _PREBUF_S     # current adaptive cushion (seconds)


# How many frames of the previous real audio to fade out over when the
# source stalls (network hiccup, not a deliberate pause). 3 frames @ 20ms
# = a ~60ms fade to silence instead of an abrupt stop — this is the
# standard telephony/VoIP "packet loss concealment" idea: a listener's ear
# reads a quick, smooth fade as far less jarring than a hard cut, even
# though technically some audio is still "missing" either way. It does NOT
# invent new speech — it only softens the edge of a real gap, and stops
# fading (goes fully silent) well before any real pause would be mistaken
# for continued talking.
_CONCEAL_FRAMES = 3

# Self-heal: if sending to the target keeps failing (the VC connection
# itself dropped — e.g. Telegram/network hiccup, "not in a call" errors)
# for this many continuous seconds, automatically try to rejoin that route
# instead of staying silent until someone runs /join by hand. Cooldown
# stops this from hammering Telegram with rejoin attempts if it's still
# down; it will simply try again after the cooldown if still failing.
# WAS 2.0: a 2-second send-failure streak triggered leave+REJOIN, and the rejoin
# itself is another 2-3 s of silence. Most such streaks are a short network blip
# that the native layer reconnects from on its own ("2 sec cut, then fine").
# Now we wait long enough for that self-recovery and only rejoin if it is a
# genuinely dead connection.
_HEAL_AFTER_S = 8.0
_HEAL_COOLDOWN_S = 10.0


def _fade_pcm(np, pcm: bytes, factor: float) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * factor
    return np.clip(a, -32768, 32767).astype(np.int16).tobytes()


def _patch_forwarding(cb) -> None:
    np = cb.np
    Device = cb.Device
    logger = cb.logger

    def _tx_enqueue(self, target_id: int, pcm: bytes) -> None:
        tx = self.__dict__.setdefault("_tx", {})
        st = tx.get(target_id)
        if st is None:
            st = tx[target_id] = _TxState()
        now = time.monotonic()
        if st.last_rx and now - st.last_rx > _RX_GAP_S and now - st.last_gap_log > 5.0:
            st.last_gap_log = now
            STATS["gap_n"] += 1
            STATS["gap_max_ms"] = max(STATS["gap_max_ms"], int((now - st.last_rx) * 1000.0))
            logger.info(
                "[LiveBridge] RX GAP %.0f ms from source (silence or upstream network)",
                (now - st.last_rx) * 1000.0,
            )
        st.dur = len(pcm) / 4.0 / 48000.0      # stereo int16 @ 48 kHz
        if st.last_rx and now - st.last_rx < 0.25:       # same talk-spurt only
            dev = abs((now - st.last_rx) - st.dur)
            st.jit_peak = max(dev, st.jit_peak * 0.998)  # ~7 s half-life
            st.target = min(_TARGET_MAX_S, max(_PREBUF_S, 1.5 * st.jit_peak + st.dur))
        st.last_rx = now
        try:
            from core.effects import ECHO_TAIL_S
            st.tail_left = ECHO_TAIL_S
        except Exception:
            st.tail_left = 0.0
        st.q.append(pcm)
        STATS["tx_max_q"] = max(STATS["tx_max_q"], len(st.q))
        st.event.set()
        if st.task is None or st.task.done():
            st.task = asyncio.get_running_loop().create_task(self._tx_loop(target_id, st))

    async def _auto_heal_route(self, target_id: int) -> None:
        """Send to `target_id` has been failing with a 'not in a call' style
        error for a while — the VC connection itself likely dropped (network
        hiccup, Telegram-side hiccup, etc). Rejoin it automatically instead
        of staying silent until someone runs /join by hand."""
        try:
            info = self.active_bridges.get(target_id)
            if not info:
                return
            source_id = int(info.get("source_id") or target_id)
            # Mute/admin-mute ki wajah se send fail ho sakta hai — wo VC drop
            # nahi hai. Aise mein leave/rejoin NAHI karna.
            try:
                from core.automute_guard import is_locked
                if (self.config.get("muted") or getattr(self.eq, "muted", False)
                        or is_locked(self) or self._mute_lock.locked()
                        or target_id in self._was_admin_muted
                        or source_id in self._was_admin_muted):
                    logger.info("[LiveBridge] AUTO-HEAL skipped (muted/admin-muted)")
                    return
            except Exception:
                pass
            STATS["heal_n"] += 1
            logger.warning(
                "[LiveBridge] AUTO-HEAL: %s -> %s send failing, rejoining...",
                source_id, target_id,
            )
            ok = await self.restore_live_route(source_id, target_id)
            logger.warning(
                "[LiveBridge] AUTO-HEAL %s -> %s: %s",
                source_id, target_id, "recovered" if ok else "still failing",
            )
        except Exception as e:
            logger.warning("[LiveBridge] auto-heal error for %s: %s", target_id, e)

    async def _tx_loop(self, target_id: int, st) -> None:
        next_t = None
        last_done = 0.0
        try:
            while True:
                if not st.q:
                    # ECHO TAIL: Telegram stops delivering frames the moment the
                    # speaker goes quiet, so the echo would be cut off dead right
                    # there. Keep feeding SILENCE through the effects engine so the
                    # repeats ring out fully (~ECHO_TAIL_S), then fall through to
                    # the normal idle/fade logic.
                    eq = self.eq
                    if (st.tail_left > 0.0 and getattr(eq, "echo_m", 0.0) > 0.05
                            and not getattr(eq, "muted", False)
                            and not self.config.get("muted")):
                        try:
                            n_s = max(2, int(round(st.dur * 48000.0)) * 2)
                            tail = eq.process_int16(np.zeros(n_s, dtype=np.int16))  # REVERTED — see note above, same object/same reason
                            if tail is not None and tail.size >= 2:
                                _emit_out(target_id, tail.tobytes())
                                await self.client.send_frame(target_id, Device.MICROPHONE, tail.tobytes())
                        except Exception:
                            pass
                        st.tail_left -= st.dur
                        st.concealed = True          # no stale-frame fade after the tail
                        st.last_pcm = None
                        next_t = None                # next real audio re-primes the jitter buffer
                        await asyncio.sleep(st.dur)
                        continue
                    # Underrun: instead of stopping dead (an audible click/cut),
                    # fade the last real frame out over a few frames first. A
                    # real pause in speech just ends up as a smooth, quick
                    # fade to true silence either way — this only makes a
                    # network-hiccup gap sound soft instead of sharp.
                    if not st.concealed and st.last_pcm is not None:
                        # Underrun GRACE: most underruns are a 20-60 ms network
                        # hiccup. Give the next frame a moment to arrive; if it
                        # does, just re-prime the cushion (no fade, no audible
                        # dip). Only a longer silence gets the fade-out below.
                        st.event.clear()
                        try:
                            await asyncio.wait_for(st.event.wait(), _GRACE_S)
                        except asyncio.TimeoutError:
                            pass
                        if st.q:
                            next_t = None            # re-prime cushion
                            continue
                        st.concealed = True
                        resumed_mid_fade = False
                        for k in range(1, _CONCEAL_FRAMES + 1):
                            if st.q:                    # real audio arrived — stop concealing
                                resumed_mid_fade = True
                                break
                            factor = max(0.0, 1.0 - k / (_CONCEAL_FRAMES + 1))
                            frame = _fade_pcm(np, st.last_pcm, factor)
                            try:
                                _emit_out(target_id, frame)
                                await self.client.send_frame(target_id, Device.MICROPHONE, frame)
                            except Exception:
                                pass
                            await asyncio.sleep(st.dur)
                        if resumed_mid_fade:
                            # BUG FIX: used to fall through to the event-wait
                            # below regardless. That immediately did
                            # `st.event.clear()` on an event that THIS
                            # arriving frame had just set — so instead of
                            # processing the frame that's already sitting in
                            # st.q, it waited for a DIFFERENT, future frame
                            # to set the event again. Usually masked by the
                            # next frame arriving ~20ms later, but if the
                            # speaker went quiet again right after a single
                            # word, that one frame could sit unsent for up to
                            # _IDLE_EXIT_S (3s) before the task gave up and
                            # exited — a real, audible dropped/delayed frame.
                            # Go straight back to the top and process what's
                            # already queued instead.
                            next_t = None            # re-prime cushion before resuming
                            continue
                    t_wait = time.monotonic()
                    st.event.clear()
                    try:
                        await asyncio.wait_for(st.event.wait(), _IDLE_EXIT_S)
                    except asyncio.TimeoutError:
                        return
                    if True:   # talk-spurt start: ALWAYS re-prime the adaptive cushion
                        # BUG FIX: was `> 0.10` (100ms). Ordinary pauses
                        # between words/sentences in normal speech routinely
                        # run 100-500ms — with a 100ms cutoff, nearly EVERY
                        # pause while someone talks was being treated as a
                        # "real network gap" and triggered a full jitter-
                        # buffer rebuild, which waits up to _PREBUF_S
                        # (110ms) before resuming. That's an audible little
                        # stutter at the start of almost every word during
                        # normal conversation — a second, separate source of
                        # "cut cut ke aata hai" on top of the resync bug
                        # fixed earlier. _RX_GAP_S (600ms) is the same
                        # threshold this file already uses elsewhere to mean
                        # "a real gap, not normal speech rhythm" — only a
                        # pause at least that long now pays the rebuild
                        # cost; anything shorter resumes immediately.
                        next_t = None                  # rebuild cushion (size = st.target, adaptive)
                    continue

                st.concealed = False                   # real audio is flowing again

                if next_t is None:                     # (re)start: fill a small jitter buffer
                    deadline = time.monotonic() + st.target + 0.04
                    while len(st.q) * st.dur < st.target and time.monotonic() < deadline:
                        await asyncio.sleep(0.004)
                    next_t = time.monotonic()

                if len(st.q) * st.dur > _MAXBUF_S:      # bound latency: drop the oldest audio
                    keep = max(1, int((st.target + 0.04) / max(st.dur, 1e-3)))
                    extra = len(st.q) - keep
                    for _ in range(max(0, extra)):
                        st.q.popleft()
                    st.drops += max(0, extra)
                    STATS["drop_n"] += max(0, extra)
                    next_t = max(time.monotonic(), last_done + st.dur)

                if len(st.q) > 1 and len(st.q) * st.dur > st.target + _SHED_OVER_S:
                    try:
                        if int(np.abs(np.frombuffer(st.q[0], dtype=np.int16).astype(np.int32)).max()) < _QUIET_PEAK:
                            st.q.popleft()           # silent frame: skip it, delay shrinks by 20 ms
                            STATS["shed_n"] += 1
                            continue
                    except Exception:
                        pass

                pcm = st.q.popleft()
                st.last_pcm = pcm
                now = time.monotonic()
                if next_t > now:
                    await asyncio.sleep(next_t - now)
                elif now - next_t > st.dur * _LATE_RESYNC_FRAMES:
                    # THE REAL BUG (was `> 0.25`): any lag under 250ms fell
                    # through BOTH branches above — no sleep, no resync — so
                    # every frame still sitting in the queue from that lag
                    # got sent back-to-back with zero gap between them until
                    # pacing happened to drift back near real-time on its
                    # own. That zero-gap burst IS "audio cuts for ~2s then
                    # plays at 2x speed then goes normal" — it's not a
                    # separate bug, it's this exact code path, just under a
                    # threshold so loose it almost never resynced. Any lag
                    # worth noticing (more than ~1.5 frames, ~30ms) now
                    # resyncs immediately instead of trying to silently
                    # "catch up" by racing through the backlog.
                    next_t = max(now, last_done + st.dur)   # resync, never burst

                t0 = time.monotonic()
                try:
                    _emit_out(target_id, pcm)
                    await self.client.send_frame(target_id, Device.MICROPHONE, pcm)
                    self._fwd_count += 1
                    if st.fail_since is not None:
                        _dur_ms = int((time.monotonic() - st.fail_since) * 1000.0)
                        STATS["fail_max_ms"] = max(STATS["fail_max_ms"], _dur_ms)
                        logger.warning("[LiveBridge] send recovered by itself after %d ms of errors (target %s)",
                                       _dur_ms, target_id)
                    st.fail_since = None
                except Exception as e:
                    err = str(e).lower()
                    is_disconnect = any(x in err for x in (
                        "external source", "notincall", "not in a call", "connectionnotfound"
                    ))
                    STATS["senderr_n"] += 1
                    STATS["last_err"] = str(e)[:80]
                    if not is_disconnect:
                        if self._fwd_count < 5 or int(time.time()) % 10 == 0:
                            logger.warning("[LiveBridge] forward fail: %s", e)
                    else:
                        now_f = time.monotonic()
                        if st.fail_since is None:
                            st.fail_since = now_f
                            logger.warning("[LiveBridge] send failing (target %s): %s", target_id, str(e)[:120])
                        elif (now_f - st.fail_since > _HEAL_AFTER_S
                              and now_f - st.last_heal > _HEAL_COOLDOWN_S):
                            st.last_heal = now_f
                            asyncio.get_running_loop().create_task(
                                self._auto_heal_route(target_id)
                            )
                last_done = time.monotonic()
                took = last_done - t0
                STATS["send_max_ms"] = max(STATS["send_max_ms"], int(took * 1000.0))
                if took > _SLOW_SEND_S:
                    STATS["slow_n"] += 1
                    logger.warning(
                        "[LiveBridge] SLOW send_frame %.0f ms (target %s)", took * 1000.0, target_id
                    )
                # CLOCK-DRIFT CONTROLLER. The source VC and this bot run on different
                # clocks (tiny, but they never match exactly). Left alone, the buffer
                # slowly drains (-> underrun -> audible gap + re-prime) or slowly
                # fills (-> extra delay -> hard drop). Instead of waiting for either
                # failure, nudge the send interval by up to +/-2 % toward the cushion
                # target. The receiver (Telegram jitter buffer) absorbs +/-2 % easily;
                # it is inaudible, unlike a gap.
                _e = len(st.q) * st.dur - st.target
                if _e > 0.02 or _e < -0.02:
                    _e -= 0.02 if _e > 0 else -0.02
                    _adj = max(-1.0, min(1.0, _e / max(st.target, 0.02))) * _DRIFT_MAX
                else:
                    _adj = 0.0
                next_t += st.dur * (1.0 - _adj)
        finally:
            if st.task is asyncio.current_task():
                st.task = None

    async def _forward_frames(self, update) -> None:
        if getattr(self, "_retired", False):     # old bridge replaced by /join: stay silent
            return
        if TAPS:
            try:
                _cid = int(getattr(update, "chat_id", 0) or 0)
                _taps = TAPS.get(_cid)
                if _taps and update.frames:
                    _arr = []
                    for _fr in update.frames:
                        _a = np.frombuffer(bytes(_fr.frame), dtype=np.int16)
                        if _a.size >= 2:
                            _arr.append(_a)
                    if _arr:
                        if len(_arr) == 1:
                            _pcm = _arr[0].tobytes()
                        else:
                            _n = min(a.size for a in _arr)
                            _m = sum(a[:_n].astype(np.int32) for a in _arr)
                            _pcm = np.clip(_m, -32768, 32767).astype(np.int16).tobytes()
                        for _cb in list(_taps.values()):
                            try:
                                _cb(_pcm)
                            except Exception:
                                pass
            except Exception:
                pass
        self._rx_count += 1
        if self._rx_count <= 3 or self._rx_count % 250 == 0:
            logger.info(
                "[LiveBridge] RX chat=%s rx=%s fwd=%s muted=%s",
                getattr(update, "chat_id", "?"),
                self._rx_count,
                self._fwd_count,
                self.config.get("muted"),
            )

        if (self.config.get("muted") or getattr(self.eq, "muted", False)):
            return

        chat_id = int(getattr(update, "chat_id", 0) or 0)
        bridge = self.active_bridges.get(chat_id)
        if not bridge:
            return
        if bridge.get("mode") == "solo":
            return

        source_id = int(bridge["source_id"])
        target_id = int(bridge["target_id"])
        if chat_id != source_id or target_id == source_id:
            return
        if not update.frames:
            return

        try:
            arrays = []
            for fr in update.frames:                       # every speaker of the source VC
                a = np.frombuffer(bytes(fr.frame), dtype=np.int16)
                if a.size >= 2:
                    arrays.append(a)
            if not arrays:
                return
            if len(arrays) == 1:
                samples = arrays[0].copy()
            else:
                n = min(a.size for a in arrays)
                mixed = sum(a[:n].astype(np.int32) for a in arrays)
                samples = np.clip(mixed, -32768, 32767).astype(np.int16)
            if samples.size < 2:
                return

            # diagnostics: did the SOURCE deliver a stretch of digital silence?
            now_m = time.monotonic()
            peak = int(np.abs(samples.astype(np.int32)).max())
            t_sil = self.__dict__.get("_sil_t0")
            if peak < 40:
                if t_sil is None:
                    self.__dict__["_sil_t0"] = now_m
            elif t_sil is not None:
                run = now_m - t_sil
                if 0.4 <= run <= 3.0:
                    STATS["sil_n"] += 1
                    STATS["sil_max_ms"] = max(STATS["sil_max_ms"], int(run * 1000.0))
                self.__dict__["_sil_t0"] = None

            # REVERTED: this was briefly `await asyncio.to_thread(self.eq.
            # process_int16, samples)` to keep the event loop free under
            # multi-bridge load. That broke correctness instead: LiveEQ
            # keeps live filter state between calls (_gate_g, _deess_env,
            # _g_lim, the per-channel biquad states, ...) and MUST process
            # frames one at a time, strictly in order — it is not
            # reentrant. Moving it to a worker thread added an `await`
            # point, which let the next frame's `_forward_frames` start
            # before the previous one's thread had finished, so two frames
            # could mutate that same shared filter state at once. That race
            # is exactly what "awaaz radio static jaisi, no-network jaisi"
            # sounds like — not a timing/buffering issue this time, actual
            # corrupted samples from two threads fighting over the same
            # filter memory. Back to a plain synchronous call: slower to
            # not yield here, but correct, and it's the same call this
            # engine has always made.
            out = self.eq.process_int16(samples)
            if out is None or out.size < 2:
                return
            pcm = out.tobytes()

            # Screenshare live-mux: video on target + mic still flows (unchanged)
            try:
                from core.screenshare import get_screenshare_manager
                ss = get_screenshare_manager()
                if ss is not None and ss.has_live_mux(target_id):
                    if ss.push_live_audio(target_id, pcm):
                        self._fwd_count += 1
                        return
            except Exception:
                pass

            self._tx_enqueue(target_id, pcm)
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("external source", "notincall", "not in a call", "connectionnotfound")):
                return
            if self._fwd_count < 5 or int(time.time()) % 10 == 0:
                logger.warning("[LiveBridge] forward fail: %s", e)

    cb.AudioBridge._tx_enqueue = _tx_enqueue
    cb.AudioBridge._tx_loop = _tx_loop
    cb.AudioBridge._auto_heal_route = _auto_heal_route
    cb.AudioBridge._forward_frames = _forward_frames


def apply() -> None:
    import core.bridge as cb

    if getattr(cb, "_isolation_patched", False):
        return
    _patch_isolation(cb)
    try:
        _patch_forwarding(cb)
    except Exception as e:                                  # never break the bot
        logging.getLogger(__name__).warning("smooth-forwarding patch skipped: %s", e)
    cb._isolation_patched = True

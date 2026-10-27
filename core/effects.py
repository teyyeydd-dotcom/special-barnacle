"""
Audio engine — loud, clear and stable VC bridge sound.
UI scale: gain/loudness/bass/treble/clarity/echo/pitch = 0–100

Chain (per frame, all filter state is carried between frames = no clicks):
  DC/rumble block -> bass shelf -> treble -> presence -> pre-gain
  -> (echo) -> compressor -> lookahead limiter (never clips)
Stereo aware (interleaved L,R from PyTgCalls), per-channel filter state.

Clarity patch: single dynamics pass, slower compressor release (80ms),
tuned defaults — speech stays loud AND intelligible.
"""
import math
from typing import Dict, Tuple

import numpy as np

_BASS_KERNEL = np.ones(24, dtype=np.float32) / 24.0  # kept for compatibility

# 0–100 UI ranges
RANGES: Dict[str, Tuple[float, float]] = {
    "gain":     (0, 99999),   # uncapped — /gain 300, /gain 5000, anything goes
    "loudness": (0, 99999),   # uncapped — /loudness 200, /loudness 500, anything goes
    "bass":     (0, 100),
    "treble":   (0, 100),
    "clarity":  (0, 100),
    "echo":     (0, 100),
    "pitch":    (0, 100),
    "swap":     (0, 1),       # 1 = left/right channels swapped
}

# Clarity-tuned defaults — loud enough to cut through VC but consonants stay
# sharp and every word is intelligible. gain 130 + loudness 82 still hits
# hard; bass 65 keeps body without smearing the 250-500Hz mud band (where
# the recording was peaking at 30.5% energy); treble 85 + clarity 92 push
# the presence shelf so "k/t/s/p" land cleanly. echo/pitch = 0 as before.
DEFAULT_EFFECTS = {
    "gain": 220,        # max loudness priority (VC-fight use case)
    "loudness": 140,    # already saturates the caps below — physically as
                         # loud as this engine can go, regardless of bass
    "bass": 18,          # was 72 — baby confirmed: NOT heavy/boomy, wants
                         # loud+sharp, not loud+thick. bass_db/warm_db/exc
                         # all scale off this one slider, so dropping it
                         # removes the "bhaari" low-end character. Loudness
                         # itself is driven by gain+loudness only (separate
                         # formula below), so this does NOT make it quieter —
                         # it just frees up the low-mids that bass/warm were
                         # filling, so treble/clarity/exciter cut through
                         # cleaner = sounds loud AND sharp, not loud and muddy
    "treble": 100,       # was 92 — maxed, this is what makes it sit "on top"
    "clarity": 100,      # already maxed
    "echo": 75,
    "pitch": 0,
    "swap": 0,
    "muted": False,
    "_v": 14,            # bumped: forces upgrade (max gain/loudness + soft-knee)
}
CONFIG_VERSION = 14

# ---- crystal-clear voice tuning (edit these to taste) ----------------------
# The old chain stacked ~137 dB pre-gain + ~175 dB makeup + drive ~117, so EVERY
# sample (even mic noise in silence) was slammed to full scale = a square wave.
# CLEAN_MODE keeps it LOUD but bounded: speech is levelled by the compressor and
# only the peaks are rounded by the limiter. Set CLEAN_MODE = False for the old
# "infinite clip" behaviour.
CLEAN_MODE = True
# baby ne confirm kiya: abhi clarity second priority hai, MAX LOUDNESS
# first priority hai (VC fight me doosre bande se awaaz dabni nahi chahiye).
# These are the caps that actually matter — `_dsp_params()` is the real
# live path (LiveEQ.configure calls this), not `ui_to_eq()` which turned
# out to be dead code nowhere actually called. Pushed hard here:
PRE_DB_CAP = 120.0      # was 85 (orig 80) — pushed well past original.
                        # The compressor/limiter are clean (no saturation
                        # happens here, DRIVE below is the only saturation
                        # stage and it's deliberately kept low), so a bigger
                        # pre-gain doesn't add distortion — it just guarantees
                        # the chain is slamming the ceiling even for a quiet
                        # mic/quiet talker, so nobody's voice ever sits below
                        # anyone else's in the VC.
RATIO_CAP = 30.0        # was 24 (orig 28) — now past the original, safe to
                        # push this high because the two things that used to
                        # make high ratios sound bad are already fixed:
                        # attack_ms is no longer sub-millisecond (see
                        # _dsp_params) and COMP_KNEE below is wide, so gain
                        # reduction eases in instead of snapping.
THR_CLEAN = 0.012       # was 0.016 (orig 0.014) — lower than original now,
                        # grabs essentially the full signal for max density.
                        # Safe because DRIVE is low and attack is no longer
                        # instant (those were the real distortion sources,
                        # not the threshold itself).
MAKEUP_DB_CAP = 48.0    # was 42 (orig 48) — back to the original ceiling.
                        # The limiter clamps every peak to CEIL regardless of
                        # how much makeup arrives, so this adds consistency/
                        # loudness on quieter input without adding its own
                        # distortion (that was never what caused the garble).
DRIVE_CAP = 2.0         # UNCHANGED from the clarity pass — this stays low on
                        # purpose. This was the #1 proven cause of "samajh
                        # nahi aata": tanh saturation at 6-14x turns speech
                        # waveforms into near-square waves. Loudness doesn't
                        # need this (the limiter already guarantees max
                        # level) so it is NOT being raised even though
                        # everything else above is being pushed harder —
                        # that's what keeps "max loud" and "zero complaints
                        # about clarity" both true at once.
COMP_KNEE = 0.70        # was 0.65 — widened again since ratio/threshold went
                        # up further, keeps the now-very-dense compression
                        # blending smoothly instead of snapping/pumping.
GATE_THR = 0.006        # was 0.004 — slightly more selective about what
                        # counts as "voice" vs faint background rustle, now
                        # that the floor below is true silence it matters
                        # more that the gate only opens for real speech.
GATE_HOLD_MS = 260
GATE_FLOOR = 0.0         # was 0.0008 — THE ACTUAL NOISE BUG: the gate runs
                        # on the raw mic signal BEFORE `vol` (pre-gain) is
                        # applied. 0.0008 was "basically silent" back when
                        # pre-gain was modest, but with PRE_DB_CAP pushed
                        # this high, that same 0.0008 "floor" gets multiplied
                        # by the pre-gain right along with real speech and
                        # comes back out as a clearly audible hiss between
                        # words — the gate was never actually gating once
                        # loudness got pushed hard. True 0.0 fixes this
                        # completely: between words the output is real
                        # digital silence, no matter how high the gain goes.
                        # No "chop" artifact either — the fade into/out of
                        # that silence is already smoothed by GATE_RELEASE /
                        # a_open below, a nonzero floor was never what made
                        # it sound soft, the smoothing coefficient is.
GATE_RELEASE = 0.025

# ---- exciter / presence (new) ----------------------------------------------
# Separate from `clarity` (3.5kHz band) — this is a dedicated harmonic
# exciter + de-ess pair so consonants ("s","t","k") stay sharp and crystal
# instead of getting buried once gain/loudness are pushed hard.
EXCITER_HZ = 2800.0     # ear-sensitivity sweet spot (Fletcher-Munson) —
                        # sits "on top" of a bass-heavy voice at same raw dB
EXCITER_AMT = 0.35      # was 0.55 (orig 0.80) — this is its own tanh
                        # saturation stage sitting right on 2.8kHz, exactly
                        # the band speech consonants live in — at high
                        # amounts it was adding distortion in the one band
                        # that most determines "can I understand the words".
                        # Clean brightness now comes mainly from clarity_db /
                        # treble_db below (pure EQ, no saturation) instead.
DEESS_HZ = 7200.0
DEESS_THR = 0.20        # was 0.18 — exciter is driven even less now, so
                        # there's less added sibilance to catch; de-esser
                        # only needs to grab genuine harsh spikes, not shave
                        # consonants on every word (that was eating clarity).
DEESS_AMT = 0.45        # was 0.55 — matches the relaxed threshold above

# ---- echo tuning -----------------------------------------------------------
ECHO_DELAY_MS = 280     # gap between repeats
ECHO_HP_HZ = 220.0      # back to 220 — keeps repeats out of the vocal mud band
ECHO_DUCK = 0.72        # was 0.40 — THIS was the other intelligibility killer.
                        # At 0.40 the previous word's repeat was still nearly
                        # full volume by the time the NEXT word started, so
                        # words literally overlapped each other ("gunjta hai
                        # 3-4 baar, samajh nahi aata" = exactly this). 0.72
                        # ducks the repeats hard while you're actively
                        # speaking, so the LIVE word stays clean and the
                        # echo is only really heard in the gaps between words.
ECHO_TAIL_S = 2.2       # back to 2.2 — shorter ring-out so repeats finish
                        # before the next sentence starts


def ui_to_eq(cfg: Dict) -> Dict:
    g = float(cfg.get("gain", 0) or 0)
    loud = float(cfg.get("loudness", 0) or 0)
    bass = float(cfg.get("bass", 0) or 0)
    treble = float(cfg.get("treble", 0) or 0)
    clarity = float(cfg.get("clarity", 0) or 0)
    echo = float(cfg.get("echo", 0) or 0)
    pitch = float(cfg.get("pitch", 0) or 0)
    return {
        # level/gain/treble/clarity ranges widened so the loudness knob has
        # more real headroom to push before the limiter/curve flattens it out
        "level": 25.0 + (max(0.0, min(100.0, loud)) / 100.0) * 55.0,   # was 20+40
        "gain": (max(0.0, min(100.0, g)) / 100.0) * 22.0,              # was 15
        "bass": (max(0.0, min(100.0, bass)) / 100.0) * 25.0,
        "treble": (max(0.0, min(100.0, treble)) / 100.0) * 35.0,
        "clarity": (max(0.0, min(100.0, clarity)) / 100.0) * 20.0,     # was 15
        "echo": max(0.0, min(100.0, echo)),
        "pitch": max(0.0, min(100.0, pitch)),
        "muted": bool(cfg.get("muted", False)),
    }


def _dsp_params(cfg: Dict) -> Dict:
    """Single source of truth: UI 0-100 (gain 0-150) -> DSP values (used by LiveEQ and ffmpeg).

    Compressor/makeup/drive now scale on t = (loudness/100)**2 (quadratic,
    not linear) instead of loudness directly. This was ported from the
    "James Fighter" browser mic extension (uploaded reference) after
    comparing its actual Web Audio graph node-by-node against this chain:
    James keeps its compressor threshold LOW (-40..-35dB) and ratio HIGH
    (12-20:1) almost regardless of its own "loudness" knob, and gets most
    of its extra intensity from a much bigger makeup gain (up to ~18-20dB)
    and much heavier tanh saturation (its waveshaper curve is the exact
    same tanh(k*x)/tanh(k) formula this chain's `drive` already uses — just
    with k reaching ~12 instead of this chain's old max of 1.5). The
    quadratic curve reproduces that "stays close to normal until pushed
    near the top, then gets extreme" feel, while t=0 still collapses every
    one of these to their original values — so gentler presets (fight,
    voice) that sit at moderate loudness are barely affected, and only
    loudness=90-100 (bambam/james) reaches the new, much more aggressive
    ceiling. The limiter (CEIL=0.97, unchanged) still guarantees zero
    clipping at any setting — James had no limiter of its own and had to
    self-limit its gain; porting into this chain's protected pipeline
    means the same dense/loud/saturated character comes through without
    the harsh digital clipping James itself was prone to."""
    cfg = cfg or {}

    # NO UPPER CLAMP — only floor at 0 (negative gain/loudness is meaningless).
    # Type /gain 5000, /loudness 9999, whatever — it all feeds straight into
    # the formulas below with no ceiling. bass/treble/clarity/echo/pitch stay
    # unclamped too.
    def f(key: str) -> float:
        return max(0.0, float(cfg.get(key, 0) or 0))

    # Raw pre-gain (vol) and the compressor's ratio/threshold/makeup ALL keep
    # climbing with no cap — the only thing that stays fixed is the final
    # lookahead limiter's ceiling (CEIL in LiveEQ / alimiter in ffmpeg). That
    # is not a volume limit — it's the point past which "more gain" stops
    # meaning "louder" and starts meaning "digital clipping / garbled noise".
    # Everything before that stage is uncapped, so pushing gain/loudness
    # higher always drives the compressor harder (quiet parts squashed up to
    # the loud parts = higher perceived loudness) right up until the engine
    # is basically slamming the limiter 100% of the time — the loudest this
    # chain can physically go without turning into noise.
    g = f("gain")
    loud = f("loudness")
    bass, treble, clarity = f("bass"), f("treble"), f("clarity")
    pre_db = g / 100.0 * 80.0 + loud / 100.0 * 40.0

    # t drives the compressor/makeup (the part that actually controls final
    # loudness). It now responds to BOTH /gain and /loudness together (not
    # just loudness) — so turning either knob up makes it louder — and uses
    # sqrt growth instead of the old quadratic. Quadratic hit its 100%-brick
    # wall (every block slammed to the limiter ceiling) by around
    # loudness~150-200 — past that, raising the number further produced a
    # BIT-IDENTICAL output, because the limiter was already pinned flat out
    # the whole time. sqrt stretches that runway out much further so a much
    # wider range of numbers each audibly change the result before hitting
    # that same, unavoidable, floor-of-physics ceiling.
    drive_level = (g + loud) / 100.0
    t = drive_level ** 0.5   # uncapped — keeps climbing forever, just slower

    # _safe_db only exists so `10 ** (db/20)` below can't raise a Python
    # OverflowError and crash the process on a truly absurd input (like
    # /gain 999999999999). It is NOT an audible ceiling: 5000 dB is a
    # multiplier of 10**250 — the limiter is already pinned at 100% duty
    # cycle thousands of dB before this point, so nothing above this number
    # is audibly different anyway, it would just be a Python crash instead
    # of "loud".
    def _safe_db(db: float) -> float:
        return db if db < 5000.0 else 5000.0

    if CLEAN_MODE:
        # knobs still go "more = louder", but along a bounded, distortion-free curve
        pre_db = PRE_DB_CAP * (1.0 - math.exp(-pre_db / PRE_DB_CAP / 3.0)) if pre_db > 0 else 0.0
        ratio = min(RATIO_CAP, 3.0 + 150.0 * t)
        comp_thr = THR_CLEAN
        makeup_db = min(MAKEUP_DB_CAP, 120.0 * t)
        drive = min(DRIVE_CAP, 1.0 + 80.0 * t)
    else:
        ratio = 3.0 + 150.0 * t
        comp_thr = max(1e-6, 0.20 * math.exp(-0.75 * t))
        makeup_db = _safe_db(120.0 * t)
        drive = 1.0 + 80.0 * t

    return {
        "dry": g <= 0 and loud <= 0 and bass <= 0 and treble <= 0 and clarity <= 0,
        "vol": 10 ** (_safe_db(pre_db) / 20.0),
        "bass_db": bass / 100.0 * 22.0,
        "treble_db": treble / 100.0 * 16.0,
        "clarity_db": clarity / 100.0 * 24.0,  # was 20 (orig 14) — this is a
                                                 # clean parametric EQ boost
                                                 # (no saturation), so pushing
                                                 # it higher adds real
                                                 # "understand the words"
                                                 # presence without the
                                                 # distortion side-effects
                                                 # that DRIVE/EXCITER had.
        "attack_ms": 3.0 + 5.0 * bass / 100.0,      # was 0.3+0.8*bass/100 — at
                                                 # bass=0 (the fyter preset)
                                                 # that gave a fixed 0.3ms
                                                 # attack: the compressor was
                                                 # reacting inside a single
                                                 # pitch cycle of normal
                                                 # speech, literally squashing
                                                 # the front of every
                                                 # consonant ("p","t","k","s")
                                                 # instantly — a second real
                                                 # cause of "samajh nahi
                                                 # aata" on top of DRIVE. 3ms
                                                 # floor lets those transients
                                                 # punch through intact while
                                                 # still being fast enough to
                                                 # catch sustained loud parts
                                                 # for loudness control.
        "warm_db": bass / 100.0 * 13.0,               # was 24 — this 120-520Hz "chest" boost
                                                       # was the 3rd intelligibility killer:
                                                       # 38% of the recording's whole energy was
                                                       # sitting in this exact band, masking the
                                                       # short, quiet consonant transients that
                                                       # actually carry the words. Still gives
                                                       # body/warmth, just not enough to bury speech.
        "exc": 0.70 * bass / 100.0,                   # was 1.40 — halved, same reason: less
                                                       # extra low-order harmonic buildup in the mud band
        "ratio": ratio,
        "comp_thr": comp_thr,
        "makeup_db": makeup_db,
        "drive": drive,
        "echo": f("echo"),
        "pitch": f("pitch"),
        "muted": bool(cfg.get("muted", False)),
    }


class _OnePole:
    """Stateful one-pole low-pass, fully vectorised (no scipy needed).

    y[n] = a*y[n-1] + (1-a)*x[n]  computed with the cumsum trick in safe
    sub-blocks, so state carries across frames and there are no edge clicks.
    """

    def __init__(self, fc: float, fs: float):
        self.a = math.exp(-2.0 * math.pi * fc / fs)
        self.y = 0.0
        lam = -math.log(self.a)
        self.S = int(max(16, min(8192, 200.0 / lam)))
        k = np.arange(self.S, dtype=np.float64)
        self.apow = self.a ** k
        self.ainv = self.a ** (-k)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        out = np.empty(n, dtype=np.float64)
        a = self.a
        y0 = self.y
        i = 0
        while i < n:
            m = min(self.S, n - i)
            seg = x[i:i + m]
            y = self.apow[:m] * (a * y0 + (1.0 - a) * np.cumsum(seg * self.ainv[:m]))
            out[i:i + m] = y
            y0 = float(y[-1])
            i += m
        self.y = y0
        return out


class _PitchShifter:
    """Streaming pitch shifter (two-tap crossfading delay line).

    Unlike the old per-frame resampler it never drops audio, its state is carried
    between frames (exact regardless of chunk size) and it works on all channels.
    ratio < 1 = deeper / heavier voice.  Extra latency: ~W/2 samples (~20 ms).
    """

    def __init__(self, channels: int, window: int = 1920):
        self.W = int(window)
        self.H = self.W + 2
        self.hist = np.zeros((self.H, channels), dtype=np.float64)
        self.phase = 0.0

    def feed(self, x: np.ndarray) -> None:
        """Keep history fresh while pitch is off (no glitch when it is switched on)."""
        if x.shape[0] >= self.H:
            self.hist = x[-self.H:].copy()
        else:
            self.hist = np.concatenate((self.hist, x), axis=0)[-self.H:]

    def process(self, x: np.ndarray, ratio: float) -> np.ndarray:
        n = x.shape[0]
        W, H = self.W, self.H
        ext = np.concatenate((self.hist, x), axis=0)
        i = np.arange(n)
        d1 = (self.phase + i * (1.0 - ratio)) % W
        d2 = (d1 + W / 2.0) % W
        out = np.zeros_like(x)
        last = ext.shape[0] - 1
        for d in (d1, d2):
            pos = (H + i) - d
            p0 = np.floor(pos).astype(np.int64)
            fr = (pos - p0)[:, None]
            a = ext[np.clip(p0, 0, last)]
            b = ext[np.clip(p0 + 1, 0, last)]
            w = (np.sin(np.pi * d / W) ** 2)[:, None]
            out += w * (a * (1.0 - fr) + b * fr)
        self.phase = (self.phase + n * (1.0 - ratio)) % W
        self.hist = ext[-H:].copy()
        return out


class LiveEQ:
    """Live VC processing: clean EQ + compressor + lookahead limiter.

    Stereo aware: frames from PyTgCalls are interleaved int16 (L,R,L,R...).
    Every channel has its own filter state; dynamics are linked across channels
    so the stereo image never shifts.
    """

    BLOCK = 48      # 1 ms @ 48 kHz — dynamics resolution
    CEIL = 0.9995   # 1 LSB from digital clip — absolute physical ceiling

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self.fs = sample_rate
        self.channels = max(1, int(channels))
        self.muted = False
        self.vol = 1.0
        self.bass_g = 1.0
        self.treble_g = 1.0
        self.clarity_g = 1.0
        self.echo_m = 0.0
        self.pitch_f = 1.0
        self.comp_ratio = 4.0
        self.comp_thr = 0.35
        self.makeup = 1.0
        self.drive = 1.0
        self.warm_g = 1.0
        self.exc = 0.0
        self._dry = True
        self.swap = False
        self._ps = _PitchShifter(self.channels)
        # feedback echo: ring buffer holding (input + fb * delayed) per channel
        self._echo_D = max(64, int(self.fs * ECHO_DELAY_MS / 1000.0))
        self._echo_buf = np.zeros((self._echo_D, self.channels), dtype=np.float64)
        self._echo_pos = 0
        self.echo_wet = 0.0
        self.echo_fb = 0.0
        self._echo_hp = [_OnePole(ECHO_HP_HZ, float(sample_rate)) for _ in range(self.channels)]
        self._echo_duck_env = 0.0
        self._g_lim_echo = 1.0
        # noise gate state
        self._gate_g = 1.0
        self._gate_hold = 0
        self._gc_prev = 1.0      # last compressor gain  (carried so frame joins have NO step/click)
        self._duck_prev = 1.0
        self._gate_prev = 1.0

        fs = float(sample_rate)
        self._chains = [
            {
                "dc": _OnePole(30.0, fs),   # minimal rumble cut — let ALL bass through
                "b1": _OnePole(55.0, fs),
                "b2": _OnePole(55.0, fs),
                "hi": _OnePole(5500.0, fs),
                "ph": _OnePole(5500.0, fs),
                "pl": _OnePole(800.0, fs),
                "wl": _OnePole(520.0, fs),   # was 420 — sits inside Telegram VC's real passband
                "wm": _OnePole(120.0, fs),   # was 75  — 75Hz was below where Telegram VC even
                                             # carries energy, so that "body" band was doing
                                             # almost nothing audible before
                "ex": _OnePole(160.0, fs),
                # new: harmonic exciter band + de-esser band (crystal clarity stage)
                "exh": _OnePole(EXCITER_HZ, fs),
                "des": _OnePole(DEESS_HZ, fs),
            }
            for _ in range(self.channels)
        ]
        self._deess_env = [0.0 for _ in range(self.channels)]

        B = self.BLOCK
        self._att_c = math.exp(-B / (fs * 0.005))   # compressor attack 5ms
        self._rel_c = math.exp(-B / (fs * 0.080))   # compressor release 80ms — breathing room for consonants
        self._rel_l = math.exp(-B / (fs * 0.006))   # limiter release 6ms
        self._env = 0.0
        self._g_lim = 1.0

        self.configure(dict(DEFAULT_EFFECTS))

    def configure(self, cfg: Dict):
        cfg = cfg or {}
        p = _dsp_params(cfg)
        self.muted = p["muted"]
        self.echo_m = min(1.0, p["echo"] / 100.0)
        # echo 70 -> repeats at ~0.69, 0.35, 0.18, 0.09 (2-3 clearly audible)
        self.echo_wet = 0.30 + 0.55 * self.echo_m
        self.echo_fb = 0.30 + 0.30 * self.echo_m
        self.pitch_f = 2 ** ((p["pitch"] / 100.0) * 0.7)
        self.swap = bool(float(cfg.get("swap", 0) or 0) >= 1)
        if self.echo_m <= 0.05:
            self._echo_buf.fill(0)

        self._dry = p["dry"]
        if self._dry:
            self.vol = 1.0
            self.bass_g = 1.0
            self.treble_g = 1.0
            self.clarity_g = 1.0
            self.warm_g = 1.0
            self.exc = 0.0
            return

        self.vol = p["vol"]
        self.bass_g = 10 ** (p["bass_db"] / 20.0)
        self.treble_g = 10 ** (p["treble_db"] / 20.0)
        self.clarity_g = 10 ** (p["clarity_db"] / 20.0)
        self.warm_g = 10 ** (p["warm_db"] / 20.0)
        self.exc = p["exc"]
        self._att_c = math.exp(-self.BLOCK / (float(self.fs) * p["attack_ms"] / 1000.0))
        self.comp_ratio = p["ratio"]
        self.comp_thr = p["comp_thr"]
        self.makeup = 10 ** (p["makeup_db"] / 20.0)
        self.drive = p["drive"]

    # ------------------------------------------------------------------
    def _block_peaks(self, a: np.ndarray, nb: int, pad: int) -> np.ndarray:
        ap = np.abs(a)
        if pad:
            ap = np.concatenate((ap, np.zeros((pad, ap.shape[1]))), axis=0)
        return ap.reshape(nb, self.BLOCK, ap.shape[1]).max(axis=(1, 2))

    def _dynamics(self, y: np.ndarray) -> np.ndarray:
        """Compressor (density) + lookahead limiter (guaranteed no clipping).

        y has shape (n, channels); gain is linked across channels.
        """
        n = y.shape[0]
        B = self.BLOCK
        nb = (n + B - 1) // B
        pad = nb * B - n
        idx = np.arange(n)

        # ---- compressor: smoothed block-peak level -> gain (soft-knee) ----
        peak = self._block_peaks(y, nb, pad)
        thr = self.comp_thr
        knee = thr * COMP_KNEE          # soft-knee half-width around threshold
        inv_r = 1.0 / self.comp_ratio
        env = self._env
        att, rel = self._att_c, self._rel_c
        gc = np.ones(nb)
        for i in range(nb):
            p = float(peak[i])
            c = att if p > env else rel
            env = c * env + (1.0 - c) * p
            # Soft knee: below (thr-knee) untouched, above (thr+knee) full
            # ratio, in between a smooth quadratic blend — no sudden "grab".
            # At THR_CLEAN this aggressive, the old hard-knee version was
            # snapping gain reduction on/off every block = audible pumping.
            lo, hi = thr - knee, thr + knee
            if env <= lo:
                pass  # untouched, gc[i] stays 1.0
            elif env >= hi:
                gc[i] = (thr + (env - thr) * inv_r) / env
            else:
                # quadratic knee interpolation (standard soft-knee formula)
                x = (env - lo) / (2.0 * knee) if knee > 1e-9 else 1.0
                over = x * x * (env - lo)
                reduced = env - over * (1.0 - inv_r)
                gc[i] = reduced / env
        self._env = env
        y = y * self._smooth_gain(gc, n, self._gc_prev)[:, None]
        self._gc_prev = float(gc[-1])

        # ---- makeup + smooth tanh saturation (loud & dense, not harsh) ----
        y = y * self.makeup
        d = self.drive
        y = np.tanh(y * d) * (1.0 / math.tanh(d))

        # ---- limiter: 1-block lookahead, linear gain ramps ----
        y, self._g_lim = self._limit(y, self._g_lim)
        return np.clip(y, -0.99, 0.99)

    def _smooth_gain(self, gains: np.ndarray, n: int, prev: float) -> np.ndarray:
        """Per-block gains -> per-sample curve that STARTS at the previous frame's
        last gain (np.interp alone clamps to the first block = a step at every
        frame edge = audible click/crackle/'cut')."""
        B = self.BLOCK
        xs = np.concatenate(([-0.5 * B], (np.arange(len(gains)) + 0.5) * B))
        ys = np.concatenate(([prev], gains))
        return np.interp(np.arange(n), xs, ys)

    def _limit(self, y: np.ndarray, g: float):
        """1-block lookahead limiter. Returns (limited y, final gain state)."""
        n = y.shape[0]
        B = self.BLOCK
        nb = (n + B - 1) // B
        pad = nb * B - n
        idx = np.arange(n)
        p2 = self._block_peaks(y, nb, pad)
        req = np.minimum(1.0, self.CEIL / np.maximum(p2, 1e-9))
        la = req.copy()
        if nb > 1:
            la[:-1] = np.minimum(req[:-1], req[1:])
        rel_l = self._rel_l
        g = min(g, float(req[0]))
        G = np.empty(nb + 1)
        G[0] = g
        for i in range(nb):
            rec = 1.0 - (1.0 - g) * rel_l
            g = min(float(la[i]), rec)
            G[i + 1] = g
        y = y * np.interp(idx, np.arange(nb + 1) * B, G)[:, None]
        return y, g

    def _noise_gate(self, x: np.ndarray) -> np.ndarray:
        """Soft noise gate with hold: background hiss is NOT boosted by the
        huge makeup gain, and word endings are never chopped."""
        n = x.shape[0]
        B = self.BLOCK
        nb = (n + B - 1) // B
        pad = nb * B - n
        peak = self._block_peaks(x, nb, pad)
        hold_blocks = int(GATE_HOLD_MS)            # BLOCK = 1 ms
        g, hold = self._gate_g, self._gate_hold
        a_open, a_close = 0.6, GATE_RELEASE         # fast open, slow smooth close (per ms)
        gains = np.empty(nb)
        for i in range(nb):
            if peak[i] > GATE_THR:
                hold = hold_blocks
                target = 1.0
            elif hold > 0:
                hold -= 1
                target = 1.0
            else:
                target = GATE_FLOOR
            g += (target - g) * (a_open if target > g else a_close)
            gains[i] = g
        prev = self._gate_prev
        self._gate_g, self._gate_hold, self._gate_prev = g, hold, float(gains[-1])
        return x * self._smooth_gain(gains, n, prev)[:, None]

    def _echo(self, x: np.ndarray) -> np.ndarray:
        """Feedback echo: speak once -> voice repeats 2-3 times, decaying.
        Runs AFTER the loud dynamics so repeats stay clean (not re-saturated),
        are high-passed (no mud) and duck while you talk (voice stays crystal clear)."""
        n, C = x.shape
        D = self._echo_D
        fb = self.echo_fb
        taps = np.empty_like(x)
        i = 0
        while i < n:
            m = min(D, n - i)
            idx = (self._echo_pos + np.arange(m)) % D
            dl = self._echo_buf[idx]
            taps[i:i + m] = dl
            self._echo_buf[idx] = x[i:i + m] + fb * dl
            self._echo_pos = (self._echo_pos + m) % D
            i += m
        for c in range(C):
            t = taps[:, c]
            taps[:, c] = t - self._echo_hp[c].process(t)      # high-pass the repeats
        # duck repeats under live voice
        B = self.BLOCK
        nb = (n + B - 1) // B
        pad = nb * B - n
        pk = self._block_peaks(x, nb, pad)
        env = self._echo_duck_env
        att, rel = 0.5, 0.012
        duck = np.empty(nb)
        for k in range(nb):
            env += (pk[k] - env) * (att if pk[k] > env else rel)
            duck[k] = 1.0 - ECHO_DUCK * min(1.0, env * 1.5)
        self._echo_duck_env = env
        duck_s = self._smooth_gain(duck, n, self._duck_prev)[:, None]
        self._duck_prev = float(duck[-1])
        return x + self.echo_wet * taps * duck_s

    def process_int16(self, pcm: np.ndarray) -> np.ndarray:
        if self.muted or pcm is None or pcm.size == 0:
            return np.zeros(0 if pcm is None else pcm.shape, dtype=np.int16)

        # Almost dry = zero processing lag
        if self._dry and self.echo_m <= 0.05 and self.pitch_f <= 1.04:
            if self.swap and self.channels == 2 and pcm.size >= 2:
                sw = pcm.astype(np.int16, copy=True).reshape(-1, 2)[:, ::-1]
                return np.ascontiguousarray(sw).reshape(pcm.shape)
            return pcm.astype(np.int16, copy=False)

        shape = pcm.shape
        flat = pcm.reshape(-1)
        C = self.channels
        nfr = flat.size // C
        if nfr == 0:
            return pcm.astype(np.int16, copy=False)
        used = nfr * C

        # interleaved (L,R,L,R...) -> (frames, channels)
        x = flat[:used].astype(np.float64).reshape(nfr, C) * (1.0 / 32768.0)
        x = np.nan_to_num(x)

        if self.pitch_f > 1.04:
            x = self._ps.process(x, 1.0 / self.pitch_f)   # deeper / heavier voice
        else:
            self._ps.feed(x)

        if not self._dry:
            x = self._noise_gate(x)
            for c in range(C):
                ch = self._chains[c]
                xc = x[:, c]
                xc = xc - ch["dc"].process(xc)                       # DC / rumble
                low = ch["b2"].process(ch["b1"].process(xc))          # bass band
                high = xc - ch["hi"].process(xc)                      # treble band
                band = ch["ph"].process(xc) - ch["pl"].process(xc)    # presence
                warm = ch["wl"].process(xc) - ch["wm"].process(xc)    # chest / body
                lowb = (self.bass_g - 1.0) * low
                if self.exc > 0.0:                                    # bass harmonics
                    h = np.tanh(8.0 * lowb)
                    exc = self.exc * (h - ch["ex"].process(h))
                else:
                    exc = 0.0

                # ---- harmonic exciter: adds crisp upper-mid "air" so
                # consonants stay defined even once loudness is pushed hard
                # (separate from the `clarity`/presence band above) ----
                hi_band = xc - ch["exh"].process(xc)
                air = EXCITER_AMT * np.tanh(3.0 * hi_band) * self.clarity_g

                x[:, c] = (
                    xc
                    + lowb
                    + (self.treble_g - 1.0) * high
                    + (self.clarity_g - 1.0) * band
                    + (self.warm_g - 1.0) * warm
                    + exc
                    + air
                ) * self.vol

                # ---- de-esser: once the whole chain is this loud, raw
                # sibilance (s/sh/t) can spike and sound harsh — duck just
                # that narrow band, not the whole voice ----
                sib = x[:, c] - ch["des"].process(x[:, c])
                sib_peak = np.abs(sib)
                # PERF: same maths as before, bit-for-bit, but without the
                # per-sample min()/max()/numpy-scalar overhead (this loop was
                # ~70% of the whole per-frame CPU cost -> event-loop lag ->
                # buffering on small hosts). tolist() gives plain floats.
                env = float(self._deess_env[c])
                _thr = DEESS_THR
                _inv = 1.0 / DEESS_THR
                _amt = DEESS_AMT
                _pk = sib_peak.tolist()
                _red = [1.0] * len(_pk)
                for i, p in enumerate(_pk):
                    if p > env:
                        env = 0.5 * env + 0.5 * p
                    else:
                        env = 0.9 * env + 0.1 * p
                    if env > _thr:
                        r = (env - _thr) * _inv
                        _red[i] = 1.0 - _amt * (r if r < 1.0 else 1.0)
                reduced = np.asarray(_red)
                self._deess_env[c] = env
                x[:, c] = x[:, c] - sib * (1.0 - reduced)

        if not self._dry:
            x = self._dynamics(x)   # single pass — clarity over brick-wall density
        else:
            x = np.clip(x, -0.98, 0.98)

        if self.echo_m > 0.05:
            x = self._echo(x)
            x, self._g_lim_echo = self._limit(x, self._g_lim_echo)   # echo-sum safety limiter
            x = np.clip(x, -0.99, 0.99)

        if self.swap and C == 2:
            x = x[:, ::-1]                          # stereo channel swap (L<->R)
        out = np.rint(np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).reshape(-1)
        if used != flat.size:                       # unaligned tail: pass through
            out = np.concatenate((out, flat[used:].astype(np.int16)))
        return out.reshape(shape)


class AudioEffects:
    @staticmethod
    def get_default_effects() -> Dict:
        return dict(DEFAULT_EFFECTS)

    @staticmethod
    def is_legacy_config(cfg: Dict) -> bool:
        if not cfg:
            return False
        if "level" in cfg and "loudness" not in cfg:
            return True
        # gain > 200 is now valid — no legacy flag
        if int(cfg.get("_v", 0) or 0) < CONFIG_VERSION:
            return True     # saved before the max-loud default -> upgrade
        return False

    @staticmethod
    def merge_config(stored: Dict) -> Dict:
        defaults = AudioEffects.get_default_effects()
        if not stored or AudioEffects.is_legacy_config(stored):
            return defaults
        out = dict(defaults)
        # merge ALL keys from stored (not just default keys) so user-set
        # gain=300 / loudness=200 survive across restarts
        for key, val in stored.items():
            if key != "_v":          # _v is version tag, never copy into live config
                out[key] = val
        out["_v"] = CONFIG_VERSION   # always stamp current version
        return out

    @staticmethod
    def validate_value(value: float, min_val: float, max_val: float) -> bool:
        return min_val <= value <= max_val

    @staticmethod
    def clamp(name: str, value: float) -> float:
        lo, hi = RANGES.get(name, (0, 100))
        return max(lo, min(hi, value))

    @staticmethod
    def list_presets() -> list:
        return ["fyter"]

    @staticmethod
    def get_preset(preset_name: str) -> Dict:
        presets = {
            # FYTER: ek TG fyter ke liye — sabse zyada awaaz, zero echo,
            # zero bass (no mud), max clarity + treble (kaan fatane wali
            # sharp presence), zero pitch shift (natural voice, no gimmick).
            # gain=220 + loudness=140 = is engine ka absolute maximum.
            "fyter": {
                "gain": 220, "echo": 0, "loudness": 140, "bass": 0,
                "pitch": 0, "treble": 100, "clarity": 100, "muted": False,
            },
        }
        # fyter hi default — koi aur preset nahi
        return dict(presets.get(preset_name, presets["fyter"]))

    @staticmethod
    def build_ffmpeg_filter(cfg: Dict = None, **kwargs) -> str:
        cfg = dict(cfg or {})
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        swap = float(cfg.get("swap", 0) or 0) >= 1
        if not any(float(cfg.get(k, 0) or 0) for k in ("gain", "loudness", "bass", "treble", "clarity", "echo", "pitch")):
            return "pan=stereo|c0=c1|c1=c0" if swap else "anull"
        p = _dsp_params(cfg)
        if p["muted"]:
            return "volume=0"
        parts = [
            "highpass=f=35",
            f"bass=g={p['bass_db']:.1f}:f=110:w=0.7",
            f"equalizer=f=3500:t=q:w=1.0:g={p['clarity_db']:.1f}",
            # new: dedicated exciter bump (air/definition, separate from clarity)
            f"equalizer=f={EXCITER_HZ:.0f}:t=q:w=1.2:g={p['clarity_db'] * 0.4:.1f}",
            f"treble=g={p['treble_db']:.1f}:f=6500:w=0.7",
            # new: gentle de-ess so loud treble doesn't turn into hiss/spit
            f"equalizer=f={DEESS_HZ:.0f}:t=q:w=2.0:g=-2.5",
            f"volume={p['vol']:.4f}",
        ]
        if p["echo"] > 0:
            e = min(1.0, p["echo"] / 100.0)
            w = 0.30 + 0.55 * e
            parts.append(
                f"aecho=0.9:0.8:{ECHO_DELAY_MS}|{ECHO_DELAY_MS * 2}|{ECHO_DELAY_MS * 3}:"
                f"{w:.2f}|{w * 0.5:.2f}|{w * 0.25:.2f}"
            )
        if p["pitch"] > 0:
            factor = 2 ** ((p["pitch"] / 100.0) * 0.7)   # deeper by this factor
            parts.append(
                f"asetrate={48000.0 / factor:.0f},aresample=48000,atempo={factor:.4f}"
            )
        parts.append(
            f"acompressor=threshold={p['comp_thr']:.3f}:ratio={p['ratio']:.1f}:attack={p['attack_ms']:.0f}:release=120:"
            f"makeup={10 ** (p['makeup_db'] / 20.0):.2f}"
        )
        parts.append("asoftclip=type=tanh")
        parts.append("alimiter=limit=0.97:attack=2:release=50")
        if swap:
            parts.append("pan=stereo|c0=c1|c1=c0")
        return ",".join(parts)

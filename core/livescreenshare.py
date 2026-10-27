"""
VCFight V2 - LIVE Video/Screenshare Bridge (ONE-WAY)
Source group mein jo bhi assistant/participant apni screen share karega,
wahi frame-by-frame target group mein real-time relay hoga.

Same architecture jo core/bridge.py (AudioBridge) mein hai — bas
Device.MICROPHONE ki jagah Device.SCREEN/CAMERA aur raw audio PCM ki
jagah raw video frame. Isliye ye AudioBridge ke saath saath chalta hai,
usko replace nahi karta — mic bridge alag rehta hai, video bridge alag.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from collections import deque
from pathlib import Path
from typing import Dict, Optional, Tuple

from config.settings import TEMP_DIR
from core import media_state

import numpy as np
try:
    from PIL import Image  # high-quality (antialiased) downscale
except Exception:  # pragma: no cover
    Image = None
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import Device, Direction, ExternalMedia, MediaStream, RecordStream, StreamFrames
from pytgcalls.types.raw import AudioParameters, VideoParameters

logger = logging.getLogger(__name__)

# Quality / smoothness knobs — sab Railway env var se badal sakte ho (redeploy
# ke bina code touch kiye). Defaults: 720p @ 20fps, 2 sec ke segments.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360
DEFAULT_FPS = max(5, int(os.getenv("LSS_FPS", "20")))
# Har mp4 segment ki "asli" length (sec). Kam = kam delay, par zyada player-swap.
SEG_SEC = float(os.getenv("LSS_SEG_SEC", "2.0"))
# Har segment ke end mein itna EXTRA tail (sec) record hota hai. Next segment ke
# ready hone tak purani segment isi tail par chalti rehti hai -> swap ke waqt
# freeze/atakna nahi. (Pehle yahi atakna / "ek hi picture" ka main karan tha.)
SEG_OVERLAP = float(os.getenv("LSS_SEG_OVERLAP", "0.6"))
SEG_FRAMES = max(2, int(round((SEG_SEC + SEG_OVERLAP) * DEFAULT_FPS)))
# Output size source ki orientation ke hisaab se: landscape screen -> OUT_LAND,
# phone (portrait) screen -> OUT_PORT. Aspect ratio KABHI stretch nahi hota
# (letterbox/pillarbox black padding ke saath fit hota hai).
_OUT_SHORT = max(240, int(os.getenv("LSS_OUT_SHORT", "720")) // 2 * 2)   # 720 -> 1280x720
_OUT_LONG = _OUT_SHORT * 16 // 9 // 2 * 2
OUT_LAND = (_OUT_LONG, _OUT_SHORT)
OUT_PORT = (_OUT_SHORT, _OUT_LONG)
X264_CRF = int(os.getenv("LSS_CRF", "21"))              # kam = better quality
X264_PRESET = os.getenv("LSS_PRESET", "ultrafast")      # CPU kam rakhne ke liye
SEG_MAX_WAIT = 3.0       # player agli segment ka max itna wait karke phir dobara check karta hai
# /rcplay (ya /yt) ki file-audio chal rahi ho to har swap par audio dobara start
# hoti hai — swap kam rakhne ke liye is dauran segments lambi (sec) banti hain.
SEG_SEC_AUDIO = float(os.getenv("LSS_AUDIO_SEG_SEC", "4.0"))
SRC_LOCK_TIMEOUT = 2.0   # locked source stream itne sec chup rahe to hi doosri stream par switch

VIDEO_PARAMETERS = VideoParameters(width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, frame_rate=DEFAULT_FPS)
AUDIO_PARAMETERS = AudioParameters(bitrate=48000, channels=2)

# Jis device se banda screen share karta hai wo library ke hisaab se
# CAMERA ya SCREEN dono naam se aa sakta hai — dono sun rahe hain taaki
# jo bhi active ho wahi pakda jaye.
_VIDEO_DEVICES = (Device.SCREEN, Device.CAMERA)

# "Shape sahi dikhta hai, color/detail blur/smeared" — ye classic symptom
# NV12 buffer ko galti se I420 planar maan ke padhne ka hota hai (Y plane
# dono format mein same jagah hoti hai isliye shape/luminance sahi aata
# hai, sirf U/V chroma scrambled hota hai). Bina confirmed log ke ye
# HYPOTHESIS hai, guarantee nahi — isliye ek line se flip karne layak
# banaya hai: Railway env var VIDEO_PIXEL_FORMAT=i420 daal ke restart
# karo agar ye wrong nikle (nv12 default hai).
VIDEO_PIXEL_FORMAT = os.getenv("VIDEO_PIXEL_FORMAT", "i420").strip().lower()


_INSTANCES: "weakref.WeakSet" = weakref.WeakSet()


def find_live_route(target_id: int) -> Optional["LiveScreenShare"]:
    """Wo LiveScreenShare jo `target_id` par abhi video chala raha hai (ya None).
    core/rcplay.py isse pata karta hai ki plain play() KARNA hai ya nahi —
    plain play() live video ko maar deta hai."""
    tid = int(target_id)
    for inst in list(_INSTANCES):
        try:
            if tid in inst.active_routes and tid in inst._seg_state:
                return inst
        except Exception:
            continue
    return None


class LiveScreenShare:
    def __init__(self, client: PyTgCalls, sudo_users: list, mtproto=None):
        _INSTANCES.add(self)
        self._play_lead = 0.15   # play() ka measured latency (EMA) — audio seam compensation
        self.client = client
        self.mtproto = mtproto
        self.sudo_users = sudo_users
        self.active_routes: Dict[int, dict] = {}   # source_id -> {target_id, ...}
        self._handler_registered = False
        self._bound_client_id: Optional[int] = None
        self._rx_count = 0
        self._fwd_count = 0
        self._last_error = ""
        # FIFO-based relay (this pass): send_frame()+ExternalMedia.VIDEO
        # is pytgcalls build (2.3.3) mein kabhi kaam nahi kiya — har baar
        # "External source not initialized", chahe kuch bhi try kiya.
        # core/screenshare.py ka PROVEN tareeka (video hamesha ek REAL FILE
        # se play hota hai, kabhi ExternalMedia.VIDEO nahi) use kar rahe
        # hain: apna ffmpeg encoder chalao, uska output ek named-pipe
        # (FIFO) mein daalo, aur pytgcalls ko wahi FIFO "file" ki tarah do.
        # Segment pipeline state: target_id -> {queue, ready, lock, current, last_play, ...}
        self._seg_state: Dict[int, dict] = {}
        # target_id -> (raw_bytes, src_w, src_h, seq, ts) — sirf latest RAW frame cache hota hai;
        # heavy resize/convert recorder ke thread mein 15fps par hota hai (event loop block nahi).
        self._latest_raw: Dict[int, tuple] = {}
        self._register_handlers()

    # ------------------------------------------------------------------
    # frame relay — ye hi core hai, exactly bridge.py ke _forward_frames jaisa
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        if self._handler_registered and self._bound_client_id == id(self.client):
            return
        if self.client is None:
            return

        me = self

        for device in _VIDEO_DEVICES:
            @self.client.on_update(fl.stream_frame(Direction.INCOMING, device))
            async def _on_incoming_video(_: PyTgCalls, update: StreamFrames, _dev=device):
                await me._forward_video(update, _dev)

        async def _on_end(_client, update):
            await me._on_stream_end(update)

        registered = False
        try:
            self.client.on_update(fl.stream_end())(_on_end)
            registered = True
        except Exception as e:
            logger.debug("[LiveScreenShare] fl.stream_end() unavailable: %s", e)
        if not registered:
            try:
                self.client.on_stream_end()(_on_end)
            except Exception as e:
                logger.warning("[LiveScreenShare] stream_end handler not registered (watchdog use hoga): %s", e)

        self._handler_registered = True
        self._bound_client_id = id(self.client)
        logger.info("[LiveScreenShare] video frame handlers ON client=%s", self._bound_client_id)

    def bind_client(self, client: PyTgCalls, mtproto=None) -> None:
        if client is None:
            return
        cid = id(client)
        if self._handler_registered and self._bound_client_id == cid:
            if mtproto is not None:
                self.mtproto = mtproto
            return
        self.client = client
        if mtproto is not None:
            self.mtproto = mtproto
        self._handler_registered = False
        self._bound_client_id = None
        self._register_handlers()

    # BUG FIX: pytgcalls/ntgcalls ki bahut si custom exceptions (jaise
    # NoActiveGroupCall, AlreadyJoinedError, ConnectionNotFound...) bina
    # koi message ke raise hoti hain — sirf class ka NAAM hi meaningful
    # hota hai. Plain `str(e)` in cases mein "" (khaali) aata hai, isliye
    # "Target join fail: " jaisa error message bilkul blank dikhta tha aur
    # asli wajah kabhi pata hi nahi chalti thi. Ye helper hamesha kam se
    # kam exception CLASS ka naam include karta hai, chahe message khaali
    # ho — ab error kabhi silently blank nahi jaayega.
    @staticmethod
    def _fmt_exc(e: BaseException) -> str:
        msg = str(e).strip()
        name = type(e).__name__
        text = f"{name}: {msg}" if msg else name
        return text[:200]

    # BUG FIX: neutral-black I420 placeholder frame, generated once per
    # resolution and cached — used to "prime" the FIFO so ffmpeg always
    # has something to encode/output even before any real screenshare
    # frame has arrived from the source group.
    _BLACK_FRAME_CACHE: Dict[Tuple[int, int], bytes] = {}

    @classmethod
    def _black_frame(cls, w: int, h: int) -> bytes:
        key = (w, h)
        frame = cls._BLACK_FRAME_CACHE.get(key)
        if frame is None:
            y_size = w * h
            uv_size = (w // 2) * (h // 2)
            frame = bytes([16]) * y_size + bytes([128]) * (2 * uv_size)
            cls._BLACK_FRAME_CACHE[key] = frame
        return frame

    # I420/YUV420 raw frame ka expected byte size — agar actual frame size
    # isse match nahi karta, to Telegram target pe blank/corrupt aayega
    # (send_frame ko bytes chahiye jo negotiated VIDEO_PARAMETERS ke exact
    # resolution ke hisaab se hon; koi resize/encode nahi hota beech mein).
    @staticmethod
    def _expected_i420_size(width: int, height: int) -> int:
        return int(width * height * 3 // 2)

    @staticmethod
    def _sanitize_chroma(i420: bytes, w: int, h: int) -> bytes:
        """Safety net against the exact "green screen" symptom.

        In YUV, U=V=128 is neutral (gray/no color shift). If the U/V
        planes end up effectively empty or garbage — wrong width/height
        guess slicing the buffer at the wrong offsets, a failed NV12->I420
        de-interleave, a source frame whose real layout doesn't match our
        assumptions — those bytes are usually near 0, not 128. Feeding
        U=V=0 into YUV->RGB math doesn't give black or blur: it mathematically
        pushes R and B toward 0 and G toward max for almost any Y value,
        which is a strong, uniform GREEN cast — exactly what was reported.

        This can't fix wrong color, but it guarantees we never SEND that
        specific broken-green frame: if the chroma planes look degenerate
        (mean far from the neutral 128, i.e. near-zero or otherwise
        clearly not real chroma data), force them to neutral 128. Worst
        case then becomes a grayscale/desaturated frame — still visibly
        wrong, but nowhere near as jarring as solid green, and shape/motion
        stays correct either way since only U/V are touched, never Y."""
        try:
            arr = np.frombuffer(i420, dtype=np.uint8)
            y_size = w * h
            uv_w, uv_h = w // 2, h // 2
            uv_size = uv_w * uv_h
            if arr.size < y_size + 2 * uv_size:
                return i420
            uv = arr[y_size:y_size + 2 * uv_size]
            mean = float(uv.mean()) if uv.size else 128.0
            # Real, valid chroma for on-screen content is essentially never
            # this close to 0 across an entire frame — legit content varies
            # a lot but centers near 128. This threshold only catches the
            # degenerate "basically empty/garbage" case, not real color.
            if mean < 40.0:
                out = bytearray(arr.tobytes())
                out[y_size:y_size + 2 * uv_size] = bytes([128]) * (2 * uv_size)
                return bytes(out)
            return i420
        except Exception:
            return i420

    # Common real device/screen-share resolutions, most-likely-first. Used
    # to disambiguate the length-based guess: many (w, h) pairs can have
    # the identical total pixel count, but only a handful are what an
    # actual phone screen looks like — picking a mathematically-plausible
    # but wrong one silently misaligns every Y/U/V slice downstream, which
    # is a second, sneakier way to get the same green-corruption symptom
    # (the code "succeeds" at every step, just with the wrong geometry).
    _COMMON_RESOLUTIONS = (
        (1080, 2400), (1080, 2340), (1080, 2280), (1080, 1920),
        (720, 1600), (720, 1560), (720, 1280),
        (828, 1792), (1170, 2532), (1284, 2778),
        (640, 360), (1280, 720), (1920, 1080),
    )

    @classmethod
    def _guess_resolution_from_length(cls, length: int) -> Optional[Tuple[int, int]]:
        """Frame object mein width/height field na milne par fallback:
        I420/NV12 dono 1.5 bytes/pixel lete hain, isliye total pixel count
        = length / 1.5 hai — chahe format kuch bhi ho. Pehle un COMMON
        device resolutions mein se try karo jinka pixel count match kare
        (in mein galat pick hone ka chance bahut kam hai), tabhi generic
        16-aligned-aspect-ratio guessing pe fallback karo."""
        total_px = (length * 2) // 3
        if total_px <= 0 or (length * 2) % 3 != 0:
            return None

        for w, h in cls._COMMON_RESOLUTIONS:
            if w * h == total_px:
                return (w, h)

        best = None
        best_score = None
        for w in range(16, 2049, 16):
            if total_px % w != 0:
                continue
            h = total_px // w
            if h < 16 or h > 4096 or h % 16 != 0:
                continue
            ratio = w / h
            if ratio < 0.2 or ratio > 5.0:
                continue
            # Standard-ish aspect ratios ke kareeb wale ko priority do
            # (16:9, 9:16, 4:3, 3:4, 19.5:9, 9:19.5, 1:1).
            targets = (16/9, 9/16, 4/3, 3/4, 19.5/9, 9/19.5, 1.0)
            closeness = min(abs(ratio - t) for t in targets)
            if best_score is None or closeness < best_score:
                best_score = closeness
                best = (w, h)
        return best

    @staticmethod
    def _nv12_to_i420(raw: bytes, w: int, h: int) -> Optional[bytes]:
        """NV12 = Y plane (w*h bytes) + interleaved UV plane (U,V,U,V...).
        I420 = Y plane + separate U plane + separate V plane. Total byte
        size EXACT same for both — isliye size-check kabhi is mismatch ko
        pakadta nahi, aur wrong-format-as-I420 read hamesha 'blur/smeared
        color, sahi shape' jaisa dikhta hai. De-interleave karke asli I420
        banata hai. Shape mismatch ho to None (caller raw-as-is bhejega)."""
        try:
            arr = np.frombuffer(raw, dtype=np.uint8)
            y_size = w * h
            uv_w, uv_h = w // 2, h // 2
            uv_total = uv_w * uv_h * 2
            if arr.size < y_size + uv_total or w <= 0 or h <= 0:
                return None
            y = arr[:y_size]
            uv = arr[y_size:y_size + uv_total].reshape(uv_h, uv_w, 2)
            u = uv[:, :, 0].copy()
            v = uv[:, :, 1].copy()
            return y.tobytes() + u.tobytes() + v.tobytes()
        except Exception:
            return None

    @staticmethod
    def _resize_i420(raw: bytes, sw: int, sh: int, dw: int, dh: int) -> Optional[bytes]:
        """Source ka actual capture resolution aksar target ke negotiated
        resolution se alag hota hai (phone screen 1080p, humara VIDEO_PARAMETERS
        640x360) — send_frame ko bytes chahiye EXACT expected size ke, warna
        target par blank/corrupt aata hai. Ye cheap nearest-neighbor resize
        Y/U/V planes ko alag-alag scale karta hai taaki I420 format valid rahe.
        Kuch bhi shape mismatch ho to None deta hai (caller original raw
        bhejega, crash nahi hoga)."""
        try:
            arr = np.frombuffer(raw, dtype=np.uint8)
            y_size = sw * sh
            uv_w, uv_h = sw // 2, sh // 2
            uv_size = uv_w * uv_h
            if arr.size < y_size + 2 * uv_size or sw <= 0 or sh <= 0:
                return None

            y = arr[:y_size].reshape(sh, sw)
            u = arr[y_size:y_size + uv_size].reshape(uv_h, uv_w)
            v = arr[y_size + uv_size:y_size + 2 * uv_size].reshape(uv_h, uv_w)

            def _scale(plane: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
                row_idx = (np.arange(new_h) * plane.shape[0] // max(new_h, 1)).astype(np.int32)
                col_idx = (np.arange(new_w) * plane.shape[1] // max(new_w, 1)).astype(np.int32)
                return plane[row_idx][:, col_idx]

            dy = _scale(y, dw, dh)
            duv_w, duv_h = dw // 2, dh // 2
            du = _scale(u, duv_w, duv_h)
            dv = _scale(v, duv_w, duv_h)
            return dy.tobytes() + du.tobytes() + dv.tobytes()
        except Exception:
            return None

    async def _forward_video(self, update: StreamFrames, device) -> None:
        """Bahut halka handler: sirf latest RAW frame + uski dimensions
        cache karta hai. Heavy resize/convert recorder mein, thread par hota hai.

        FIXES (is pass):
        1) Pehle >20fps ke frames DROP hote the (`now - prev < 0.05`). Screen
           share event-driven hota hai (static screen par naye frame aate hi
           nahi), isliye burst ka AAKHRI frame drop ho jaaye to target par
           beech ka purana picture hamesha ke liye chipka rehta tha. Ab har
           frame cache hota hai (sirf latest rehta hai, memory nahi badhti).
        2) Ek hi source mein camera + screen (ya 2 participants) ek saath
           frames bhejein to pehle frames[0] alternate hota tha -> "kabhi
           kuchh kabhi kuchh" flicker. Ab route ek (device, ssrc) stream par
           LOCK ho jaata hai; SCREEN ko CAMERA par priority; locked stream
           SRC_LOCK_TIMEOUT sec chup rahe tabhi doosri par switch hota hai."""
        self._rx_count += 1
        chat_id = int(getattr(update, "chat_id", 0) or 0)
        route = self.active_routes.get(chat_id)
        if self._rx_count <= 3 or self._rx_count % 500 == 0:
            logger.info(
                "[LiveScreenShare][DIAG] RX#%s chat=%s dev=%s route_found=%s",
                self._rx_count, chat_id, device, bool(route),
            )
        if not route or int(route.get("source_id", 0)) != chat_id:
            return
        target_id = int(route["target_id"])
        if target_id not in self._seg_state or not update.frames:
            return

        now = time.monotonic()
        try:
            frames = list(update.frames)
            lock = route.get("lock")            # (device, ssrc) ya None
            frame_obj = None
            if lock is not None and lock[0] != device and device == Device.SCREEN and lock[0] == Device.CAMERA:
                lock = None                     # screen > camera
            if lock is not None:
                for f in frames:
                    if (device, getattr(f, "ssrc", None)) == lock:
                        frame_obj = f
                        break
                if frame_obj is None:
                    if now - route.get("lock_ts", 0.0) < SRC_LOCK_TIMEOUT:
                        return                  # doosri stream ka frame — ignore
                    lock = None                 # locked stream mar gayi -> naya chuno
            if frame_obj is None:
                frame_obj = frames[0]
                lock = (device, getattr(frame_obj, "ssrc", None))
            route["lock"] = lock
            route["lock_ts"] = now

            raw = bytes(frame_obj.frame)
            if not raw:
                return
            info_obj = getattr(frame_obj, "info", None)
            w = getattr(info_obj, "width", None) if info_obj else None
            h = getattr(info_obj, "height", None) if info_obj else None
            if not w or not h:
                w = getattr(frame_obj, "width", None)
                h = getattr(frame_obj, "height", None)
            w, h = (int(w) if w else 0), (int(h) if h else 0)
            if not w or not h or len(raw) != self._expected_i420_size(w, h):
                guess = self._guess_resolution_from_length(len(raw))
                if guess:
                    w, h = guess
            if not w or not h or len(raw) != self._expected_i420_size(w, h):
                if self._fwd_count <= 5:
                    logger.warning("[LiveScreenShare][DIAG] frame dims unusable len=%s wh=%sx%s", len(raw), w, h)
                return
            seq = self._fwd_count
            self._latest_raw[target_id] = (raw, w, h, seq, now)
            self._fwd_count += 1
            if self._fwd_count <= 3 or self._fwd_count % 500 == 0:
                logger.info(
                    "[LiveScreenShare][DIAG] cached frame#%s src=%sx%s target=%s",
                    self._fwd_count, w, h, target_id,
                )
        except Exception as e:
            self._last_error = f"forward fail: {self._fmt_exc(e)}"
            if self._fwd_count < 5:
                logger.warning("[LiveScreenShare][DIAG] forward fail: %s", self._fmt_exc(e))

    @staticmethod
    def _out_dims_for(src_w: int, src_h: int) -> Tuple[int, int]:
        return OUT_PORT if src_h > src_w else OUT_LAND

    def _fit_i420(self, raw: bytes, sw: int, sh: int, ow: int, oh: int) -> Optional[bytes]:
        """Source I420/NV12 frame ko (ow x oh) canvas mein ASPECT-PRESERVING
        fit karo (antialiased downscale + black padding). Stretch/squash
        kabhi nahi. Thread mein chalta hai (numpy/PIL GIL chhodte hain)."""
        try:
            if VIDEO_PIXEL_FORMAT == "nv12":
                conv = self._nv12_to_i420(raw, sw, sh)
                if conv is None:
                    return None
                raw = conv
            if sw % 2 or sh % 2:
                return None
            ysz = sw * sh
            uvw, uvh = sw // 2, sh // 2
            usz = uvw * uvh
            if len(raw) < ysz + 2 * usz:
                return None
            scale = min(ow / sw, oh / sh)
            fw = min(ow, max(2, int(sw * scale) // 2 * 2))
            fh = min(oh, max(2, int(sh * scale) // 2 * 2))
            ox = (ow - fw) // 2 // 2 * 2
            oy = (oh - fh) // 2 // 2 * 2

            ycan = np.full((oh, ow), 16, dtype=np.uint8)
            ucan = np.full((oh // 2, ow // 2), 128, dtype=np.uint8)
            vcan = np.full((oh // 2, ow // 2), 128, dtype=np.uint8)
            mv = memoryview(raw)

            def _plane(off: int, pw: int, ph: int, tw: int, th: int, luma: bool = False) -> np.ndarray:
                if pw == tw and ph == th:
                    return np.frombuffer(mv[off:off + pw * ph], dtype=np.uint8).reshape(ph, pw)
                if Image is not None:
                    im = Image.frombuffer("L", (pw, ph), bytes(mv[off:off + pw * ph]), "raw", "L", 0, 1)
                    # luma = detail/text -> LANCZOS (sharp); chroma -> BILINEAR (sasta)
                    return np.asarray(im.resize((tw, th), Image.LANCZOS if luma else Image.BILINEAR))
                arr = np.frombuffer(mv[off:off + pw * ph], dtype=np.uint8).reshape(ph, pw)
                ri = (np.arange(th) * ph // th).astype(np.int32)
                ci = (np.arange(tw) * pw // tw).astype(np.int32)
                return arr[ri][:, ci]

            ycan[oy:oy + fh, ox:ox + fw] = _plane(0, sw, sh, fw, fh, luma=True)
            ucan[oy // 2:oy // 2 + fh // 2, ox // 2:ox // 2 + fw // 2] = _plane(ysz, uvw, uvh, fw // 2, fh // 2)
            vcan[oy // 2:oy // 2 + fh // 2, ox // 2:ox // 2 + fw // 2] = _plane(ysz + usz, uvw, uvh, fw // 2, fh // 2)
            return ycan.tobytes() + ucan.tobytes() + vcan.tobytes()
        except Exception as e:
            logger.debug("[LiveScreenShare] _fit_i420 fail: %s", e)
            return None

    # ------------------------------------------------------------------
    # join helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _build_media_stream(**kwargs) -> MediaStream:
        """MediaStream(...) banate waqt agar installed pytgcalls build
        koi kwarg (jaise video_parameters) support nahi karta, to us
        specific kwarg ko drop karke retry karta hai — generic protection,
        RecordStream wale fix jaisa hi, taaki version-mismatch dobara na aaye."""
        kw = dict(kwargs)
        for _ in range(len(kw) + 1):
            try:
                return MediaStream(ExternalMedia.VIDEO, **kw)
            except TypeError as e:
                msg = str(e)
                dropped = None
                for key in list(kw.keys()):
                    if f"'{key}'" in msg and "unexpected keyword argument" in msg:
                        dropped = key
                        break
                if not dropped:
                    raise
                logger.warning("[LiveScreenShare] MediaStream '%s' unsupported is build mein — drop: %s", dropped, msg)
                kw.pop(dropped, None)
        return MediaStream(ExternalMedia.VIDEO)

    async def _force_rejoin_with_video(self, chat_id: int, stream: MediaStream) -> None:
        """BUG FOUND FROM LIVE LOGS (this pass): pehle "already" wale
        exception ko chup-chaap True maan liya jaata tha (assume ki library
        khud stream ko combined audio+video mein upgrade kar degi). Real
        logs ne confirm kiya ki aisa NAHI hota — /join ka audio-only play()
        pehle se chal raha call "already in a call" bol ke doosre play()
        ko reject karta hai, aur us existing call mein video ka external
        source kabhi register hi nahi hota. Isi wajah se send_frame har
        baar 'External source not initialized' deta tha — 100% frames
        reject, ek bhi kabhi target tak nahi pahuncha.

        Fix: upgrade try mat karo — purani (audio-only) call chhodo aur
        FRESH combined audio+video stream se turant rejoin karo. Isse
        mic bridge 1 chhoti si sleep ke liye interrupt hota hai, phir
        wapas usi combined call ke through chalta hai.

        BUG FIX (this pass): pehle yahan `await self.client.play(...)` ka
        koi try/except hi nahi tha — agar rejoin ke baad bhi play() fail
        hota (call abhi TIGHT rejoin ke turant baad "settle" nahi hua
        hota — common WebRTC race), to wahi (aksar KHAALI-message) exception
        seedha bahar propagate ho jaata tha aur caller mein `str(e)[:200]`
        se "" ban jaata tha — poori tarah untraceable error. Ab: 1 retry,
        thoda lamba sleep (call ko settle hone ka poora mauka), aur agar
        dono attempts fail ho to ek CLEAR wrapped message ke saath raise
        (kabhi khaali nahi, `_fmt_exc` guarantee karta hai)."""
        try:
            await self.client.leave_call(chat_id)
        except Exception:
            pass

        last_err: Optional[BaseException] = None
        for attempt in range(2):
            await asyncio.sleep(0.6 + attempt * 0.6)
            try:
                await self.client.play(chat_id, stream)
                return
            except Exception as e:
                last_err = e
                logger.warning(
                    "[LiveScreenShare] force-rejoin play attempt %s/2 chat=%s: %s",
                    attempt + 1, chat_id, self._fmt_exc(e),
                )

        raise RuntimeError(
            f"rejoin-with-video play failed after retry: {self._fmt_exc(last_err)}"
        ) from last_err

    async def _join_source(self, chat_id: int) -> Tuple[bool, str]:
        """Source group mein join karke incoming video capture ON karta hai.

        BUG (fixed): record() ko pehle MediaStream(video_parameters=...) diya
        ja raha tha — record() ka signature RecordStream leta hai, MediaStream
        nahi. Isse har baar TypeError aata tha jo `except: pass` mein chup
        chaap nigal liya jaata tha — record kabhi arm hi nahi hota tha, isliye
        koi incoming video frame kabhi capture nahi hota tha aur target tak
        kuch relay hi nahi hota tha (join/"Live!" message phir bhi aa jaata
        tha kyunki join alag step hai, record alag).
        """
        try:
            # audio_path=ExternalMedia.AUDIO isi single play() call mein
            # zaroori hai — /join ka audio bridge is chat par pehle se
            # ExternalMedia.AUDIO wala play() chala chuka hota hai
            # (core/bridge.py). Agar hum yahan sirf video wala alag play()
            # call karte, to ye dusra/conflicting play() call ban jaata hai.
            stream = self._build_media_stream(
                audio_parameters=AUDIO_PARAMETERS,
                video_parameters=VIDEO_PARAMETERS,
                audio_path=ExternalMedia.AUDIO,
            )
            try:
                await self.client.play(chat_id, stream)
            except Exception as e:
                if "already" in str(e).lower():
                    await self._force_rejoin_with_video(chat_id, stream)
                else:
                    raise

            record_err = ""
            record_kwargs = dict(
                audio=True,
                audio_parameters=AUDIO_PARAMETERS,
                camera=True,
                screen=True,
            )
            for attempt in range(6):
                try:
                    # audio=True (with AUDIO_PARAMETERS) rakha hai kyunki
                    # record() ek hi recording-config leta hai — agar
                    # AudioBridge (mic relay) pehle se isi chat pe
                    # record(audio=True) laga chuka hai, to yahan audio=False
                    # bhejne se wo silently overwrite/disable ho jaata,
                    # aur mic bridge tut jaata. audio=True rakhne se dono
                    # (mic bridge + live screenshare) ek saath safe rehte hain.
                    await self.client.record(chat_id, RecordStream(**record_kwargs))
                    record_err = ""
                    break
                except TypeError as e:
                    # Installed pytgcalls/ntgcalls version RecordStream ke
                    # kuch kwargs support nahi karta (jaise video_parameters
                    # is build mein tha) — us specific kwarg ko drop karke
                    # turant retry karo, bina naye deploy ke.
                    msg = str(e)
                    dropped = None
                    for key in list(record_kwargs.keys()):
                        if f"'{key}'" in msg and "unexpected keyword argument" in msg:
                            dropped = key
                            break
                    if dropped:
                        logger.warning(
                            "[LiveScreenShare] RecordStream '%s' unsupported is build mein — "
                            "drop karke retry: %s", dropped, msg,
                        )
                        record_kwargs.pop(dropped, None)
                        continue
                    record_err = self._fmt_exc(e)
                    break
                except Exception as e:
                    if "already" in str(e).lower():
                        record_err = ""
                        break
                    record_err = self._fmt_exc(e)
                    logger.warning(
                        "[LiveScreenShare] record attempt %s/6 chat=%s: %s",
                        attempt + 1, chat_id, e,
                    )
                    await asyncio.sleep(0.25 + attempt * 0.1)

            if record_err:
                # Record na lagne ka matlab hai incoming video kabhi capture
                # nahi hoga — isliye ye ab hard-fail hai, silent pass nahi.
                return False, f"Source record (video) failed: {record_err}"

            return True, ""
        except Exception as e:
            return False, self._fmt_exc(e)

    # ------------------------------------------------------------------
    # SEGMENT PIPELINE v2 (transport WAHI proven: finished +faststart mp4 ->
    # client.play(), core/screenshare.py jaisa. Sirf scheduling theek ki.)
    #
    # Purani problems:
    #  * agli segment ka wait/stream_end/watchdog(5s) -> har segment ke baad
    #    2-3s freeze ("atak atak", "ek hi picture"); late hone par PURANI
    #    segment dobara replay (peeche ka video).
    #  * Encoder slow ho to segment wall-clock se lambi/chhoti bankar
    #    fast-forward / growing delay.
    #
    # Ab:
    #  * ek "pump" task FPS ke wall-clock ticks par chalta hai, latest frame
    #    ko sirf EK baar fit karta hai (sab active writers ke liye share).
    #  * har SEG_SEC par naya ffmpeg writer start hota hai, jo SEG_SEC +
    #    SEG_OVERLAP length record karta hai (overlap = swap ke liye cushion).
    #  * finished segment ki finalize (faststart) alag task mein — recording
    #    beech mein ruk-ti nahi.
    #  * player: jaise hi NAYI segment ready, turant play() (jump-to-live,
    #    replay kabhi nahi). Purani segment ki overlap-tail swap tak chalti
    #    rehti hai, isliye beech mein freeze nahi.
    # ------------------------------------------------------------------
    @staticmethod
    async def _drain_tail(stream, limit: int = 2000) -> bytes:
        tail = bytearray()
        try:
            while stream is not None:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > limit:
                    del tail[:-limit]
        except Exception:
            pass
        return bytes(tail)

    @staticmethod
    def _kill_proc(proc) -> None:
        try:
            if proc is not None and proc.returncode is None:
                proc.kill()
        except Exception:
            pass

    async def _start_writer(self, target_id: int, idx: int, seg_len: float = SEG_SEC) -> Optional[dict]:
        path = str(Path(TEMP_DIR) / f"lss_{target_id}_{idx}.mp4")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        latest = self._latest_raw.get(target_id)
        ow, oh = self._out_dims_for(latest[1], latest[2]) if latest else OUT_LAND
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{ow}x{oh}", "-r", str(DEFAULT_FPS),
            "-i", "-",
            "-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(X264_CRF),
            "-threads", "2",
            "-g", str(DEFAULT_FPS), "-pix_fmt", "yuv420p", "-an",
            "-movflags", "+faststart",
            path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            self._last_error = f"ffmpeg spawn failed: {self._fmt_exc(e)}"
            logger.error("[LiveScreenShare][SEG] %s", self._last_error)
            return None
        return {
            "idx": idx, "path": path, "dims": (ow, oh), "proc": proc,
            "err": asyncio.create_task(self._drain_tail(proc.stderr)),
            "left": max(2, int(round((seg_len + SEG_OVERLAP) * DEFAULT_FPS))),
        }

    async def _finalize_writer(self, target_id: int, w: dict) -> None:
        """Writer ki stdin band -> ffmpeg finish (+faststart) -> queue mein.
        Alag task mein chalta hai, isliye recording beech mein nahi rukti."""
        proc, path = w["proc"], w["path"]
        try:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=25)
            except asyncio.TimeoutError:
                self._kill_proc(proc)
            tail = await w["err"]
        except asyncio.CancelledError:
            self._kill_proc(proc)
            w["err"].cancel()
            raise
        st = self._seg_state.get(target_id)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        ok = proc.returncode == 0 and size >= 500
        if st is None or st.get("stopped"):
            try:
                os.remove(path)
            except Exception:
                pass
            return
        if not ok:
            st["fails"] = st.get("fails", 0) + 1
            self._last_error = f"segment encode fail code={proc.returncode} size={size}"
            logger.error(
                "[LiveScreenShare][SEG] %s stderr_tail=%r",
                self._last_error, tail[-500:].decode(errors="replace"),
            )
            if st["fails"] >= 3:
                st["error"] = self._last_error
                st["ready"].set()
            return
        st["fails"] = 0
        ow, oh = w["dims"]
        st["queue"].append((path, ow, oh))
        while len(st["queue"]) > 3:  # bahut peeche -> purani drop (live rehne ke liye)
            old = st["queue"].popleft()
            try:
                os.remove(old[0])
            except Exception:
                pass
        st["ready"].set()
        st["made"] = st.get("made", 0) + 1
        if st["made"] <= 3 or st["made"] % 30 == 0:
            logger.info("[LiveScreenShare][SEG] ready seg#%s %sx%s target=%s size=%s",
                        w["idx"], ow, oh, target_id, size)

    async def _segment_recorder(self, target_id: int) -> None:
        """Pump: FPS ke wall-clock ticks par latest frame -> sab active
        ffmpeg writers. Har SEG_SEC par naya writer."""
        interval = 1.0 / max(1, DEFAULT_FPS)
        writers: list = []
        fit_cache: Dict[Tuple[int, int], Tuple[int, bytes]] = {}
        idx = 0
        start_fails = 0
        now = time.monotonic()
        next_seg_at = now
        next_tick = now
        try:
            while True:
                st = self._seg_state.get(target_id)
                if st is None or st.get("stopped"):
                    return
                now = time.monotonic()

                if now >= next_seg_at:
                    seg_len = SEG_SEC_AUDIO if media_state.has_audio(target_id) else SEG_SEC
                    w = await self._start_writer(target_id, idx, seg_len)
                    if w is None:
                        start_fails += 1
                        if start_fails >= 3:
                            st["error"] = self._last_error or "ffmpeg start failed"
                            st["ready"].set()
                            return
                        next_seg_at = time.monotonic() + 1.0
                    else:
                        start_fails = 0
                        writers.append(w)
                        idx += 1
                        next_seg_at += seg_len
                        if next_seg_at < time.monotonic() - seg_len:
                            next_seg_at = time.monotonic() + seg_len

                lr = self._latest_raw.get(target_id)
                for dims in {wr["dims"] for wr in writers}:
                    cached = fit_cache.get(dims)
                    if lr is not None and (cached is None or cached[0] != lr[3]):
                        fitted = await asyncio.to_thread(self._fit_i420, lr[0], lr[1], lr[2], dims[0], dims[1])
                        if fitted is not None:
                            fit_cache[dims] = (lr[3], self._sanitize_chroma(fitted, dims[0], dims[1]))
                        # fit fail -> seq update NAHI karte: agli tick par phir try hoga
                        # (pehle fail par purani frame hamesha ke liye chipak jaati thi)
                    if fit_cache.get(dims) is None:
                        fit_cache[dims] = (-1, self._black_frame(dims[0], dims[1]))

                for wr in list(writers):
                    frame = fit_cache[wr["dims"]][1]
                    try:
                        wr["proc"].stdin.write(frame)
                        await wr["proc"].stdin.drain()
                        wr["left"] -= 1
                    except (BrokenPipeError, ConnectionResetError, RuntimeError):
                        wr["left"] = 0
                    if wr["left"] <= 0:
                        writers.remove(wr)
                        t = asyncio.create_task(self._finalize_writer(target_id, wr))
                        st.setdefault("fin", set()).add(t)
                        t.add_done_callback(lambda _t, _st=st: _st.get("fin", set()).discard(_t))

                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:      # bahut peeche (>1s) -> catch-up chhodo
                    next_tick = time.monotonic()
        except asyncio.CancelledError:
            pass
        finally:
            for wr in writers:
                self._kill_proc(wr["proc"])
                wr["err"].cancel()

    async def _audio_for_swap(self, chat_id: int):
        """Har video swap par kaunsi audio niche chalni chahiye.
        Returns (audio_path_or_ExternalMedia, temp_slice_path_or_None).

        * /rcplay /yt ki file chal rahi ho -> wahi file, CURRENT position se
          (wall-clock + measured play latency) — screenshare.py ke
          media_state.remaining_audio jaisa hi, bas -t se limited taaki lambi
          file har swap par poori copy na ho.
        * warna live mic bridge (ExternalMedia.AUDIO)."""
        try:
            e = media_state._entry(chat_id)
        except Exception:
            e = None
        if not e:
            return ExternalMedia.AUDIO, None
        path = e.get("path") or ""
        if path.startswith(("http://", "https://")):
            return path, None            # radio/live URL — position ka concept nahi
        if not os.path.exists(path):
            return ExternalMedia.AUDIO, None
        elapsed = max(0.0, time.time() - float(e.get("started_at") or time.time())) + self._play_lead
        dur = e.get("duration")
        if dur and elapsed >= float(dur) - 0.2:
            return ExternalMedia.AUDIO, None
        if elapsed < 0.3:
            return path, None
        span = 2 * SEG_SEC_AUDIO + SEG_OVERLAP + 3.0
        ext = os.path.splitext(path)[1] or ".wav"
        out = str(Path(TEMP_DIR) / f"lssa_{int(chat_id)}_{int(time.time() * 1000)}{ext}")
        for extra in (["-c", "copy"], ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le"]):
            if extra[0] != "-c":
                out = out.rsplit(".", 1)[0] + ".wav"
            try:
                p = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-ss", f"{elapsed:.2f}", "-i", path, "-t", f"{span:.1f}", *extra, out,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(p.communicate(), timeout=20)
                if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
                    return out, out
            except Exception:
                pass
        logger.warning("[LiveScreenShare] audio cut fail chat=%s — file audio 0:00 se", chat_id)
        return path, None

    async def _play_file(self, chat_id: int, path: str, w: int = DEFAULT_WIDTH, h: int = DEFAULT_HEIGHT,
                         audio=None) -> Tuple[bool, str]:
        """Exactly screenshare.py ka proven call: file video + audio_path.
        audio = ExternalMedia.AUDIO (live mic) ya file-audio path (/rcplay).
        video_parameters segment ki asli dims ke saath (stretch nahi hota)."""
        stream = MediaStream(
            path,
            audio_parameters=AUDIO_PARAMETERS,
            video_parameters=VideoParameters(width=w, height=h, frame_rate=DEFAULT_FPS),
            audio_path=audio if audio is not None else ExternalMedia.AUDIO,
        )
        try:
            await self.client.play(chat_id, stream)
            return True, ""
        except Exception as e:
            if "already" in str(e).lower():
                try:
                    await self._force_rejoin_with_video(chat_id, stream)
                    return True, ""
                except Exception as e2:
                    return False, self._fmt_exc(e2)
            return False, self._fmt_exc(e)

    async def _play_next_segment(self, target_id: int, wait: float = SEG_MAX_WAIT) -> Tuple[bool, str]:
        """NAYI segment ka intezaar (max `wait` sec) aur turant play.
        (False, "") = abhi koi nayi segment nahi aayi (replay nahi karte).
        Queue mein ek se zyada ho to sabse nayi lete hain (jump-to-live).
        Exception: st["audio_dirty"] (rcplay start/stop hua) -> current segment
        turant nayi audio ke saath dobara play, taaki audio 2-4s late na ho."""
        st = self._seg_state.get(target_id)
        if st is None:
            return False, "no segment state"
        async with st["lock"]:
            if st.get("stopped"):
                return False, "stopped"
            seg = None
            deadline = time.monotonic() + wait
            while True:
                if st["queue"]:
                    while len(st["queue"]) > 1:
                        stale = st["queue"].popleft()
                        try:
                            os.remove(stale[0])
                        except Exception:
                            pass
                    seg = st["queue"].popleft()
                    break
                cur = st.get("current")
                if st.get("audio_dirty") and cur and os.path.exists(cur[0]):
                    seg = cur
                    break
                if st.get("error"):
                    return False, st["error"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, ""
                st["ready"].clear()
                if st["queue"] or st.get("audio_dirty"):
                    continue
                try:
                    await asyncio.wait_for(st["ready"].wait(), remaining)
                except asyncio.TimeoutError:
                    pass
            if st.get("stopped"):   # stop aa gaya jab hum wait kar rahe the
                if not st.get("current") or seg[0] != st["current"][0]:
                    try:
                        os.remove(seg[0])
                    except Exception:
                        pass
                return False, "stopped"
            path, w, h = seg
            st["audio_dirty"] = False
            audio, slice_path = await self._audio_for_swap(target_id)
            t_play = time.monotonic()
            ok, err = await self._play_file(target_id, path, w, h, audio)
            if ok:
                # play() ka asli latency EMA — agli audio cut is lead se aage se shuru hogi
                took = time.monotonic() - t_play
                self._play_lead = min(0.6, max(0.0, 0.7 * self._play_lead + 0.3 * took))
                old = st.get("current")
                st["current"] = seg
                st["last_play"] = time.monotonic()
                if old and old[0] != path:
                    try:
                        os.remove(old[0])
                    except Exception:
                        pass
                prev_slice = st.get("audio_slice")
                st["audio_slice"] = slice_path
                if prev_slice and prev_slice != slice_path:
                    try:
                        os.remove(prev_slice)
                    except Exception:
                        pass
                st["played"] = st.get("played", 0) + 1
                if st["played"] <= 3 or st["played"] % 30 == 0:
                    logger.info("[LiveScreenShare][SEG] playing %s %sx%s audio=%s target=%s",
                                os.path.basename(path), w, h,
                                "FILE" if slice_path or isinstance(audio, str) else "LIVE-MIC", target_id)
            else:
                logger.warning("[LiveScreenShare][SEG] play fail target=%s: %s", target_id, err)
                if slice_path:
                    try:
                        os.remove(slice_path)
                    except Exception:
                        pass
                if not st.get("current") or seg[0] != st["current"][0]:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            return ok, err

    async def refresh_audio(self, target_id: int) -> bool:
        """/rcplay (ya /yt) start ya stop hua — video wahi rakhke sirf audio
        badlo. False = is target par live screenshare chal hi nahi raha
        (caller ko apna normal tareeka use karna chahiye)."""
        tid = int(target_id)
        st = self._seg_state.get(tid)
        if st is None or tid not in self.active_routes or st.get("stopped"):
            return False
        st["audio_dirty"] = True
        st["ready"].set()
        return True

    async def _on_stream_end(self, update) -> None:
        # Ab zaroorat nahi: player nayi segment aate hi khud swap karta hai.
        # (Purane stream_end + watchdog chain se hi har segment ke baad atakna
        # aur duplicate play() hota tha.) Handler sirf compatibility ke liye.
        return

    async def _player_loop(self, target_id: int) -> None:
        try:
            while True:
                st = self._seg_state.get(target_id)
                if st is None or st.get("stopped"):
                    return
                if st.get("error") and not st["queue"]:
                    logger.warning("[LiveScreenShare][SEG] recorder dead target=%s: %s", target_id, st["error"])
                    return
                ok, err = await self._play_next_segment(target_id, wait=SEG_MAX_WAIT)
                if not ok and err:
                    if err == "stopped":
                        return
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _stop_segments(self, target_id: int) -> None:
        st = self._seg_state.pop(target_id, None)
        self._latest_raw.pop(target_id, None)
        if st is None:
            return
        st["stopped"] = True
        st["ready"].set()
        tasks = [st.get("task"), st.get("player")] + list(st.get("fin", ()))
        for t in tasks:
            if t is not None:
                t.cancel()
        for t in tasks:
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        # koi play() beech mein ho to use khatam hone do (warna stop ke BAAD
        # video wapas chalu ho sakta tha)
        try:
            await asyncio.wait_for(st["lock"].acquire(), timeout=6)
            st["lock"].release()
        except Exception:
            pass
        paths = [q[0] for q in st.get("queue", [])]
        if st.get("current"):
            paths.append(st["current"][0])
        if st.get("audio_slice"):
            paths.append(st["audio_slice"])
        for pth in paths:
            try:
                os.remove(pth)
            except Exception:
                pass
        try:
            for f in Path(TEMP_DIR).glob(f"lss_{target_id}_*.mp4"):
                try:
                    f.unlink()
                except Exception:
                    pass
            for f in Path(TEMP_DIR).glob(f"lssa_{target_id}_*"):
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    async def _join_target(self, chat_id: int) -> Tuple[bool, str]:
        """Target par segment pipeline start karo: pehli mp4 segment bane
        tak wait, phir proven play() se chalao; baaki player loop chain karta hai."""
        await self._stop_segments(chat_id)
        st = {
            "queue": deque(), "ready": asyncio.Event(), "lock": asyncio.Lock(),
            "current": None, "last_play": 0.0, "error": "", "starting": True,
            "stopped": False, "fin": set(),
        }
        self._seg_state[chat_id] = st
        st["task"] = asyncio.create_task(self._segment_recorder(chat_id))
        try:
            await asyncio.wait_for(st["ready"].wait(), timeout=SEG_SEC + SEG_OVERLAP + 15)
        except asyncio.TimeoutError:
            await self._stop_segments(chat_id)
            return False, "first video segment not produced (ffmpeg slow/missing?)"
        if not st["queue"]:
            err = st.get("error") or "segment recorder failed"
            await self._stop_segments(chat_id)
            return False, err
        ok, err = await self._play_next_segment(chat_id, wait=1.0)
        if not ok:
            await self._stop_segments(chat_id)
            return False, err or "first segment play failed"
        st["starting"] = False
        st["player"] = asyncio.create_task(self._player_loop(chat_id))
        return True, ""

    async def _restore_audio_only(self, chat_id: int, record: bool) -> None:
        """Stop ke baad chat ko WAISE hi wapas karo jaise /join ne chhoda tha:
        audio-only external stream (+ source par mic record). VC LEAVE NAHI —
        core/screenshare.py ke stop_screenshare jaisa hi proven tareeka.

        Pehle stop_route dono chats se leave_call() karta tha. Par AudioBridge
        ka route (active_bridges) zinda rehta hai, isliye vc_join_watchdog
        (25s) ko lagta tha "assistant VC se gir gaya" aur wo khud rejoin kar
        deta tha -> "stop karte hi assistant bhaagi, kuchh sec baad phir aa gayi"."""
        try:
            from core import media_state
            file_audio = await media_state.remaining_audio(chat_id)
        except Exception:
            file_audio = None
        src = file_audio if file_audio else ExternalMedia.AUDIO
        last_err = None
        for attempt in range(2):
            try:
                await self.client.play(chat_id, MediaStream(src, AUDIO_PARAMETERS))
                last_err = None
                break
            except Exception as e:
                if "already" in str(e).lower():
                    last_err = None
                    break
                last_err = e
                await asyncio.sleep(0.4)
        if last_err is not None:
            logger.warning("[LiveScreenShare] restore audio chat=%s: %s", chat_id, self._fmt_exc(last_err))
        if record:
            for attempt in range(3):
                try:
                    await self.client.record(chat_id, RecordStream(True, AUDIO_PARAMETERS))
                    break
                except Exception as e:
                    if "already" in str(e).lower():
                        break
                    if attempt == 2:
                        logger.warning("[LiveScreenShare] re-arm record chat=%s: %s", chat_id, self._fmt_exc(e))
                    else:
                        await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # public API — commands se yahi call hota hai
    # ------------------------------------------------------------------
    async def start_route(self, source_id: int, target_id: int) -> Tuple[bool, str]:
        self.bind_client(self.client, self.mtproto)

        ok_src, err_src = await self._join_source(source_id)
        if not ok_src:
            self._last_error = f"Source join fail: {err_src}"
            return False, self._last_error

        # Route pehle register (frames cache hone lagein) — segment
        # pipeline start hone se pehle hi source frames aa sakte hain.
        info = {"source_id": source_id, "target_id": target_id, "started_at": time.time()}
        self.active_routes[source_id] = info
        self.active_routes[target_id] = info

        ok_tgt, err_tgt = True, ""
        if target_id != source_id:
            ok_tgt, err_tgt = await self._join_target(target_id)
        if not ok_tgt:
            self.active_routes.pop(source_id, None)
            self.active_routes.pop(target_id, None)
            self._last_error = f"Target join fail: {err_tgt}"
            # fail par bhi chats ko audio-only state mein wapas (leave nahi)
            await self._restore_audio_only(target_id, record=False)
            await self._restore_audio_only(source_id, record=True)
            return False, self._last_error

        logger.info("[LiveScreenShare] LIVE video %s -> %s (segment mode v2)", source_id, target_id)
        return True, ""

    async def stop_route(self, chat_id: int) -> bool:
        route = self.active_routes.pop(chat_id, None)
        if not route:
            return False
        source_id = route["source_id"]
        target_id = route["target_id"]
        self.active_routes.pop(source_id, None)
        self.active_routes.pop(target_id, None)
        await self._stop_segments(target_id)
        # VC se LEAVE nahi — sirf video hatao, audio bridge jaisa tha waisa.
        await self._restore_audio_only(target_id, record=False)
        if source_id != target_id:
            await self._restore_audio_only(source_id, record=True)
        logger.info("[LiveScreenShare] STOPPED %s -> %s (VC mein hi, no leave)", source_id, target_id)
        return True

    def is_active(self, chat_id: int) -> bool:
        return chat_id in self.active_routes

    def get_last_error(self) -> str:
        return self._last_error

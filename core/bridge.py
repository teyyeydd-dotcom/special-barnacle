"""
VCFight V2 - LIVE Audio Bridge (ONE-WAY)
Source mic → target. Screenshare uses live-mux so voice continues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import numpy as np

from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import Device, Direction, ExternalMedia, MediaStream, RecordStream, StreamFrames
from pytgcalls.types.raw import AudioParameters

from core.effects import AudioEffects, LiveEQ
from core.bridge_state import set_live_bridge
from core.frame_queue import FrameQueue
from core import media_state

logger = logging.getLogger(__name__)

AUDIO_PARAMETERS = AudioParameters(bitrate=48000, channels=2)


class AudioBridge:
    def __init__(self, client: PyTgCalls, self_user_id: int = 0, mtproto=None):
        self.client = client
        self.mtproto = mtproto
        self.self_user_id = int(self_user_id or 0)
        self.active_bridges: Dict[int, dict] = {}
        self.config = AudioEffects.get_default_effects()
        self.eq = LiveEQ()
        self._handler_registered = False
        self._bound_client_id = None
        self._was_admin_muted: Set[int] = set()
        self._send_busy = False
        self._mute_lock = asyncio.Lock()
        self._fwd_count = 0
        self._drop_count = 0
        self._rx_count = 0
        self._last_join_error: str = ""
        # receive and send now run on separate async paths through this
        # bounded queue — a slow send_frame() call can no longer block the
        # next incoming mic frame, which is what was causing cut/buffer stalls
        self._frame_queue = FrameQueue(
            process_fn=self._process_frame_sync,
            send_fn=self._send_pcm,
            maxsize=48,   # ~960 ms intake capacity; pacer enforces real max-latency
        )
        self._frame_queue.start()
        self._register_handlers()
        self.sync_effects()
        set_live_bridge(self)

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
        set_live_bridge(self)
        logger.info("[LiveBridge] bound handlers to PyTgCalls id=%s", cid)

    def sync_effects(self) -> None:
        self.eq.configure(self.config)
        if hasattr(self.eq, "muted"):
            self.eq.muted = bool(self.config.get("muted", False))

    def _save_muted_config(self) -> None:
        try:
            from core.effects import CONFIG_VERSION
            path = Path("bridge_config.json")
            data = {}
            if path.exists():
                with open(path, "r") as f:
                    data = json.load(f) or {}
            effects = dict(data.get("effects") or self.config)
            for k in ("gain", "loudness", "bass", "treble", "clarity", "echo", "pitch", "muted"):
                if k in self.config:
                    effects[k] = self.config[k]
            effects["_v"] = CONFIG_VERSION   # stamp version — prevents legacy-wipe on restart
            data["effects"] = effects
            with open(path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.warning(f"Could not save config: {e}")

    def _register_handlers(self) -> None:
        if self._handler_registered and self._bound_client_id == id(self.client):
            return
        if self.client is None:
            return

        bridge_self = self

        @self.client.on_update(fl.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def _on_incoming_audio(_: PyTgCalls, update: StreamFrames):
            await bridge_self._forward_frames(update)

        try:
            from pytgcalls.types import GroupCallParticipant, UpdatedGroupCallParticipant

            @self.client.on_update(
                fl.call_participant(
                    GroupCallParticipant.Action.UPDATED
                    | GroupCallParticipant.Action.JOINED
                )
            )
            async def _on_participant(_: PyTgCalls, update: UpdatedGroupCallParticipant):
                await bridge_self._handle_participant_update(update)

            logger.info("[LiveBridge] participant handler registered")
        except Exception as e:
            logger.warning(f"[LiveBridge] participant handler skip: {e}")

        self._handler_registered = True
        self._bound_client_id = id(self.client)
        logger.info("[LiveBridge] stream_frame handler ON client=%s", self._bound_client_id)

    async def _handle_participant_update(self, update) -> None:
        try:
            participant = getattr(update, "participant", None)
            if participant is None:
                return

            uid = int(getattr(participant, "user_id", 0) or 0)
            if self.self_user_id and uid and uid != self.self_user_id:
                return

            chat_id = int(getattr(update, "chat_id", 0) or 0)
            muted = bool(getattr(participant, "muted", False))
            muted_by_admin = bool(getattr(participant, "muted_by_admin", False))
            can_self_unmute = getattr(participant, "can_self_unmute", None)

            # FIX: volume slider se bot ka volume badhane/ghatane par Telegram
            # participant update bhejta hai (volume / volume_by_admin badalta
            # hai). Pehle `muted_by_admin` akela hi admin-mute maan liya jaata
            # tha, jisse bot auto-lock ho jaata tha. Ab sirf tab admin-mute
            # maano jab participant SACH MEIN muted ho.
            admin_forced = muted and (muted_by_admin or can_self_unmute is False)
            logger.debug(
                "[LiveBridge] participant upd chat=%s muted=%s by_admin=%s can_self_unmute=%s volume=%s vol_by_admin=%s",
                chat_id, muted, muted_by_admin, can_self_unmute,
                getattr(participant, "volume", None),
                getattr(participant, "volume_by_admin", None),
            )

            if admin_forced:
                if chat_id:
                    if chat_id not in self._was_admin_muted:
                        logger.info("[LiveBridge] admin-muted chat=%s (no auto hand-raise; use /handrise)", chat_id)
                    self._was_admin_muted.add(chat_id)
                return

            if chat_id and chat_id in self._was_admin_muted:
                self._was_admin_muted.discard(chat_id)
                # admin lifted the mute -> lower the hand again
                asyncio.create_task(self._lower_hand(chat_id))
        except Exception as e:
            if int(time.time()) % 30 == 0:
                logger.warning(f"[LiveBridge] participant update error: {e}")

    async def _lower_hand(self, chat_id: int) -> None:
        try:
            if self.mtproto is None:
                return
            from core.vc_mute import mtproto_raise_hand
            await mtproto_raise_hand(self.mtproto, chat_id, False)
        except Exception as e:
            logger.debug(f"[LiveBridge] lower-hand error: {e}")

    async def _forward_frames(self, update: StreamFrames) -> None:
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
        mode = bridge.get("mode")
        if mode == "solo":
            return

        source_id = int(bridge["source_id"])
        target_id = int(bridge["target_id"])

        if chat_id != source_id or target_id == source_id:
            return
        if not update.frames:
            return

        # Mix ALL speakers of the source VC into one PCM frame before pushing.
        # Old code only used frames[0] — any second (or third) speaker was
        # silently dropped, which showed up as gaps whenever multiple people
        # talked at once.  Now every active speaker's contribution is summed
        # and clipped, exactly like bridge_patch._forward_frames already did.
        try:
            arrays = []
            for fr in update.frames:
                a = np.frombuffer(bytes(fr.frame), dtype=np.int16)
                if a.size >= 2:
                    arrays.append(a)
            if not arrays:
                return
            if len(arrays) == 1:
                raw = arrays[0].tobytes()
            else:
                n = min(a.size for a in arrays)
                mixed = sum(a[:n].astype(np.int32) for a in arrays)
                raw = np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return
        self._frame_queue.push(chat_id, target_id, raw)

    def _process_frame_sync(self, chat_id: int, raw: bytes):
        """Runs on the FrameQueue consumer task: pure EQ, NO network I/O.
        Kept synchronous and fast on purpose — the network write
        (`_send_pcm`) happens right after, awaited separately, so one
        slow send never stalls the EQ for the next queued frame either."""
        samples = np.frombuffer(raw, dtype=np.int16).copy()
        if samples.size < 2:
            return None
        out = self.eq.process_int16(samples)
        if out is None or out.size < 2:
            return None
        return out.tobytes()

    async def _send_pcm(self, target_id: int, pcm: bytes) -> None:
        """Runs on the FrameQueue consumer task, after `_process_frame_sync`.
        Only network I/O lives here: screenshare live-mux first, else the
        normal send_frame() call."""
        try:
            try:
                from core.screenshare import get_screenshare_manager
                ss = get_screenshare_manager()
                if ss is not None and ss.has_live_mux(target_id):
                    if ss.push_live_audio(target_id, pcm):
                        self._fwd_count += 1
                        return
            except Exception:
                pass

            await self.client.send_frame(target_id, Device.MICROPHONE, pcm)
            self._fwd_count += 1
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("external source", "notincall", "not in a call", "connectionnotfound")):
                return
            if self._fwd_count < 5 or int(time.time()) % 10 == 0:
                logger.warning("[LiveBridge] forward fail: %s", e)

    def _is_already_joined_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return any(
            x in msg
            for x in (
                "already", "alreadyparticipant", "already joined",
                "alreadyjoined", "call already", "already in",
            )
        )

    async def _ensure_mtproto(self) -> bool:
        try:
            from core.mtproto_patch import patch_client_is_connected, patch_pytgcalls_binding
        except Exception:
            patch_client_is_connected = None
            patch_pytgcalls_binding = None

        app = self.mtproto
        if app is None:
            app = getattr(self.client, "_app", None) or getattr(self.client, "app", None)
            if app is not None and hasattr(app, "client"):
                app = getattr(app, "client", app) or app

        if app is None:
            if patch_pytgcalls_binding:
                try:
                    patch_pytgcalls_binding(self.client)
                except Exception:
                    pass
            return True

        try:
            try:
                if not getattr(app, "is_connected", True):
                    await app.connect()
            except Exception:
                pass
            await app.get_me()
            if patch_client_is_connected:
                patch_client_is_connected(app)
            if patch_pytgcalls_binding:
                patch_pytgcalls_binding(self.client)
            return True
        except Exception as e:
            logger.warning(f"[LiveBridge] mtproto reconnect: {e}")
            try:
                try:
                    await app.stop()
                except Exception:
                    pass
                await asyncio.sleep(0.25)
                await app.start()
                await app.get_me()
                if patch_client_is_connected:
                    patch_client_is_connected(app)
                if patch_pytgcalls_binding:
                    patch_pytgcalls_binding(self.client)
                return True
            except Exception as e2:
                logger.error(f"[LiveBridge] mtproto dead: {e2}")
                self._last_join_error = f"MTProto client not connected: {e2}"
                return False

    async def _play_external_audio(self, chat_id: int) -> None:
        media_state.clear_audio(chat_id)  # live mic wapas => file audio khatam
        stream = MediaStream(ExternalMedia.AUDIO, AUDIO_PARAMETERS)
        try:
            from pytgcalls.types import GroupCallConfig
            await self.client.play(chat_id, stream, GroupCallConfig(auto_start=True))
            return
        except TypeError:
            pass
        except Exception as e:
            if self._is_already_joined_error(e):
                return
            logger.debug(f"[LiveBridge] play+config chat={chat_id}: {e}")
        await self.client.play(chat_id, stream)

    def _is_transient_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return any(
            x in msg
            for x in (
                "timeout", "timed out", "flood", "wait", "retry", "temporary",
                "network", "connection", "not connected", "disconnected",
                "transport", "server", "busy", "try again",
            )
        )

    async def _join_live(self, chat_id: int, record: bool = True) -> Tuple[bool, str]:
        if not await self._ensure_mtproto():
            await asyncio.sleep(0.2)
            if not await self._ensure_mtproto():
                return False, self._last_join_error or "MTProto client not connected"

        self.bind_client(self.client, self.mtproto)

        play_ok = False
        play_err = ""
        record_ok = not record
        record_err = ""

        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                if attempt > 0:
                    await self._ensure_mtproto()
                    await asyncio.sleep(0.25 + attempt * 0.1)
                await self._play_external_audio(chat_id)
                play_ok = True
                play_err = ""
                break
            except Exception as e:
                if self._is_already_joined_error(e):
                    play_ok = True
                    play_err = ""
                    break
                play_err = str(e)[:200]
                logger.warning(
                    f"[LiveBridge] play chat={chat_id} attempt={attempt+1}/{max_attempts}: {e}"
                )
                await asyncio.sleep(0.3 if self._is_transient_error(e) else 0.45)

        if not play_ok:
            self._last_join_error = play_err or "play() failed"
            logger.error(f"[LiveBridge] join FAIL chat={chat_id}: {self._last_join_error}")
            return False, self._last_join_error

        if record:
            for attempt in range(4):
                try:
                    await self.client.record(
                        chat_id,
                        RecordStream(True, AUDIO_PARAMETERS),
                    )
                    record_ok = True
                    logger.info(f"[LiveBridge] RECORD on chat={chat_id}")
                    break
                except Exception as e:
                    if self._is_already_joined_error(e):
                        record_ok = True
                        break
                    record_err = str(e)[:150]
                    await asyncio.sleep(0.2 + attempt * 0.1)
            if not record_ok:
                self._last_join_error = f"Source record failed: {record_err}"
                logger.error(f"[LiveBridge] RECORD FAIL chat={chat_id}: {record_err}")
                return False, self._last_join_error

        try:
            await self._try_call_mute(chat_id, muted=bool(self.config.get("muted", False)))
        except Exception:
            pass

        logger.info(f"[LiveBridge] joined VC chat={chat_id} record={record and record_ok}")
        return True, ""

    async def _try_call_mute(self, chat_id: int, muted: bool) -> None:
        if muted:
            names = ("mute", "mute_stream", "set_is_mute")
        else:
            names = ("unmute", "unmute_stream", "resume", "set_is_mute")

        for name in names:
            fn = getattr(self.client, name, None)
            if not callable(fn):
                continue
            try:
                if name == "set_is_mute":
                    await fn(muted)
                else:
                    await fn(chat_id)
                break
            except TypeError:
                try:
                    if name == "set_is_mute":
                        await fn(chat_id, muted)
                    else:
                        await fn()
                    break
                except Exception:
                    pass
            except Exception:
                continue

        # The pytgcalls call above only (un)mutes OUR OWN outgoing audio.
        # It does not touch Telegram's server-side "muted" flag on our
        # participant row, which is what actually drives the mic icon
        # everyone sees in the Video Chat participant list. Set that flag
        # too so the icon updates instead of staying stuck on the old
        # state. Never breaks anything — purely additive, errors swallowed.
        if self.mtproto is not None:
            try:
                from core.vc_mute import mtproto_set_muted
                await mtproto_set_muted(self.mtproto, chat_id, muted)
            except Exception as e:
                logger.debug("[LiveBridge] vc_mute icon-fix %s: %s", chat_id, e)

    async def _hard_leave(self, chat_id: int) -> None:
        media_state.clear_audio(chat_id)
        for _ in range(3):
            try:
                await self.client.leave_call(chat_id)
                break
            except Exception:
                await asyncio.sleep(0.08)
        # PyTgCalls' own leave_call() can succeed locally while Telegram's
        # server-side participant row survives (e.g. after a rebind_pytgcalls()
        # left a stale PyTgCalls object holding the real join). Always also
        # run the corrected raw-MTProto leave (real SSRC, looked up fresh from
        # Telegram) so stop_bridge/stop_all_bridges get the same fix as
        # /leaveplay instead of only relying on the library call.
        if self.mtproto is not None:
            try:
                from core.vc_leave import mtproto_leave_vc
                await mtproto_leave_vc(self.mtproto, chat_id)
            except Exception as e:
                logger.debug("[LiveBridge] vc_leave fallback %s: %s", chat_id, e)

    def _resolve_route_chats(self, chat_id: int) -> Set[int]:
        chats: Set[int] = set()
        info = self.active_bridges.get(chat_id)
        if info:
            for k in ("source_id", "target_id"):
                v = info.get(k)
                if v is not None:
                    chats.add(int(v))
        for cid, binfo in list(self.active_bridges.items()):
            sid = binfo.get("source_id")
            tid = binfo.get("target_id")
            if cid == chat_id or sid == chat_id or tid == chat_id:
                chats.add(int(cid))
                if sid is not None:
                    chats.add(int(sid))
                if tid is not None:
                    chats.add(int(tid))
        if not chats:
            chats.add(int(chat_id))
        return chats

    def _clear_route_only(self, *chat_ids: int) -> None:
        for cid in chat_ids:
            info = self.active_bridges.pop(cid, None)
            if not info:
                continue
            for k in ("source_id", "target_id"):
                v = info.get(k)
                if v is not None:
                    self.active_bridges.pop(v, None)

    async def _leave_only_if_not_needed(self, keep: Set[int]) -> None:
        to_leave: Set[int] = set()
        for cid, info in list(self.active_bridges.items()):
            for x in (cid, info.get("source_id"), info.get("target_id")):
                if x is not None and x not in keep:
                    to_leave.add(x)
        for cid in to_leave:
            await self._hard_leave(cid)
            self.active_bridges.pop(cid, None)
            self._was_admin_muted.discard(cid)

    async def join_vc_only(self, chat_id: int) -> bool:
        try:
            existing = self.active_bridges.get(chat_id)
            if existing and existing.get("mode") == "oneway":
                ok, err = await self._join_live(
                    chat_id, record=(int(existing.get("source_id") or 0) == chat_id)
                )
                return ok

            ok, err = await self._join_live(chat_id, record=True)
            if ok:
                self.active_bridges[chat_id] = {
                    "source_id": chat_id,
                    "target_id": chat_id,
                    "mode": "solo",
                    "started_at": time.time(),
                    **dict(self.config),
                }
            else:
                self._last_join_error = err or "join_vc_only failed"
            return ok
        except Exception as e:
            self._last_join_error = str(e)[:200]
            logger.error(f"join_vc_only error: {e}")
            return False

    async def start_bridge(
        self,
        source_id: int,
        target_id: int,
        audio_source: Optional[str] = None,
    ) -> Tuple[bool, str]:
        try:
            self.config["muted"] = False
            self.eq.muted = False
            self._was_admin_muted.clear()
            self.sync_effects()
            self._save_muted_config()
            self.bind_client(self.client, self.mtproto)

            existing = self.active_bridges.get(source_id) or self.active_bridges.get(target_id)
            if existing and int(existing.get("source_id") or 0) == source_id and int(existing.get("target_id") or 0) == target_id:
                logger.info("[LiveBridge] same route — soft refresh")
            else:
                keep = {source_id, target_id}
                await self._leave_only_if_not_needed(keep)
                self._clear_route_only(source_id, target_id)

            await self._ensure_mtproto()

            ok_src, err_src = await self._join_live(source_id, record=True)
            ok_tgt, err_tgt = True, ""
            if target_id != source_id:
                ok_tgt, err_tgt = await self._join_live(target_id, record=False)

            if not ok_src:
                await asyncio.sleep(0.35)
                await self._ensure_mtproto()
                ok_src, err_src = await self._join_live(source_id, record=True)

            if target_id != source_id and not ok_tgt:
                await asyncio.sleep(0.35)
                await self._ensure_mtproto()
                ok_tgt, err_tgt = await self._join_live(target_id, record=False)

            if not ok_src:
                msg = f"Source VC fail: {err_src or 'unknown'}"
                self._last_join_error = msg
                return False, msg

            if target_id != source_id and not ok_tgt:
                msg = f"Target VC fail: {err_tgt or 'unknown'}"
                self._last_join_error = msg
                return False, msg

            await asyncio.gather(
                self._try_call_mute(source_id, muted=False),
                self._try_call_mute(target_id, muted=False)
                if target_id != source_id
                else asyncio.sleep(0),
            )

            info = {
                "source_id": source_id,
                "target_id": target_id,
                "audio_source": audio_source,
                "started_at": time.time(),
                "mode": "oneway",
                **dict(self.config),
            }
            info["muted"] = False
            self.active_bridges[source_id] = info
            if target_id != source_id:
                self.active_bridges[target_id] = info

            self.sync_effects()
            set_live_bridge(self)
            logger.info(f"[LiveBridge] LIVE {source_id} → {target_id}")
            return True, ""
        except Exception as e:
            logger.exception(f"start_bridge error: {e}")
            self._last_join_error = str(e)[:200]
            return False, str(e)[:200]

    async def restore_source_capture(self, source_id: int, target_id: int = None) -> bool:
        try:
            await self._ensure_mtproto()
            self.bind_client(self.client, self.mtproto)
            await self._play_external_audio(source_id)
            try:
                await self.client.record(source_id, RecordStream(True, AUDIO_PARAMETERS))
            except Exception as e:
                if not self._is_already_joined_error(e):
                    logger.warning("[LiveBridge] record re-arm: %s", e)
            existing = self.active_bridges.get(source_id) or {}
            tgt = int(target_id if target_id is not None else existing.get("target_id") or source_id)
            info = {
                "source_id": source_id,
                "target_id": tgt,
                "started_at": existing.get("started_at") or time.time(),
                "mode": "oneway",
                **dict(self.config),
            }
            info["muted"] = bool(self.config.get("muted", False))
            self.sync_effects()          # mute state PRESERVED (rejoin must not unmute)
            self.active_bridges[source_id] = info
            if tgt != source_id:
                self.active_bridges[tgt] = info
            set_live_bridge(self)
            return True
        except Exception as e:
            logger.error("[LiveBridge] restore_source_capture: %s", e)
            return False

    async def restore_live_route(self, source_id: int, target_id: int) -> bool:
        try:
            await self._ensure_mtproto()
            self.bind_client(self.client, self.mtproto)
            ok_s, _ = await self._join_live(source_id, record=True)
            ok_t = True
            if target_id != source_id:
                ok_t, _ = await self._join_live(target_id, record=False)
            info = {
                "source_id": source_id,
                "target_id": target_id,
                "started_at": time.time(),
                "mode": "oneway",
                **dict(self.config),
            }
            info["muted"] = bool(self.config.get("muted", False))
            self.sync_effects()          # mute state PRESERVED (rejoin must not unmute)
            self.active_bridges[source_id] = info
            if target_id != source_id:
                self.active_bridges[target_id] = info
            set_live_bridge(self)
            logger.info("[LiveBridge] route restored %s → %s", source_id, target_id)
            return bool(ok_s and ok_t)
        except Exception as e:
            logger.error("[LiveBridge] restore_live_route: %s", e)
            return False

    async def update_effects(self, chat_id: int, **kwargs) -> bool:
        for key, value in kwargs.items():
            if key in ("gain", "loudness", "bass", "treble", "clarity", "echo", "pitch", "muted"):
                self.config[key] = value
                for b in self.active_bridges.values():
                    if key in b:
                        b[key] = value
        self.sync_effects()
        self._save_muted_config()
        return True

    async def set_muted(self, muted: bool) -> bool:
        async with self._mute_lock:
            chats: Set[int] = set()
            for b in list(self.active_bridges.values()):
                chats.add(int(b["source_id"]))
                chats.add(int(b["target_id"]))
            self.config["muted"] = bool(muted)
            self.eq.muted = bool(muted)
            self.sync_effects()
            self._save_muted_config()
            if not muted:
                self._was_admin_muted.clear()
            for cid in chats:
                await self._try_call_mute(cid, muted=bool(muted))
            if not muted:
                await asyncio.sleep(0.1)
                for cid in chats:
                    await self._try_call_mute(cid, muted=False)
                self.config["muted"] = False
                self.eq.muted = False
                self.sync_effects()

                # Ground-truth check: Telegram can silently keep our
                # server-side "muted" flag ON (we were force-muted by an
                # admin, can_self_unmute=False) even though the unmute
                # call above raised no error — that's exactly why the
                # mic icon looked stuck. Leaving + rejoining the call
                # clears that per-session restriction (the same fix as
                # doing /leave + /join by hand), so do it automatically.
                if self.mtproto is not None:
                    from core.vc_mute import get_self_call_state
                    for cid in chats:
                        try:
                            still_muted, can_self = await get_self_call_state(self.mtproto, cid)
                            if still_muted:
                                # Telegram thoda late update karta hai — ek baar
                                # ruk kar dobara check karo, hadbadi mein rejoin nahi.
                                await asyncio.sleep(0.6)
                                await self._try_call_mute(cid, muted=False)
                                await asyncio.sleep(0.4)
                                still_muted, can_self = await get_self_call_state(self.mtproto, cid)
                        except Exception as e:
                            logger.debug("[LiveBridge] unmute verify %s: %s", cid, e)
                            continue
                        # Rejoin SIRF tab jab admin ne sach mein lock kiya ho
                        # (can_self_unmute == False). Warna VC mein bane raho.
                        if not (still_muted and can_self is False):
                            continue
                        logger.info(
                            "[LiveBridge] chat=%s admin-restricted (can_self_unmute=False) "
                            "after /unmute — rejoining once to clear it", cid,
                        )
                        info = self.active_bridges.get(cid) or {}
                        record = int(info.get("source_id") or cid) == cid
                        try:
                            await self._hard_leave(cid)
                            await asyncio.sleep(0.15)
                            await self._join_live(cid, record=record)
                            await self._try_call_mute(cid, muted=False)
                        except Exception as e:
                            logger.debug("[LiveBridge] unmute-rejoin %s: %s", cid, e)
            return True

    async def stop_bridge(self, chat_id: int) -> bool:
        chats = self._resolve_route_chats(chat_id)
        left_any = False
        for cid in list(chats):
            await self._hard_leave(cid)
            left_any = True
            self._was_admin_muted.discard(cid)
            self.active_bridges.pop(cid, None)
        for cid, info in list(self.active_bridges.items()):
            sid = info.get("source_id")
            tid = info.get("target_id")
            if sid in chats or tid in chats or cid in chats:
                await self._hard_leave(cid)
                self.active_bridges.pop(cid, None)
                self._was_admin_muted.discard(cid)
        return left_any

    async def stop_all_bridges(self) -> int:
        ids: Set[int] = set()
        for b in list(self.active_bridges.values()):
            ids.add(b["source_id"])
            ids.add(b["target_id"])
        count = 0
        for sid in list(ids):
            if await self.stop_bridge(sid):
                count += 1
        for cid in list(self.active_bridges.keys()):
            await self._hard_leave(cid)
            self.active_bridges.pop(cid, None)
        return count

    def get_bridge_count(self) -> int:
        seen = set()
        for b in self.active_bridges.values():
            seen.add((b["source_id"], b["target_id"]))
        return len(seen)

    def get_bridge_info(self, chat_id: int) -> Optional[dict]:
        return self.active_bridges.get(chat_id)

    def get_last_error(self) -> str:
        return self._last_join_error or ""

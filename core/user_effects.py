"""Sudo users ki personal effects settings (user_id -> config dict).

Owner ki config pehle ki tarah bridge_config.json mein rehti hai (main.py load karta hai).
"""
import json
import logging
from pathlib import Path
from typing import Dict

from core.effects import AudioEffects

logger = logging.getLogger(__name__)

_FILE = Path("user_effects.json")
_CFGS: Dict[int, dict] = {}
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8") or "{}")
            for k, v in data.items():
                cfg = AudioEffects.merge_config(v or {})
                cfg.setdefault("muted", False)
                _CFGS[int(k)] = cfg
    except Exception as e:
        logger.warning(f"user_effects load fail: {e}")


def get_user_config(user_id: int) -> dict:
    """Is user ki apni config (nahi hai to default banti hai)."""
    _load()
    uid = int(user_id)
    cfg = _CFGS.get(uid)
    if cfg is None:
        cfg = AudioEffects.merge_config({})
        cfg.setdefault("muted", False)
        _CFGS[uid] = cfg
    return cfg


def save_user_configs() -> None:
    try:
        _FILE.write_text(
            json.dumps({str(k): v for k, v in _CFGS.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"user_effects save fail: {e}")

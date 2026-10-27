"""Runtime variables: runtime_vars.json wins over environment variables.

Used for RECORD_GROUP / LOGGER_GROUP (and later the /set... commands), so a value
changed from Telegram survives restarts without touching the host's env panel.
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_FILE = Path("runtime_vars.json")


def _read() -> dict:
    try:
        if _FILE.exists():
            return json.loads(_FILE.read_text(encoding="utf-8") or "{}")
    except Exception as e:
        logger.warning("runtime_vars read fail: %s", e)
    return {}


def get_var(name: str, default: str = "") -> str:
    v = _read().get(name)
    if v in (None, ""):
        v = os.environ.get(name, "")
    v = str(v).strip() if v is not None else ""
    return v or default


def set_var(name: str, value) -> None:
    data = _read()
    data[name] = value
    try:
        _FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning("runtime_vars write fail: %s", e)


def get_int(name: str) -> Optional[int]:
    v = get_var(name)
    try:
        return int(v) if v else None
    except ValueError:
        return None

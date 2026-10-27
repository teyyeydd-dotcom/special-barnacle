"""Sudo users ke personal assistant sessions (user_id -> session string).

Owner ka session pehle ki tarah assistant_session.txt / .env / DB se load hota hai.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_FILE = Path("user_sessions.json")


def load_user_sessions() -> Dict[int, str]:
    try:
        if _FILE.exists():
            raw = _FILE.read_text(encoding="utf-8").strip() or "{}"
            data = json.loads(raw)
            return {int(k): str(v) for k, v in data.items() if v}
    except Exception as e:
        logger.warning(f"user_sessions load fail: {e}")
    return {}


def _write(data: Dict[int, str]) -> None:
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({str(k): v for k, v in data.items()}),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(_FILE)


def save_user_session(user_id: int, session_string: str) -> None:
    data = load_user_sessions()
    data[int(user_id)] = session_string
    _write(data)


def remove_user_session(user_id: int) -> bool:
    data = load_user_sessions()
    if int(user_id) in data:
        del data[int(user_id)]
        _write(data)
        return True
    return False

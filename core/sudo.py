"""
Access control.

Owner = OWNER_ID only.
Sudo  = owner + SUDO_USERS from config (env, fixed) + dynamic_sudo.json
        (added/removed at runtime via /sudoadd /sudodel, owner only).
        An env-listed ID can also be blocked at runtime via /sudodel —
        that goes into removed_env_sudo.json as an override, without
        touching the .env file. /sudoadd on that same ID clears the
        override again. Every sudo user gets his own assistant session
        (max 1 per user).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

MAX_ASSISTANTS = 1
OWNER_ID = 8841848847

logger = logging.getLogger(__name__)
_DYN_FILE = Path("dynamic_sudo.json")
_REMOVED_ENV_FILE = Path("removed_env_sudo.json")


def _read_json_list(path: Path) -> List[int]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "[]")
            return [int(x) for x in data if str(x).strip()]
    except Exception as e:
        logger.warning("%s read fail: %s", path, e)
    return []


def _write_json_list(path: Path, ids: List[int]) -> None:
    try:
        path.write_text(json.dumps(sorted(set(ids)), indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("%s write fail: %s", path, e)


def _read_dynamic() -> List[int]:
    return _read_json_list(_DYN_FILE)


def _write_dynamic(ids: List[int]) -> None:
    _write_json_list(_DYN_FILE, ids)


def _read_removed_env() -> List[int]:
    return _read_json_list(_REMOVED_ENV_FILE)


def _write_removed_env(ids: List[int]) -> None:
    _write_json_list(_REMOVED_ENV_FILE, ids)


def add_sudo(user_id: int) -> bool:
    """Returns False if already sudo (env or dynamic), True if newly added.
    If this id had been blocked via /sudodel while it was an env id, that
    block is lifted here too."""
    uid = int(user_id)
    removed = _read_removed_env()
    if uid in removed:
        _write_removed_env([x for x in removed if x != uid])
    ids = _read_dynamic()
    if uid in set(_env_owners()) or uid in ids:
        return False
    ids.append(uid)
    _write_dynamic(ids)
    return True


def remove_sudo(user_id: int) -> bool:
    """Removes from the dynamic (runtime-added) list, OR, if the id comes
    from the env SUDO_USERS list, blocks it via a runtime override instead
    (removed_env_sudo.json) — no .env edit needed. Returns False only if
    the id wasn't sudo at all (owner is handled separately by the caller)."""
    uid = int(user_id)
    ids = _read_dynamic()
    if uid in ids:
        ids = [x for x in ids if x != uid]
        _write_dynamic(ids)
        return True
    if uid in set(_env_owners()):
        removed = _read_removed_env()
        if uid not in removed:
            removed.append(uid)
            _write_removed_env(removed)
        return True
    return False


def list_dynamic_sudo() -> List[int]:
    return _read_dynamic()


def list_removed_env_sudo() -> List[int]:
    return _read_removed_env()


def _env_owners() -> List[int]:
    """Owner + SUDO_USERS (sudo list)."""
    owners: List[int] = [OWNER_ID]
    try:
        from config.settings import SUDO_USERS

        for x in SUDO_USERS or []:
            try:
                uid = int(x)
                if uid and uid not in owners:
                    owners.append(uid)
            except Exception:
                pass
    except Exception:
        pass
    return owners


def is_owner(user_id: int) -> bool:
    """Strictly the owner. Sudo users are NOT owners."""
    try:
        return int(user_id) == OWNER_ID
    except Exception:
        return False


def is_sudo(user_id: int) -> bool:
    """Owner, anyone in SUDO_USERS (env) not runtime-blocked, or anyone
    added via /sudoadd."""
    try:
        uid = int(user_id)
    except Exception:
        return False
    if uid == OWNER_ID:
        return True
    if uid in set(_read_dynamic()):
        return True
    if uid in set(_env_owners()) and uid not in set(_read_removed_env()):
        return True
    return False


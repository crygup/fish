from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from .emojis import *
from .paths import FILES_ROOT

USER_BADGES_SEED_PATH = FILES_ROOT / "data" / "user_badges.json"
# Production mounts the source tree read-only. Operators can point this at a
# writable persistent mount while local runs continue using the checked-in
# catalog directly.
USER_BADGES_PATH = Path(
    os.getenv("FISHIE_USER_BADGES_PATH", str(USER_BADGES_SEED_PATH))
)


def load_user_badges_document() -> dict[str, Any]:
    """Load the editable user badge catalog from disk.

    The catalog is deliberately kept outside Python source so owner-managed
    badges can be edited without changing the code.  A malformed or missing
    catalog is treated as empty and can be repaired by the owner badge tools.
    """

    path = USER_BADGES_PATH
    if not path.exists() and USER_BADGES_SEED_PATH != path:
        path = USER_BADGES_SEED_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"flags": {}, "users": {}}
    if not isinstance(payload, dict):
        return {"flags": {}, "users": {}}
    flags = payload.get("flags")
    users = payload.get("users")
    return {
        "flags": dict(flags) if isinstance(flags, dict) else {},
        "users": dict(users) if isinstance(users, dict) else {},
    }


def render_user_badge(entry: object) -> str:
    """Render one badge entry for user-facing output.

    Badge data is editable JSON, so this function deliberately treats every
    value as untrusted.  A malformed emoji ID should not prevent the bot from
    importing its settings or rendering an otherwise valid profile.
    """

    if not isinstance(entry, dict):
        return ""
    emoji_name = str(entry.get("emoji_name") or "")
    emoji_id = entry.get("emoji_id")
    if entry.get("is_custom") and emoji_id is not None and emoji_name:
        try:
            parsed_id = int(emoji_id)
        except (TypeError, ValueError, OverflowError):
            return ""
        if not 0 < parsed_id <= 9_223_372_036_854_775_807 or not (
            1 <= len(emoji_name) <= 32
            and all(
                character.isascii() and (character.isalnum() or character in "_~")
                for character in emoji_name
            )
        ):
            return ""
        animated = "a" if entry.get("animated") else ""
        emoji_name = f"<{animated}:{emoji_name}:{parsed_id}>"
    elif entry.get("is_custom"):
        return ""
    text = discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(entry.get("text") or "").strip())
    )
    return f"{emoji_name} {text}".strip()


# Kept as a private alias for older extensions that imported the helper while
# the JSON-backed badge catalog was being introduced.
_render_user_badge = render_user_badge


def _flatten_user_badges(document: dict[str, Any]) -> dict[object, str]:
    result: dict[object, str] = {}
    flags = document.get("flags", {})
    for key, entry in flags.items():
        rendered = render_user_badge(entry)
        if rendered:
            result[str(key)] = rendered
    users = document.get("users", {})
    for key, entry in users.items():
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            continue
        rendered = render_user_badge(entry)
        if rendered:
            result[user_id] = rendered
    return result


USER_BADGES_DOCUMENT: dict[str, Any] = {"flags": {}, "users": {}}
USER_FLAGS: dict[object, str] = {}
_USER_BADGES_MTIME_NS: int | None = None


def reload_user_badges() -> dict[str, Any]:
    """Refresh the in-memory compatibility map after a file edit."""

    global _USER_BADGES_MTIME_NS
    document = load_user_badges_document()
    USER_BADGES_DOCUMENT.clear()
    USER_BADGES_DOCUMENT.update(document)
    USER_FLAGS.clear()
    USER_FLAGS.update(_flatten_user_badges(document))
    try:
        _USER_BADGES_MTIME_NS = USER_BADGES_PATH.stat().st_mtime_ns
    except OSError:
        _USER_BADGES_MTIME_NS = None
    return USER_BADGES_DOCUMENT


def refresh_user_badges() -> dict[str, Any]:
    """Reload the catalog when an operator edits the JSON file in place."""

    try:
        mtime_ns = USER_BADGES_PATH.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    if mtime_ns != _USER_BADGES_MTIME_NS:
        return reload_user_badges()
    return USER_BADGES_DOCUMENT


def save_user_badges_document(document: dict[str, Any]) -> None:
    """Atomically write the editable badge catalog and refresh its cache."""

    normalized = {
        "flags": dict(document.get("flags", {})),
        "users": dict(document.get("users", {})),
    }
    USER_BADGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = USER_BADGES_PATH.with_name(
        f".{USER_BADGES_PATH.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(USER_BADGES_PATH)
    USER_BADGES_DOCUMENT.clear()
    USER_BADGES_DOCUMENT.update(normalized)
    USER_FLAGS.clear()
    USER_FLAGS.update(_flatten_user_badges(normalized))
    global _USER_BADGES_MTIME_NS
    try:
        _USER_BADGES_MTIME_NS = USER_BADGES_PATH.stat().st_mtime_ns
    except OSError:
        _USER_BADGES_MTIME_NS = None


def get_user_badge(user_id: int) -> dict[str, Any] | None:
    """Return the editable badge for a user, reloading a changed JSON file."""

    document = refresh_user_badges()
    entry = document.get("users", {}).get(str(int(user_id)))
    return dict(entry) if isinstance(entry, dict) else None


def set_user_badge(
    user_id: int,
    *,
    emoji_name: str,
    emoji_id: int | None,
    is_custom: bool,
    text: str,
    animated: bool = False,
) -> None:
    """Persist the current user badge in the editable catalog."""

    document = refresh_user_badges()
    users = dict(document.get("users", {}))
    users[str(int(user_id))] = {
        "emoji_name": emoji_name,
        "emoji_id": emoji_id,
        "is_custom": bool(is_custom),
        "animated": bool(animated) if is_custom else False,
        "text": text,
    }
    save_user_badges_document({"flags": document.get("flags", {}), "users": users})


def remove_user_badge(user_id: int) -> None:
    """Remove a user's custom badge from the editable catalog."""

    document = refresh_user_badges()
    users = dict(document.get("users", {}))
    users.pop(str(int(user_id)), None)
    save_user_badges_document({"flags": document.get("flags", {}), "users": users})


reload_user_badges()

lastfm_period = {
    "overall": "overall",
    "7day": "weekly",
    "1month": "monthly",
    "3month": "quarterly",
    "6month": "half-yearly",
    "12month": "yearly",
}

base_header = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
}


@dataclass()
class GoogleImageData:
    image_url: str
    url: str
    snippet: str
    query: str
    author: discord.User | discord.Member


@dataclass()
class SpotifySearchData:
    track: str
    album: str
    artist: str


@dataclass()
class ReviewSender:
    user_id: int
    profilePhoto: str
    username: str


@dataclass()
class Review:
    id: int
    sender: ReviewSender
    comment: str
    timestamp: int
    target_id: int

from __future__ import annotations

import asyncio
import html
import math
import re
from collections import Counter
from io import BytesIO
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from core import Cog
from utils import to_thread
from utils.credentials import decrypt_credential

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 8_192


async def _read_remote_image(response: aiohttp.ClientResponse) -> bytes | None:
    if response.content_length and response.content_length > MAX_REMOTE_IMAGE_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_REMOTE_IMAGE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


ANILIST_MEDIA_RE = re.compile(
    r"(?P<kind>img\d*|webm|youtube)\((?P<url>https?://[^\s)<>]+)\)",
    re.IGNORECASE,
)
ANILIST_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s<>\)]+)\)")
ANILIST_SPOILER_RE = re.compile(r"~!(.*?)!~", re.DOTALL)
CHARACTER_HEIGHT_RE = re.compile(
    r"(?im)^[ \t]*(?:\*\*|__)?height(?:\*\*|__)?[ \t]*:[ \t]*(.+?)[ \t]*(?=\n|$)"
)
CHARACTER_METADATA_RE = re.compile(
    r"(?im)^[ \t]*(?P<spoiler>~!)?(?P<marker>\*\*|__)(?P<label>[^*_\n:]+):(?P=marker)"
    r"[ \t]*(?P<value>.*?)[ \t]*$"
)
MEDIA_DESCRIPTION_SOURCE_RE = re.compile(
    r"(?im)^[ \t]*\(?[ \t]*(?:\*\*|__)?"
    r"(?:description[ \t]+)?source(?:\*\*|__)?[ \t]*:[ \t]*"
    r"(?P<source>[^)\n]+?)[ \t]*\)?[ \t]*(?=\n|$)"
)
MEDIA_DESCRIPTION_NOTES_RE = re.compile(
    r"(?ims)(?:^|\n)[ \t]*(?:#{1,6}[ \t]*)?"
    r"(?:\*\*|__)?notes?(?:\*\*|__)?[ \t]*:[ \t]*.*$"
)
MEDIA_DESCRIPTION_SPECIAL_EPISODES_RE = re.compile(
    r"(?ims)(?:^|\n)[ \t]*(?:[*_~!]+[ \t]*)?"
    r"(?:this includes the following )?special episodes[ \t]*:[ \t]*.*$"
)
DESCRIPTION_PREVIEW_LIMIT = 650
ANILIST_PROFILE_QUERY = """
query ($name: String) {
  User(name: $name) {
    id
    name
    siteUrl
    about
    avatar { large }
    statistics {
      anime {
        count
        episodesWatched
        minutesWatched
        meanScore
      }
      manga {
        count
        chaptersRead
        volumesRead
        meanScore
      }
    }
    favourites {
      anime {
        nodes {
          siteUrl
          isAdult
          coverImage { extraLarge }
          title { romaji }
        }
      }
      manga {
        nodes {
          siteUrl
          isAdult
          coverImage { extraLarge }
          title { romaji }
        }
      }
      characters {
        nodes {
          siteUrl
          image { large }
          name { full }
          media(perPage: 6) { nodes { isAdult } }
        }
      }
    }
  }
}
"""
ANILIST_MEDIA_QUERY = """
query ($search: String!, $type: MediaType!) {
  Page(perPage: 1) {
    media(search: $search, type: $type, sort: SEARCH_MATCH) {
      id
      type
      siteUrl
      title { romaji english native userPreferred }
      description
      format
      status
      episodes
      chapters
      volumes
      duration
      nextAiringEpisode { airingAt episode }
      season
      seasonYear
      averageScore
      genres
      isAdult
      coverImage { extraLarge }
      isFavourite
      mediaListEntry { id status progress score }
      relations {
        edges {
          relationType
          node {
            id
            type
            title { romaji english native userPreferred }
          }
        }
      }
    }
  }
}
"""
ANILIST_MEDIA_BY_ID_QUERY = """
query ($id: Int!) {
  Media(id: $id) {
    id
    type
    siteUrl
    title { romaji english native userPreferred }
    description
    format
    status
    episodes
    chapters
    volumes
    duration
    nextAiringEpisode { airingAt episode }
    season
    seasonYear
    averageScore
    genres
    isAdult
    coverImage { extraLarge }
    isFavourite
    mediaListEntry { id status progress score }
    relations {
      edges {
        relationType
        node {
          id
          type
          title { romaji english native userPreferred }
        }
      }
    }
  }
}
"""
ANILIST_CURRENT_LIST_QUERY = """
query ($userName: String!, $type: MediaType!, $statuses: [MediaListStatus!]!) {
  MediaListCollection(
    userName: $userName
    type: $type
    status_in: $statuses
  ) {
    lists {
      entries {
        id
        mediaId
        status
        progress
        score
        repeat
        media {
          id
          siteUrl
          title { userPreferred english romaji native }
          episodes
          chapters
        }
      }
    }
  }
  Viewer { mediaListOptions { scoreFormat } }
}
"""
ANILIST_CURRENT_LIST_PUBLIC_QUERY = """
query ($userName: String!, $type: MediaType!, $statuses: [MediaListStatus!]!) {
  MediaListCollection(
    userName: $userName
    type: $type
    status_in: $statuses
  ) {
    lists {
      entries {
        id
        mediaId
        status
        progress
        score
        repeat
        media {
          id
          siteUrl
          title { userPreferred english romaji native }
          episodes
          chapters
        }
      }
    }
  }
}
"""
ANILIST_VIEWER_OPTIONS_QUERY = """
query {
  Viewer { mediaListOptions { scoreFormat } }
}
"""
ANILIST_CHARACTER_QUERY = """
query ($search: String!) {
  Page(perPage: 1) {
    characters(search: $search, sort: SEARCH_MATCH) {
      id
      siteUrl
      name { full native alternative alternativeSpoiler }
      image { large }
      description
      gender
      age
      bloodType
      dateOfBirth { year month day }
      favourites
      media(perPage: 6, sort: POPULARITY_DESC) {
        nodes {
          id
          type
          siteUrl
          title { userPreferred english romaji native }
          isAdult
        }
      }
    }
  }
}
"""
ANILIST_SAVE_MEDIA_MUTATION = """
mutation (
  $mediaId: Int!,
  $status: MediaListStatus,
  $progress: Int
) {
  SaveMediaListEntry(
    mediaId: $mediaId,
    status: $status,
    progress: $progress
  ) {
    id
    status
    progress
    score
  }
}
"""
ANILIST_SAVE_RATING_MUTATION = """
mutation ($mediaId: Int!, $score: Float) {
  SaveMediaListEntry(mediaId: $mediaId, score: $score) {
    id
    status
    progress
    score
  }
}
"""
ANILIST_TOGGLE_FAVOURITE_MUTATION = """
mutation ($animeId: Int, $mangaId: Int) {
  ToggleFavourite(animeId: $animeId, mangaId: $mangaId) {
    anime { nodes { id } }
    manga { nodes { id } }
  }
}
"""
ANILIST_LIST_STATUSES = (
    ("CURRENT", "Watching", "Currently watching this anime"),
    ("PLANNING", "Planning", "Plan to watch/read this title"),
    ("COMPLETED", "Completed", "Finished watching this anime"),
    ("PAUSED", "On Hold", "Temporarily paused"),
    ("DROPPED", "Dropped", "Stopped watching this anime"),
)
ANILIST_SMILEY_SCORE_FORMATS = {"POINT_3", "SMILEY"}
ANILIST_SCORE_LIMITS = {
    "POINT_100": 100,
    "POINT_10": 10,
    "POINT_10_DECIMAL": 10,
    "POINT_5": 5,
    "THREE_POINT": 3,
}
ANILIST_SMILEY_EMOJIS = ("😿", "🐱", "😸")
ANILIST_RATING_REMOVE_EMOJI = "\U0001f5d1\ufe0f"


def _clean_about(
    value: str | None,
    *,
    max_length: int = 900,
    fallback: str = "No profile bio provided.",
) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    text = re.sub(r"(?m)^[ \t]*\\+[ \t]*(?=\n|$)", "", text)
    text = re.sub(r"\\+(?=\s*(?:\n|$))", "", text)

    def _media_link(match: re.Match[str]) -> str:
        kind = match.group("kind").lower()
        if kind.startswith("img"):
            label = "image"
        elif kind in {"webm", "youtube"}:
            label = "video"
        else:
            label = kind
        return f"[{label}]({match.group('url')})"

    text = ANILIST_MEDIA_RE.sub(_media_link, text)
    text = ANILIST_SPOILER_RE.sub(r"||\1||", text)
    protected: list[str] = []

    def _safe_link_label(label: str) -> str:
        label = discord.utils.escape_mentions(label)
        label = re.sub(r"\\(?=(?:\*{1,2}|_{1,2}))", "", label)
        markers = list(re.finditer(r"\*\*|__|\*|_", label))
        counts = Counter(marker.group(0) for marker in markers)
        if markers and not all(count % 2 == 0 for count in counts.values()):
            return discord.utils.escape_markdown(label)

        marker_values = [marker.group(0) for marker in markers]
        for index, marker in enumerate(marker_values):
            label = label.replace(marker, f"\x01{index}\x01", 1)
        label = discord.utils.escape_markdown(label)
        for index, marker in enumerate(marker_values):
            label = label.replace(f"\x01{index}\x01", marker)
        return label

    def _protect_link(match: re.Match[str]) -> str:
        label = _safe_link_label(match.group(1))
        protected.append(f"[{label}]({match.group(2)})")
        return f"\x00{len(protected) - 1}\x00"

    text = ANILIST_LINK_RE.sub(_protect_link, text)

    def _protect_literal(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(
        r"---+|\||^[ \t]*[-*+](?=\s+)",
        _protect_literal,
        text,
        flags=re.MULTILINE,
    )
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    for index, item in enumerate(protected):
        text = text.replace(f"\x00{index}\x00", item)
    if len(text) > max_length:
        preview, _ = _description_preview(text, max_length=max_length - 3)
        if preview.count("||") % 2:
            preview, _ = _description_preview(text, max_length=max_length - 5)
            text = preview.rstrip() + "...||"
        else:
            text = preview.rstrip() + "..."
    return text or fallback


def _media_description_parts(value: str | None) -> tuple[str, str | None]:
    """Clean a media description and remove notes/source attributions."""

    fallback = "No description provided."
    if not isinstance(value, str) or not value.strip():
        return fallback, None
    normalized = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    normalized = html.unescape(re.sub(r"<[^>]+>", "", normalized)).strip()
    special_episodes_match = MEDIA_DESCRIPTION_SPECIAL_EPISODES_RE.search(normalized)
    if special_episodes_match:
        normalized = normalized[: special_episodes_match.start()].rstrip()
    notes_match = MEDIA_DESCRIPTION_NOTES_RE.search(normalized)
    if notes_match:
        normalized = normalized[: notes_match.start()].rstrip()
    source_match = MEDIA_DESCRIPTION_SOURCE_RE.search(normalized)
    if source_match:
        normalized = (
            normalized[: source_match.start()] + normalized[source_match.end() :]
        )
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    # Discord's TextDisplay limit is 4,000 characters.  Keep the complete
    # cleaned description below that limit so the visual preview can be
    # expanded without inheriting the shorter preview cutoff.
    return _clean_about(normalized, max_length=4000, fallback=fallback), None


def _description_preview(
    value: str,
    *,
    max_length: int = DESCRIPTION_PREVIEW_LIMIT,
) -> tuple[str, bool]:
    """Return a readable preview without cutting through a word or sentence."""

    if len(value) <= max_length:
        return value, False

    paragraph_matches = list(
        re.finditer(
            r"\n{2,}",
            value,
        )
    )
    section_header = re.compile(
        r"(?i)^(?:notes?|source|synopsis|relations?|characters?|staff)\s*:"
    )
    header_boundaries = [
        match.end()
        for match in paragraph_matches
        if section_header.match(value[match.end() :].lstrip())
    ]
    boundaries = [
        match.end()
        for match in re.finditer(
            r"\n+|(?<=[.!?])(?:[ \t]+)",
            value,
        )
    ]

    link_ranges = [
        (match.start(), match.end())
        for match in re.finditer(
            r"\[[^\]\n]*\]\(https?://[^\s<>\)]+\)",
            value,
        )
    ]

    def _safe_boundary(position: int) -> bool:
        preview = value[:position].rstrip()
        if not preview or preview.count("||") % 2:
            return False
        return not any(start < position < end for start, end in link_ranges)

    usable_headers = [
        position
        for position in header_boundaries
        if position <= max_length and _safe_boundary(position)
    ]
    if usable_headers:
        position = usable_headers[-1]
    else:
        usable_paragraphs = [
            position
            for position in (match.end() for match in paragraph_matches)
            if position <= max_length and _safe_boundary(position)
        ]
        if usable_paragraphs:
            position = usable_paragraphs[-1]
        else:
            usable = [
                position
                for position in boundaries
                if position <= max_length and _safe_boundary(position)
            ]
            if usable:
                position = usable[-1]
            else:
                whitespace = [
                    match.start()
                    for match in re.finditer(r"\s+", value[: max_length + 1])
                    if 0 < match.start() <= max_length and _safe_boundary(match.start())
                ]
                if whitespace:
                    position = whitespace[-1]
                else:
                    position = max_length
                    containing_link = next(
                        (start for start, end in link_ranges if start < position < end),
                        None,
                    )
                    if containing_link is not None:
                        position = containing_link

    preview = value[:position].rstrip()
    return preview, True


def _character_age_value(value: object) -> str | None:
    if value is None:
        return None
    age = str(value).strip()
    age = re.sub(r"\s*-$", "", age)
    return age or None


def _number(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _anime_title(media: dict[str, Any]) -> str:
    title = media.get("title")
    if not isinstance(title, dict):
        return "Unknown anime"
    for key in ("userPreferred", "english", "romaji", "native"):
        value = title.get(key)
        if value:
            return str(value)
    return "Unknown anime"


def _media_relation_edges(media: dict[str, Any]) -> list[dict[str, Any]]:
    relations = media.get("relations")
    edges = relations.get("edges") if isinstance(relations, dict) else None
    return (
        [edge for edge in edges if isinstance(edge, dict)]
        if isinstance(edges, list)
        else []
    )


def _media_relation_target(
    media: dict[str, Any],
    *,
    relation_types: set[str] | None = None,
    media_type: str | None = None,
    preferred_id: int | None = None,
) -> dict[str, Any] | None:
    """Return the first usable related media node matching the requested view."""

    candidates: list[dict[str, Any]] = []
    for edge in _media_relation_edges(media):
        node = edge.get("node")
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        relation_type = str(edge.get("relationType") or "").upper()
        node_type = str(node.get("type") or "").upper()
        if relation_types is not None and relation_type not in relation_types:
            continue
        if media_type is not None and node_type != media_type.upper():
            continue
        try:
            node_id = int(node["id"])
        except (TypeError, ValueError):
            continue
        if node_id == int(media.get("id") or 0):
            continue
        node = dict(node)
        node["_relationType"] = relation_type
        candidates.append(node)
    if preferred_id is not None:
        for candidate in candidates:
            if int(candidate.get("id") or 0) == preferred_id:
                return candidate
    return candidates[0] if candidates else None


def _media_enum_label(value: object) -> str:
    """Format AniList enum values for the compact media details block."""

    return str(value or "").replace("_", " ").title()


def _media_score_format(media: dict[str, Any]) -> str:
    return str(media.get("_viewerScoreFormat") or "POINT_100")


def _media_rating_limit(media: dict[str, Any]) -> float:
    return float(ANILIST_SCORE_LIMITS.get(_media_score_format(media), 100))


def _media_rating_limit_text(media: dict[str, Any]) -> str:
    """Return the score limit in the format the user selected on AniList."""

    score_format = _media_score_format(media)
    limit = _media_rating_limit(media)
    return f"{limit:.1f}" if score_format == "POINT_10_DECIMAL" else str(int(limit))


def _normalise_media_rating(media: dict[str, Any], value: float) -> float:
    """Clamp and round a rating to AniList's selected score format."""

    if value <= 0:
        return 0.0

    score = min(value, _media_rating_limit(media))
    if _media_score_format(media) == "POINT_10_DECIMAL":
        score = round(score, 1)
    else:
        score = float(round(score))
    return score if score > 0 else 0.0


def _media_user_rating(media: dict[str, Any]) -> str | None:
    """Render the authenticated user's AniList rating in their chosen format."""

    entry = media.get("mediaListEntry")
    if not isinstance(entry, dict):
        return None
    raw_score = entry.get("score")
    if isinstance(raw_score, bool) or raw_score is None:
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    if score <= 0:
        return None

    score_format = _media_score_format(media)
    if score_format in ANILIST_SMILEY_SCORE_FORMATS:
        # AniList's current smiley setting is POINT_3. Keep SMILEY for
        # compatibility with older cached responses.
        index = max(0, min(len(ANILIST_SMILEY_EMOJIS) - 1, round(score) - 1))
        return ANILIST_SMILEY_EMOJIS[index]

    if score_format == "POINT_10_DECIMAL":
        score_text = f"{score:.1f}"
        return f"{score_text}/10.0"

    denominator = int(ANILIST_SCORE_LIMITS.get(score_format, 100))
    if score.is_integer():
        score_text = str(int(score))
    else:
        score_text = f"{score:g}"
    return f"{score_text}/{denominator}"


def _context_allows_adult_art(ctx: Context) -> bool:
    """Return whether artwork marked for adults may be shown in this context."""

    if getattr(ctx, "guild", None) is None:
        return True
    channel = getattr(ctx, "channel", None)
    is_nsfw = getattr(channel, "is_nsfw", None)
    if callable(is_nsfw):
        try:
            return bool(is_nsfw())
        except (AttributeError, TypeError):
            return False
    return bool(getattr(channel, "nsfw", False))


def _is_adult_media(media: object) -> bool:
    if not isinstance(media, dict):
        return False
    if bool(media.get("isAdult")):
        return True
    genres = media.get("genres")
    return isinstance(genres, list) and any(
        isinstance(genre, str) and genre.casefold() == "hentai" for genre in genres
    )


def _is_adult_character(character: object) -> bool:
    if not isinstance(character, dict):
        return False
    if bool(character.get("isAdult")):
        return True
    media = character.get("media")
    nodes = media.get("nodes") if isinstance(media, dict) else None
    return isinstance(nodes, list) and any(_is_adult_media(item) for item in nodes)


def _character_description_data(
    value: str | None,
) -> tuple[str, dict[str, str]]:
    """Clean a character description and extract AniList metadata lines.

    AniList often prefixes descriptions with emphasized fields such as
    ``__Affiliation:__`` and ``__Bounty:__``.  Escaping the complete
    description would otherwise leave those fields looking broken.  Keeping
    them as separate details also means the profile information remains
    visible when the description itself is empty or malformed.
    """

    fallback = "No character description provided."
    if not isinstance(value, str) or not value.strip():
        return fallback, {}

    normalized = html.unescape(value)
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", "", normalized)
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", normalized)
    metadata: dict[str, str] = {}

    def _metadata_line(match: re.Match[str]) -> str:
        label = re.sub(r"\s+", " ", match.group("label")).strip().casefold()
        raw_value = str(match.group("value")).strip()
        trailing_spoiler = bool(re.search(r"!~\s*$", raw_value))
        spoiler = bool(match.group("spoiler")) or trailing_spoiler
        value_has_opening_spoiler = raw_value.startswith("~!")
        if spoiler and not value_has_opening_spoiler:
            # Some AniList bios wrap the whole metadata line in ~! ... !~
            # rather than wrapping only the value.
            raw_value = re.sub(r"\s*!~\s*$", "", raw_value).strip()
        detail = _clean_about(
            raw_value,
            max_length=600,
            fallback="",
        )
        if detail:
            if spoiler and not detail.startswith("||"):
                detail = f"||{detail}||"
            metadata[label] = detail
        return ""

    normalized = CHARACTER_METADATA_RE.sub(_metadata_line, normalized)
    height_match = CHARACTER_HEIGHT_RE.search(normalized)
    if height_match:
        height = _clean_about(
            height_match.group(1),
            max_length=300,
            fallback="",
        )
        if height:
            metadata.setdefault("height", height)
        normalized = (
            normalized[: height_match.start()] + normalized[height_match.end() :]
        )
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return (
        _clean_about(
            normalized,
            max_length=4000,
            fallback=fallback,
        ),
        metadata,
    )


def _character_description_parts(value: str | None) -> tuple[str, str | None]:
    description, metadata = _character_description_data(value)
    return description, metadata.get("height")


def _clean_character_description(value: str | None) -> str:
    return _character_description_parts(value)[0]


def _character_media_text(character: dict[str, Any]) -> str | None:
    connection = character.get("media")
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return None
    entries: list[str] = []
    for media in nodes[:6]:
        if not isinstance(media, dict):
            continue
        title = _anime_title(media)
        url = media.get("siteUrl")
        media_type = str(media.get("type") or "MEDIA").title()
        escaped = discord.utils.escape_markdown(title)
        entry = (
            f"[{escaped}]({url}) ({media_type})" if url else f"{escaped} ({media_type})"
        )
        entries.append(entry)
    return "\n".join(entries) or None


def _character_name_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        key = name.casefold()
        if name and key not in seen:
            values.append(name)
            seen.add(key)
    return values


def _media_status_label(status: str | None, media_kind: str) -> str:
    labels = {
        "CURRENT": "Watching" if media_kind == "anime" else "Reading",
        "PLANNING": "Plan to watch" if media_kind == "anime" else "Plan to read",
        "COMPLETED": "Completed",
        "PAUSED": "On Hold",
        "DROPPED": "Dropped",
    }
    return labels.get(str(status or "").upper(), "Not on list")


def _media_status_description(status: str, media_kind: str) -> str:
    activity = "watching" if media_kind == "anime" else "reading"
    infinitive = "watch" if media_kind == "anime" else "read"
    past_activity = "watching" if media_kind == "anime" else "reading"
    return {
        "CURRENT": f"Currently {activity} this {media_kind}",
        "PLANNING": f"Plan to {infinitive} this {media_kind}",
        "COMPLETED": f"Finished {past_activity} this {media_kind}",
        "PAUSED": "Temporarily paused",
        "DROPPED": f"Stopped {activity} this {media_kind}",
    }.get(status, "Update the AniList list status")


def _media_progress_text(media: dict[str, Any], media_kind: str) -> str:
    entry = media.get("mediaListEntry")
    progress = int(entry.get("progress") or 0) if isinstance(entry, dict) else 0
    field = "episodes" if media_kind == "anime" else "chapters"
    unit = "episodes" if media_kind == "anime" else "chapters"
    total = media.get(field)
    if total:
        return f"**Progress:** {progress:,}/{int(total):,} {unit}"
    return f"**Progress:** {progress:,} {unit}"


def _media_next_airing_text(media: dict[str, Any]) -> str | None:
    airing = media.get("nextAiringEpisode")
    if not isinstance(airing, dict):
        return None
    airing_at_value = airing.get("airingAt")
    if airing_at_value is None:
        return None
    try:
        airing_at = int(str(airing_at_value))
    except (TypeError, ValueError):
        return None
    if airing_at <= 0:
        return None
    episode = airing.get("episode")
    episode_number: int | None = None
    if episode is not None:
        try:
            episode_number = int(str(episode))
        except (TypeError, ValueError):
            pass
    if (
        str(media.get("status") or "").upper() == "RELEASING"
        and episode_number is not None
    ):
        total = media.get("episodes")
        try:
            total_episodes = int(total) if total else None
        except (TypeError, ValueError):
            total_episodes = None
        aired = max(0, episode_number - 1)
        episode_count = f"{aired}/{total_episodes}" if total_episodes else str(aired)
        return (
            f"**Episodes:** {episode_count} "
            f"(Episode {episode_number} airs <t:{airing_at}:R>)"
        )
    episode_text = f"Episode {episode_number}: " if episode_number else ""
    return f"**Next episode:** {episode_text}<t:{airing_at}:R>"


def _graphql_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    for error in errors:
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return None


def _media_statistics(profile: dict[str, Any], media_type: str) -> dict[str, Any]:
    statistics = profile.get("statistics")
    if not isinstance(statistics, dict):
        return {}
    media_statistics = statistics.get(media_type)
    return media_statistics if isinstance(media_statistics, dict) else {}


def _favourite_media_entries(
    profile: dict[str, Any], media_type: str
) -> list[tuple[str, str | None, str | None, bool]]:
    favourites_data = profile.get("favourites")
    if not isinstance(favourites_data, dict):
        return []
    media_favourites = favourites_data.get(media_type)
    if not isinstance(media_favourites, dict):
        return []
    nodes = media_favourites.get("nodes") or []
    entries: list[tuple[str, str | None, str | None, bool]] = []
    for item in nodes:
        if not isinstance(item, dict) or not isinstance(item.get("title"), dict):
            continue
        title = item["title"].get("romaji")
        if not title:
            continue
        url = item.get("siteUrl")
        cover_image = item.get("coverImage")
        cover_url = (
            cover_image.get("extraLarge") if isinstance(cover_image, dict) else None
        )
        entries.append(
            (
                str(title),
                (
                    str(url)
                    if isinstance(url, str) and url.startswith(("http://", "https://"))
                    else None
                ),
                (
                    str(cover_url)
                    if isinstance(cover_url, str)
                    and cover_url.startswith(("http://", "https://"))
                    else None
                ),
                _is_adult_media(item),
            )
        )
    return entries


def _favourite_entries(
    profile: dict[str, Any], media_type: str
) -> list[tuple[str, str | None]]:
    return [
        (title, url)
        for title, url, _, _ in _favourite_media_entries(profile, media_type)
    ]


def _favourite_character_entries(
    profile: dict[str, Any],
) -> list[tuple[str, str | None, str | None, bool]]:
    favourites_data = profile.get("favourites")
    if not isinstance(favourites_data, dict):
        return []
    characters = favourites_data.get("characters")
    if not isinstance(characters, dict):
        return []
    entries: list[tuple[str, str | None, str | None, bool]] = []
    for item in characters.get("nodes") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), dict):
            continue
        name = item["name"].get("full")
        if not name:
            continue
        url = item.get("siteUrl")
        image = item.get("image")
        image_url = image.get("large") if isinstance(image, dict) else None
        entries.append(
            (
                str(name),
                (
                    str(url)
                    if isinstance(url, str) and url.startswith(("http://", "https://"))
                    else None
                ),
                (
                    str(image_url)
                    if isinstance(image_url, str)
                    and image_url.startswith(("http://", "https://"))
                    else None
                ),
                _is_adult_character(item),
            )
        )
    return entries


def _favourites_text(label: str, entries: list[tuple[str, str | None]]) -> str:
    if not entries:
        return f"**Favourite {label}:** None listed"
    return f"**Favourite {label}:** " + ", ".join(
        (
            f"[{discord.utils.escape_markdown(title)}]({url})"
            if url
            else discord.utils.escape_markdown(title)
        )
        for title, url in entries[:4]
    )


ANILIST_POSTER_HEIGHT = 300


@to_thread
def _stitch_media_row(images: list[bytes]) -> BytesIO | None:
    loaded: list[Image.Image] = []
    try:
        for data in images:
            if not data:
                continue
            try:
                with Image.open(BytesIO(data)) as image:
                    if (
                        image.width > MAX_IMAGE_DIMENSION
                        or image.height > MAX_IMAGE_DIMENSION
                        or image.width * image.height > MAX_IMAGE_PIXELS
                    ):
                        continue
                    loaded.append(image.convert("RGB"))
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
        if not loaded:
            return None
        resized: list[Image.Image] = []
        total_width = 0
        for image in loaded:
            ratio = ANILIST_POSTER_HEIGHT / image.height
            width = max(1, int(image.width * ratio))
            resized_image = image.resize(
                (width, ANILIST_POSTER_HEIGHT), Image.Resampling.LANCZOS
            )
            resized.append(resized_image)
            total_width += width
        canvas = Image.new("RGB", (total_width, ANILIST_POSTER_HEIGHT), (255, 255, 255))
        x = 0
        for image in resized:
            canvas.paste(image, (x, 0))
            x += image.width
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    finally:
        for image in loaded:
            image.close()


def _media_stats_text(media_type: str, statistics: dict[str, Any]) -> str:
    score = statistics.get("meanScore")
    score_text = f"{float(score):.1f}" if score is not None else "N/A"
    if media_type == "manga":
        return (
            f"**Manga entries:** {_number(statistics.get('count'))}\n"
            f"**Chapters read:** {_number(statistics.get('chaptersRead'))}\n"
            f"**Volumes read:** {_number(statistics.get('volumesRead'))}\n"
            f"**Mean score:** {score_text}"
        )
    minutes = int(statistics.get("minutesWatched") or 0)
    return (
        f"**Anime entries:** {_number(statistics.get('count'))}\n"
        f"**Episodes watched:** {_number(statistics.get('episodesWatched'))}\n"
        f"**Time watched:** {_number(minutes // 60)} hours\n"
        f"**Mean score:** {score_text}"
    )


class AniListProfileView(discord.ui.LayoutView):
    def __init__(
        self,
        ctx: Context,
        profile: dict[str, Any],
        media_type: str,
        available_media: set[str],
        image_files: dict[str, str],
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.profile = profile
        self.media_type = media_type
        self.available_media = available_media
        self.image_files = image_files
        self._render()

    def _render(self) -> None:
        self.clear_items()
        profile = self.profile
        name = str(profile.get("name") or "Unknown AniList User")
        profile_url = str(profile.get("siteUrl") or f"https://anilist.co/user/{name}")
        avatar = (profile.get("avatar") or {}).get("large")
        statistics = _media_statistics(profile, self.media_type)
        if self.media_type == "characters":
            character_entries = _favourite_character_entries(profile)
            favourites = [(name, url) for name, url, _, _ in character_entries]
        else:
            favourites = _favourite_entries(profile, self.media_type)
        children: list[discord.ui.Item[Any]] = []

        if avatar:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"## [{discord.utils.escape_markdown(name)}]({profile_url})\n"
                        f"-# ID: {profile.get('id')}"
                    ),
                    discord.ui.TextDisplay(_clean_about(profile.get("about"))),
                    accessory=discord.ui.Thumbnail(str(avatar)),
                )
            )
        else:
            children.extend(
                [
                    discord.ui.TextDisplay(
                        f"## [{discord.utils.escape_markdown(name)}]({profile_url})\n"
                        f"-# ID: {profile.get('id')}"
                    ),
                    discord.ui.TextDisplay(_clean_about(profile.get("about"))),
                ]
            )

        if self.media_type == "characters":
            children.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(_favourites_text("Characters", favourites)),
                ]
            )
        elif int(statistics.get("count") or 0) > 0:
            children.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        _media_stats_text(self.media_type, statistics)
                    ),
                    discord.ui.TextDisplay(
                        _favourites_text(self.media_type, favourites)
                    ),
                ]
            )
        filename = self.image_files.get(self.media_type)
        if filename:
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{filename}")
                )
            )

        buttons: list[discord.ui.Button] = []
        for media_type, label in (
            ("anime", "Anime"),
            ("manga", "Manga"),
            ("characters", "Characters"),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                disabled=media_type not in self.available_media
                or media_type == self.media_type,
            )

            async def _switch(
                interaction: discord.Interaction,
                selected: str = media_type,
            ) -> None:
                self.media_type = selected
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = _switch
            buttons.append(button)
        children.append(discord.ui.ActionRow(*buttons))

        container = discord.ui.Container(
            *children, accent_color=self.ctx.bot.embedcolor
        )
        self.add_item(container)


class CharacterLookupView(discord.ui.LayoutView):
    def __init__(
        self,
        ctx: Context,
        character: dict[str, Any],
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.character = character
        self.description_expanded = False
        self.appearances_shown = False
        self._render()

    def _render(self) -> None:
        self.clear_items()
        character = self.character
        names = character.get("name")
        names = names if isinstance(names, dict) else {}
        name = str(names.get("full") or "Unknown character")
        native_name = names.get("native")
        site_url = str(
            character.get("siteUrl")
            or f"https://anilist.co/character/{character.get('id')}"
        )
        image = character.get("image")
        image_url = (
            image.get("large")
            if isinstance(image, dict)
            and (
                _context_allows_adult_art(self.ctx)
                or not _is_adult_character(character)
            )
            else None
        )

        title = (
            f"## [{discord.utils.escape_markdown(name)}]({site_url})\n"
            f"-# ID: {character.get('id')}"
        )
        if native_name and str(native_name) != name:
            title += f"\n-# {discord.utils.escape_markdown(str(native_name))}"

        children: list[discord.ui.Item[Any]] = []
        description, character_metadata = _character_description_data(
            character.get("description")
        )
        height = character_metadata.get("height")
        description_preview, description_truncated = _description_preview(description)
        description_text = (
            description if self.description_expanded else description_preview
        )
        description_display = discord.ui.TextDisplay(description_text)
        if image_url:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(title),
                    description_display,
                    accessory=discord.ui.Thumbnail(str(image_url)),
                )
            )
        else:
            children.extend(
                (
                    discord.ui.TextDisplay(title),
                    description_display,
                )
            )

        if description_truncated:
            more = discord.ui.Button(
                label="^" if self.description_expanded else "...",
                style=discord.ButtonStyle.secondary,
            )
            more.callback = self._toggle_description
            children.append(discord.ui.ActionRow(more))

        details: list[str] = []
        for label, value in (
            ("Gender", character.get("gender") or character_metadata.get("gender")),
            (
                "Age",
                _character_age_value(
                    character.get("age") or character_metadata.get("age")
                ),
            ),
            ("Height", height),
            (
                "Blood type",
                character.get("bloodType") or character_metadata.get("blood type"),
            ),
        ):
            if value:
                details.append(f"**{label}:** {_display_character_value(value)}")
        birthday = character.get("dateOfBirth")
        if isinstance(birthday, dict):
            date_parts = [
                str(birthday.get(key))
                for key in ("year", "month", "day")
                if birthday.get(key)
            ]
            if date_parts:
                details.append(f"**Birthday:** {'-'.join(date_parts)}")
        favourites = character.get("favourites")
        if favourites is not None:
            details.append(f"**Favourites:** {_number(favourites)}")
        standard_labels = {
            "gender",
            "age",
            "height",
            "blood type",
            "birthday",
            "favourites",
            "other names",
        }
        for label, value in character_metadata.items():
            if label in standard_labels or not value:
                continue
            display_label = label[:1].upper() + label[1:]
            details.append(f"**{display_label}:** {value}")
        alternative_values = _character_name_values(names.get("alternative"))
        spoiler_values = _character_name_values(names.get("alternativeSpoiler"))
        spoiler_keys = {value.casefold() for value in spoiler_values}
        alternative_values = [
            value
            for value in alternative_values
            if value.casefold() not in spoiler_keys
        ]
        name_parts = [
            _display_character_value(value) for value in alternative_values[:5]
        ]
        name_parts.extend(
            _spoiler_character_value(value) for value in spoiler_values[:5]
        )
        if name_parts:
            details.append(f"**Other names:** {', '.join(name_parts)}")
        if details:
            children.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.TextDisplay("\n".join(details)),
                )
            )

        related_media = _character_media_text(character)
        if related_media:
            if self.appearances_shown:
                children.extend(
                    (
                        discord.ui.Separator(),
                        discord.ui.TextDisplay(f"### Appears in\n{related_media}"),
                    )
                )
            else:
                show_appearances = discord.ui.Button(
                    label="Show appearances",
                    style=discord.ButtonStyle.secondary,
                )
                show_appearances.callback = self._show_appearances
                children.append(discord.ui.ActionRow(show_appearances))

        self.add_item(
            discord.ui.Container(*children, accent_color=self.ctx.bot.embedcolor)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to use these buttons.", ephemeral=True
        )
        return False

    async def _toggle_description(self, interaction: discord.Interaction) -> None:
        self.description_expanded = not self.description_expanded
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _show_appearances(self, interaction: discord.Interaction) -> None:
        self.appearances_shown = True
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _display_character_value(value: object) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _spoiler_character_value(value: object) -> str:
    return f"||{_display_character_value(value)}||"


class MediaProgressModal(discord.ui.Modal, title="Set Progress"):
    def __init__(self, view: "MediaLookupView") -> None:
        super().__init__()
        self.view = view
        unit = "episode" if view.media_kind == "anime" else "chapter"
        self.progress = discord.ui.TextInput(
            label=f"{unit.title()} progress",
            placeholder=f"Enter the {unit} number",
            required=True,
            max_length=6,
        )
        self.add_item(self.progress)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view.ctx.author.id:
            await interaction.response.send_message(
                "This progress dialog is not for you.", ephemeral=True
            )
            return
        try:
            value = int(str(self.progress.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number for the episode progress.", ephemeral=True
            )
            return
        total = self.view._progress_total()
        unit = "episode" if self.view.media_kind == "anime" else "chapter"
        if value < 0 or (total and value > total):
            maximum = f" and no more than {total}" if total else ""
            await interaction.response.send_message(
                f"{unit.title()} progress must be 0{maximum}.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.view._save_entry(interaction, progress=value)


class MediaRatingModal(discord.ui.Modal, title="Set Rating"):
    def __init__(
        self,
        view: "MediaLookupView",
        source_message: discord.Message | None,
    ) -> None:
        super().__init__()
        self.view = view
        self.source_message = source_message
        maximum = _media_rating_limit_text(view.media)
        score_range = (
            f"0.0-{maximum}"
            if _media_score_format(view.media) == "POINT_10_DECIMAL"
            else f"0-{maximum}"
        )
        self.rating = discord.ui.TextInput(
            label=f"Rating (0 removes it, max {maximum})",
            placeholder=f"Enter {score_range}; 0 removes your rating",
            required=True,
            max_length=6,
        )
        self.add_item(self.rating)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view.ctx.author.id:
            await interaction.response.send_message(
                "This rating dialog is not for you.", ephemeral=True
            )
            return
        raw_value = str(self.rating.value).strip()
        try:
            value = float(raw_value)
        except ValueError:
            await interaction.response.send_message(
                "Enter a number for the rating. Use 0 to remove it.", ephemeral=True
            )
            return
        if not math.isfinite(value):
            await interaction.response.send_message(
                "Enter a finite number for the rating. Use 0 to remove it.",
                ephemeral=True,
            )
            return
        score = _normalise_media_rating(self.view.media, value)
        await interaction.response.defer()
        saved = await self.view._save_entry(
            interaction,
            score=score,
            source_message=self.source_message,
            update_original=False,
        )
        if saved:
            rating = _media_user_rating(self.view.media)
            await interaction.followup.send(
                view=MediaRatingNoticeView(
                    self.view,
                    f"Rating updated to {rating}." if rating else "Rating removed.",
                ),
                ephemeral=True,
            )


class MediaRatingChoiceView(discord.ui.LayoutView):
    def __init__(
        self,
        view: "MediaLookupView",
        source_message: discord.Message | None,
    ) -> None:
        super().__init__(timeout=120)
        self.media_view = view
        self.source_message = source_message
        self._render()

    def _render(self) -> None:
        title = _anime_title(self.media_view.media)
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(
                f"### Set rating for {discord.utils.escape_markdown(title)}\n"
            )
        ]
        smiling = [
            discord.ui.Button(emoji=ANILIST_SMILEY_EMOJIS[0]),
            discord.ui.Button(emoji=ANILIST_SMILEY_EMOJIS[1]),
            discord.ui.Button(emoji=ANILIST_SMILEY_EMOJIS[2]),
            discord.ui.Button(emoji=ANILIST_RATING_REMOVE_EMOJI),
        ]
        smiling[0].callback = self._set_frown
        smiling[1].callback = self._set_neutral
        smiling[2].callback = self._set_smile
        smiling[3].callback = self._remove_rating
        children.append(discord.ui.ActionRow(*smiling))
        self.add_item(
            discord.ui.Container(
                *children,
                accent_color=self.media_view.ctx.bot.embedcolor,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.media_view.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to manage AniList ratings.", ephemeral=True
        )
        return False

    async def _set_score(self, interaction: discord.Interaction, score: float) -> None:
        await interaction.response.defer()
        saved = await self.media_view._save_entry(
            interaction,
            score=score,
            source_message=self.source_message,
            update_original=False,
        )
        if saved:
            rating = _media_user_rating(self.media_view.media)
            await interaction.edit_original_response(
                view=MediaRatingNoticeView(
                    self.media_view,
                    f"Rating updated to {rating}." if rating else "Rating removed.",
                )
            )

    async def _set_smile(self, interaction: discord.Interaction) -> None:
        await self._set_score(interaction, 3)

    async def _set_neutral(self, interaction: discord.Interaction) -> None:
        await self._set_score(interaction, 2)

    async def _set_frown(self, interaction: discord.Interaction) -> None:
        await self._set_score(interaction, 1)

    async def _remove_rating(self, interaction: discord.Interaction) -> None:
        await self._set_score(interaction, 0)


class MediaRatingNoticeView(discord.ui.LayoutView):
    def __init__(self, view: "MediaLookupView", message: str) -> None:
        super().__init__(timeout=60)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(message),
                accent_color=view.ctx.bot.embedcolor,
            )
        )


class MediaListProgressModal(discord.ui.Modal, title="Set Progress"):
    def __init__(self, view: "MediaListView") -> None:
        super().__init__()
        self.view = view
        unit = "episode" if view.media_kind == "anime" else "chapter"
        self.progress = discord.ui.TextInput(
            label=f"{unit.title()} progress",
            placeholder=f"Enter the {unit} number",
            required=True,
            max_length=6,
        )
        self.add_item(self.progress)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view.ctx.author.id:
            await interaction.response.send_message(
                "This progress dialog is not for you.", ephemeral=True
            )
            return
        try:
            value = int(str(self.progress.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number for the progress.", ephemeral=True
            )
            return
        total = self.view._progress_total()
        unit = "episode" if self.view.media_kind == "anime" else "chapter"
        if value < 0 or (total and value > total):
            maximum = f" and no more than {total}" if total else ""
            await interaction.response.send_message(
                f"{unit.title()} progress must be 0{maximum}.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.view._save_progress(interaction, value)


class MediaListView(discord.ui.LayoutView):
    """Components V2 view for a user's currently active AniList entries."""

    PAGE_SIZE = 5

    def __init__(
        self,
        cog: "Anime",
        ctx: Context,
        entries: list[dict[str, Any]],
        media_kind: str,
        access_token: str | None,
        score_format: str | None,
        username: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.entries = entries
        self.media_kind = media_kind
        self.access_token = access_token
        self.score_format = score_format
        self.username = username
        self.page = 0
        self.selected_index = 0 if entries else None
        self.media_select: discord.ui.Select[Any] | None = None
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_entries(self) -> list[tuple[int, dict[str, Any]]]:
        start = self.page * self.PAGE_SIZE
        return list(enumerate(self.entries[start : start + self.PAGE_SIZE], start))

    def _selected_entry(self) -> dict[str, Any] | None:
        if self.selected_index is None:
            return None
        if 0 <= self.selected_index < len(self.entries):
            return self.entries[self.selected_index]
        return None

    def _entry_media(self, entry: dict[str, Any]) -> dict[str, Any]:
        media = entry.get("media")
        media_data = dict(media) if isinstance(media, dict) else {}
        media_data["mediaListEntry"] = entry
        media_data["_viewerScoreFormat"] = self.score_format
        return media_data

    def _entry_title(self, entry: dict[str, Any]) -> str:
        media = entry.get("media")
        return _anime_title(media) if isinstance(media, dict) else "Unknown title"

    def _progress_total_for(self, entry: dict[str, Any]) -> int | None:
        media = entry.get("media")
        field = "episodes" if self.media_kind == "anime" else "chapters"
        value = media.get(field) if isinstance(media, dict) else None
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _progress_for(self, entry: dict[str, Any]) -> int:
        try:
            return max(0, int(entry.get("progress") or 0))
        except (TypeError, ValueError):
            return 0

    def _entry_line(self, number: int, entry: dict[str, Any]) -> str:
        media = entry.get("media")
        title = discord.utils.escape_mentions(
            discord.utils.escape_markdown(self._entry_title(entry))
        )
        site_url = media.get("siteUrl") if isinstance(media, dict) else None
        title_text = (
            f"[{title}]({site_url})"
            if isinstance(site_url, str)
            and site_url.startswith(("https://", "http://"))
            else title
        )
        repeat = 0
        try:
            repeat = int(entry.get("repeat") or 0)
        except (TypeError, ValueError):
            pass
        repeating = entry.get("status") == "REPEATING" or repeat > 0
        label = (
            "Rewatching"
            if repeating and self.media_kind == "anime"
            else (
                "Rereading"
                if repeating
                else "Watching" if self.media_kind == "anime" else "Reading"
            )
        )
        rating = _media_user_rating(self._entry_media(entry))
        score = f" • Score: {rating}" if rating else ""
        unit = "episodes" if self.media_kind == "anime" else "chapters"
        progress = self._progress_for(entry)
        total = self._progress_total_for(entry)
        progress_value = f"{progress:,}/{total:,}" if total else f"{progress:,}"
        progress_text = f" · {progress_value} {unit}"
        return f"**{number}.** {title_text}\n-# {label}{progress_text}{score}"

    def _selected_details(self, entry: dict[str, Any]) -> str:
        unit = "episode" if self.media_kind == "anime" else "chapter"
        progress = self._progress_for(entry)
        total = self._progress_total_for(entry)
        progress_text = f"{progress:,}/{total:,}" if total else f"{progress:,}"
        return f"**Progress:** {progress_text} {unit}s"

    def _progress_total(self) -> int | None:
        entry = self._selected_entry()
        return self._progress_total_for(entry) if entry else None

    def _render(self) -> None:
        self.clear_items()
        label = "Watching" if self.media_kind == "anime" else "Reading"
        username = discord.utils.escape_mentions(
            discord.utils.escape_markdown(self.username)
        )
        title = f"## Currently {label.lower()} for {username}"
        if not self.entries:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(title),
                    discord.ui.TextDisplay(
                        f"You are not currently {label.lower()} any "
                        f"{self.media_kind} titles."
                    ),
                    accent_color=self.ctx.bot.embedcolor,
                )
            )
            return

        page_entries = self._page_entries()
        lines = [self._entry_line(index + 1, entry) for index, entry in page_entries]
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(title),
            discord.ui.TextDisplay("\n\n".join(lines)),
        ]

        media_select: discord.ui.Select[Any] | None = None
        if self.access_token:
            options = []
            for index, entry in page_entries:
                option_label = self._entry_title(entry)[:100] or "Unknown title"
                option_description = (
                    f"{self._progress_for(entry):,} "
                    f"{'episodes' if self.media_kind == 'anime' else 'chapters'}"
                )
                options.append(
                    discord.SelectOption(
                        label=option_label,
                        description=option_description[:100],
                        value=str(index),
                        default=index == self.selected_index,
                    )
                )
            media_select = discord.ui.Select(
                placeholder=f"Select a {self.media_kind} title",
                min_values=1,
                max_values=1,
                options=options,
            )
            media_select.callback = self._select_entry
            self.media_select = media_select

        selected = self._selected_entry()
        if selected is not None and media_select is not None:
            children.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(self._selected_details(selected)),
                    discord.ui.ActionRow(media_select),
                ]
            )
            if self.access_token:
                progress = self._progress_for(selected)
                total = self._progress_total_for(selected)
                minus = discord.ui.Button(
                    label="-",
                    style=discord.ButtonStyle.secondary,
                    disabled=progress <= 0,
                )
                plus = discord.ui.Button(
                    label="+",
                    style=discord.ButtonStyle.secondary,
                    disabled=bool(total and progress >= total),
                )
                set_progress = discord.ui.Button(
                    label="Set Progress", style=discord.ButtonStyle.primary
                )
                minus.callback = self._decrease_progress
                plus.callback = self._increase_progress
                set_progress.callback = self._open_progress_modal
                children.append(discord.ui.ActionRow(minus, plus, set_progress))

        children.extend(
            [
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# Page {self.page + 1}/{self.page_count} · "
                    f"{len(self.entries)} active {self.media_kind} titles"
                ),
            ]
        )
        if self.page_count > 1:
            previous = discord.ui.Button(
                label="<",
                style=discord.ButtonStyle.secondary,
            )
            next_page = discord.ui.Button(
                label=">",
                style=discord.ButtonStyle.secondary,
            )
            previous.callback = self._previous_page
            next_page.callback = self._next_page
            children.append(discord.ui.ActionRow(previous, next_page))

        self.add_item(
            discord.ui.Container(*children, accent_color=self.ctx.bot.embedcolor)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to manage AniList progress.", ephemeral=True
        )
        return False

    async def _select_entry(self, interaction: discord.Interaction) -> None:
        if self.media_select is None or not self.media_select.values:
            await interaction.response.send_message(
                "Select a title first.", ephemeral=True
            )
            return
        self.selected_index = int(self.media_select.values[0])
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        self.page = (self.page - 1) % self.page_count
        start = self.page * self.PAGE_SIZE
        if self.selected_index is None or not (
            start <= self.selected_index < start + self.PAGE_SIZE
        ):
            self.selected_index = start
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = (self.page + 1) % self.page_count
        start = self.page * self.PAGE_SIZE
        if self.selected_index is None or not (
            start <= self.selected_index < start + self.PAGE_SIZE
        ):
            self.selected_index = start
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _decrease_progress(self, interaction: discord.Interaction) -> None:
        await self._change_progress(interaction, -1)

    async def _increase_progress(self, interaction: discord.Interaction) -> None:
        await self._change_progress(interaction, 1)

    async def _change_progress(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        entry = self._selected_entry()
        if entry is None:
            await interaction.response.send_message(
                "Select a title first.", ephemeral=True
            )
            return
        current = self._progress_for(entry)
        total = self._progress_total_for(entry)
        new_progress = max(0, current + amount)
        if total:
            new_progress = min(new_progress, total)
        if new_progress == current:
            await interaction.response.send_message(
                "Progress is already at that limit.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._save_progress(interaction, new_progress)

    async def _open_progress_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MediaListProgressModal(self))

    async def _save_progress(
        self, interaction: discord.Interaction, progress: int
    ) -> None:
        entry = self._selected_entry()
        if entry is None:
            await interaction.followup.send("Select a title first.", ephemeral=True)
            return
        raw_media_id: object = entry.get("mediaId")
        if raw_media_id is None:
            media = entry.get("media")
            raw_media_id = media.get("id") if isinstance(media, dict) else None
        if isinstance(raw_media_id, bool) or not isinstance(raw_media_id, (int, str)):
            await interaction.followup.send(
                "AniList did not return a valid media entry.", ephemeral=True
            )
            return
        try:
            media_id = int(raw_media_id)
        except (TypeError, ValueError):
            await interaction.followup.send(
                "AniList did not return a valid media entry.", ephemeral=True
            )
            return
        try:
            response_status, payload = await self.cog._anilist_request(
                ANILIST_SAVE_MEDIA_MUTATION,
                {"mediaId": media_id, "progress": progress},
                self.access_token,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        error_message = _graphql_error(payload)
        if response_status != 200 or error_message:
            detail = f" ({error_message})" if error_message else ""
            await interaction.followup.send(
                f"AniList could not update this title{detail}.", ephemeral=True
            )
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        updated = data.get("SaveMediaListEntry") if isinstance(data, dict) else None
        if not isinstance(updated, dict):
            await interaction.followup.send(
                "AniList did not return an updated list entry.", ephemeral=True
            )
            return
        entry.update(updated)
        self._render()
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class MediaLookupView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "Anime",
        ctx: Context,
        media: dict[str, Any],
        access_token: str | None,
        media_kind: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.media = media
        self.access_token = access_token
        self.media_kind = media_kind
        self.description_expanded = False
        self.editor_open = False
        self._last_media_by_kind: dict[str, int] = {}
        try:
            self._last_media_by_kind[media_kind] = int(media["id"])
        except (KeyError, TypeError, ValueError):
            pass
        self._navigation_lock = asyncio.Lock()
        self._render()

    def _progress(self) -> int:
        entry = self.media.get("mediaListEntry")
        return int(entry.get("progress") or 0) if isinstance(entry, dict) else 0

    def _progress_total(self) -> int | None:
        field = "episodes" if self.media_kind == "anime" else "chapters"
        value = self.media.get(field)
        return int(value) if value else None

    def _render(self) -> None:
        self.clear_items()
        title = _anime_title(self.media)
        site_url = self.media.get("siteUrl") or (
            f"https://anilist.co/{self.media_kind}/{self.media.get('id')}"
        )
        title_text = f"## [{discord.utils.escape_markdown(title)}]({site_url})"
        cover = (
            (self.media.get("coverImage") or {}).get("extraLarge")
            if _context_allows_adult_art(self.ctx) or not _is_adult_media(self.media)
            else None
        )
        description, _description_source = _media_description_parts(
            self.media.get("description")
        )
        description_preview, description_truncated = _description_preview(description)
        description_text = (
            description if self.description_expanded else description_preview
        )
        details: list[str] = []
        if self.media.get("format"):
            details.append(f"**Format:** {_media_enum_label(self.media['format'])}")
        if self.media.get("status"):
            details.append(
                f"**Release status:** {_media_enum_label(self.media['status'])}"
            )
        next_airing = _media_next_airing_text(self.media)
        if next_airing:
            details.append(next_airing)
        progress_field = "episodes" if self.media_kind == "anime" else "chapters"
        progress_label = "Episodes" if self.media_kind == "anime" else "Chapters"
        releasing_airing = (
            self.media_kind == "anime"
            and str(self.media.get("status") or "").upper() == "RELEASING"
            and next_airing is not None
        )
        if self.media.get(progress_field) and not releasing_airing:
            details.append(
                f"**{progress_label}:** {_number(self.media[progress_field])}"
            )
        if self.media_kind == "manga" and self.media.get("volumes"):
            details.append(f"**Volumes:** {_number(self.media['volumes'])}")
        if self.media.get("averageScore"):
            details.append(f"**Score:** {self.media['averageScore']}/100")
        if self.media.get("genres"):
            genres = ", ".join(
                discord.utils.escape_markdown(str(genre))
                for genre in self.media["genres"][:8]
            )
            details.append(f"**Genres:** {genres}")
        children: list[discord.ui.Item[Any]] = []
        profile_text = title_text
        if cover:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(profile_text),
                    discord.ui.TextDisplay(description_text),
                    accessory=discord.ui.Thumbnail(str(cover)),
                )
            )
        else:
            children.extend(
                [
                    discord.ui.TextDisplay(profile_text),
                    discord.ui.TextDisplay(description_text),
                ]
            )
        if description_truncated:
            more = discord.ui.Button(
                label="^" if self.description_expanded else "...",
                style=discord.ButtonStyle.secondary,
            )
            more.callback = self._expand_description
            children.append(discord.ui.ActionRow(more))
        if details:
            children.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay("\n".join(details)),
                ]
            )

        if self.access_token:
            entry = self.media.get("mediaListEntry")
            status = entry.get("status") if isinstance(entry, dict) else None
            children.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        f"**List status:** {_media_status_label(status, self.media_kind)}\n"
                        f"{_media_progress_text(self.media, self.media_kind)}"
                    ),
                ]
            )
            if self.editor_open:
                options = [
                    discord.SelectOption(
                        label=_media_status_label(value, self.media_kind),
                        description=_media_status_description(value, self.media_kind),
                        value=value,
                        default=status == value,
                    )
                    for value, _, _ in ANILIST_LIST_STATUSES
                ]
                status_select = discord.ui.Select(
                    placeholder="Update list status",
                    min_values=1,
                    max_values=1,
                    options=options,
                )
                self.status_select = status_select
                status_select.callback = self._status_selected
                children.append(discord.ui.ActionRow(status_select))

                progress = self._progress()
                total = self._progress_total()
                minus = discord.ui.Button(
                    label="-",
                    style=discord.ButtonStyle.secondary,
                    disabled=progress <= 0,
                )
                plus = discord.ui.Button(
                    label="+",
                    style=discord.ButtonStyle.secondary,
                    disabled=bool(total and progress >= total),
                )
                jump = discord.ui.Button(
                    label="Set Progress",
                    style=discord.ButtonStyle.primary,
                )
                favourite = discord.ui.Button(
                    emoji=(
                        "\u2764\ufe0f"
                        if self.media.get("isFavourite")
                        else "\U0001fa76"
                    ),
                    style=discord.ButtonStyle.secondary,
                )
                rating = _media_user_rating(self.media)
                rating_button = discord.ui.Button(
                    label=rating or "Rate",
                    style=discord.ButtonStyle.secondary,
                )
                minus.callback = self._decrease_progress
                plus.callback = self._increase_progress
                jump.callback = self._open_progress_modal
                favourite.callback = self._toggle_favourite
                rating_button.callback = self._open_rating
                children.append(
                    discord.ui.ActionRow(minus, plus, jump, favourite, rating_button)
                )
        else:
            children.append(
                discord.ui.TextDisplay(
                    "Connect AniList to manage your list and episode progress."
                )
            )

        container = discord.ui.Container(
            *children, accent_color=self.ctx.bot.embedcolor
        )
        self.add_item(container)
        navigation: list[discord.ui.Button] = []
        previous = _media_relation_target(
            self.media,
            relation_types={"PREQUEL"},
            media_type=self.media_kind.upper(),
        )
        sequel = _media_relation_target(
            self.media,
            relation_types={"SEQUEL"},
            media_type=self.media_kind.upper(),
        )
        # A title at either end of a season chain still gets both controls so
        # the buttons can wrap around to the opposite end of the chain.
        if previous is not None or sequel is not None:
            previous_button = discord.ui.Button(
                label="<",
                style=discord.ButtonStyle.secondary,
            )
            previous_button.callback = self._show_previous_media
            navigation.append(previous_button)

        if previous is not None or sequel is not None:
            next_button = discord.ui.Button(
                label=">",
                style=discord.ButtonStyle.secondary,
            )
            next_button.callback = self._show_next_media
            navigation.append(next_button)

        opposite_kind = "manga" if self.media_kind == "anime" else "anime"
        opposite = _media_relation_target(
            self.media,
            media_type=opposite_kind.upper(),
            preferred_id=self._last_media_by_kind.get(opposite_kind),
        )
        if opposite is not None:
            type_button = discord.ui.Button(
                label=opposite_kind.title(),
                style=discord.ButtonStyle.secondary,
            )
            type_button.callback = self._show_opposite_media
            navigation.append(type_button)
        if self.access_token:
            editor = discord.ui.Button(
                label="Editor",
                style=(
                    discord.ButtonStyle.primary
                    if self.editor_open
                    else discord.ButtonStyle.secondary
                ),
            )
            editor.callback = self._toggle_editor
            navigation.append(editor)
        if navigation:
            self.add_item(discord.ui.ActionRow(*navigation))

    async def _show_related_media(
        self,
        interaction: discord.Interaction,
        relation: dict[str, Any] | None,
        *,
        deferred: bool = False,
    ) -> None:
        if relation is None:
            message = "That related AniList title is not available."
            if deferred:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        try:
            media_id = int(relation["id"])
        except (KeyError, TypeError, ValueError):
            message = "AniList did not return a valid related title."
            if deferred:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

        if not deferred:
            await interaction.response.defer()
        try:
            current_id = int(self.media["id"])
        except (KeyError, TypeError, ValueError):
            current_id = None
        if current_id == media_id:
            return
        async with self._navigation_lock:
            try:
                response_status, payload = await self.cog._anilist_request(
                    ANILIST_MEDIA_BY_ID_QUERY,
                    {"id": media_id},
                    self.access_token,
                )
            except commands.BadArgument as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            error_message = _graphql_error(payload)
            data = payload.get("data") if isinstance(payload, dict) else None
            media = data.get("Media") if isinstance(data, dict) else None
            if response_status != 200 or error_message or not isinstance(media, dict):
                detail = f" ({error_message})" if error_message else ""
                await interaction.followup.send(
                    f"AniList could not load that related title{detail}.",
                    ephemeral=True,
                )
                return
            score_format = self.media.get("_viewerScoreFormat")
            if score_format:
                media["_viewerScoreFormat"] = score_format
            media_type = str(media.get("type") or "").casefold()
            if media_type not in {"anime", "manga"}:
                await interaction.followup.send(
                    "AniList returned an unsupported related title.", ephemeral=True
                )
                return
            self.media = media
            self.media_kind = media_type
            self._last_media_by_kind[media_type] = media_id
            self.description_expanded = False
            self._render()
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _chain_edge_id(self, relation_type: str) -> int | None:
        """Find the far end of a season chain by following its relations."""

        try:
            current_id = int(self.media["id"])
        except (KeyError, TypeError, ValueError):
            return None
        current = self.media
        seen = {current_id}
        for _ in range(50):
            relation = _media_relation_target(
                current,
                relation_types={relation_type},
                media_type=self.media_kind.upper(),
            )
            if relation is None:
                return current_id
            try:
                related_id = int(relation["id"])
            except (KeyError, TypeError, ValueError):
                return current_id
            if related_id in seen:
                return current_id
            seen.add(related_id)
            response_status, payload = await self.cog._anilist_request(
                ANILIST_MEDIA_BY_ID_QUERY,
                {"id": related_id},
                self.access_token,
            )
            error_message = _graphql_error(payload)
            data = payload.get("data") if isinstance(payload, dict) else None
            next_media = data.get("Media") if isinstance(data, dict) else None
            if (
                response_status != 200
                or error_message
                or not isinstance(next_media, dict)
            ):
                detail = f" ({error_message})" if error_message else ""
                raise commands.BadArgument(
                    f"AniList could not load the related title{detail}."
                )
            current = next_media
            current_id = related_id
        raise commands.BadArgument(
            "AniList returned an excessively long relation chain."
        )

    async def _show_previous_media(self, interaction: discord.Interaction) -> None:
        relation = _media_relation_target(
            self.media,
            relation_types={"PREQUEL"},
            media_type=self.media_kind.upper(),
        )
        if relation is not None:
            await self._show_related_media(interaction, relation)
            return
        await interaction.response.defer()
        try:
            media_id = await self._chain_edge_id("SEQUEL")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._show_related_media(
            interaction,
            {"id": media_id} if media_id is not None else None,
            deferred=True,
        )

    async def _show_next_media(self, interaction: discord.Interaction) -> None:
        relation = _media_relation_target(
            self.media,
            relation_types={"SEQUEL"},
            media_type=self.media_kind.upper(),
        )
        if relation is not None:
            await self._show_related_media(interaction, relation)
            return
        await interaction.response.defer()
        try:
            media_id = await self._chain_edge_id("PREQUEL")
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await self._show_related_media(
            interaction,
            {"id": media_id} if media_id is not None else None,
            deferred=True,
        )

    async def _show_opposite_media(self, interaction: discord.Interaction) -> None:
        opposite_kind = "manga" if self.media_kind == "anime" else "anime"
        relation = _media_relation_target(
            self.media,
            media_type=opposite_kind.upper(),
            preferred_id=self._last_media_by_kind.get(opposite_kind),
        )
        await self._show_related_media(interaction, relation)

    async def _expand_description(self, interaction: discord.Interaction) -> None:
        self.description_expanded = not self.description_expanded
        self._render()
        await interaction.response.edit_message(view=self)

    async def _toggle_editor(self, interaction: discord.Interaction) -> None:
        self.editor_open = not self.editor_open
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to manage AniList progress.", ephemeral=True
        )
        return False

    async def _status_selected(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._save_entry(interaction, status=self.status_select.values[0])

    async def _decrease_progress(self, interaction: discord.Interaction) -> None:
        await self._change_progress(interaction, -1)

    async def _increase_progress(self, interaction: discord.Interaction) -> None:
        await self._change_progress(interaction, 1)

    async def _change_progress(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        progress = self._progress()
        total = self._progress_total()
        new_progress = max(0, progress + amount)
        if total:
            new_progress = min(new_progress, total)
        if new_progress == progress:
            await interaction.response.send_message(
                "Progress is already at that limit.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._save_entry(interaction, progress=new_progress)

    async def _open_progress_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MediaProgressModal(self))

    async def _open_rating(self, interaction: discord.Interaction) -> None:
        source_message = interaction.message
        if _media_score_format(self.media) in ANILIST_SMILEY_SCORE_FORMATS:
            await interaction.response.send_message(
                view=MediaRatingChoiceView(self, source_message),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(MediaRatingModal(self, source_message))

    async def _toggle_favourite(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            media_id = int(self.media["id"])
        except (KeyError, TypeError, ValueError):
            await interaction.followup.send(
                "AniList did not return a valid media entry.", ephemeral=True
            )
            return
        variables = (
            {"animeId": media_id}
            if self.media_kind == "anime"
            else {"mangaId": media_id}
        )
        try:
            response_status, payload = await self.cog._anilist_request(
                ANILIST_TOGGLE_FAVOURITE_MUTATION,
                variables,
                self.access_token,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        error_message = _graphql_error(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            response_status != 200
            or error_message
            or not isinstance(data, dict)
            or not isinstance(data.get("ToggleFavourite"), dict)
        ):
            detail = f" ({error_message})" if error_message else ""
            await interaction.followup.send(
                f"AniList could not update this favourite{detail}.", ephemeral=True
            )
            return
        self.media["isFavourite"] = not bool(self.media.get("isFavourite"))
        self._render()
        await interaction.edit_original_response(view=self)

    async def _save_entry(
        self,
        interaction: discord.Interaction,
        *,
        status: str | None = None,
        progress: int | None = None,
        score: float | None = None,
        source_message: discord.Message | None = None,
        update_original: bool = True,
    ) -> bool:
        variables: dict[str, Any] = {"mediaId": int(self.media["id"])}
        if status is not None:
            variables["status"] = status
        if progress is not None:
            variables["progress"] = progress
        if score is not None:
            variables["score"] = score
        try:
            response_status, payload = await self.cog._anilist_request(
                (
                    ANILIST_SAVE_RATING_MUTATION
                    if score is not None
                    else ANILIST_SAVE_MEDIA_MUTATION
                ),
                variables,
                self.access_token,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        error_message = _graphql_error(payload)
        if response_status != 200 or error_message:
            detail = f" ({error_message})" if error_message else ""
            await interaction.followup.send(
                f"AniList could not update this anime{detail}.", ephemeral=True
            )
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        entry = data.get("SaveMediaListEntry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            await interaction.followup.send(
                "AniList did not return an updated list entry.", ephemeral=True
            )
            return False
        self.media["mediaListEntry"] = entry
        self._render()
        if source_message is not None:
            try:
                await source_message.edit(view=self)
            except discord.HTTPException:
                self.cog.bot.logger.debug(
                    "Could not refresh AniList media message after saving entry"
                )
        if update_original:
            await interaction.edit_original_response(view=self)
        return True


class AniListSettingsView(discord.ui.LayoutView):
    def __init__(self, ctx: Context, default_media: str) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.default_media = default_media
        self._render()

    def _render(self) -> None:
        self.clear_items()
        buttons: list[discord.ui.Button] = []
        for media_type, label in (
            ("anime", "Anime"),
            ("manga", "Manga"),
            ("characters", "Favourite Characters"),
        ):
            button = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.success
                    if media_type == self.default_media
                    else discord.ButtonStyle.primary
                ),
                disabled=media_type == self.default_media,
            )

            async def _select(
                interaction: discord.Interaction,
                selected: str = media_type,
            ) -> None:
                await self.ctx.bot.pool.execute(
                    """
                    INSERT INTO user_settings (user_id, anilist_default_media)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET anilist_default_media = EXCLUDED.anilist_default_media
                    """,
                    self.ctx.author.id,
                    selected,
                )
                self.default_media = selected
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = _select
            buttons.append(button)

        container = discord.ui.Container(
            discord.ui.TextDisplay("## AniList settings"),
            discord.ui.TextDisplay(
                "Choose which section should be shown first by default."
            ),
            discord.ui.TextDisplay("-# New settings coming soon."),
            discord.ui.ActionRow(*buttons),
            accent_color=self.ctx.bot.embedcolor,
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to change AniList settings.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False


def _profile_view(
    ctx: Context,
    profile: dict[str, Any],
    media_type: str = "anime",
    image_files: dict[str, str] | None = None,
) -> discord.ui.LayoutView:
    available_media = {
        media
        for media in ("anime", "manga")
        if int(_media_statistics(profile, media).get("count") or 0) > 0
    }
    if _favourite_character_entries(profile):
        available_media.add("characters")
    if media_type not in available_media and available_media:
        media_type = next(
            (
                media
                for media in ("anime", "manga", "characters")
                if media in available_media
            ),
            "anime",
        )
    return AniListProfileView(
        ctx,
        profile,
        media_type,
        available_media,
        image_files or {},
    )


class Anime(Cog):
    """Anime and AniList profile commands."""

    emoji = discord.PartialEmoji(name="\U0001f3ac")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def _top_media_file(
        self,
        entries: list[tuple[str, str | None, str | None, bool]],
        media_type: str,
        allow_adult_art: bool,
    ) -> discord.File | None:
        cover_urls: list[str] = []
        for _, _, cover, is_adult in entries:
            if cover and (allow_adult_art or not is_adult):
                cover_urls.append(cover)
            if len(cover_urls) >= 4:
                break
        if not cover_urls:
            return None

        async def _download(url: str) -> bytes:
            try:
                async with self.bot.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=20)
                ) as response:
                    if response.status == 200:
                        data = await _read_remote_image(response)
                        if data is not None:
                            return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self.bot.logger.debug(
                    "Could not fetch AniList %s cover: %s", media_type, url
                )
            return b""

        images = await asyncio.gather(*(_download(url) for url in cover_urls))
        image = await _stitch_media_row(list(images))
        if image is None:
            return None
        return discord.File(image, filename=f"anilist-{media_type}.png")

    async def _anilist_request(
        self,
        query: str,
        variables: dict[str, Any],
        access_token: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        try:
            async with self.bot.session.post(
                ANILIST_GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=headers,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, TypeError, aiohttp.ContentTypeError):
                    payload = None
                return response.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise commands.BadArgument(
                "AniList is temporarily unavailable. Please try again shortly."
            ) from error

    async def _lookup_media(self, ctx: Context, search: str, media_type: str):
        search = search.strip()
        if not search:
            raise commands.BadArgument(
                f"Provide a {media_type.lower()} title to search for."
            )
        account = await self.bot.pool.fetchrow(
            "SELECT anilist_access_token FROM accounts WHERE user_id = $1",
            ctx.author.id,
        )
        access_token = (
            decrypt_credential(account["anilist_access_token"]) if account else None
        )
        response_status, payload = await self._anilist_request(
            ANILIST_MEDIA_QUERY,
            {"search": search, "type": media_type},
            access_token,
        )
        error_message = _graphql_error(payload)
        if response_status == 429:
            raise commands.BadArgument(
                "AniList is currently rate limited. Please try again in a minute."
            )
        if response_status == 401:
            raise commands.BadArgument(
                "The AniList connection has expired. Please reconnect AniList "
                "with `fish link anilist`."
            )
        if response_status >= 500:
            raise commands.BadArgument(
                "AniList is temporarily unavailable. Please try again shortly."
            )
        if response_status != 200:
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(
                f"AniList rejected the {media_type.lower()} search{detail}."
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        page = data.get("Page") if isinstance(data, dict) else None
        results = page.get("media") if isinstance(page, dict) else None
        media = results[0] if isinstance(results, list) and results else None
        if not isinstance(media, dict):
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(
                f"No {media_type.lower()} found for **{search}**{detail}."
            )
        if access_token:
            # AniList stores the list score as a number, while the denominator
            # and smiley display are a user preference. Fetch that preference
            # only for connected users so anonymous searches stay one request.
            options_status, options_payload = await self._anilist_request(
                ANILIST_VIEWER_OPTIONS_QUERY,
                {},
                access_token,
            )
            if options_status == 200:
                options_data = (
                    options_payload.get("data")
                    if isinstance(options_payload, dict)
                    else None
                )
                viewer = (
                    options_data.get("Viewer")
                    if isinstance(options_data, dict)
                    else None
                )
                options = (
                    viewer.get("mediaListOptions") if isinstance(viewer, dict) else None
                )
                score_format = (
                    options.get("scoreFormat") if isinstance(options, dict) else None
                )
                if score_format:
                    media["_viewerScoreFormat"] = score_format
        await ctx.send(
            view=MediaLookupView(self, ctx, media, access_token, media_type.lower()),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _resolve_anilist_target(
        self, ctx: Context, user: object
    ) -> tuple[str, str | None]:
        """Resolve a Discord user or AniList username for an AniList request."""

        target_user: discord.User | discord.Member | None = None
        raw_target = user.strip() if isinstance(user, str) else ""
        if isinstance(user, (discord.User, discord.Member)):
            target_user = user
        elif not raw_target:
            target_user = ctx.author
        elif re.fullmatch(r"<@!?[0-9]+>|[0-9]{15,22}", raw_target):
            try:
                target_user = await commands.UserConverter().convert(ctx, raw_target)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "That Discord user could not be found."
                ) from error

        if target_user is None:
            username = raw_target.lstrip("@").strip()
            if not username:
                raise commands.BadArgument("Provide an AniList username.")
            return username, None

        account = await self.bot.pool.fetchrow(
            "SELECT anilist, anilist_access_token FROM accounts WHERE user_id = $1",
            target_user.id,
        )
        username = account["anilist"] if account else None
        if not username:
            if target_user.id == ctx.author.id:
                raise commands.BadArgument(
                    "Connect AniList first with `fish link anilist`."
                )
            raise commands.BadArgument(
                f"{target_user.display_name} has not connected an AniList account."
            )

        # Never use another user's OAuth token. Their public AniList profile/list
        # can still be looked up by username, while progress editing stays local.
        access_token = None
        if target_user.id == ctx.author.id and account:
            encrypted_token = account["anilist_access_token"]
            if encrypted_token:
                access_token = decrypt_credential(encrypted_token)
        return str(username), access_token

    async def _show_active_list(
        self, ctx: Context, media_type: str, user: object
    ) -> None:
        username, access_token = await self._resolve_anilist_target(ctx, user)

        async with ctx.typing():
            response_status, payload = await self._anilist_request(
                (
                    ANILIST_CURRENT_LIST_QUERY
                    if access_token
                    else ANILIST_CURRENT_LIST_PUBLIC_QUERY
                ),
                {
                    "userName": str(username),
                    "type": media_type,
                    "statuses": ["CURRENT", "REPEATING"],
                },
                access_token,
            )
        error_message = _graphql_error(payload)
        if response_status == 429:
            raise commands.BadArgument(
                "AniList is currently rate limited. Please try again in a minute."
            )
        if response_status == 401 and access_token:
            raise commands.BadArgument(
                "The AniList connection has expired. Please reconnect AniList "
                "with `fish link anilist`."
            )
        if response_status >= 500:
            raise commands.BadArgument(
                "AniList is temporarily unavailable. Please try again shortly."
            )
        if response_status != 200 or error_message:
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(f"AniList rejected the list request{detail}.")

        data = payload.get("data") if isinstance(payload, dict) else None
        collection = data.get("MediaListCollection") if isinstance(data, dict) else None
        lists = collection.get("lists") if isinstance(collection, dict) else None
        entries: list[dict[str, Any]] = []
        seen_media: set[str] = set()
        if isinstance(lists, list):
            for group in lists:
                if not isinstance(group, dict):
                    continue
                group_entries = group.get("entries")
                if not isinstance(group_entries, list):
                    continue
                for entry in group_entries:
                    if not isinstance(entry, dict) or not isinstance(
                        entry.get("media"), dict
                    ):
                        continue
                    media = entry["media"]
                    media_id = entry.get("mediaId") or media.get("id")
                    key = (
                        str(media_id) if media_id is not None else str(entry.get("id"))
                    )
                    if key in seen_media:
                        continue
                    seen_media.add(key)
                    entries.append(entry)

        viewer = data.get("Viewer") if isinstance(data, dict) else None
        options = viewer.get("mediaListOptions") if isinstance(viewer, dict) else None
        score_format = options.get("scoreFormat") if isinstance(options, dict) else None
        await ctx.send(
            view=MediaListView(
                self,
                ctx,
                entries,
                media_type.lower(),
                access_token,
                str(score_format) if score_format else None,
                str(username),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="character",
        aliases=("anime-character", "animecharacter", "acharacter", "achar"),
    )
    @app_commands.describe(search="The AniList character name to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def character(self, ctx: Context, *, search: str):
        """Look up an anime or manga character on AniList."""
        search = search.strip()
        if not search:
            raise commands.BadArgument("Provide a character name to search for.")

        async with ctx.typing():
            response_status, payload = await self._anilist_request(
                ANILIST_CHARACTER_QUERY,
                {"search": search},
            )
        error_message = _graphql_error(payload)
        if response_status == 429:
            raise commands.BadArgument(
                "AniList is currently rate limited. Please try again in a minute."
            )
        if response_status >= 500:
            raise commands.BadArgument(
                "AniList is temporarily unavailable. Please try again shortly."
            )
        if response_status != 200:
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(
                f"AniList rejected the character search{detail}."
            )

        data = payload.get("data") if isinstance(payload, dict) else None
        page = data.get("Page") if isinstance(data, dict) else None
        results = page.get("characters") if isinstance(page, dict) else None
        character = results[0] if isinstance(results, list) and results else None
        if not isinstance(character, dict):
            raise commands.BadArgument(f"No AniList character found for **{search}**.")
        async with ctx.typing():
            await ctx.send(
                view=CharacterLookupView(ctx, character),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.hybrid_command(name="anime")
    @app_commands.describe(search="The anime title to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def anime(self, ctx: Context, *, search: str):
        """Look up an anime and manage its AniList progress when connected."""
        async with ctx.typing():
            await self._lookup_media(ctx, search, "ANIME")

    @commands.hybrid_command(name="manga")
    @app_commands.describe(search="The manga title to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def manga(self, ctx: Context, *, search: str):
        """Look up manga and manage its AniList progress when connected."""
        async with ctx.typing():
            await self._lookup_media(ctx, search, "MANGA")

    @commands.hybrid_command(name="watching")
    @app_commands.describe(user="A Discord user mention or AniList username")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def watching(
        self,
        ctx: Context,
        user: str = commands.param(
            default=commands.Author,
            description="A Discord user mention or AniList username",
        ),
    ):
        """Show the anime you are currently watching or rewatching."""
        await self._show_active_list(ctx, "ANIME", user)

    @commands.hybrid_command(name="reading")
    @app_commands.describe(user="A Discord user mention or AniList username")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reading(
        self,
        ctx: Context,
        user: str = commands.param(
            default=commands.Author,
            description="A Discord user mention or AniList username",
        ),
    ):
        """Show the manga you are currently reading or rereading."""
        await self._show_active_list(ctx, "MANGA", user)

    @commands.hybrid_group(name="anilist", aliases=("ani",), fallback="profile")
    @app_commands.describe(user="A Discord user mention or AniList username")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def anilist(
        self,
        ctx: Context,
        user: str = commands.param(
            default=commands.Author,
            description="A Discord user mention or AniList username",
        ),
    ):
        """Show an AniList profile by Discord user or AniList username."""
        username, access_token = await self._resolve_anilist_target(ctx, user)

        async with ctx.typing():
            headers = (
                {"Authorization": f"Bearer {access_token}"} if access_token else None
            )
            async with self.bot.session.post(
                ANILIST_GRAPHQL_URL,
                json={
                    "query": ANILIST_PROFILE_QUERY,
                    "variables": {"name": str(username)},
                },
                headers=headers,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, TypeError, aiohttp.ContentTypeError):
                    payload = None

        payload_data = payload.get("data") if isinstance(payload, dict) else None
        profile = payload_data.get("User") if isinstance(payload_data, dict) else None
        error_message = _graphql_error(payload)
        if response.status == 429:
            raise commands.BadArgument(
                "AniList is currently rate limited. Please try again in a minute."
            )
        if response.status == 401:
            raise commands.BadArgument(
                "The AniList connection has expired. Please reconnect AniList "
                "with `fish link anilist`."
            )
        if response.status >= 500:
            raise commands.BadArgument(
                "AniList is temporarily unavailable. Please try again shortly."
            )
        if response.status != 200:
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(f"AniList rejected the profile request{detail}.")
        if not isinstance(profile, dict):
            detail = (
                f" ({discord.utils.escape_markdown(error_message)})"
                if error_message
                else ""
            )
            raise commands.BadArgument(
                f"Could not find the AniList profile for **{username}**{detail}."
            )
        preference = await self.bot.pool.fetchval(
            "SELECT anilist_default_media FROM user_settings WHERE user_id = $1",
            ctx.author.id,
        )
        preferred_media = (
            preference if preference in {"anime", "manga", "characters"} else "anime"
        )
        available_media = {
            media
            for media in ("anime", "manga")
            if int(_media_statistics(profile, media).get("count") or 0) > 0
        }
        if _favourite_character_entries(profile):
            available_media.add("characters")
        if preferred_media not in available_media and available_media:
            preferred_media = next(
                (
                    media
                    for media in ("anime", "manga", "characters")
                    if media in available_media
                ),
                "anime",
            )

        async with ctx.typing():
            image_files: dict[str, discord.File] = {}
            for media_type in available_media:
                entries = (
                    _favourite_character_entries(profile)
                    if media_type == "characters"
                    else _favourite_media_entries(profile, media_type)
                )
                image_file = await self._top_media_file(
                    entries,
                    media_type,
                    _context_allows_adult_art(ctx),
                )
                if image_file is not None:
                    image_files[media_type] = image_file

            view = _profile_view(
                ctx,
                profile,
                preferred_media,
                {media: image.filename for media, image in image_files.items()},
            )
            send_kwargs: dict[str, Any] = {
                "view": view,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if image_files:
                send_kwargs["files"] = list(image_files.values())
            await ctx.send(**send_kwargs)

    @anilist.command(name="settings")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def anilist_settings(self, ctx: Context):
        """Choose whether AniList profiles open on anime or manga details."""
        default_media = await self.bot.pool.fetchval(
            "SELECT anilist_default_media FROM user_settings WHERE user_id = $1",
            ctx.author.id,
        )
        if default_media not in {"anime", "manga", "characters"}:
            default_media = "anime"
        await ctx.send(
            view=AniListSettingsView(ctx, default_media),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Fishie):
    await bot.add_cog(Anime(bot))

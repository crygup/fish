"""Shared AniList title, date, and relation helpers."""

from __future__ import annotations

import datetime
import difflib
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

ANILIST_TEMPORARY_OUTAGE_MESSAGE = (
    "AniList is currently unavailable. Please try again in 10 minutes."
)


def anilist_temporarily_unavailable(status: int, payload: object) -> bool:
    """Recognize service outages separately from bad input or missing results."""

    if 500 <= status <= 599:
        return True
    if status != 403 or not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(error, dict)
        and "temporarily disabled" in str(error.get("message") or "").casefold()
        for error in errors
    )


def normalize_anilist_title(value: object) -> str:
    """Normalize an AniList title for comparisons.

    AniList title variants can differ in apostrophe style, punctuation, and
    whitespace.  Keep only normalized words so a copied long title such as
    ``Wasn't a Guy At All`` still matches AniList's curly-apostrophe variant.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Apostrophes are not semantically meaningful for title lookup.  Removing
    # them (rather than replacing them with spaces) makes ``wasn't`` and
    # ``wasnt`` equivalent while punctuation such as dashes remains a word
    # boundary.
    text = re.sub(r"['\u2019\u2018`´]", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def anilist_media_titles(media: dict[str, Any]) -> tuple[str, ...]:
    """Return all normalized title variants exposed by an AniList result."""

    title = media.get("title")
    values: list[str] = []
    if isinstance(title, dict):
        for key in ("userPreferred", "english", "romaji", "native"):
            value = title.get(key)
            normalized = normalize_anilist_title(value)
            if normalized and normalized not in values:
                values.append(normalized)
    fallback = normalize_anilist_title(media.get("name"))
    if fallback and fallback not in values:
        values.append(fallback)
    return tuple(values)


def anilist_search_variants(value: str) -> tuple[str, ...]:
    """Return de-duplicated AniList search strings for a title."""

    original = " ".join(str(value or "").strip().split())
    if not original:
        return ()
    punctuation_light = re.sub(r"[^\w\s]", " ", original, flags=re.UNICODE)
    punctuation_light = " ".join(punctuation_light.split())
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in (original, punctuation_light):
        key = normalize_anilist_title(candidate)
        if candidate and key not in seen:
            variants.append(candidate)
            seen.add(key)
    return tuple(variants)


def select_anilist_media(
    results: Iterable[object], search: str
) -> dict[str, Any] | None:
    """Choose the best matching result from an AniList search page.

    AniList's ``SEARCH_MATCH`` ordering is useful but not infallible for
    lengthy titles.  Prefer an exact normalized variant, then use a
    deterministic combination of token overlap and fuzzy similarity before
    falling back to the API's original ordering.
    """

    entries = [entry for entry in results if isinstance(entry, dict)]
    if not entries:
        return None
    wanted = normalize_anilist_title(search)
    if not wanted:
        return entries[0]

    wanted_tokens = set(wanted.split())

    def score(item: tuple[int, dict[str, Any]]) -> tuple[float, ...]:
        index, entry = item
        titles = anilist_media_titles(entry)
        if not titles:
            return (0.0, 0.0, 0.0, 0.0, 0.0, -float(index))
        exact = max(float(title == wanted) for title in titles)
        starts = max(
            float(title.startswith(wanted) or wanted.startswith(title))
            for title in titles
        )
        contains = max(float(wanted in title or title in wanted) for title in titles)
        ratio = max(
            difflib.SequenceMatcher(None, wanted, title).ratio() for title in titles
        )
        overlap = max(
            len(wanted_tokens & set(title.split()))
            / max(len(wanted_tokens | set(title.split())), 1)
            for title in titles
        )
        length_delta = -min(abs(len(wanted) - len(title)) for title in titles)
        # Keep the API's relevance order as the final deterministic tie-break.
        return (
            exact,
            starts,
            contains,
            ratio,
            overlap,
            float(length_delta),
            -float(index),
        )

    return dict(max(enumerate(entries), key=score)[1])


def anilist_datetime(value: object) -> datetime.datetime | None:
    """Convert an AniList date object to UTC.

    AniList may provide only a year or year/month.  Missing components are
    represented as the first day of the first month rather than discarding a
    useful release date.
    """

    if not isinstance(value, dict):
        return None
    try:
        year = int(value.get("year") or 0)
        if not year:
            return None
        month = int(value.get("month") or 1)
        day = int(value.get("day") or 1)
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def anilist_airing_datetime(
    value: object,
) -> tuple[datetime.datetime | None, int | None]:
    """Convert ``nextAiringEpisode`` data into UTC time and episode number."""

    if not isinstance(value, dict):
        return None, None
    try:
        episode = int(value["episode"]) if value.get("episode") else None
        airing_at = int(value["airingAt"]) if value.get("airingAt") else 0
        airing = (
            datetime.datetime.fromtimestamp(
                airing_at,
                tz=datetime.timezone.utc,
            )
            if airing_at > 0
            else None
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None, None
    return airing, episode


ANILIST_NOTIFICATION_STATUSES = frozenset({"RELEASING", "AIRING", "NOT_YET_RELEASED"})


def anilist_notification_schedule(
    media: dict[str, Any],
    now: datetime.datetime | None = None,
) -> tuple[
    datetime.datetime | None,
    int | None,
    datetime.datetime | None,
]:
    """Return the next valid notification time for an AniList anime.

    Finished, cancelled, and paused entries are deliberately excluded.  A
    not-yet-released entry is actionable only when its (possibly partial)
    start date is in the future.  Airing/releasing entries require a future
    ``nextAiringEpisode`` value.  This keeps a completed season from being
    followed just because AniList still returns its historical start date.
    """

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    status = str(media.get("status") or "").upper()
    if status not in ANILIST_NOTIFICATION_STATUSES:
        return None, None, None
    end_date = anilist_datetime(media.get("endDate"))
    if end_date is not None and end_date <= now:
        return None, None, None

    airing, episode = anilist_airing_datetime(media.get("nextAiringEpisode"))
    start_date = anilist_datetime(media.get("startDate"))
    if status == "NOT_YET_RELEASED":
        if start_date is not None and start_date > now:
            return None, None, start_date
        return None, None, None
    if status in {"AIRING", "RELEASING"}:
        if airing is not None and airing > now:
            return airing, episode, None
    return None, None, None


def anilist_successors(media: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sequel/successor media nodes from a relation response."""

    relations = media.get("relations")
    edges = relations.get("edges") if isinstance(relations, dict) else None
    if not isinstance(edges, list):
        return []
    current_id = str(media.get("id") or "")
    successors: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        relation_type = str(edge.get("relationType") or "").upper()
        if relation_type not in {"SEQUEL", "SUCCESSOR"}:
            continue
        node = edge.get("node")
        if not isinstance(node, dict) or str(node.get("id") or "") == current_id:
            continue
        successor = dict(node)
        successor["_relationType"] = relation_type
        successors.append(successor)
    return successors


def latest_anilist_successor(media: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest sequel/successor with schedule information."""

    successors = anilist_successors(media)
    if not successors:
        return None

    def _sort_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
        airing, _episode = anilist_airing_datetime(candidate.get("nextAiringEpisode"))
        start = anilist_datetime(candidate.get("startDate"))
        schedule = airing or start
        timestamp = int(schedule.timestamp()) if schedule else 0
        try:
            media_id = int(candidate.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            media_id = 0
        return (1 if airing else 0, timestamp, media_id)

    return max(successors, key=_sort_key)


__all__ = [
    "ANILIST_TEMPORARY_OUTAGE_MESSAGE",
    "anilist_airing_datetime",
    "anilist_datetime",
    "anilist_media_titles",
    "anilist_notification_schedule",
    "anilist_temporarily_unavailable",
    "ANILIST_NOTIFICATION_STATUSES",
    "anilist_search_variants",
    "anilist_successors",
    "latest_anilist_successor",
    "normalize_anilist_title",
    "select_anilist_media",
]

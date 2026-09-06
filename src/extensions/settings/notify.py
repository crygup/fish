"""Helpers and views used by the personal/server notification commands.

The command callbacks live in :mod:`settings.notify_commands`. Keeping
rendering and normalisation here keeps the settings cogs readable and gives
the event worker the same formatting helpers to use for delivery messages.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from typing import Any

import discord

from utils.anilist import (
    anilist_airing_datetime,
    anilist_datetime,
    anilist_media_titles,
    anilist_notification_schedule,
    anilist_search_variants,
    anilist_successors,
    latest_anilist_successor,
    normalize_anilist_title,
    select_anilist_media,
)

TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
TWITCH_URL_RE = re.compile(
    r"https?://(?:www\.)?twitch\.tv/(?P<name>[A-Za-z0-9_]{1,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)
ANILIST_MEDIA_URL_RE = re.compile(
    r"https?://(?:www\.)?anilist\.co/(?P<kind>anime|manga)/(?P<id>[0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)

# Notification lookups need the banner and external links in addition to the
# fields used by the regular anime command.  Keeping this query here avoids
# coupling settings to the presentation query in ``extensions.anime``.
ANILIST_NOTIFY_QUERY = """
query ($search: String!, $type: MediaType!) {
  Page(perPage: 10) {
    media(search: $search, type: $type, sort: SEARCH_MATCH) {
      id
      type
      status
      siteUrl
      title { userPreferred english romaji native }
      bannerImage
      coverImage { extraLarge }
      startDate { year month day }
      endDate { year month day }
      nextAiringEpisode { airingAt episode }
      externalLinks { site url }
      relations {
        edges {
          relationType
          node {
            id
            type
            status
            siteUrl
            title { userPreferred english romaji native }
            startDate { year month day }
            endDate { year month day }
            nextAiringEpisode { airingAt episode }
          }
        }
      }
    }
  }
}
"""
ANILIST_NOTIFY_BY_ID_QUERY = """
query ($id: Int!) {
  Media(id: $id) {
    id
    type
    status
    siteUrl
    title { userPreferred english romaji native }
    bannerImage
    coverImage { extraLarge }
    startDate { year month day }
    endDate { year month day }
    nextAiringEpisode { airingAt episode }
    externalLinks { site url }
    relations {
      edges {
        relationType
        node {
          id
          type
          status
          siteUrl
          title { userPreferred english romaji native }
          startDate { year month day }
          endDate { year month day }
          nextAiringEpisode { airingAt episode }
        }
      }
    }
  }
}
"""
# Autocomplete runs while a user is typing, so keep this query deliberately
# small.  The full notification query above includes relations and external
# links needed when a follow is saved; suggestions only need a stable ID and
# human-readable titles.
ANILIST_AUTOCOMPLETE_QUERY = """
query ($search: String!) {
  Page(perPage: 25) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { userPreferred english romaji native }
    }
  }
}
"""
ANILIST_NOTIFY_BATCH_QUERY = """
query ($ids: [Int!]!) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      type
      status
      siteUrl
      title { userPreferred english romaji native }
      bannerImage
      coverImage { extraLarge }
      startDate { year month day }
      endDate { year month day }
      nextAiringEpisode { airingAt episode }
      externalLinks { site url }
      relations {
        edges {
          relationType
          node {
            id
            type
            status
            siteUrl
            title { userPreferred english romaji native }
            startDate { year month day }
            endDate { year month day }
            nextAiringEpisode { airingAt episode }
          }
        }
      }
    }
  }
}
"""


def normalize_twitch_channel(value: str) -> str:
    """Return a canonical Twitch login from a login, mention, or URL."""

    value = value.strip().lstrip("@")
    match = TWITCH_URL_RE.fullmatch(value)
    if match:
        value = match.group("name")
    if not TWITCH_CHANNEL_RE.fullmatch(value):
        raise ValueError("Enter a valid Twitch channel name or Twitch URL.")
    return value.casefold()


def anilist_media_id(value: str) -> int | None:
    """Extract an AniList media ID from a canonical AniList URL."""

    match = ANILIST_MEDIA_URL_RE.fullmatch(value.strip())
    if not match:
        return None
    try:
        return int(match.group("id"))
    except ValueError:
        return None


def media_title(media: dict[str, Any]) -> str:
    title = media.get("title")
    if isinstance(title, dict):
        # Notifications are intended for a broad audience.  Prefer AniList's
        # English title when it is available, while retaining the preferred
        # (usually romaji) title as a fallback for entries that have not been
        # translated.
        for key in ("english", "userPreferred", "romaji", "native"):
            value = title.get(key)
            if value:
                return str(value).strip()
    return str(media.get("name") or "Unknown anime").strip()


def media_external_link(media: dict[str, Any], site: str) -> str | None:
    """Find an AniList external link by site name (case-insensitive)."""

    links = media.get("externalLinks")
    if not isinstance(links, list):
        return None
    needle = site.casefold()
    for link in links:
        if not isinstance(link, dict):
            continue
        if str(link.get("site") or "").casefold() == needle:
            url = link.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None


def _safe_text(value: object, limit: int = 2_000) -> str:
    text = discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ""))
    )
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _timestamp(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = int(value)
        else:
            parsed = discord.utils.parse_time(str(value))
            if parsed is None:
                return None
            timestamp = int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


def _as_datetime(timestamp: int) -> datetime.datetime:
    """Convert a Unix timestamp to an aware datetime for ``format_dt``."""

    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)


def notify_allowed_mentions(
    mention_role_id: object, mention_everyone: object
) -> discord.AllowedMentions:
    """Allow only the configured role or ``@everyone`` notification mention."""

    everyone = bool(mention_everyone)
    roles: list[discord.Role | discord.Object] = []
    if isinstance(mention_role_id, (str, int)):
        try:
            roles.append(discord.Object(id=int(mention_role_id)))
        except (TypeError, ValueError, OverflowError):
            roles = []
    return discord.AllowedMentions(
        everyone=everyone,
        roles=roles,
        users=False,
        replied_user=False,
    )


def mention_text(mention_role_id: object, mention_everyone: object) -> str | None:
    if bool(mention_everyone):
        return "@everyone"
    if isinstance(mention_role_id, (str, int)):
        try:
            return f"<@&{int(mention_role_id)}>"
        except (TypeError, ValueError, OverflowError):
            return None
    return None


class NotifyView(discord.ui.LayoutView):
    """Simple Components V2 panel shared by notify list/info responses."""

    def __init__(
        self,
        title: str,
        details: str,
        *,
        image: str | None = None,
        links: Iterable[tuple[str, str]] = (),
        mention: str | None = None,
        accent_color: discord.Colour | int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        if isinstance(accent_color, int):
            accent_color = discord.Colour(accent_color)
        children: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(title)]
        if details:
            children.append(discord.ui.TextDisplay(details))
        if image:
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image)))
        link_items = [
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.link, url=url)
            for label, url in links
            if isinstance(url, str) and url.startswith(("http://", "https://"))
        ]
        if link_items:
            # Action rows support at most five buttons.
            for index in range(0, len(link_items), 5):
                children.append(discord.ui.ActionRow(*link_items[index : index + 5]))
        if mention:
            children.extend(
                (discord.ui.Separator(), discord.ui.TextDisplay(f"-# {mention}"))
            )
        self.add_item(discord.ui.Container(*children, accent_color=accent_color))


class NotifyListView(discord.ui.LayoutView):
    """Components V2 panel for the combined Twitch/anime follow list."""

    def __init__(
        self,
        title: str,
        sections: Iterable[tuple[str, str]],
        *,
        mention: str | None = None,
        accent_color: discord.Colour | int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        if isinstance(accent_color, int):
            accent_color = discord.Colour(accent_color)
        # Keep the heading visually separate from the first follow section,
        # then separate the Twitch and anime sections below it.
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(title),
            discord.ui.Separator(),
        ]
        for index, (heading, details) in enumerate(sections):
            if index:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(f"### {heading}\n{details}"))
        if mention:
            children.extend(
                (discord.ui.Separator(), discord.ui.TextDisplay(f"-# {mention}"))
            )
        self.add_item(discord.ui.Container(*children, accent_color=accent_color))


def twitch_list_details(rows: Iterable[Any]) -> str:
    lines: list[str] = []
    for row in rows:
        follow_id = row.get("id")
        if follow_id is None:
            # Rows created by the notify tables always have an ID.  Keep the
            # fallback readable if an older database adapter omits it rather
            # than exposing the Python ``None`` representation.
            follow_id = "?"
        name = _safe_text(row["channel_name"], 80)
        destination = (
            f"<#{row['announce_channel_id']}>"
            if row.get("announce_channel_id")
            else "DM"
        )
        last_live = _timestamp(row.get("last_live_at") or row.get("last_offline_at"))
        when = (
            f" · last live {discord.utils.format_dt(_as_datetime(last_live), 'R')}"
            if last_live
            else ""
        )
        mention = mention_text(row.get("mention_role_id"), row.get("mention_everyone"))
        metadata = f"{destination}{when}"
        if mention:
            metadata = f"{destination} · {mention}{when}"
        lines.append(f"{follow_id} · {name}\n-# *{metadata}*")
    return "\n".join(lines)


def anime_list_details(rows: Iterable[Any]) -> str:
    lines: list[str] = []
    for row in rows:
        follow_id = row.get("id")
        if follow_id is None:
            follow_id = "?"
        title = _safe_text(row["title"], 120)
        airing = _timestamp(row.get("next_airing_at") or row.get("release_at"))
        episode = row.get("next_episode")
        if airing:
            release = discord.utils.format_dt(_as_datetime(airing), "R")
            suffix = (
                f" · Episode {episode} · Releases {release}"
                if episode
                else f" · Releases {release}"
            )
        else:
            suffix = " · no upcoming episode"
        destination = (
            f"<#{row['announce_channel_id']}>"
            if row.get("announce_channel_id")
            else "DM"
        )
        mention = mention_text(row.get("mention_role_id"), row.get("mention_everyone"))
        metadata = f"{destination}{suffix}"
        if mention:
            metadata = f"{destination} · {mention}{suffix}"
        lines.append(f"{follow_id} · {title}\n-# *{metadata}*")
    return "\n".join(lines)


__all__ = [
    "ANILIST_MEDIA_URL_RE",
    "ANILIST_AUTOCOMPLETE_QUERY",
    "ANILIST_NOTIFY_BY_ID_QUERY",
    "ANILIST_NOTIFY_BATCH_QUERY",
    "ANILIST_NOTIFY_QUERY",
    "NotifyView",
    "NotifyListView",
    "anilist_airing_datetime",
    "anilist_datetime",
    "anilist_media_titles",
    "anilist_notification_schedule",
    "anilist_media_id",
    "anilist_search_variants",
    "anilist_successors",
    "anime_list_details",
    "latest_anilist_successor",
    "media_external_link",
    "media_title",
    "mention_text",
    "normalize_anilist_title",
    "normalize_twitch_channel",
    "notify_allowed_mentions",
    "select_anilist_media",
    "twitch_list_details",
]

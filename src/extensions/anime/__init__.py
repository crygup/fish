from __future__ import annotations

import asyncio
import html
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
      anime { nodes { siteUrl coverImage { extraLarge } title { romaji } } }
      manga { nodes { siteUrl coverImage { extraLarge } title { romaji } } }
      characters { nodes { siteUrl image { large } name { full } } }
    }
  }
}
"""
ANILIST_MEDIA_QUERY = """
query ($search: String!, $type: MediaType!) {
  Page(perPage: 1) {
    media(search: $search, type: $type, sort: SEARCH_MATCH) {
      id
      siteUrl
      title { romaji english native userPreferred }
      description
      format
      status
      episodes
      chapters
      volumes
      duration
      season
      seasonYear
      averageScore
      genres
      coverImage { extraLarge }
      mediaListEntry { id status progress }
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


def _clean_about(value: str | None) -> str:
    if not value:
        return "No profile bio provided."
    text = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
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

    text = re.sub(r"---+|\|", _protect_literal, text)
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    for index, item in enumerate(protected):
        text = text.replace(f"\x00{index}\x00", item)
    if len(text) > 900:
        text = text[:897].rstrip() + "..."
    return text or "No profile bio provided."


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


def _media_status_label(status: str | None, media_kind: str) -> str:
    labels = {value: label for value, label, _ in ANILIST_LIST_STATUSES}
    if status == "CURRENT":
        return "Watching" if media_kind == "anime" else "Reading"
    return labels.get(status or "", "Not on list")


def _media_progress_text(media: dict[str, Any], media_kind: str) -> str:
    entry = media.get("mediaListEntry")
    progress = int(entry.get("progress") or 0) if isinstance(entry, dict) else 0
    field = "episodes" if media_kind == "anime" else "chapters"
    unit = "episodes" if media_kind == "anime" else "chapters"
    total = media.get(field)
    if total:
        return f"**Progress:** {progress:,}/{int(total):,} {unit}"
    return f"**Progress:** {progress:,} {unit}"


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
) -> list[tuple[str, str | None, str | None]]:
    favourites_data = profile.get("favourites")
    if not isinstance(favourites_data, dict):
        return []
    media_favourites = favourites_data.get(media_type)
    if not isinstance(media_favourites, dict):
        return []
    nodes = media_favourites.get("nodes") or []
    entries: list[tuple[str, str | None, str | None]] = []
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
            )
        )
    return entries


def _favourite_entries(
    profile: dict[str, Any], media_type: str
) -> list[tuple[str, str | None]]:
    return [
        (title, url) for title, url, _ in _favourite_media_entries(profile, media_type)
    ]


def _favourite_character_entries(
    profile: dict[str, Any],
) -> list[tuple[str, str | None, str | None]]:
    favourites_data = profile.get("favourites")
    if not isinstance(favourites_data, dict):
        return []
    characters = favourites_data.get("characters")
    if not isinstance(characters, dict):
        return []
    entries: list[tuple[str, str | None, str | None]] = []
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
            favourites = [(name, url) for name, url, _ in character_entries]
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
                await interaction.response.edit_message(view=self)

            button.callback = _switch
            buttons.append(button)
        children.append(discord.ui.ActionRow(*buttons))

        container = discord.ui.Container(
            *children, accent_color=self.ctx.bot.embedcolor
        )
        self.add_item(container)


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
            f"https://anilist.co/anime/{self.media.get('id')}"
        )
        title_text = f"## [{discord.utils.escape_markdown(title)}]({site_url})"
        cover = (self.media.get("coverImage") or {}).get("extraLarge")
        description = _clean_about(self.media.get("description"))
        details: list[str] = []
        if self.media.get("format"):
            details.append(f"**Format:** {self.media['format']}")
        if self.media.get("status"):
            details.append(f"**Release status:** {self.media['status']}")
        progress_field = "episodes" if self.media_kind == "anime" else "chapters"
        progress_label = "Episodes" if self.media_kind == "anime" else "Chapters"
        if self.media.get(progress_field):
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
                    discord.ui.TextDisplay(description),
                    accessory=discord.ui.Thumbnail(str(cover)),
                )
            )
        else:
            children.extend(
                [
                    discord.ui.TextDisplay(profile_text),
                    discord.ui.TextDisplay(description),
                ]
            )
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
                    discord.ui.TextDisplay(
                        f"**List status:** {_media_status_label(status, self.media_kind)}\n"
                        f"{_media_progress_text(self.media, self.media_kind)}"
                    )
                ]
            )
            options = [
                discord.SelectOption(
                    label=(
                        "Watching"
                        if value == "CURRENT" and self.media_kind == "anime"
                        else "Reading" if value == "CURRENT" else label
                    ),
                    description=(
                        "Currently watching this anime"
                        if value == "CURRENT" and self.media_kind == "anime"
                        else (
                            "Currently reading this manga"
                            if value == "CURRENT"
                            else (
                                "Plan to watch this anime"
                                if value == "PLANNING" and self.media_kind == "anime"
                                else (
                                    "Plan to read this manga"
                                    if value == "PLANNING"
                                    else (
                                        "Finished watching this anime"
                                        if value == "COMPLETED"
                                        and self.media_kind == "anime"
                                        else (
                                            "Finished reading this manga"
                                            if value == "COMPLETED"
                                            else (
                                                "Stopped watching this anime"
                                                if value == "DROPPED"
                                                and self.media_kind == "anime"
                                                else (
                                                    "Stopped reading this manga"
                                                    if value == "DROPPED"
                                                    else description_text
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    ),
                    value=value,
                    default=status == value,
                )
                for value, label, description_text in ANILIST_LIST_STATUSES
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
            minus.callback = self._decrease_progress
            plus.callback = self._increase_progress
            jump.callback = self._open_progress_modal
            children.append(discord.ui.ActionRow(minus, plus, jump))
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

    async def _save_entry(
        self,
        interaction: discord.Interaction,
        *,
        status: str | None = None,
        progress: int | None = None,
    ) -> None:
        variables: dict[str, Any] = {"mediaId": int(self.media["id"])}
        if status is not None:
            variables["status"] = status
        if progress is not None:
            variables["progress"] = progress
        try:
            response_status, payload = await self.cog._anilist_request(
                ANILIST_SAVE_MEDIA_MUTATION,
                variables,
                self.access_token,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        error_message = _graphql_error(payload)
        if response_status != 200 or error_message:
            detail = f" ({error_message})" if error_message else ""
            await interaction.followup.send(
                f"AniList could not update this anime{detail}.", ephemeral=True
            )
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        entry = data.get("SaveMediaListEntry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            await interaction.followup.send(
                "AniList did not return an updated list entry.", ephemeral=True
            )
            return
        self.media["mediaListEntry"] = entry
        self._render()
        await interaction.edit_original_response(view=self)


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
                await interaction.response.edit_message(view=self)

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
            "Run the command yourself to change AniList settings.", ephemeral=True
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
        entries: list[tuple[str, str | None, str | None]],
        media_type: str,
    ) -> discord.File | None:
        cover_urls = [cover for _, _, cover in entries[:4] if cover]
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
        async with ctx.typing():
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
        await ctx.send(
            view=MediaLookupView(self, ctx, media, access_token, media_type.lower()),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="anime")
    @app_commands.describe(search="The anime title to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def anime(self, ctx: Context, *, search: str):
        """Look up an anime and manage its AniList progress when connected."""
        await self._lookup_media(ctx, search, "ANIME")

    @commands.hybrid_command(name="manga")
    @app_commands.describe(search="The manga title to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def manga(self, ctx: Context, *, search: str):
        """Look up manga and manage its AniList progress when connected."""
        await self._lookup_media(ctx, search, "MANGA")

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
        target_user: discord.User | discord.Member | None = None
        raw_target = user.strip() if isinstance(user, str) else ""
        if isinstance(user, discord.User):
            target_user = user
        elif not raw_target:
            target_user = ctx.author
        elif raw_target:
            mention = re.fullmatch(r"<@!?([0-9]+)>", raw_target)
            if mention:
                target_user = self.bot.get_user(int(mention.group(1)))
                if target_user is None:
                    try:
                        target_user = await self.bot.fetch_user(int(mention.group(1)))
                    except discord.HTTPException:
                        raise commands.BadArgument(
                            "That Discord user could not be found."
                        )

        if target_user is not None:
            account = await self.bot.pool.fetchrow(
                "SELECT anilist, anilist_access_token FROM accounts WHERE user_id = $1",
                target_user.id,
            )
            username = account["anilist"] if account else None
            access_token = (
                decrypt_credential(account["anilist_access_token"])
                if account
                else None
            )
            if not username:
                raise commands.BadArgument(
                    f"{target_user.display_name} has not connected an AniList account."
                )
        else:
            username = raw_target.lstrip("@") or ctx.author.name
            access_token = None

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
        await ctx.send(view=AniListSettingsView(ctx, default_media))


async def setup(bot: Fishie):
    await bot.add_cog(Anime(bot))

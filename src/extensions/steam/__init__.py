from __future__ import annotations

import asyncio
import datetime
import html
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlencode, urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils.converters import SteamConverter, SteamGroupConverter
from utils.paginator import SimplePages

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


STEAM_ICON = "https://cdn.discordapp.com/emojis/1095791386999132231.png"
STEAM_BADGE_AVERAGE_COST_USD = 0.55
STEAM_BADGE_MAX_LEVEL = 1_000_000


def _steam_level_xp(level: int) -> int:
    """Return the XP required to reach a Steam profile level.

    Steam requires 100 XP per level through level 10, then increases the
    per-level requirement by 100 XP for each completed ten-level tier.
    """
    if level <= 0:
        return 0
    completed_tiers, remainder = divmod(level, 10)
    tier_xp = 1_000 * completed_tiers * (completed_tiers + 1) // 2
    remainder_xp = remainder * (completed_tiers + 1) * 100
    return tier_xp + remainder_xp


def _steam_badge_estimate(
    starting_level: int, desired_level: int
) -> tuple[int, int, float]:
    """Return (XP needed, badges needed, estimated USD cost)."""
    xp_needed = max(
        0,
        _steam_level_xp(desired_level) - _steam_level_xp(starting_level),
    )
    badges = xp_needed // 100
    return xp_needed, badges, round(badges * STEAM_BADGE_AVERAGE_COST_USD, 2)


def _steam_clean_text(value: object, limit: int = 1_500) -> str:
    """Convert Steam's HTML snippets to safe Discord component text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</?(?:p|div|li|ul|ol|h[1-6])(?:\s[^>]*)?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _steam_safe_url(value: object) -> str | None:
    """Only expose normal HTTP(S) links returned by Steam."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _steam_percent(positive: object, total: object) -> str:
    try:
        positive_count = int(str(positive or 0))
        total_count = int(str(total or 0))
    except (TypeError, ValueError):
        return "0%"
    if total_count <= 0:
        return "0%"
    return f"{positive_count / total_count * 100:.1f}%"


def _steam_playtime_text(minutes: int) -> str:
    hours, remainder = divmod(max(0, minutes), 60)
    if hours and remainder:
        return f"{hours:,}h {remainder}m"
    if hours:
        return f"{hours:,}h"
    return f"{remainder}m"


def _steam_game_is_free(game: dict) -> bool:
    """Return whether Steam identifies a game as free to play.

    Steam does not expose the same pricing fields for every free title. Some
    apps set ``is_free_app``, while others only mark a package group or a
    zero-value price as free. Keep those representations together so a valid
    free game is not rendered as ``Price unavailable``.
    """
    if game.get("is_free_app") is True or game.get("is_free") is True:
        return True

    package_groups = game.get("package_groups")
    if isinstance(package_groups, list) and any(
        isinstance(group, dict) and group.get("is_free") is True
        for group in package_groups
    ):
        return True

    price = game.get("price_overview")
    if isinstance(price, dict) and price.get("final") is not None:
        try:
            if int(str(price.get("final"))) == 0:
                return True
        except (TypeError, ValueError):
            pass

    for collection_key in ("genres", "categories"):
        collection = game.get(collection_key)
        if isinstance(collection, list) and any(
            isinstance(item, dict)
            and str(item.get("description") or "").casefold()
            in {"free to play", "free-to-play"}
            for item in collection
        ):
            return True
    return False


def _steam_format_review_summary(reviews: dict) -> str:
    payload = reviews.get("overall", reviews)
    if not isinstance(payload, dict):
        payload = {}
    summary = payload.get("query_summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    store_summary = reviews.get("_store_summary")
    if isinstance(store_summary, dict):
        try:
            api_total = int(str(summary.get("total_reviews") or 0))
        except (TypeError, ValueError):
            api_total = 0
        try:
            store_total = int(str(store_summary.get("total_reviews") or 0))
        except (TypeError, ValueError):
            store_total = 0
        # Steam's appreviews endpoint can return a language-bucket count that
        # is much smaller than the total shown on the public store page.
        if store_total > api_total:
            summary = store_summary
    try:
        total = int(str(summary.get("total_reviews") or 0))
    except (TypeError, ValueError):
        total = 0
    try:
        total_positive = int(str(summary.get("total_positive") or 0))
    except (TypeError, ValueError):
        total_positive = 0
    description = _steam_clean_text(summary.get("review_score_desc"), 80) or "No rating"
    review_text = (
        f"{description} · {_steam_percent(total_positive, total)} "
        f"({total_positive:,} positive / {total:,} total)"
        if total
        else "No reviews"
    )
    return f"**Reviews:** {review_text}"


def _steam_store_review_summary(page_html: str) -> dict[str, object]:
    """Extract the primary review total from Steam's public store markup."""
    if not page_html:
        return {}

    def hidden_value(field_id: str) -> int | None:
        match = re.search(
            rf'<input[^>]+id=["\']{re.escape(field_id)}["\'][^>]+value=["\'](\d+)["\']',
            page_html,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    total = hidden_value("review_summary_num_reviews")
    positive = hidden_value("review_summary_num_positive_reviews")
    if total is None:
        match = re.search(
            r'class=["\'][^"\']*app_reviews_count[^"\']*["\'][^>]*>\s*\(([\d,]+)\s+reviews?\)',
            page_html,
            flags=re.IGNORECASE,
        )
        if match:
            total = int(match.group(1).replace(",", ""))
    if total is None:
        return {}

    description = ""
    match = re.search(
        r'class=["\'][^"\']*game_review_summary[^"\']*["\'][^>]*>(.*?)</span>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        description = _steam_clean_text(match.group(1), 80)
    return {
        "total_reviews": total,
        "total_positive": positive or 0,
        "review_score_desc": description or "No rating",
    }


def _steam_free_to_keep_games(page_html: str) -> list[str]:
    """Return safe markdown entries for paid games currently discounted to free."""
    soup = BeautifulSoup(page_html, "html.parser")
    entries: list[str] = []
    seen: set[int] = set()
    for row in soup.select("a.search_result_row[data-ds-appid]"):
        raw_app_id = row.get("data-ds-appid")
        if not isinstance(raw_app_id, str) or not raw_app_id.isdecimal():
            continue
        app_id = int(raw_app_id)
        if app_id in seen:
            continue
        original = row.select_one(".discount_original_price")
        discount = row.select_one(".discount_block")
        if original is None or discount is None:
            continue
        if str(discount.get("data-price-final") or "") != "0":
            continue
        title_node = row.select_one(".title")
        title = _steam_clean_text(
            title_node.get_text(" ", strip=True) if title_node else "", 120
        )
        if not title:
            continue
        original_price = _steam_clean_text(original.get_text(" ", strip=True), 40)
        url = f"https://store.steampowered.com/app/{app_id}/"
        entries.append(f"[{title}]({url}) · ~~{original_price}~~ → **Free**")
        seen.add(app_id)
    return entries


def _steam_profile_asset_urls(page_html: str) -> dict[str, str]:
    """Find public profile showcase assets from the Steam profile page."""
    assets: dict[str, str] = {}
    markers = (
        ("Background", "profile_background"),
        ("Avatar background", "avatar_background"),
        ("Avatar frame", "avatar_frame"),
        ("Border", "profile_border"),
        ("Profile item", "profile_item"),
    )
    url_pattern = re.compile(r"""https?://[^"'\s)]+""")
    for label, marker in markers:
        match = re.search(marker, page_html, flags=re.IGNORECASE)
        if not match:
            continue
        block = page_html[match.start() : match.start() + 4_000]
        for candidate in url_pattern.findall(block):
            candidate = html.unescape(candidate).replace("\\/", "/")
            url = _steam_safe_url(candidate)
            if url:
                assets[label] = url
                break
    return assets


def _steam_favorite_games(page_html: str) -> list[tuple[str, str]]:
    """Extract favorite game links shown in a public profile showcase."""
    starts = [
        match.start()
        for match in re.finditer(r"favorite[_-]?game", page_html, flags=re.IGNORECASE)
    ]
    if not starts:
        return []

    games: list[tuple[str, str]] = []
    seen: set[str] = set()
    link_pattern = re.compile(
        r'(?is)<a[^>]+href="(?P<url>https?://store\.steampowered\.com/app/\d+/[^\"]*)"[^>]*>(?P<body>.*?)</a>'
    )
    for start in starts:
        block = page_html[start : start + 30_000]
        for match in link_pattern.finditer(block):
            url = _steam_safe_url(html.unescape(match.group("url")))
            if not url or url in seen:
                continue
            opening_tag = match.group(0).split(">", 1)[0]
            title_match = re.search(
                r'(?:title|aria-label)="([^"]+)"', opening_tag, flags=re.IGNORECASE
            )
            name_source = title_match.group(1) if title_match else match.group("body")
            name = _steam_clean_text(name_source, 100)
            if not name:
                name = "Steam game"
            seen.add(url)
            games.append((name, url))
            if len(games) >= 5:
                return games
    return games


def _steam_featured_badge(page_html: str) -> str | None:
    """Read the featured badge title when Steam exposes one publicly."""
    for marker in ("profile_featuredbadge", "featured_badge", "profile_badges_badge"):
        match = re.search(marker, page_html, flags=re.IGNORECASE)
        if not match:
            continue
        block = page_html[match.start() : match.start() + 12_000]
        title_match = re.search(
            r'(?is)class="[^"]*badge_title[^"]*"[^>]*>(.*?)</', block
        )
        if not title_match:
            continue
        title = _steam_clean_text(title_match.group(1), 120)
        if not title:
            continue
        level_match = re.search(
            r'(?is)class="[^"]*badge_level[^"]*"[^>]*>\s*(\d+)', block
        )
        return f"{title} (Level {level_match.group(1)})" if level_match else title
    return None


class SteamUserView(discord.ui.LayoutView):
    """Components V2 profile view for a Steam user."""

    def __init__(
        self,
        ctx: Context,
        sid: str,
        data: dict,
        friends: int | None,
        recent_games: list,
    ) -> None:
        super().__init__(timeout=600)
        self.ctx = ctx
        self.sid = sid
        self.data = data
        self.friends = friends
        self.recent_games = recent_games
        self.message: discord.Message | None = None
        self._active = "profile"
        self.title = discord.ui.TextDisplay(self._profile_title())
        self.details = discord.ui.TextDisplay(self._summary())
        self.container = discord.ui.Container(accent_color=self.ctx.bot.embedcolor)
        avatar = _steam_safe_url(data.get("avatarfull"))
        if avatar:
            self.container.add_item(
                discord.ui.Section(
                    self.title,
                    self.details,
                    accessory=discord.ui.Thumbnail(avatar),
                )
            )
        else:
            self.container.add_item(self.title)
            self.container.add_item(self.details)
        self.container.add_item(discord.ui.Separator())
        self.stats = discord.ui.TextDisplay(self._profile_stats())
        self.container.add_item(self.stats)
        created = data.get("timecreated")
        created_text = ""
        try:
            if created:
                created_text = f" · Created: <t:{int(created)}:D>"
        except (TypeError, ValueError):
            pass
        self.container.add_item(
            discord.ui.TextDisplay(
                f"-# Steam ID: {sid}{created_text} · Data from Steam"
            )
        )
        self.add_item(self.container)

        buttons = [
            discord.ui.Button(
                label="Steam profile",
                style=discord.ButtonStyle.link,
                url=f"https://steamcommunity.com/profiles/{sid}/",
            )
        ]
        for label, url in (data.get("_assets") or {}).items():
            safe_url = _steam_safe_url(url)
            if safe_url:
                buttons.append(
                    discord.ui.Button(
                        label=label[:80],
                        style=discord.ButtonStyle.link,
                        url=safe_url,
                    )
                )
        self.recent_btn: discord.ui.Button | None = None
        if recent_games:
            self.recent_btn = discord.ui.Button(
                label="Recently Played", style=discord.ButtonStyle.blurple
            )
            self.recent_btn.callback = self._toggle_recent
            buttons.append(self.recent_btn)
        for index in range(0, len(buttons), 5):
            self.add_item(discord.ui.ActionRow(*buttons[index : index + 5]))

    def _summary(self) -> str:
        summary = discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(self.data.get("_summary") or ""))
        ).strip()
        for key, value in (self.data.get("_links") or {}).items():
            if not isinstance(value, tuple) or len(value) != 2:
                continue
            text, url = value
            safe_url = _steam_safe_url(url)
            if safe_url:
                summary = summary.replace(
                    key,
                    f"[{discord.utils.escape_markdown(str(text))}]({safe_url})",
                )
        return summary or "No profile description provided."

    def _profile_title(self) -> str:
        name = _steam_clean_text(self.data.get("personaname") or "Unknown", 120)
        return f"## {name}"

    def _status_details(self) -> str | None:
        status_text = {
            0: "Offline",
            1: "Online",
            2: "Busy",
            3: "Away",
            4: "Snooze",
            5: "Looking to trade",
            6: "Looking to play",
        }
        try:
            state = int(self.data.get("personastate", 0) or 0)
        except (TypeError, ValueError):
            state = 0
        if state == 0:
            try:
                last_online = int(self.data.get("lastlogoff") or 0)
            except (TypeError, ValueError):
                last_online = 0
            return f"**Last online:** <t:{last_online}:R>" if last_online else None
        return f"**Status:** {status_text.get(state, 'Online')}"

    def _profile_stats(self) -> str:
        lines: list[str] = []
        if status_details := self._status_details():
            lines.append(status_details)
        badge_count = self.data.get("_badge_count")
        if badge_count is not None:
            lines.append(f"**Badges:** {int(badge_count):,}")
        if featured := self.data.get("_featured_badge"):
            lines.append(f"**Featured badge:** {featured}")
        game_count = self.data.get("_game_count")
        if game_count is not None:
            lines.append(f"**Games:** {int(game_count):,}")
        if self.friends is not None:
            lines.append(f"**Friends:** {self.friends:,}")
        groups = self.data.get("_groups")
        if groups:
            lines.append(f"**Groups:** {int(groups):,}")
        if level := self.data.get("_level"):
            lines.append(f"**Level:** {int(level):,}")
        favorites = self.data.get("_favorite_games") or []
        favorite_links = [
            f"[{discord.utils.escape_markdown(name)}]({url})"
            for name, url in favorites
            if _steam_safe_url(url)
        ]
        if favorite_links:
            lines.append(f"**Favourite game(s):** {', '.join(favorite_links)}")
        if playing := self.data.get("gameextrainfo"):
            lines.append(f"**Playing:** {_steam_clean_text(playing, 120)}")
        return "\n".join(lines) or "No additional public profile details were found."

    def _recent_title(self) -> str:
        name = _steam_clean_text(self.data.get("personaname") or "Unknown", 120)
        return f"## {name}'s Recently Played Games"

    def _recent_content(self) -> str:
        lines: list[str] = []
        for game in self.recent_games[:10]:
            game_name = _steam_clean_text(game.get("name") or "Unknown", 120)
            try:
                minutes = max(0, int(game.get("playtime_2weeks") or 0))
            except (TypeError, ValueError):
                minutes = 0
            hours, remainder = divmod(minutes, 60)
            played = f"{hours}h {remainder}m" if hours else f"{remainder}m"
            lines.append(f"**{game_name}** ({played})")
        return "\n".join(lines)

    async def _toggle_recent(self, interaction: discord.Interaction) -> None:
        if self.recent_btn is None:
            return
        if self._active == "profile":
            self._active = "recent"
            self.recent_btn.label = "Profile"
            self.title.content = self._recent_title()
            self.details.content = self._recent_content()
        else:
            self._active = "profile"
            self.recent_btn.label = "Recently Played"
            self.title.content = self._profile_title()
            self.details.content = self._summary()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if self.recent_btn is not None:
            self.recent_btn.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This is not your command.", ephemeral=True
        )
        return False


class Steam(Cog):
    """Steam profile lookup."""

    emoji = discord.PartialEmoji(name="steam", id=1095791386999132231)

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_group(name="steam", fallback="user")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Steam ID, profile URL, vanity name, or Discord user.")
    async def steam(
        self,
        ctx: Context,
        *,
        query: str = commands.param(
            default=commands.Author,
            description="SteamID, profile URL, vanity name, or @user.",
        ),
    ):
        """Lookup a steam account."""
        async with ctx.typing():
            sid = await SteamConverter().convert(ctx, query)
            data = await self._get_player(ctx, sid)
            if not data:
                raise commands.BadArgument(f"No Steam profile found for **{sid}**.")
            friends, recent = await asyncio.gather(
                self._get_friends(ctx, sid), self._get_recent_games(sid)
            )
            view = SteamUserView(ctx, sid, data, friends, recent)
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )

    @steam.command(
        name="badge",
        aliases=("badges",),
        extras={"usage": "<level> [@user|starting_level]"},
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        level="Desired Steam profile level.",
        target="Discord user mention or starting Steam level. Defaults to you.",
    )
    async def steam_badge(
        self,
        ctx: Context,
        level: int,
        *,
        target: str | None = commands.param(
            default=None,
            description="Discord user mention or starting Steam level. Defaults to you.",
        ),
    ):
        """Estimate badges and USD cost to reach a Steam profile level."""
        if not 1 <= level <= STEAM_BADGE_MAX_LEVEL:
            raise commands.BadArgument(
                f"The desired level must be between 1 and {STEAM_BADGE_MAX_LEVEL:,}."
            )

        async with ctx.typing():
            starting_name: str
            if target is None or not str(target).strip():
                target_user: discord.User | discord.Member = ctx.author
                starting_level, starting_name = await self._steam_badge_user_level(
                    ctx, target_user
                )
            else:
                target_value = str(target).strip()
                try:
                    starting_level = int(target_value)
                except ValueError:
                    try:
                        target_user = await commands.UserConverter().convert(
                            ctx, target_value
                        )
                    except commands.UserNotFound as error:
                        raise commands.BadArgument(
                            "The second argument must be a Discord user or a starting Steam level."
                        ) from error
                    starting_level, starting_name = await self._steam_badge_user_level(
                        ctx, target_user
                    )
                else:
                    starting_name = "Starting level"

            if not 0 <= starting_level <= STEAM_BADGE_MAX_LEVEL:
                raise commands.BadArgument(
                    f"The starting level must be between 0 and {STEAM_BADGE_MAX_LEVEL:,}."
                )
            if starting_level > level:
                raise commands.BadArgument(
                    "The desired level must be at least the starting level."
                )

            xp_needed, badges, estimated_cost = _steam_badge_estimate(
                starting_level, level
            )
            title = (
                f"## Steam badge estimate for {starting_name}"
                if starting_name != "Starting level"
                else "## Steam badge estimate"
            )
            details = "\n".join(
                (
                    title,
                    "",
                    f"**Starting level:** {starting_level:,}",
                    f"**Desired level:** {level:,}",
                )
            )
            estimate = "\n".join(
                (
                    f"**XP needed:** {xp_needed:,}",
                    f"**Badges needed:** {badges:,}",
                    f"**Estimated cost:** ${estimated_cost:,.2f}",
                )
            )
            container = discord.ui.Container(
                discord.ui.TextDisplay(details),
                discord.ui.Separator(),
                discord.ui.TextDisplay(estimate),
                accent_color=self.bot.embedcolor,
            )
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(container)
            await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _steam_badge_user_level(
        self,
        ctx: Context,
        user: discord.User | discord.Member,
    ) -> tuple[int, str]:
        # SteamConverter accepts Discord users at runtime, but its inherited
        # converter signature is string-only for command parsing.
        sid = await SteamConverter().convert(ctx, str(user.id))
        data = await self._get_player(ctx, sid)
        if not data:
            raise commands.BadArgument(
                f"No Steam profile found for **{user.display_name}**."
            )
        raw_level = data.get("_level")
        if raw_level is None:
            raise commands.BadArgument(
                f"Steam did not return a profile level for **{user.display_name}**."
            )
        try:
            level = int(str(raw_level))
        except (TypeError, ValueError) as error:
            raise commands.BadArgument(
                f"Steam did not return a profile level for **{user.display_name}**."
            ) from error
        return level, _steam_clean_text(
            data.get("personaname") or user.display_name,
            120,
        )

    @steam.command(name="game")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Steam game name, app ID, or Steam store URL.")
    async def steam_game(
        self,
        ctx: Context,
        *,
        query: str = commands.param(
            description="Steam game name, app ID, or Steam store URL."
        ),
    ):
        """Look up a Steam game and its store information."""
        async with ctx.typing():
            game, reviews = await self._get_game(query)
            if game is None:
                raise commands.BadArgument(
                    f"No Steam game found for **{query.strip()}**."
                )
            app_id = int(game.get("steam_appid") or game.get("appid") or 0)
            playtime_minutes = (
                await self._get_linked_game_playtime(ctx.author.id, app_id)
                if app_id
                else None
            )
            view = self._game_view(game, reviews or {}, playtime_minutes)
            await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @steam.command(name="free", aliases=("freegames",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def steam_free(self, ctx: Context) -> None:
        """Show paid Steam games currently free to keep during a promotion."""
        async with ctx.typing():
            entries = await self._get_free_to_keep_games()
        if not entries:
            raise commands.BadArgument(
                "Steam did not find any paid games currently free to keep."
            )
        pages = SimplePages(entries=entries, per_page=10, ctx=ctx)
        pages.embed.title = "Steam games free to keep"
        pages.embed.description = (
            "Paid games currently discounted to free. Promotions can end at any time."
        )
        await pages.start(ctx)

    @steam.command(name="group")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Steam group URL or custom group name.")
    async def steam_group(
        self,
        ctx: Context,
        *,
        query: str = commands.param(description="Group URL or custom name."),
    ):
        """Look up a Steam group."""
        async with ctx.typing():
            name = await SteamGroupConverter().convert(ctx, query)
            data = await self._get_group(ctx, name)
            if not data:
                raise commands.BadArgument(f"No Steam group found for **{name}**.")
            embed = self._group_embed(data)
            await ctx.send(embed=embed)

    async def _steam_json(self, url: str) -> dict | None:
        """Fetch one of Steam's fixed public JSON endpoints safely."""
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with self.bot.session.get(
                url,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _get_store_review_summary(self, app_id: int) -> dict[str, object]:
        """Fetch the review total displayed on the public Steam store page."""
        try:
            async with self.bot.session.get(
                f"https://store.steampowered.com/app/{app_id}/?l=english",
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "Fishie/1.0"},
            ) as resp:
                if resp.status != 200:
                    return {}
                page_html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, AttributeError):
            return {}
        return _steam_store_review_summary(page_html)

    async def _get_free_to_keep_games(self) -> list[str]:
        """Fetch Steam's special-search results and keep only paid-to-free offers."""

        async def fetch_page(start: int) -> dict | None:
            params = {
                "query": "",
                "start": start,
                "count": 100,
                "dynamic_data": "",
                "sort_by": "_ASC",
                "maxprice": "free",
                "specials": 1,
                "category1": 998,
                "infinite": 1,
                "cc": "us",
                "l": "english",
            }
            url = "https://store.steampowered.com/search/results/?" + urlencode(params)
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with self.bot.session.get(
                    url,
                    timeout=timeout,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Fishie/1.0",
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return None
            return payload if isinstance(payload, dict) else None

        first_page = await fetch_page(0)
        if first_page is None:
            return []
        pages = [first_page]
        try:
            total_count = max(0, int(first_page.get("total_count") or 0))
        except (TypeError, ValueError):
            total_count = 0
        # Steam currently returns at most 100 rows per response. Fetch every
        # result page so promotions beyond the first page are not omitted.
        for start in range(100, total_count, 100):
            page = await fetch_page(start)
            if page is None:
                break
            pages.append(page)

        entries: list[str] = []
        for page in pages:
            results_html = page.get("results_html")
            if isinstance(results_html, str):
                entries.extend(_steam_free_to_keep_games(results_html))
        return list(dict.fromkeys(entries))

    async def _get_player_counts(self, app_id: int) -> dict[str, int]:
        """Fetch current and 24-hour peak player counts for a Steam app."""
        counts: dict[str, int] = {}
        current_payload = await self._steam_json(
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?"
            + urlencode({"appid": app_id})
        )
        response = current_payload.get("response", {}) if current_payload else {}
        if isinstance(response, dict):
            raw_current = response.get("player_count")
            try:
                current = int(raw_current) if raw_current is not None else None
            except (TypeError, ValueError):
                current = None
            if current is not None and current >= 0:
                counts["current"] = current

        # Steam's public API does not expose a peak value. SteamCharts is a
        # fixed public page, so scrape its 24-hour peak when available.
        try:
            async with self.bot.session.get(
                f"https://steamcharts.com/app/{app_id}",
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "Fishie/1.0"},
            ) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    matches = re.findall(
                        r"(?is)<span\s+class=[\"']num[\"']\s*>\s*([\d,]+)\s*</span>"
                        r".*?<span\s+class=[\"']label[\"']\s*>\s*([^<]+)",
                        html_text,
                    )
                    for value, label in matches:
                        try:
                            parsed = int(value.replace(",", ""))
                        except ValueError:
                            continue
                        normalized = re.sub(r"\s+", " ", label).strip().casefold()
                        if "playing now" in normalized and "current" not in counts:
                            counts["current"] = parsed
                        if "24-hour peak" in normalized or "24 hour peak" in normalized:
                            counts["peak"] = parsed
                            break
        except (aiohttp.ClientError, asyncio.TimeoutError, AttributeError):
            pass
        return counts

    async def _get_linked_game_playtime(self, user_id: int, app_id: int) -> int | None:
        """Return the linked user's playtime for one game, when it is public."""

        row = await self.bot.pool.fetchrow(
            "SELECT steam FROM accounts WHERE user_id = $1", user_id
        )
        steam_id = row["steam"] if row else None
        if not steam_id:
            return None
        key = self.bot.config["keys"]["steam"]
        games_url = (
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
            + urlencode(
                {
                    "key": key,
                    "steamid": str(steam_id),
                    "include_appinfo": 0,
                    # Steam omits free-to-play titles from this response
                    # unless this flag is explicitly requested.
                    "include_played_free_games": 1,
                }
            )
        )
        payload = await self._steam_json(games_url)
        response = payload.get("response", {}) if payload else {}
        games = response.get("games", []) if isinstance(response, dict) else []
        if not isinstance(games, list):
            return None
        for game in games:
            if not isinstance(game, dict):
                continue
            try:
                owned_app_id = int(str(game.get("appid")))
                minutes = int(game.get("playtime_forever") or 0)
            except (TypeError, ValueError):
                continue
            if owned_app_id == app_id and minutes > 0:
                return minutes
        return None

    async def _get_game(self, query: str) -> tuple[dict | None, dict | None]:
        query = query.strip()
        app_id: int | None = None
        store_match = re.fullmatch(
            r"https?://store\.steampowered\.com/app/(\d+)(?:/[^?]*)?(?:\?.*)?",
            query,
            re.IGNORECASE,
        )
        if store_match:
            app_id = int(store_match.group(1))
        elif query.isdecimal() and len(query) <= 10:
            app_id = int(query)

        if app_id is None:
            search_url = "https://store.steampowered.com/api/storesearch/?" + urlencode(
                {"term": query, "cc": "us", "l": "english"}
            )
            search = await self._steam_json(search_url)
            results = search.get("items", []) if search else []
            if not isinstance(results, list) or not results:
                return None, None
            exact = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict)
                    and str(item.get("name", "")).casefold() == query.casefold()
                ),
                None,
            )
            selected = exact or results[0]
            if not isinstance(selected, dict) or not selected.get("id"):
                return None, None
            app_id = int(selected["id"])

        details_url = "https://store.steampowered.com/api/appdetails/?" + urlencode(
            {"appids": app_id, "cc": "us", "l": "english"}
        )
        details = await self._steam_json(details_url)
        result = details.get(str(app_id), {}) if details else {}
        if not isinstance(result, dict) or not result.get("success"):
            return None, None
        game = result.get("data")
        if not isinstance(game, dict):
            return None, None

        reviews_url = (
            f"https://store.steampowered.com/appreviews/{app_id}?"
            + urlencode(
                {"json": 1, "language": "all", "filter": "all", "num_per_page": 1}
            )
        )
        # Reviews and player counts are helpful but should not make a valid
        # game lookup fail if one of the optional endpoints is unavailable.
        reviews, player_counts, store_summary = await asyncio.gather(
            self._steam_json(reviews_url),
            self._get_player_counts(app_id),
            self._get_store_review_summary(app_id),
        )
        if not isinstance(reviews, dict):
            reviews = {}
        if store_summary:
            reviews["_store_summary"] = store_summary
        if player_counts:
            game["_player_counts"] = player_counts
        return game, reviews

    def _game_view(
        self,
        game: dict,
        reviews: dict,
        playtime_minutes: int | None = None,
    ) -> discord.ui.LayoutView:
        app_id = int(game.get("steam_appid") or game.get("appid") or 0)
        store_url = f"https://store.steampowered.com/app/{app_id}/"
        name = _steam_clean_text(game.get("name") or "Unknown game", 160)
        title = f"## {name}"

        children: list[discord.ui.Item] = [discord.ui.TextDisplay(title)]

        description = _steam_clean_text(
            game.get("short_description") or game.get("detailed_description"),
            1_400,
        )
        children.append(
            discord.ui.TextDisplay(description or "No description available.")
        )
        children.append(discord.ui.Separator())

        genres = [
            _steam_clean_text(item.get("description"), 40)
            for item in (game.get("genres") or [])
            if isinstance(item, dict) and item.get("description")
        ]
        if not genres:
            genres = [
                _steam_clean_text(item.get("description"), 40)
                for item in (game.get("categories") or [])
                if isinstance(item, dict) and item.get("description")
            ]
        genres = [genre for genre in genres if genre][:5]

        release = game.get("release_date") or {}
        if not isinstance(release, dict):
            release = {}
        release_date = (
            "Coming soon"
            if release.get("coming_soon")
            else _steam_clean_text(release.get("date"), 80) or "Unknown"
        )
        developers = [
            _steam_clean_text(value, 80)
            for value in (game.get("developers") or [])
            if value
        ]
        publishers = [
            _steam_clean_text(value, 80)
            for value in (game.get("publishers") or [])
            if value
        ]
        platforms = [
            platform.title()
            for platform, enabled in (game.get("platforms") or {}).items()
            if enabled
        ]
        price = game.get("price_overview") or {}
        is_free = _steam_game_is_free(game)
        price_text = "Free" if is_free else "Price unavailable"
        if isinstance(price, dict) and price.get("final_formatted") and not is_free:
            try:
                final_price = int(str(price.get("final") or 0))
            except (TypeError, ValueError):
                final_price = None
            if final_price == 0:
                price_text = "Free"
            else:
                price_text = _steam_clean_text(price["final_formatted"], 60)
            try:
                discount = int(str(price.get("discount_percent") or 0))
            except (TypeError, ValueError):
                discount = 0
            if discount and price_text != "Free":
                price_text += f" ({discount}% off)"
        meta = [f"**Release date:** {release_date}", f"**Price:** {price_text}"]
        player_counts = game.get("_player_counts")
        if isinstance(player_counts, dict):
            raw_current = player_counts.get("current")
            try:
                current_players = int(raw_current) if raw_current is not None else None
            except (TypeError, ValueError):
                current_players = None
            if current_players is not None:
                meta.append(f"**Active players:** {current_players:,}")
            raw_peak = player_counts.get("peak")
            try:
                peak_players = int(raw_peak) if raw_peak is not None else None
            except (TypeError, ValueError):
                peak_players = None
            if peak_players is not None:
                meta.append(f"**Peak active players:** {peak_players:,}")
        if playtime_minutes is not None and playtime_minutes > 0:
            meta.append(f"**Your playtime:** {_steam_playtime_text(playtime_minutes)}")
        if developers:
            meta.append(f"**Developer:** {', '.join(developers[:3])}")
        if publishers:
            meta.append(f"**Publisher:** {', '.join(publishers[:3])}")
        if genres:
            meta.append(f"**Tags:** {', '.join(genres)}")
        if platforms:
            meta.append(f"**Platforms:** {', '.join(platforms)}")

        meta.append(_steam_format_review_summary(reviews))

        link_buttons = [
            discord.ui.Button(
                label="Steam store",
                style=discord.ButtonStyle.link,
                url=store_url,
            )
        ]
        website = _steam_safe_url(game.get("website"))
        if website:
            link_buttons.append(
                discord.ui.Button(
                    label="Official website",
                    style=discord.ButtonStyle.link,
                    url=website,
                )
            )

        children.append(discord.ui.TextDisplay("\n".join(meta)))

        banner = _steam_safe_url(game.get("header_image"))
        if banner:
            children.append(discord.ui.Separator())
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(banner)))
        children.append(
            discord.ui.TextDisplay(f"-# Steam app ID: {app_id} · Data from Steam")
        )

        container = discord.ui.Container(*children, accent_color=self.bot.embedcolor)
        view_type = type("SteamGameView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        view.add_item(discord.ui.ActionRow(*link_buttons))
        return view

    async def _get_player(self, ctx: Context, sid: str) -> dict | None:
        key = self.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={sid}"
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            players = data.get("response", {}).get("players", [])
            player = players[0] if players else None
            if not player:
                return None
        # Scrape profile page for summary/bio.
        profile_url = f"https://steamcommunity.com/profiles/{sid}/"
        async with self.bot.session.get(profile_url) as page_resp:
            page_html = await page_resp.text()
            m = re.search(
                r'<div class="profile_summary"[^>]*>(.*?)</div>', page_html, re.DOTALL
            )
            if m:
                raw = m.group(1)
                raw = re.sub(r'<img[^>]+alt="([^"]*)"[^>]*>', r"\1", raw)
                # Replace linkfilter <a> tags with placeholders so
                # escape_markdown doesn't break them.
                links: dict[str, tuple[str, str]] = {}
                _counter = 0

                def _unlink(m: re.Match) -> str:
                    nonlocal _counter
                    encoded = m.group(1)
                    text = m.group(2).strip()
                    url = unquote(encoded)
                    key = f"\x00LINK{_counter}\x00"
                    links[key] = (text, url)
                    _counter += 1
                    return key

                raw = re.sub(
                    r'<a class="bb_link" href="[^"]*\?u=([^"&]+)[^"]*"[^>]*>\s*(.*?)\s*</a>\s*<span class="bb_link_host">\[[^\]]*\]</span>',
                    _unlink,
                    raw,
                )

                raw = re.sub(r"<[^>]+>", "", raw)
                raw = html.unescape(raw).strip()
                player["_summary"] = raw
                player["_links"] = links
            # Steam level.
            lvl_m = re.search(
                r'<span class="friendPlayerLevelNum">(\d+)</span>', page_html
            )
            if lvl_m:
                player["_level"] = int(lvl_m.group(1))
            # Group count. Steam has used both class names across profile layouts.
            player["_groups"] = len(
                re.findall(
                    r'class="[^"]*\b(?:groupBlock|profile_group)\b[^"]*"',
                    page_html,
                )
            )
            player["_assets"] = _steam_profile_asset_urls(page_html)
            player["_favorite_games"] = _steam_favorite_games(page_html)
            featured_badge = _steam_featured_badge(page_html)
            if featured_badge:
                player["_featured_badge"] = featured_badge
        badge_count, badge_level = await self._get_badge_data(sid)
        if badge_count is not None:
            player["_badge_count"] = badge_count
        if badge_level is not None and "_level" not in player:
            player["_level"] = badge_level
        if player.get("communityvisibilitystate") == 3:
            games_url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={key}&steamid={sid}&include_appinfo=0"
            async with self.bot.session.get(games_url) as games_resp:
                data = await games_resp.json()
                count = data.get("response", {}).get("game_count")
                if count is not None:
                    player["_game_count"] = count
        return player

    async def _get_badge_data(self, sid: str) -> tuple[int | None, int | None]:
        key = self.bot.config["keys"]["steam"]
        url = "https://api.steampowered.com/IPlayerService/GetBadges/v1/?" + urlencode(
            {"key": key, "steamid": sid}
        )
        data = await self._steam_json(url)
        response = data.get("response", {}) if data else {}
        if not isinstance(response, dict):
            return None, None
        badges = response.get("badges")
        count = len(badges) if isinstance(badges, list) else None
        level = response.get("player_level")
        try:
            level_value = int(level) if level is not None else None
        except (TypeError, ValueError):
            level_value = None
        return count, level_value

    async def _get_friends(self, ctx: Context, sid: str) -> int | None:
        key = self.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/GetFriendList/v1/?key={key}&steamid={sid}"
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            friends = data.get("friendslist", {}).get("friends", [])
            return len(friends) if data.get("friendslist") else None

    async def _get_recent_games(self, sid: str) -> list:
        key = self.bot.config["keys"]["steam"]
        url = (
            f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
            f"?key={key}&steamid={sid}"
        )
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            return data.get("response", {}).get("games", [])

    def _user_embed(self, sid: str, data: dict, friends: int | None) -> discord.Embed:
        name = data.get("personaname", "Unknown")
        av = data.get("avatarfull", "")
        summary = (
            discord.utils.escape_markdown(data.get("_summary", "")).strip() or None
        )
        if summary and data.get("_links"):
            for key, (text, url) in data["_links"].items():
                summary = summary.replace(key, f"[{text}]({url})")
        status_text = {
            0: "Offline",
            1: "Online",
            2: "Busy",
            3: "Away",
            4: "Snooze",
            5: "Looking to trade",
            6: "Looking to play",
        }
        state = data.get("personastate", 0)
        status_line = status_text.get(state, "Offline")
        loc = data.get("loccountrycode", "")
        flag = ""
        if loc and len(loc) == 2:
            flag = chr(ord(loc[0]) + 0x1F1A5) + chr(ord(loc[1]) + 0x1F1A5) + " "
        embed = discord.Embed(
            color=self.bot.embedcolor,
            url=f"https://steamcommunity.com/profiles/{sid}/",
            description=summary,
        )
        if av:
            embed.set_thumbnail(url=av)
        embed.set_author(
            name=f"{flag}{name} - {status_line}",
            icon_url=av,
            url=f"https://steamcommunity.com/profiles/{sid}/",
        )
        if gc := data.get("_game_count"):
            embed.add_field(name="Games", value=f"{gc:,}", inline=True)
        if friends is not None:
            embed.add_field(name="Friends", value=f"{friends:,}", inline=True)
        if data.get("gameextrainfo"):
            embed.add_field(name="Playing", value=data["gameextrainfo"], inline=True)
        if lo := data.get("lastlogoff"):
            label = "Online since" if state != 0 else "Last Online"
            embed.add_field(name=label, value=f"<t:{lo}:R>", inline=True)
        if lvl := data.get("_level"):
            embed.add_field(name="Level", value=str(lvl), inline=True)
        if groups := data.get("_groups"):
            embed.add_field(name="Groups", value=str(groups), inline=True)
        created = data.get("timecreated")
        if created:
            embed.timestamp = datetime.datetime.fromtimestamp(
                created, tz=datetime.timezone.utc
            )
        embed.set_footer(
            text=f"ID: {sid} | Created at",
            icon_url=STEAM_ICON,
        )
        return embed

    async def _get_group(self, ctx: Context, name: str) -> dict | None:
        xml_url = f"https://steamcommunity.com/groups/{name}/memberslistxml/?xml=1"
        async with self.bot.session.get(xml_url) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
        m = re.search(r"<groupID64>(\d+)</groupID64>", text)
        if not m:
            return None
        gid = m.group(1)
        name_m = re.search(r"<groupName><!\[CDATA\[(.*?)\]\]></groupName>", text)
        summary_m = re.search(r"<summary><!\[CDATA\[(.*?)\]\]></summary>", text)
        members_m = re.search(r"<memberCount>(\d+)</memberCount>", text)

        avatar = None
        page_url = f"https://steamcommunity.com/groups/{name}/"
        async with self.bot.session.get(page_url) as page_resp:
            if page_resp.status == 200:
                html = await page_resp.text()
                av_m = re.search(r'<link rel="image_src" href="([^"]+)"', html)
                if av_m:
                    avatar = av_m.group(1)

        return {
            "gid": gid,
            "name": name_m.group(1) if name_m else name,
            "summary": summary_m.group(1) if summary_m else "",
            "members": int(members_m.group(1)) if members_m else 0,
            "avatar": avatar,
        }

    def _group_embed(self, data: dict) -> discord.Embed:
        embed = discord.Embed(
            color=self.bot.embedcolor,
            url=f"https://steamcommunity.com/gid/{data['gid']}/",
            description=data.get("summary") or None,
        )
        av = data.get("avatar") or ""
        embed.set_author(
            name=data["name"],
            icon_url=av,
            url=f"https://steamcommunity.com/gid/{data['gid']}/",
        )
        if av:
            embed.set_thumbnail(url=av)
        embed.add_field(name="Members", value=f"{data['members']:,}", inline=True)
        embed.set_footer(text=f"ID: {data['gid']}", icon_url=STEAM_ICON)
        return embed


async def setup(bot: Fishie):
    await bot.add_cog(Steam(bot))

from __future__ import annotations

import asyncio
import datetime
import json
import re
import shlex
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast
from urllib.parse import urljoin

import aiohttp
import discord
from defusedxml import ElementTree as ET
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from core import Cog
from extensions.context import Context
from utils import (
    ROBLOX_ASSET_RE,
    LayoutPager,
    LayoutPageSource,
    read_bounded_response,
    validate_connected_peer,
    validate_public_url,
)
from utils.paths import FILES_ROOT

if TYPE_CHECKING:
    from core import Fishie

ROBLOX_TIMEOUT = aiohttp.ClientTimeout(total=10)

_ROBLOX_ITEM_BOOLEAN_FLAGS = frozenset({"limited", "offsale", "onsale", "roblox"})
_ROBLOX_ITEM_VALUE_FLAGS = frozenset({"creator", "type"})
_ROBLOX_ASSET_TYPE_ALIASES = {
    "image": 1,
    "tshirt": 2,
    "audio": 3,
    "mesh": 4,
    "hat": 8,
    "shirt": 11,
    "pants": 12,
    "decal": 13,
    "head": 17,
    "face": 18,
    "gear": 19,
    "animation": 24,
    "torso": 27,
    "rightarm": 28,
    "leftarm": 29,
    "leftleg": 30,
    "rightleg": 31,
    "hair": 41,
    "hairaccessory": 41,
    "faceaccessory": 42,
    "neckaccessory": 43,
    "shoulderaccessory": 44,
    "frontaccessory": 45,
    "backaccessory": 46,
    "waistaccessory": 47,
}


def _roblox_asset_type_id(value: str) -> int | None:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    if normalized.isdigit():
        parsed = int(normalized)
        return parsed if parsed > 0 else None
    return _ROBLOX_ASSET_TYPE_ALIASES.get(normalized)


def _parse_roblox_item_query(argument: str) -> tuple[str, dict[str, Any]]:
    """Split an item query from text-command filters such as ``-limited``."""
    try:
        tokens = shlex.split(argument)
    except ValueError as error:
        raise commands.BadArgument(
            "The item search contains an unmatched quote."
        ) from error

    query: list[str] = []
    filters: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag = token.lstrip("-").casefold() if token.startswith("-") else ""
        if flag in _ROBLOX_ITEM_BOOLEAN_FLAGS:
            filters[flag] = True
            index += 1
            continue
        if flag in _ROBLOX_ITEM_VALUE_FLAGS:
            index += 1
            values: list[str] = []
            while index < len(tokens):
                candidate = tokens[index]
                candidate_flag = (
                    candidate.lstrip("-").casefold()
                    if candidate.startswith("-")
                    else ""
                )
                if (
                    candidate_flag in _ROBLOX_ITEM_BOOLEAN_FLAGS
                    or candidate_flag in _ROBLOX_ITEM_VALUE_FLAGS
                ):
                    break
                values.append(candidate)
                index += 1
            if not values:
                raise commands.BadArgument(f"The `-{flag}` flag needs a value.")
            filters[flag] = " ".join(values)
            continue
        query.append(token)
        index += 1

    if filters.get("offsale") and filters.get("onsale"):
        raise commands.BadArgument("Use either `-offsale` or `-onsale`, not both.")
    if filters.get("roblox") and not filters.get("creator"):
        filters["creator"] = "Roblox"
    if "type" in filters:
        type_id = _roblox_asset_type_id(str(filters["type"]))
        if type_id is None:
            raise commands.BadArgument(
                f"Unknown Roblox asset type `{filters['type']}`. Use an asset type ID or name."
            )
        filters["type_id"] = type_id
    item_query = " ".join(query).strip()
    if not item_query:
        raise commands.BadArgument("Give me a Roblox item name, ID, or URL.")
    return item_query, filters


def _roblox_text(value: object, limit: int = 1_200) -> str:
    """Escape Roblox text before putting it into a Components V2 view."""
    text = discord.utils.escape_mentions(escape_markdown(str(value or ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _roblox_friend_text(value: object, limit: int = 1_200) -> str:
    """Keep friend names readable without treating user text as Markdown."""
    text = discord.utils.escape_mentions(str(value or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _roblox_scale(value: object) -> str:
    try:
        return f"{float(str(value)) * 100:.0f}%"
    except (TypeError, ValueError):
        return "Unknown"


def _roblox_date(value: object) -> str | None:
    """Format a Roblox timestamp for a Discord footer."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.datetime.fromtimestamp(
                float(value), tz=datetime.timezone.utc
            )
        else:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return discord.utils.format_dt(parsed, "D")
    except (TypeError, ValueError, OverflowError):
        return _roblox_text(value, 100)


def _roblox_price(value: object) -> int | None:
    """Extract a non-negative Robux price from Roblox's varied payload shapes."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.replace(",", "").strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    if isinstance(value, dict):
        for key in (
            "priceInRobux",
            "PriceInRobux",
            "price",
            "amount",
            "value",
            "robux",
        ):
            if key in value:
                price = _roblox_price(value[key])
                if price is not None:
                    return price
    return None


_ROBLOX_DISCORD_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_~+\-]{2,32}:\d+>")
_ROBLOX_SHORTCODE_EMOJI_RE = re.compile(r":[A-Za-z0-9_~+\-]{2,32}:")
_ROBLOX_UNICODE_EMOJI_RE = re.compile(
    "["
    "\\U0001F000-\\U0001FAFF"
    "\\u2300-\\u23FF"
    "\\u2600-\\u27BF"
    "\\u2B00-\\u2BFF"
    "\\uFE0E\\uFE0F"
    "\\u200D"
    "\\U0001F3FB-\\U0001F3FF"
    "]"
)


def _roblox_title_text(value: object, limit: int = 180) -> str:
    """Return a safe linked title without emoji markup breaking Markdown links."""
    text = str(value or "").strip()
    text = _ROBLOX_DISCORD_EMOJI_RE.sub("", text)
    text = _ROBLOX_SHORTCODE_EMOJI_RE.sub("", text)
    text = _ROBLOX_UNICODE_EMOJI_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return _roblox_text(text, limit) or "Roblox game"


class RobloxPageModal(discord.ui.Modal, title="Jump to page"):
    page = discord.ui.TextInput(
        label="Page number",
        placeholder="Enter a page number",
        required=True,
        max_length=6,
    )

    def __init__(self, paginator: Any) -> None:
        super().__init__()
        self.paginator = paginator

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.paginator.ctx.author.id:
            await interaction.response.send_message(
                "This page dialog is not for you.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            page = int(str(self.page.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole page number.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if page < 1:
            await interaction.response.send_message(
                "Choose a page number of 1 or greater.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        target = page - 1
        ensure_page = getattr(self.paginator, "ensure_page_number", None)
        if ensure_page is not None:
            await interaction.response.defer()
            try:
                available = await ensure_page(target)
            except commands.CommandError as error:
                await interaction.followup.send(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not available:
                await interaction.followup.send(
                    f"That paginator only has {self.paginator.page_count} page(s).",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        else:
            if page > self.paginator.page_count:
                await interaction.response.send_message(
                    f"Choose a page from 1 to {self.paginator.page_count}.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer()
        self.paginator.page = target
        await self.paginator.load_page()
        if self.paginator.message:
            await self.paginator.message.edit(
                view=self.paginator,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class RobloxOutfitPaginator(discord.ui.LayoutView):
    """Components V2 paginator showing one saved Roblox outfit at a time."""

    def __init__(
        self,
        ctx: Context,
        cog: "Roblox",
        profile: dict[str, Any],
        outfits: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.cog = cog
        self.profile = profile
        self.outfits = outfits
        self.index = 0
        self.page = 0
        self.message: discord.Message | None = None
        self.cache: dict[int, tuple[dict[str, Any] | None, str | None]] = {}
        self.details_display = discord.ui.TextDisplay("")
        self.media: discord.ui.MediaGallery | None = None
        self.footer = discord.ui.TextDisplay("")
        self.footer_added = False
        self.container = discord.ui.Container(
            self.details_display, accent_color=self.ctx.bot.embedcolor
        )
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.page_button = discord.ui.Button(
            label="#", style=discord.ButtonStyle.secondary
        )
        self.delete_button = discord.ui.Button(
            label="\U0001f5d1\ufe0f", style=discord.ButtonStyle.secondary
        )
        self.profile_button = discord.ui.Button(
            label="Open profile",
            style=discord.ButtonStyle.link,
            url=f"https://www.roblox.com/users/{int(profile['id'])}/profile",
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.page_button.callback = self._jump_page
        self.delete_button.callback = self._delete
        self.add_item(self.container)
        self.add_item(
            discord.ui.ActionRow(
                self.previous,
                self.next,
                self.page_button,
                self.delete_button,
                self.profile_button,
            )
        )
        self._render_navigation()

    def _render_navigation(self) -> None:
        # Previous/next navigation wraps around.  Only an empty result set
        # should leave these controls unavailable, while normal edge pages
        # remain clickable so the user can cycle back to the other end.
        has_outfits = bool(self.outfits)
        self.previous.disabled = not has_outfits
        self.next.disabled = not has_outfits

    @property
    def page_count(self) -> int:
        return len(self.outfits)

    @staticmethod
    def _item_link(asset: dict[str, Any]) -> str:
        asset_id = asset.get("id")
        name = _roblox_text(asset.get("name") or "Unnamed item", 180)
        if asset_id:
            return f"[{name}](https://www.roblox.com/catalog/{int(asset_id)})"
        return name

    def _render_details(
        self,
        outfit: dict[str, Any],
        details: dict[str, Any] | None,
    ) -> None:
        outfit_id = int(outfit["id"])
        name = _roblox_text(outfit.get("name") or "Unnamed outfit", 180)
        heading = f"## Outfit {self.index + 1}/{len(self.outfits)} · {name}"
        if not details:
            self.details_display.content = (
                f"{heading}\n\nCould not load the equipped items for this outfit."
            )
            self.footer.content = f"-# Outfit ID: {outfit_id} · Data from Roblox"
            return

        grouped: dict[str, list[str]] = {}
        for asset in details.get("assets", []):
            if not isinstance(asset, dict):
                continue
            asset_type = asset.get("assetType") or {}
            type_name = (
                _roblox_text(
                    asset_type.get("name") if isinstance(asset_type, dict) else "Item",
                    80,
                )
                or "Item"
            )
            grouped.setdefault(type_name, []).append(self._item_link(asset))

        item_lines = [
            f"**{category}:** {', '.join(items)}" for category, items in grouped.items()
        ]
        if not item_lines:
            item_lines = ["No equipped items were returned."]

        scale = details.get("scale") or {}
        scale_text = " · ".join(
            f"{label}: {_roblox_scale(scale.get(key))}"
            for key, label in (
                ("height", "Height"),
                ("width", "Width"),
                ("head", "Head"),
                ("depth", "Depth"),
                ("proportion", "Proportion"),
                ("bodyType", "Body type"),
            )
        )
        avatar_type = _roblox_text(details.get("playerAvatarType") or "Unknown", 40)
        self.details_display.content = "\n\n".join(
            (
                heading,
                "**Equipped items**\n" + "\n".join(item_lines),
                f"**Body scaling:** {scale_text}\n**Avatar type:** {avatar_type}",
            )
        )[:4_000]
        self.footer.content = f"-# Outfit ID: {outfit_id} · Data from Roblox"

    async def load_current(self) -> None:
        outfit = self.outfits[self.index]
        outfit_id = int(outfit["id"])
        cached = self.cache.get(outfit_id)
        if cached is None:
            details, thumbnail_data = await asyncio.gather(
                self.cog._roblox_json(
                    "GET", f"https://avatar.roblox.com/v3/outfits/{outfit_id}/details"
                ),
                self.cog._roblox_outfit_thumbnail(outfit_id),
            )
            cached = (
                details if isinstance(details, dict) else None,
                thumbnail_data,
            )
            self.cache[outfit_id] = cached

        details, image_url = cached
        self._render_details(outfit, details)
        if image_url:
            item = discord.MediaGalleryItem(
                image_url,
                description=f"Roblox outfit {self.index + 1}",
            )
            if self.media is None:
                self.media = discord.ui.MediaGallery(item)
                self.container.add_item(self.media)
            else:
                self.media.items = [item]
        if not self.footer_added:
            self.container.add_item(self.footer)
            self.footer_added = True
        self._render_navigation()

    async def load_page(self) -> None:
        self.index = self.page
        await self.load_current()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who requested these outfits can use the controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        for button in (
            self.previous,
            self.next,
            self.page_button,
            self.delete_button,
        ):
            button.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _change_page(self, interaction: discord.Interaction, change: int) -> None:
        if not self.outfits:
            await interaction.response.send_message(
                "No outfits are available to browse.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.index = (self.index + change) % len(self.outfits)
        self.page = self.index
        await interaction.response.defer()
        await self.load_current()
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _jump_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RobloxPageModal(self))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)


class RobloxInventoryPaginator(discord.ui.LayoutView):
    """Components V2 paginator for a user's public Roblox inventory."""

    PAGE_SIZE = 10

    def __init__(
        self,
        ctx: Context,
        cog: "Roblox",
        profile: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.cog = cog
        self.profile = profile
        self.items = items
        self.page = 0
        self.message: discord.Message | None = None
        self.details_cache: dict[int, dict[str, Any] | None] = {}
        self.catalog_cache: dict[int, dict[str, Any] | None] = {}
        self.details_display = discord.ui.TextDisplay("")
        self.footer = discord.ui.TextDisplay("")
        self.container = discord.ui.Container(
            self.details_display, self.footer, accent_color=self.ctx.bot.embedcolor
        )
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.page_button = discord.ui.Button(
            label="#", style=discord.ButtonStyle.secondary
        )
        self.delete_button = discord.ui.Button(
            label="\U0001f5d1\ufe0f", style=discord.ButtonStyle.secondary
        )
        profile_url = (
            f"https://www.roblox.com/users/{int(profile['id'])}/profile#!/creations"
        )
        self.profile_button = discord.ui.Button(
            label="Open inventory", style=discord.ButtonStyle.link, url=profile_url
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.page_button.callback = self._jump_page
        self.delete_button.callback = self._delete
        self.add_item(self.container)
        self.add_item(
            discord.ui.ActionRow(
                self.previous,
                self.next,
                self.page_button,
                self.delete_button,
                self.profile_button,
            )
        )
        self._render_navigation()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _render_navigation(self) -> None:
        # Keep both arrows active at the ends so navigation loops from the
        # final page to the first and vice versa.
        self.previous.disabled = False
        self.next.disabled = False

    @staticmethod
    def _asset_id(item: dict[str, Any]) -> int | None:
        raw_id = item.get("assetId") or item.get("id")
        try:
            return int(str(raw_id)) if raw_id else None
        except (TypeError, ValueError):
            return None

    async def _asset_details(self, asset_id: int) -> dict[str, Any] | None:
        if asset_id not in self.details_cache:
            try:
                data = await self.cog._roblox_json(
                    "GET", f"https://economy.roblox.com/v2/assets/{asset_id}/details"
                )
            except commands.CommandError:
                data = None
            self.details_cache[asset_id] = data if isinstance(data, dict) else None
        return self.details_cache[asset_id]

    async def _catalog_details(
        self, asset_ids: list[int]
    ) -> dict[int, dict[str, Any] | None]:
        """Fill pricing gaps with the catalog batch endpoint.

        The economy endpoint does not consistently include a price for older
        or migrated assets. Catalog details expose the current price and sale
        status for the same asset IDs in one request.
        """
        missing = [
            asset_id for asset_id in asset_ids if asset_id not in self.catalog_cache
        ]
        if missing:
            try:
                data = await self.cog._roblox_optional_json(
                    "POST",
                    "https://catalog.roblox.com/v1/catalog/items/details",
                    payload={
                        "items": [
                            {"itemType": "Asset", "id": asset_id}
                            for asset_id in missing
                        ]
                    },
                )
            except commands.CommandError:
                data = None

            entries = data.get("data", []) if isinstance(data, dict) else []
            by_id: dict[int, dict[str, Any]] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    entry_id = int(str(entry.get("id")))
                except (TypeError, ValueError):
                    continue
                by_id[entry_id] = entry

            for asset_id in missing:
                entry = by_id.get(asset_id)
                if entry is None:
                    self.catalog_cache[asset_id] = None
                    continue
                status_text = " ".join(
                    str(entry.get(key) or "")
                    for key in ("priceStatus", "itemStatus", "itemRestrictions")
                ).casefold()
                off_sale = any(
                    marker in status_text
                    for marker in (
                        "off sale",
                        "offsale",
                        "not for sale",
                        "notforsale",
                        "unavailable",
                    )
                )
                price = entry.get("price", entry.get("PriceInRobux"))
                self.catalog_cache[asset_id] = {
                    "IsForSale": (
                        False if off_sale else True if price is not None else None
                    ),
                    "PriceInRobux": price,
                }

        return {asset_id: self.catalog_cache.get(asset_id) for asset_id in asset_ids}

    @staticmethod
    def _sale_text(details: dict[str, Any] | None) -> str:
        if not details:
            return "Off-sale"
        if details.get("IsForSale") is False:
            return "Off-sale"
        price = details.get("PriceInRobux")
        try:
            return f"{int(str(price)):,} Robux" if price is not None else "Off-sale"
        except (TypeError, ValueError):
            return "Off-sale"

    async def load_page(self) -> None:
        start = self.page * self.PAGE_SIZE
        page_items = self.items[start : start + self.PAGE_SIZE]
        asset_ids = [self._asset_id(item) for item in page_items]
        details = await asyncio.gather(
            *(self._asset_details(asset_id) for asset_id in asset_ids if asset_id)
        )
        detail_by_id = {
            asset_id: detail
            for asset_id, detail in zip(
                (asset_id for asset_id in asset_ids if asset_id), details
            )
        }
        catalog_ids = [
            asset_id
            for asset_id, detail in detail_by_id.items()
            if detail is None
            or (
                detail.get("IsForSale") is not False
                and detail.get("PriceInRobux") is None
            )
        ]
        if catalog_ids:
            catalog_by_id = await self._catalog_details(catalog_ids)
            for asset_id in catalog_ids:
                catalog_detail = catalog_by_id.get(asset_id)
                if catalog_detail is None:
                    continue
                economy_detail = detail_by_id.get(asset_id) or {}
                merged = dict(economy_detail)
                merged.update(catalog_detail)
                detail_by_id[asset_id] = merged
        lines: list[str] = []
        for item in page_items:
            asset_id = self._asset_id(item)
            if asset_id is None:
                continue
            name = _roblox_text(
                item.get("assetName") or item.get("name") or "Unknown item", 150
            )
            item_url = f"https://www.roblox.com/catalog/{asset_id}/"
            sale = self._sale_text(detail_by_id.get(asset_id))
            lines.append(f"[{name}]({item_url}) (`{asset_id}`) · **{sale}**")
        if not lines:
            lines.append("No inventory items were returned for this page.")
        username = _roblox_text(self.profile.get("name") or "Roblox user", 120)
        self.details_display.content = (
            f"## Inventory for {username} · Page {self.page + 1}/{self.page_count}\n\n"
            + "\n".join(lines)
        )[:4_000]
        self.footer.content = "-# Showing up to 10 items per page · Data from Roblox"
        self._render_navigation()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who requested this inventory can use the controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        for button in (
            self.previous,
            self.next,
            self.page_button,
            self.delete_button,
        ):
            button.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _change_page(self, interaction: discord.Interaction, change: int) -> None:
        self.page = (self.page + change) % self.page_count
        await interaction.response.defer()
        await self.load_page()
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _jump_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RobloxPageModal(self))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()


class RobloxListPaginator(discord.ui.LayoutView):
    """Lazy Components V2 paginator for Roblox friends, names, groups, and badges."""

    PAGE_SIZE = 10
    TITLES = {
        "friends": "Friends",
        "names": "Past names",
        "groups": "Groups",
        "badges": "Badges",
    }

    def __init__(
        self,
        ctx: Context,
        cog: "Roblox",
        profile: dict[str, Any],
        kind: str,
        *,
        pages: list[list[dict[str, Any]]] | None = None,
        exhausted: bool = False,
    ) -> None:
        super().__init__(timeout=600)
        self.ctx = ctx
        self.cog = cog
        self.profile = profile
        self.user_id = int(profile["id"])
        self.kind = kind
        self.page = 0
        self.pages = pages or []
        self.next_cursors: list[str | None] = [None] * len(self.pages)
        self.exhausted = exhausted
        self.primary_group_id: int | None = None
        self._seen_cursors: set[str] = set()
        self.message: discord.Message | None = None
        self.details = discord.ui.TextDisplay("")
        self.footer = discord.ui.TextDisplay("")
        self.container = discord.ui.Container(
            self.details, self.footer, accent_color=self.ctx.bot.embedcolor
        )
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.page_button = discord.ui.Button(
            label="#", style=discord.ButtonStyle.secondary
        )
        self.delete_button = discord.ui.Button(
            label="\U0001f5d1\ufe0f", style=discord.ButtonStyle.secondary
        )
        profile_url = f"https://www.roblox.com/users/{self.user_id}/profile"
        self.profile_button = discord.ui.Button(
            label="Open profile", style=discord.ButtonStyle.link, url=profile_url
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.page_button.callback = self._jump_page
        self.delete_button.callback = self._delete
        self.add_item(self.container)
        self.add_item(
            discord.ui.ActionRow(
                self.previous,
                self.next,
                self.page_button,
                self.delete_button,
                self.profile_button,
            )
        )
        self._render_navigation()

    @property
    def page_count(self) -> int:
        return max(1, len(self.pages))

    @property
    def _username(self) -> str:
        return _roblox_text(self.profile.get("name") or "Roblox user", 120)

    async def ensure_page_number(self, page: int) -> bool:
        """Fetch cursor-backed pages through ``page`` when they are requested."""
        if page < 0:
            return False
        while len(self.pages) <= page:
            if not self.pages:
                cursor = None
            else:
                cursor = self.next_cursors[-1]
                if cursor is None or cursor in self._seen_cursors:
                    self.exhausted = True
                    return False
            if cursor:
                self._seen_cursors.add(cursor)
            items, next_cursor = await self.cog._roblox_list_page(
                self.kind, self.user_id, cursor
            )
            self.pages.append(items)
            self.next_cursors.append(next_cursor)
            if next_cursor is None:
                self.exhausted = True
        return page < len(self.pages)

    async def initialize(self) -> None:
        await self.ensure_page_number(0)
        if self.kind == "badges":
            # Prime the first three API pages so normal browsing is immediate.
            for page in (1, 2):
                if not await self.ensure_page_number(page):
                    break
        self._render()

    def _render_navigation(self) -> None:
        # Cursor-backed pages can be extended on demand.  Once the end is
        # known, both arrows still remain available and wrap around.
        self.previous.disabled = False
        self.next.disabled = False

    @staticmethod
    def _linked(text: str, url: str) -> str:
        return f"[{text}]({url.replace(')', '%29')})"

    def _item_text(self, item: dict[str, Any], index: int) -> str:
        if not isinstance(item, dict):
            label = "Deleted User" if self.kind == "friends" else "Unknown result"
            return f"**{index}.** {label}"

        if self.kind == "friends":
            user_id = item.get("id")
            if item.get("isDeleted") or not item.get("name"):
                username = display_name = "Deleted User"
            else:
                username = _roblox_friend_text(item.get("name"), 80)
                display_name = _roblox_friend_text(
                    item.get("displayName") or username, 100
                )
            identity = (
                display_name
                if display_name == username
                else f"{display_name} (@{username})"
            )
            try:
                friend_id = int(user_id) if user_id is not None else None
            except (TypeError, ValueError):
                friend_id = None
            if friend_id:
                identity = self._linked(
                    identity, f"https://www.roblox.com/users/{friend_id}/profile"
                )
            status = " · Online" if item.get("isOnline") else ""
            return f"**{index}.** {identity}{status}"

        if self.kind == "names":
            if isinstance(item, str):
                name = _roblox_text(item, 160)
                created = None
            else:
                name = _roblox_text(
                    item.get("name") or item.get("username") or "Unknown", 160
                )
                created = _roblox_date(item.get("created"))
            suffix = f" · Changed: {created}" if created else ""
            return f"**{index}.** {name}{suffix}"

        if self.kind == "groups":
            group = item.get("group") if isinstance(item.get("group"), dict) else item
            role = item.get("role") if isinstance(item.get("role"), dict) else {}
            if not isinstance(group, dict):
                group = {}
            group_id = group.get("id")
            group_name = _roblox_text(group.get("name") or "Unknown group", 150)
            try:
                group_id_value = int(group_id) if group_id is not None else None
            except (TypeError, ValueError):
                group_id_value = None
            if group_id_value:
                group_text = self._linked(
                    group_name, f"https://www.roblox.com/groups/{group_id_value}/"
                )
            else:
                group_text = group_name
            try:
                member_count = int(group.get("memberCount") or 0)
            except (TypeError, ValueError):
                member_count = 0
            details = [f"{member_count:,} members"]
            if isinstance(role, dict) and role.get("name"):
                details.append(_roblox_text(role["name"], 100))
            if group_id_value and group_id_value == getattr(
                self, "primary_group_id", None
            ):
                details.append("Primary")
            return f"**{index}.** {group_text} · {' · '.join(details)}"

        badge_id = item.get("id")
        badge_name = _roblox_text(
            item.get("name") or item.get("displayName") or "Unknown badge", 160
        )
        try:
            badge_id_value = int(badge_id) if badge_id is not None else None
        except (TypeError, ValueError):
            badge_id_value = None
        if badge_id_value:
            badge_name = self._linked(
                badge_name, f"https://www.roblox.com/badges/{badge_id_value}/"
            )
        created = _roblox_date(item.get("created"))
        suffix = f" · {created}" if created else ""
        description = _roblox_text(item.get("description"), 180)
        if description:
            suffix += f"\n   {description}"
        return f"**{index}.** {badge_name}{suffix}"

    def _render(self) -> None:
        items = (
            self.pages[self.page] if self.pages and self.page < len(self.pages) else []
        )
        start = self.page * self.PAGE_SIZE
        lines = [
            f"## {self.TITLES.get(self.kind, self.kind.title())} for {self._username}",
            "",
        ]
        if items:
            lines.extend(
                self._item_text(item, start + index + 1)
                for index, item in enumerate(items[: self.PAGE_SIZE])
            )
        else:
            lines.append(f"No {self.TITLES.get(self.kind, self.kind)} were found.")
        self.details.content = "\n".join(lines)[:4_000]
        total = str(self.page_count) if self.exhausted else "?"
        self.footer.content = f"-# Page {self.page + 1}/{total} · Showing up to {self.PAGE_SIZE} per page · Data from Roblox"
        self._render_navigation()

    async def load_page(self) -> None:
        if await self.ensure_page_number(self.page):
            self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who requested this Roblox list can use the controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        for button in (self.previous, self.next, self.page_button, self.delete_button):
            button.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _change_page(self, interaction: discord.Interaction, change: int) -> None:
        await interaction.response.defer()
        try:
            if change < 0 and self.page == 0:
                # A cursor paginator does not know its final page until the
                # API reports an empty next cursor. Load the remaining pages
                # before wrapping backwards from page one.
                while not self.exhausted:
                    if not await self.ensure_page_number(len(self.pages)):
                        break
                target = self.page_count - 1
            elif change > 0:
                target = self.page + 1
                if not await self.ensure_page_number(target):
                    target = 0
            else:
                target = (self.page + change) % self.page_count
        except commands.CommandError as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        self.page = target
        self._render()
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _jump_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RobloxPageModal(self))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()


class RobloxLeaksPageSource(LayoutPageSource):
    """Render Roblox's recently surfaced, not-for-sale catalog entries.

    Roblox does not publish the private/unreleased feed used by Rolimon's.
    This source intentionally only renders records returned by the official
    catalog API and labels the result accordingly.
    """

    PAGE_SIZE = 8

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def get_max_pages(self) -> int:
        return max(1, (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        start = page_number * self.PAGE_SIZE
        page = self.items[start : start + self.PAGE_SIZE]
        lines = [
            "## Recent Roblox catalog items",
            "",
        ]
        if not page:
            lines.append("No recently surfaced items were returned.")
        for item in page:
            try:
                raw_item_id = item.get("id")
                if raw_item_id is None:
                    continue
                item_id = int(str(raw_item_id))
            except (TypeError, ValueError):
                continue
            name = _roblox_text(item.get("name") or "Unnamed item", 160)
            name = name.replace("[", "\\[").replace("]", "\\]")
            created = _roblox_date(item.get("itemCreatedUtc"))
            creator = _roblox_text(item.get("creatorName") or "Unknown", 100)
            status = "Off-sale" if item.get("isOffSale") else "Not for sale"
            if item.get("priceStatus"):
                status = _roblox_text(item["priceStatus"], 40)
            details = [
                f"[{name}](https://www.roblox.com/catalog/{item_id}/) (ID: `{item_id}`)",
                f"**Creator:** {creator} · **Status:** {status}",
            ]
            if created:
                details.append(f"**Created:** {created}")
            lines.append("\n".join(details))
            lines.append("")
        lines.append(
            f"-# Page {page_number + 1}/{self.get_max_pages()} · Data from Roblox"
        )
        return [discord.ui.TextDisplay("\n".join(lines)[:4_000])]


class Roblox(Cog):
    async def self_send_asset(
        self,
        ctx: Context,
        url: str,
        extra: dict,
        *,
        cached_at: Optional[datetime.datetime] = None,
    ) -> None:
        creator_name = _roblox_text(extra.get("creator", "Unknown"), 180)
        creator_id = extra.get("creator_id")
        creator_type = extra.get("creator_type", "User")
        creator_value = creator_name
        if creator_id:
            if creator_type == "Group":
                link = f"https://www.roblox.com/groups/{creator_id}/"
            else:
                link = f"https://www.roblox.com/users/{creator_id}/profile"
            creator_value = f"[{creator_name}]({link})"

        cached_text = (
            f"Cached: {discord.utils.format_dt(cached_at, 'R')}"
            if cached_at
            else "Fetched from Roblox"
        )
        view = self._roblox_view(
            title=str(extra.get("name", "Unknown")).title(),
            url=f"https://www.roblox.com/catalog/{int(extra.get('asset_id') or 0)}/",
            media_url=url,
            sections=[f"**Made by:** {creator_value}"],
            footer=f"{cached_text} · Data from Roblox",
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _roblox_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = 2_000_000,
    ) -> Any:
        """Fetch a bounded response from one of Roblox's public APIs."""
        try:
            async with self.bot.session.request(
                method,
                url,
                json=payload,
                params=params,
                headers=headers,
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if response.status == 404:
                    return None
                if response.status in {401, 403}:
                    raise commands.BadArgument(
                        "Roblox did not make that information publicly available."
                    )
                if response.status != 200:
                    raise commands.CommandError(
                        "Roblox could not be reached right now. Try again later."
                    )
                raw = await read_bounded_response(response, max_bytes)
        except commands.CommandError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise commands.CommandError(
                "Roblox could not be reached right now. Try again later."
            ) from error

        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise commands.CommandError(
                "Roblox returned an invalid response. Try again later."
            ) from error

    @staticmethod
    def _roblox_id(value: str, *patterns: str) -> int | None:
        value = value.strip()
        if value.isdigit():
            return int(value)
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    async def _roblox_user(self, value: str | None, ctx: Context) -> dict[str, Any]:
        linked_account: object | None = None
        if value is None:
            try:
                row = await self.bot.pool.fetchrow(
                    "SELECT roblox FROM accounts WHERE user_id = $1",
                    ctx.author.id,
                )
            except Exception:
                row = None
            if row:
                linked_account = row["roblox"]

        query = str(value or linked_account or ctx.author.name).strip().lstrip("@")
        user_id = self._roblox_id(
            query,
            r"roblox\.com/users/(\d+)",
            r"[?&]userid=(\d+)",
        )
        if user_id is not None:
            profile = await self._roblox_json(
                "GET", f"https://users.roblox.com/v1/users/{user_id}"
            )
        else:
            result = await self._roblox_json(
                "POST",
                "https://users.roblox.com/v1/usernames/users",
                payload={
                    "usernames": [query[:20]],
                    "excludeBannedUsers": False,
                },
            )
            users = result.get("data", []) if isinstance(result, dict) else []
            profile = next(
                (
                    item
                    for item in users
                    if isinstance(item, dict)
                    and str(item.get("name", "")).casefold() == query.casefold()
                ),
                users[0] if users else None,
            )
            if profile and profile.get("id"):
                profile = await self._roblox_json(
                    "GET", f"https://users.roblox.com/v1/users/{int(profile['id'])}"
                )

        if not isinstance(profile, dict) or not profile.get("id"):
            raise commands.BadArgument(f"Could not find the Roblox user `{query}`.")
        return profile

    async def _roblox_thumbnail(self, user_id: int) -> str | None:
        data = await self._roblox_json(
            "GET",
            "https://thumbnails.roblox.com/v1/users/avatar-headshot",
            params={
                "userIds": user_id,
                "size": "420x420",
                "format": "Png",
                "isCircular": "false",
            },
        )
        entries = data.get("data", []) if isinstance(data, dict) else []
        image = entries[0].get("imageUrl") if entries else None
        return str(image) if image else None

    async def _roblox_optional_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Fetch optional profile metadata without failing the whole profile view."""
        try:
            return await self._roblox_json(
                method, url, payload=payload, params=params, headers=headers
            )
        except commands.CommandError:
            return None

    async def _roblox_game_universe_id(self, value: str) -> int:
        """Resolve a Roblox game name, place ID, universe ID, or URL."""
        query = value.strip()
        if not query:
            raise commands.BadArgument("Enter a Roblox game name, ID, or URL.")

        universe_id = self._roblox_id(query, r"roblox\.com/universes/(\d+)")
        place_id = self._roblox_id(
            query,
            r"roblox\.com/games/(\d+)",
            r"[?&](?:placeId|placeid)=(\d+)",
        )
        if universe_id is None and place_id is None and query.isdigit():
            candidate = int(query)
            details = await self._roblox_optional_json(
                "GET",
                "https://games.roblox.com/v1/games",
                params={"universeIds": candidate},
            )
            entries = details.get("data", []) if isinstance(details, dict) else []
            if isinstance(entries, list) and entries:
                universe_id = candidate
            else:
                place_id = candidate

        if universe_id is None and place_id is not None:
            converted = await self._roblox_optional_json(
                "GET",
                f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
            )
            if isinstance(converted, dict):
                for key in ("universeId", "universeID", "id"):
                    if converted.get(key):
                        try:
                            universe_id = int(converted[key])
                            break
                        except (TypeError, ValueError):
                            pass
            if universe_id is None:
                place_details = await self._roblox_optional_json(
                    "GET",
                    "https://games.roblox.com/v1/games/multiget-place-details",
                    params={"placeIds": place_id},
                )
                rows = (
                    place_details.get("data", [])
                    if isinstance(place_details, dict)
                    else place_details
                )
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        for key in ("universeId", "universeID"):
                            if row.get(key):
                                try:
                                    universe_id = int(row[key])
                                    break
                                except (TypeError, ValueError):
                                    pass
                        if universe_id is not None:
                            break

        if universe_id is None:
            session_id = str(
                int(
                    datetime.datetime.now(datetime.timezone.utc).timestamp() * 1_000_000
                )
            )
            omni = await self._roblox_optional_json(
                "GET",
                "https://apis.roblox.com/search-api/omni-search",
                params={
                    "searchQuery": query[:100],
                    "sessionId": session_id,
                    "pageType": "all",
                },
            )
            omni_candidates: list[dict[str, Any]] = []
            if isinstance(omni, dict):
                for group in omni.get("searchResults", []):
                    if not isinstance(group, dict):
                        continue
                    contents = group.get("contents")
                    if isinstance(contents, list):
                        omni_candidates.extend(
                            item for item in contents if isinstance(item, dict)
                        )
                if not omni_candidates and isinstance(omni.get("contents"), list):
                    omni_candidates = [
                        item for item in omni["contents"] if isinstance(item, dict)
                    ]

            list_candidates: Any = []
            if not omni_candidates:
                search = await self._roblox_optional_json(
                    "GET",
                    "https://games.roblox.com/v1/games/list",
                    params={"keyword": query[:100], "limit": 10},
                )
                list_candidates = (
                    search.get("games") or search.get("data", [])
                    if isinstance(search, dict)
                    else []
                )
            candidates = omni_candidates or list_candidates
            if isinstance(candidates, list):
                selected = next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and str(
                            item.get("name") or item.get("universeName") or ""
                        ).casefold()
                        == query.casefold()
                    ),
                    candidates[0] if candidates else None,
                )
                if isinstance(selected, dict):
                    for key in ("universeId", "universeID", "id"):
                        if selected.get(key):
                            try:
                                universe_id = int(selected[key])
                                break
                            except (TypeError, ValueError):
                                pass
                    if universe_id is None and selected.get("placeId"):
                        try:
                            place_id = int(selected["placeId"])
                        except (TypeError, ValueError):
                            place_id = None
                        if place_id is not None:
                            converted = await self._roblox_optional_json(
                                "GET",
                                f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
                            )
                            if isinstance(converted, dict):
                                for key in ("universeId", "universeID", "id"):
                                    if converted.get(key):
                                        try:
                                            universe_id = int(converted[key])
                                            break
                                        except (TypeError, ValueError):
                                            pass

        if universe_id is None:
            raise commands.BadArgument(f"Could not find the Roblox game `{query}`.")
        return universe_id

    async def _roblox_game_images(
        self, universe_id: int
    ) -> tuple[str | None, str | None]:
        """Return the game icon and first public game thumbnail."""
        icon_data, thumbnail_data = await asyncio.gather(
            self._roblox_optional_json(
                "GET",
                "https://thumbnails.roblox.com/v1/games/icons",
                params={
                    "universeIds": universe_id,
                    "size": "420x420",
                    "format": "Png",
                    "isCircular": "false",
                },
            ),
            self._roblox_optional_json(
                "GET",
                f"https://thumbnails.roblox.com/v1/games/{universe_id}/thumbnails",
                params={
                    "size": "768x432",
                    "count": 1,
                    "sortOrder": "Asc",
                    "format": "Png",
                },
            ),
        )
        icons = icon_data.get("data", []) if isinstance(icon_data, dict) else []
        thumbnails = (
            thumbnail_data.get("data", []) if isinstance(thumbnail_data, dict) else []
        )
        icon = (
            icons[0].get("imageUrl") if icons and isinstance(icons[0], dict) else None
        )
        image = next(
            (
                item.get("imageUrl")
                for item in thumbnails
                if isinstance(item, dict)
                and item.get("state") in (None, "Completed")
                and item.get("imageUrl")
            ),
            None,
        )
        return (str(icon) if icon else None, str(image) if image else None)

    async def _roblox_game_votes(
        self, universe_id: int
    ) -> tuple[int | None, int | None]:
        data = await self._roblox_optional_json(
            "GET",
            "https://games.roblox.com/v1/games/votes",
            params={"universeIds": universe_id},
        )
        rows: Any = data.get("data", []) if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return None, None
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and (
                    item.get("universeId") in (None, universe_id)
                    or item.get("id") in (None, universe_id)
                )
            ),
            rows[0] if rows and isinstance(rows[0], dict) else None,
        )
        if not isinstance(row, dict):
            return None, None

        def as_int(*keys: str) -> int | None:
            for key in keys:
                if row.get(key) is not None:
                    try:
                        return int(row[key])
                    except (TypeError, ValueError):
                        return None
            return None

        return as_int("upVotes", "likes", "thumbsUp"), as_int(
            "downVotes", "dislikes", "thumbsDown"
        )

    async def _roblox_game_social_links(self, universe_id: int) -> list[str]:
        data = await self._roblox_optional_json(
            "GET", f"https://games.roblox.com/v1/games/{universe_id}/social-links/list"
        )
        rows = data.get("data", []) if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("socialLinks") or rows.get("links") or []
        if not isinstance(rows, list):
            return []
        links: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_url = str(row.get("url") or row.get("link") or "").strip()
            if not raw_url.startswith(("https://", "http://")) or any(
                character in raw_url for character in ("\n", "\r", " ")
            ):
                continue
            label = (
                _roblox_text(
                    row.get("type") or row.get("title") or row.get("name") or "Link", 80
                )
                .replace("[", "")
                .replace("]", "")
            )
            links.append(f"[{label}]({raw_url.replace(')', '%29')})")
            if len(links) >= 8:
                break
        return links

    @staticmethod
    def _roblox_game_maturity_label(value: Any) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return _roblox_text(text, 180) if text else None
        if isinstance(value, dict):
            for key in (
                "contentRating",
                "contentRatingDescription",
                "rating",
                "ratingDescription",
                "maturity",
                "ageRecommendation",
                "displayName",
                "name",
            ):
                if key in value:
                    result = Roblox._roblox_game_maturity_label(value[key])
                    if result:
                        return result
        return None

    async def _roblox_game_maturity(self, universe_id: int) -> str | None:
        for url in (
            f"https://games.roblox.com/v1/games/{universe_id}/content-rating",
            f"https://apis.roblox.com/content-maturity-service/v1/game/{universe_id}/rating",
        ):
            data = await self._roblox_optional_json("GET", url)
            label = self._roblox_game_maturity_label(data)
            if label:
                return label
        return None

    async def _roblox_gamepass_totals(self, universe_id: int) -> tuple[int, int] | None:
        """Return the number of game passes and their listed Robux total."""
        headers = self._roblox_cookie_headers()
        endpoints = (
            (
                f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes",
                {"passView": "Full", "pageSize": 100},
            ),
            (
                f"https://games.roblox.com/v1/games/{universe_id}/game-passes",
                {"limit": 100, "sortOrder": "Asc"},
            ),
            (
                f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes/creator",
                {"pageSize": 100},
            ),
        )
        for endpoint, initial_params in endpoints:
            cursor: str | None = None
            count = 0
            total_price = 0
            missing_price_ids: list[int] = []
            saw_response = False
            for _ in range(20):
                params: dict[str, Any] = dict(initial_params)
                if cursor:
                    params[
                        "pageToken" if "pageSize" in initial_params else "cursor"
                    ] = cursor
                data = await self._roblox_optional_json(
                    "GET", endpoint, params=params, headers=headers
                )
                if not isinstance(data, dict):
                    break
                entries = data.get("data") or data.get("gamePasses") or []
                if not isinstance(entries, list):
                    break
                saw_response = True
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    count += 1
                    price_value = _roblox_price(entry)
                    if price_value is not None and not (
                        price_value == 0 and entry.get("productId")
                    ):
                        total_price += price_value
                    elif entry.get("id"):
                        try:
                            missing_price_ids.append(int(entry["id"]))
                        except (TypeError, ValueError):
                            continue
                next_cursor = (
                    data.get("nextPageToken")
                    or data.get("nextPageCursor")
                    or data.get("nextCursor")
                )
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = str(next_cursor)
            if saw_response:
                if missing_price_ids:

                    async def fetch_price(pass_id: int) -> int | None:
                        for price_url in (
                            f"https://apis.roblox.com/game-passes/v1/game-passes/{pass_id}/product-info",
                            f"https://games.roblox.com/v1/game-passes/{pass_id}/product-info",
                        ):
                            product = await self._roblox_optional_json(
                                "GET", price_url, headers=headers
                            )
                            if not isinstance(product, dict):
                                continue
                            price = _roblox_price(product)
                            if price is not None:
                                return price
                        return None

                    prices = await asyncio.gather(
                        *(fetch_price(pass_id) for pass_id in missing_price_ids[:100])
                    )
                    total_price += sum(price for price in prices if price is not None)
                return count, total_price
        return None

    async def _roblox_user_visits(self, user_id: int) -> str | None:
        """Total visits for a user's public games, when Roblox exposes them."""
        cursor: str | None = None
        total = 0
        found = False
        for _ in range(10):
            params: dict[str, Any] = {
                "accessFilter": "Public",
                "sortOrder": "Desc",
                "limit": 50,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._roblox_optional_json(
                "GET",
                f"https://games.roblox.com/v2/users/{user_id}/games",
                params=params,
            )
            if not isinstance(data, dict):
                break
            games = data.get("data")
            if not isinstance(games, list):
                break
            for game in games:
                if not isinstance(game, dict):
                    continue
                visits = game.get("placeVisits", game.get("visits"))
                if visits is None:
                    continue
                try:
                    total += int(visits)
                except (TypeError, ValueError):
                    continue
                found = True
            next_cursor = data.get("nextPageCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        return f"{total:,}" if found else None

    def _roblox_cookie_headers(self) -> dict[str, str] | None:
        """Load the optional Roblox session cookie without exposing its value."""
        cookie = str(self.bot.config["keys"].get("roblox", "") or "").strip()
        if not cookie:
            cookie_file = FILES_ROOT / "cookies" / "roblox-cookies.txt"
            if cookie_file.is_file():
                try:
                    for line in cookie_file.read_text(encoding="utf-8").splitlines():
                        if line.startswith("#"):
                            continue
                        fields = line.split("\t")
                        if len(fields) >= 7 and fields[5] == ".ROBLOSECURITY":
                            cookie = fields[6].strip()
                            break
                except (OSError, UnicodeError):
                    cookie = ""
        if not cookie:
            return None
        if not cookie.startswith(".ROBLOSECURITY="):
            cookie = f".ROBLOSECURITY={cookie}"
        return {"Cookie": cookie}

    @staticmethod
    def _roblox_membership_label(value: Any) -> str | None:
        """Normalize the different membership shapes Roblox has returned."""
        if isinstance(value, bool):
            return "Premium" if value else "None"
        if isinstance(value, str):
            normalized = value.strip().casefold().replace("_", " ").replace("-", " ")
            if "plus" in normalized:
                return "Plus"
            if "premium" in normalized or normalized in {"true", "yes"}:
                return "Premium"
            if normalized in {"false", "no", "none", "", "nbc"}:
                return "None"
            return None
        if isinstance(value, dict):
            for key in (
                "membershipType",
                "membership_type",
                "subscriptionType",
                "subscription_type",
                "plan",
                "tier",
                "productName",
                "product_name",
                "productType",
                "product_type",
                "displayName",
                "display_name",
                "name",
                "type",
            ):
                if key in value:
                    label = Roblox._roblox_membership_label(value[key])
                    if label in {"Plus", "Premium"}:
                        return label
            for key in (
                "isPlus",
                "is_plus",
                "hasPlus",
                "has_plus",
                "isPremium",
                "is_premium",
                "hasPremium",
                "has_premium",
            ):
                if key in value:
                    label = Roblox._roblox_membership_label(value[key])
                    if label in {"Plus", "Premium"}:
                        return label
                    if label == "None":
                        return "None"
            for key in ("data", "subscription", "subscriptions", "membership", "plans"):
                nested = value.get(key)
                label = Roblox._roblox_membership_label(nested)
                if label in {"Plus", "Premium"}:
                    return label
                if label == "None":
                    return "None"
            return None
        if isinstance(value, (list, tuple)):
            if not value:
                return "None"
            labels = [Roblox._roblox_membership_label(item) for item in value]
            if "Plus" in labels:
                return "Plus"
            if "Premium" in labels:
                return "Premium"
            if "None" in labels:
                return "None"
        return None

    async def _roblox_membership(self, user_id: int) -> str | None:
        """Return Plus, Premium, or None when Roblox exposes membership data."""
        headers = self._roblox_cookie_headers()
        validate, subscriptions = await asyncio.gather(
            self._roblox_optional_json(
                "GET",
                f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                headers=headers,
            ),
            self._roblox_optional_json(
                "GET",
                f"https://premiumfeatures.roblox.com/v1/users/{user_id}/subscriptions",
                headers=headers,
            ),
        )
        labels = (
            self._roblox_membership_label(validate),
            self._roblox_membership_label(subscriptions),
        )
        if "Plus" in labels:
            return "Plus"
        if "Premium" in labels:
            return "Premium"
        if "None" in labels:
            return "None"
        return None

    async def _roblox_admin_status(self, user_id: int) -> str | None:
        """Use Roblox's official badge list to identify administrator accounts."""
        data = await self._roblox_optional_json(
            "GET",
            f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges",
        )
        if isinstance(data, dict):
            badges = data.get("data") or data.get("robloxBadges") or data.get("badges")
        else:
            badges = data
        if not isinstance(badges, list):
            return None
        for badge in badges:
            if not isinstance(badge, dict):
                continue
            badge_text = " ".join(
                str(badge.get(key) or "")
                for key in ("name", "displayName", "description", "displayDescription")
            ).casefold()
            if any(
                marker in badge_text
                for marker in ("administrator", "moderator", "roblox admin")
            ):
                return "Yes"
        return None

    async def _roblox_user_stats(self, user_id: int) -> dict[str, str]:
        """Load public social and account metadata for a Roblox user."""
        friends, followers, following, visits, membership, primary, admin = (
            await asyncio.gather(
                self._roblox_optional_json(
                    "GET",
                    f"https://friends.roblox.com/v1/users/{user_id}/friends/count",
                ),
                self._roblox_optional_json(
                    "GET",
                    f"https://friends.roblox.com/v1/users/{user_id}/followers/count",
                ),
                self._roblox_optional_json(
                    "GET",
                    f"https://friends.roblox.com/v1/users/{user_id}/followings/count",
                ),
                self._roblox_user_visits(user_id),
                self._roblox_membership(user_id),
                self._roblox_optional_json(
                    "GET",
                    f"https://groups.roblox.com/v1/users/{user_id}/groups/primary/role",
                ),
                self._roblox_admin_status(user_id),
            )
        )

        def count(value: Any) -> str | None:
            if isinstance(value, dict) and value.get("count") is not None:
                try:
                    return f"{int(value['count']):,}"
                except (TypeError, ValueError):
                    pass
            return None

        visits_text: str | None = visits if isinstance(visits, str) else None
        visits_value: Any = None
        if isinstance(visits, dict):
            visits_value = visits.get("ProfileVisits")
            if visits_value is None:
                visits_value = visits.get("profileVisits", visits.get("visits"))
        if visits_text is None and visits_value is not None:
            try:
                visits_text = f"{int(visits_value):,}"
            except (TypeError, ValueError):
                visits_text = _roblox_text(visits_value, 80)

        primary_group_text: str | None = None
        if isinstance(primary, dict):
            group = primary.get("group")
            role = primary.get("role")
            if isinstance(group, dict) and group.get("id"):
                group_id = int(group["id"])
                group_name = _roblox_text(group.get("name") or "Unknown group", 180)
                primary_group_text = (
                    f"[{group_name}](https://www.roblox.com/groups/{group_id}/)"
                )
                if isinstance(role, dict) and role.get("name"):
                    primary_group_text += f" ({_roblox_text(role['name'], 120)})"
            elif isinstance(group, dict) and group.get("name"):
                primary_group_text = _roblox_text(group["name"], 180)

        stats: dict[str, str] = {}
        for key, value in (
            ("friends", count(friends)),
            ("followers", count(followers)),
            ("following", count(following)),
            ("visits", visits_text),
            ("premium", membership if isinstance(membership, str) else None),
            ("primary_group", primary_group_text),
            ("admin", admin if isinstance(admin, str) else None),
        ):
            if value is not None:
                stats[key] = value
        return stats

    def _roblox_view(
        self,
        *,
        title: str,
        url: str,
        title_suffix: str | None = None,
        description: str | None = None,
        sections: list[str] | None = None,
        media_url: str | None = None,
        thumbnail: bool = False,
        gallery_url: str | None = None,
        strip_title_emojis: bool = False,
        footer: str = "Data from Roblox",
        buttons: list[discord.ui.Button] | None = None,
    ) -> discord.ui.LayoutView:
        """Build a consistent Components V2 response for Roblox commands."""
        title_link_text = (
            _roblox_title_text(title, 180)
            if strip_title_emojis
            else _roblox_text(title, 180)
        )
        title_link_text = (
            title_link_text.replace("[", "\\[").replace("]", "\\]").strip()
        )
        if not title_link_text:
            title_link_text = "Roblox game"
        safe_title_url = str(url).replace("\\", "%5C").replace(")", "%29")
        suffix = _roblox_text(title_suffix, 120) if title_suffix else ""
        title_display = discord.ui.TextDisplay(
            f"## [{title_link_text}]({safe_title_url}){suffix}"
        )
        children: list[discord.ui.Item] = []
        if media_url and thumbnail:
            section_children: list[discord.ui.Item] = [title_display]
            if description:
                section_children.append(
                    discord.ui.TextDisplay(_roblox_text(description, 3_500))
                )
            children.append(
                discord.ui.Section(
                    *section_children,
                    accessory=discord.ui.Thumbnail(media_url),
                )
            )
        else:
            children.append(title_display)
            if description:
                children.append(
                    discord.ui.TextDisplay(_roblox_text(description, 3_500))
                )
        if sections:
            if description or (media_url and thumbnail):
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay("\n\n".join(sections)[:3_500]))
        if media_url and not thumbnail:
            if description or sections:
                children.append(discord.ui.Separator())
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        media_url, description=_roblox_text(title, 180)
                    )
                )
            )
        if gallery_url:
            if description or sections or (media_url and thumbnail):
                children.append(discord.ui.Separator())
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        gallery_url, description=f"{_roblox_text(title, 180)} thumbnail"
                    )
                )
            )
        children.append(discord.ui.TextDisplay(f"-# {_roblox_text(footer, 500)}"))

        container = discord.ui.Container(*children, accent_color=self.bot.embedcolor)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        if buttons:
            view.add_item(discord.ui.ActionRow(*buttons[:5]))
        return view

    async def _roblox_outfit_thumbnail(self, outfit_id: int) -> str | None:
        data = await self._roblox_json(
            "GET",
            "https://thumbnails.roblox.com/v1/users/outfits",
            params={
                "userOutfitIds": outfit_id,
                "size": "420x420",
                "format": "Png",
                "isCircular": "false",
            },
        )
        entries = data.get("data", []) if isinstance(data, dict) else []
        image = entries[0].get("imageUrl") if entries else None
        return str(image) if image else None

    async def _roblox_list_page(
        self, kind: str, user_id: int, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch one page for a Roblox user list.

        Roblox uses the same cursor response shape for friends, username history,
        and badges. Keeping the request in one helper lets the paginator lazily
        fetch only the pages the user actually opens.
        """
        if kind == "friends":
            # The old /friends endpoint is capped and no longer includes names.
            # /friends/find is the supported cursor-paginated endpoint and
            # returns the friend IDs in ``PageItems``.
            payload = await self._roblox_json(
                "GET",
                f"https://friends.roblox.com/v1/users/{user_id}/friends/find",
                params={
                    "limit": 10,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            if not isinstance(payload, dict):
                return [], None
            raw_rows = payload.get("PageItems") or payload.get("data") or []
            rows = (
                [row for row in raw_rows if isinstance(row, dict)]
                if isinstance(raw_rows, list)
                else []
            )

            # FindFriends deliberately returns IDs only. Hydrate the names in
            # one batch so the paginator never renders Unknown for every row.
            ids = [row.get("id") for row in rows if row.get("id") is not None]
            if ids:
                profiles = await self._roblox_optional_json(
                    "POST",
                    "https://users.roblox.com/v1/users",
                    payload={
                        "userIds": ids,
                        "excludeBannedUsers": False,
                    },
                )
                profile_rows = (
                    profiles.get("data", []) if isinstance(profiles, dict) else []
                )
                by_id = {
                    str(profile.get("id")): profile
                    for profile in profile_rows
                    if isinstance(profile, dict) and profile.get("id") is not None
                }
                enriched: list[dict[str, Any]] = []
                for row in rows:
                    profile = by_id.get(str(row.get("id")), {})
                    merged = dict(profile) if isinstance(profile, dict) else {}
                    merged.update(row)
                    enriched.append(merged)
                rows = enriched
            next_cursor = (
                payload.get("NextCursor")
                or payload.get("nextPageCursor")
                or payload.get("next_cursor")
            )
            return rows, str(next_cursor) if next_cursor else None

        endpoints = {
            "names": (
                f"https://users.roblox.com/v1/users/{user_id}/username-history",
                "Desc",
            ),
            "badges": (
                f"https://badges.roblox.com/v1/users/{user_id}/badges",
                "Desc",
            ),
        }
        endpoint = endpoints.get(kind)
        if endpoint is None:
            raise commands.BadArgument("That Roblox list is not supported.")
        url, sort_order = endpoint
        params: dict[str, Any] = {"limit": 10, "sortOrder": sort_order}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = await self._roblox_json("GET", url, params=params)
        except commands.BadArgument:
            if kind != "badges":
                raise
            payload = None

        # Badge ownership is now subject to Roblox inventory privacy. The
        # legacy endpoint still exposes public system badges, so use it as a
        # safe fallback when the full badge list is restricted.
        if kind == "badges" and payload is None:
            payload = await self._roblox_optional_json(
                "GET",
                f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges",
            )
            if payload is None:
                raise commands.BadArgument(
                    "Roblox restricts this user's badges, so no public badge list is available."
                )
        if not isinstance(payload, dict):
            rows = payload if isinstance(payload, list) else []
            next_cursor = None
            return (
                [row for row in rows if isinstance(row, dict)],
                next_cursor,
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("robloxBadges") or payload.get("badges") or []
        if not isinstance(rows, list):
            rows = []
        items = [row for row in rows if isinstance(row, dict)]
        next_cursor = (
            payload.get("nextPageCursor")
            or payload.get("NextCursor")
            or payload.get("next_cursor")
        )

        # Some privacy responses are a successful empty page. Try the public
        # system-badge endpoint before reporting that there is no data.
        if kind == "badges" and not items and not cursor:
            fallback = await self._roblox_optional_json(
                "GET",
                f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges",
            )
            if isinstance(fallback, list):
                items = [row for row in fallback if isinstance(row, dict)]
            elif isinstance(fallback, dict):
                fallback_rows = (
                    fallback.get("data")
                    or fallback.get("robloxBadges")
                    or fallback.get("badges")
                    or []
                )
                if isinstance(fallback_rows, list):
                    items = [row for row in fallback_rows if isinstance(row, dict)]
            if items:
                next_cursor = None
        return items, str(next_cursor) if next_cursor else None

    async def _roblox_list_paginator(
        self, ctx: Context, profile: dict[str, Any], kind: str
    ) -> RobloxListPaginator:
        view = RobloxListPaginator(ctx, self, profile, kind)
        await view.initialize()
        if not view.pages or not any(view.pages):
            raise commands.BadArgument(
                f"No {RobloxListPaginator.TITLES.get(kind, kind)} were found for "
                f"**{_roblox_text(profile.get('name') or 'this user', 120)}**."
            )
        return view

    async def _roblox_groups_paginator(
        self, ctx: Context, profile: dict[str, Any]
    ) -> RobloxListPaginator:
        """Fetch and sort all public group memberships, with primary first."""
        user_id = int(profile["id"])
        groups_payload, primary_payload = await asyncio.gather(
            self._roblox_json(
                "GET", f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
            ),
            self._roblox_optional_json(
                "GET",
                f"https://groups.roblox.com/v1/users/{user_id}/groups/primary/role",
            ),
        )
        rows = (
            groups_payload.get("data", []) if isinstance(groups_payload, dict) else []
        )
        rows = [row for row in rows if isinstance(row, dict)]
        primary_id: int | None = None
        if isinstance(primary_payload, dict):
            primary_group = primary_payload.get("group")
            if isinstance(primary_group, dict) and primary_group.get("id"):
                try:
                    primary_id = int(primary_group["id"])
                except (TypeError, ValueError):
                    primary_id = None

        def group_key(row: dict[str, Any]) -> tuple[int, int, str]:
            group = row.get("group")
            group = group if isinstance(group, dict) else {}
            try:
                group_id = int(group.get("id") or 0)
            except (TypeError, ValueError):
                group_id = 0
            try:
                member_count = int(group.get("memberCount") or 0)
            except (TypeError, ValueError):
                member_count = 0
            return (
                0 if primary_id and group_id == primary_id else 1,
                -member_count,
                str(group.get("name") or "").casefold(),
            )

        rows.sort(key=group_key)
        if not rows:
            raise commands.BadArgument(
                f"No groups were found for **{_roblox_text(profile.get('name') or 'this user', 120)}**."
            )
        view = RobloxListPaginator(
            ctx,
            self,
            profile,
            "groups",
            pages=[
                rows[index : index + RobloxListPaginator.PAGE_SIZE]
                for index in range(0, len(rows), RobloxListPaginator.PAGE_SIZE)
            ],
            exhausted=True,
        )
        view.primary_group_id = primary_id
        await view.initialize()
        return view

    async def _send_roblox_profile(self, ctx: Context, profile: dict[str, Any]) -> None:
        user_id = int(profile["id"])
        thumbnail, presence, stats = await asyncio.gather(
            self._roblox_thumbnail(user_id),
            self._roblox_json(
                "POST",
                "https://presence.roblox.com/v1/presence/users",
                payload={"userIds": [user_id]},
            ),
            self._roblox_user_stats(user_id),
        )
        presence_data = (
            presence.get("userPresences", []) if isinstance(presence, dict) else []
        )
        presence_row = presence_data[0] if presence_data else {}
        presence_names = {0: "Offline", 1: "Online", 2: "In game", 3: "In studio"}
        try:
            presence_type = int(presence_row.get("userPresenceType", 0))
        except (TypeError, ValueError):
            presence_type = 0

        created_text = _roblox_date(profile.get("created")) or "Unknown"

        description = str(profile.get("description") or "No description.").strip()
        profile_fields = [
            f"**Username:** `{_roblox_text(profile.get('name') or 'Unknown', 120)}`",
            f"**User ID:** `{user_id}`",
            f"**Status:** {presence_names.get(presence_type, 'Unknown')}",
        ]
        if profile.get("isBanned") is True:
            profile_fields.append("**Banned:** Yes")
        for label, key in (
            ("Friends", "friends"),
            ("Followers", "followers"),
            ("Following", "following"),
            ("Visits", "visits"),
            ("Primary group", "primary_group"),
            ("Roblox admin", "admin"),
        ):
            if value := stats.get(key):
                profile_fields.append(f"**{label}:** {value}")
        if stats.get("premium") == "Plus":
            profile_fields.append("**Plus:** Yes")
        if profile.get("hasVerifiedBadge") is True:
            profile_fields.append("**Verified:** Yes")
        sections = ["\n".join(profile_fields)]
        view = self._roblox_view(
            title=str(
                profile.get("displayName") or profile.get("name") or "Roblox user"
            ),
            url=f"https://www.roblox.com/users/{user_id}/profile",
            description=description,
            sections=sections,
            media_url=thumbnail,
            thumbnail=True,
            footer=f"Created at: {created_text} · Data from Roblox",
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name="roblox", fallback="user")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_group(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None,
            description="Roblox username, user ID, or profile URL.",
        ),
    ) -> None:
        """Look up a Roblox profile."""
        async with ctx.typing():
            profile = await self._roblox_user(username, ctx)
            await self._send_roblox_profile(ctx, profile)

    async def _roblox_recent_catalog_items(self) -> list[dict[str, Any]]:
        """Return recent not-for-sale entries from Roblox's public catalog.

        Rolimon's leak list is a private third-party dataset. Roblox does not
        expose unpublished assets or a historical leak feed, so this command
        deliberately uses the closest official-only substitute.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        cached = getattr(self, "_roblox_catalog_recent_cache", None)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_at, cached_items = cached
            if (
                isinstance(cached_at, datetime.datetime)
                and isinstance(cached_items, list)
                and (now - cached_at).total_seconds() < 900
            ):
                return list(cached_items)

        entries: dict[int, dict[str, Any]] = {}
        creator_ids: set[int] = set()

        def is_not_for_sale(item: dict[str, Any]) -> bool:
            status = str(item.get("priceStatus") or "").casefold()
            sale_location = str(item.get("saleLocationType") or "").casefold()
            raw_item_status = item.get("itemStatus") or []
            if isinstance(raw_item_status, str):
                raw_item_status = [raw_item_status]
            item_status = {str(value).casefold() for value in raw_item_status}
            return bool(
                item.get("isOffSale") is True
                or status in {"offsale", "notforsale", "noresellers"}
                or "offsale" in item_status
                or sale_location
                in {"experiencesdevapionly", "notapplicable", "notavailable"}
            )

        # Query Roblox-owned entries directly, then query a broader result set
        # so Roblox administrators can be identified from their official
        # administrator/moderator badge. A verified badge alone is not enough
        # because many ordinary UGC creators are verified too.
        queries = (
            {"CreatorTargetId": "1"},
            {},
        )
        # Past day and past week keep the result useful even when Roblox has
        # no newly surfaced entries in the shorter window.
        for aggregation in ("1", "3"):
            for creator_filter in queries:
                params: dict[str, Any] = {
                    "Category": "1",
                    "IncludeNotForSale": "true",
                    "CreatorType": "User",
                    "Limit": 30,
                    "SortType": 3,
                    "SortAggregation": aggregation,
                }
                params.update(creator_filter)
                payload = await self._roblox_json(
                    "GET",
                    "https://catalog.roblox.com/v2/search/items/details",
                    params=params,
                )
                rows = payload.get("data", []) if isinstance(payload, dict) else []
                for item in rows:
                    if not isinstance(item, dict) or not is_not_for_sale(item):
                        continue
                    try:
                        raw_item_id = item.get("id")
                        if raw_item_id is None:
                            continue
                        item_id = int(str(raw_item_id))
                        creator_id = int(str(item.get("creatorTargetId")))
                    except (TypeError, ValueError):
                        continue
                    entries[item_id] = item
                    if creator_id != 1:
                        creator_ids.add(creator_id)

        # The public Roblox APIs do not expose a role field for staff. The
        # Roblox administrator/moderator badge is the only official signal
        # available here, so never include a creator unless that badge check
        # succeeds. Keep this bounded and cache the result to avoid a request
        # per catalog row on every invocation.
        admin_check = getattr(self, "_roblox_admin_status", None)
        allowed_admins: set[int] = set()
        if callable(admin_check):
            admin_check_fn = cast(Any, admin_check)
            now = datetime.datetime.now(datetime.timezone.utc)
            admin_cache = getattr(self, "_roblox_catalog_admin_cache", {})
            if not isinstance(admin_cache, dict):
                admin_cache = {}
            pending: list[int] = []
            for creator_id in sorted(creator_ids):
                cached = admin_cache.get(creator_id)
                if (
                    isinstance(cached, tuple)
                    and len(cached) == 2
                    and isinstance(cached[0], datetime.datetime)
                    and (now - cached[0]).total_seconds() < 86_400
                ):
                    if cached[1] is True:
                        allowed_admins.add(creator_id)
                elif len(pending) < 25:
                    pending.append(creator_id)
            if pending:
                results = await asyncio.gather(
                    *(admin_check_fn(creator_id) for creator_id in pending),
                    return_exceptions=True,
                )
                for creator_id, result in zip(pending, results):
                    is_admin = result == "Yes"
                    admin_cache[creator_id] = (now, is_admin)
                    if is_admin:
                        allowed_admins.add(creator_id)
            self._roblox_catalog_admin_cache = admin_cache

        filtered_entries: dict[int, dict[str, Any]] = {}
        for item_id, item in entries.items():
            try:
                creator_id = int(str(item.get("creatorTargetId")))
            except (TypeError, ValueError):
                continue
            if creator_id == 1 or creator_id in allowed_admins:
                filtered_entries[item_id] = item
        entries = filtered_entries

        items = sorted(
            entries.values(),
            key=lambda item: str(item.get("itemCreatedUtc") or ""),
            reverse=True,
        )
        self._roblox_catalog_recent_cache = (now, items)
        return items

    @roblox_group.command("leaks", aliases=("leak",))
    async def roblox_leaks(self, ctx: Context) -> None:
        """Show recently surfaced off-sale Roblox-owned or staff catalog items."""
        async with ctx.typing():
            items = await self._roblox_recent_catalog_items()
            if not items:
                raise commands.BadArgument(
                    "Roblox did not return any recently surfaced off-sale catalog items."
                )
            view = LayoutPager(
                RobloxLeaksPageSource(items),
                ctx=ctx,
                accent_color=self.bot.embedcolor,
            )
        await view.start()

    async def _send_roblox_list(
        self, ctx: Context, username: Optional[str], kind: str
    ) -> None:
        profile = await self._roblox_user(username, ctx)
        view = (
            await self._roblox_groups_paginator(ctx, profile)
            if kind == "groups"
            else await self._roblox_list_paginator(ctx, profile, kind)
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @roblox_group.command("friends", aliases=("friend",))
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_friends(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox username, user ID, or profile URL."
        ),
    ) -> None:
        """List a Roblox user's friends, ten per page."""
        async with ctx.typing():
            await self._send_roblox_list(ctx, username, "friends")

    @roblox_group.command("names", aliases=("name",))
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_names(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox username, user ID, or profile URL."
        ),
    ) -> None:
        """Show a Roblox user's past usernames, ten per page."""
        async with ctx.typing():
            await self._send_roblox_list(ctx, username, "names")

    @roblox_group.command("groups", aliases=("group-list",))
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_groups(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox username, user ID, or profile URL."
        ),
    ) -> None:
        """List a Roblox user's groups, with the primary group first."""
        async with ctx.typing():
            await self._send_roblox_list(ctx, username, "groups")

    @roblox_group.command("badges", aliases=("badge",))
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_badges(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox username, user ID, or profile URL."
        ),
    ) -> None:
        """List a Roblox user's badges, ten per page."""
        async with ctx.typing():
            await self._send_roblox_list(ctx, username, "badges")

    @roblox_group.command("asset")
    @app_commands.describe(asset_id="Roblox catalog asset ID or URL.")
    async def roblox_asset(self, ctx: Context, asset_id: str):
        """Get the 2D clothing template for a Roblox classic t-shirt, shirt, or pants"""
        async with ctx.typing():
            try:
                aid = int(asset_id)
            except ValueError:
                result = ROBLOX_ASSET_RE.search(asset_id)
                if not result or not result.group(4):
                    raise commands.CommandError(
                        "Could not find an asset ID in that URL. Provide a Roblox catalog URL or a raw asset ID."
                    )
                aid = int(result.group(4))

            cached = self.bot.cached_roblox_templates.get(aid)
            if cached and len(cached) == 3:
                cached_extra = dict(cached[1])
                cached_extra.setdefault("asset_id", aid)
                await self.self_send_asset(
                    ctx, cached[0], cached_extra, cached_at=cached[2]
                )
                return
            elif cached:
                del self.bot.cached_roblox_templates[aid]

            row = await self.bot.pool.fetchrow(
                "SELECT image_url, extra, cached_at FROM roblox_templates WHERE asset_id = $1",
                aid,
            )
            if row:
                extra = (
                    json.loads(row["extra"])
                    if isinstance(row["extra"], str)
                    else (row["extra"] or {})
                )
                extra.setdefault("name", "Unknown")
                extra.setdefault("asset_id", aid)
                self.bot.cached_roblox_templates[aid] = (
                    row["image_url"],
                    extra,
                    row["cached_at"],
                )
                await self.self_send_asset(
                    ctx, row["image_url"], extra, cached_at=row["cached_at"]
                )
                return

            async with self.bot.session.get(
                f"https://economy.roblox.com/v2/assets/{aid}/details",
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if response.status != 200:
                    raise commands.CommandError(
                        "Failed to reach Roblox. Try again later."
                    )
                details = json.loads(await read_bounded_response(response, 1_000_000))
            asset_type = details.get("AssetTypeId")
            if asset_type not in (2, 11, 12):
                raise commands.CommandError(
                    f"Asset {aid} is type {asset_type}, not a classic t-shirt (2), shirt (11), or pants (12)."
                )

            creator = details.get("Creator", {})
            creator_name = creator.get("Name", "Unknown")
            creator_id = creator.get("Id")
            creator_type = creator.get("CreatorType", "User")
            extra = {
                "asset_id": aid,
                "name": details.get("Name", "Unknown"),
                "creator": creator_name,
                "creator_id": creator_id,
                "creator_type": creator_type,
                "type": asset_type,
            }
            if creator_type == "Group":
                extra["owner"] = creator_name

            cookie = self.bot.config["keys"].get("roblox", "")
            asset_url = f"https://assetdelivery.roblox.com/v1/asset?id={aid}"
            headers = {"Cookie": f".ROBLOSECURITY={cookie}"} if cookie else None
            async with self.bot.session.get(
                asset_url,
                headers=headers,
                allow_redirects=False,
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if 300 <= response.status < 400 and response.headers.get("Location"):
                    # Never forward the account cookie to the redirect target.
                    redirected = urljoin(asset_url, response.headers["Location"])
                    await validate_public_url(redirected)
                    async with self.bot.session.get(
                        redirected, allow_redirects=False, timeout=ROBLOX_TIMEOUT
                    ) as redirected_response:
                        validate_connected_peer(redirected_response)
                        if redirected_response.status != 200:
                            raise commands.CommandError(
                                "Failed to fetch asset XML. Try again later."
                            )
                        stdout = await read_bounded_response(
                            redirected_response, 5_000_000
                        )
                elif response.status == 200:
                    stdout = await read_bounded_response(response, 5_000_000)
                else:
                    raise commands.CommandError(
                        "Failed to fetch asset XML. Try again later."
                    )
            if stdout.startswith(b"{"):
                await ctx.send(
                    "Something went wrong while trying to get the asset, please try again later, we may be rate limited."
                )
                return

            try:
                root = ET.fromstring(stdout)
            except ET.ParseError:
                raise commands.CommandError(
                    "Roblox returned invalid XML. The asset may not be a classic t-shirt, shirt, or pants."
                )

            url_elem = root.find(".//Item/Properties/Content/url")
            if url_elem is None or not url_elem.text:
                raise commands.CommandError(
                    "No texture reference found in the asset XML."
                )

            texture_match = re.search(r"id=(\d+)", url_elem.text)
            if not texture_match:
                raise commands.CommandError(
                    "Could not parse the texture ID from the asset XML."
                )

            texture_id = texture_match.group(1)

            async with self.bot.session.get(
                "https://thumbnails.roblox.com/v1/assets",
                params={"assetIds": texture_id, "size": "420x420", "format": "Png"},
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                validate_connected_peer(response)
                if response.status != 200:
                    raise commands.CommandError(
                        "Failed to fetch template thumbnail. Try again later."
                    )
                thumb_data = json.loads(
                    await read_bounded_response(response, 1_000_000)
                )
            image_url = thumb_data["data"][0]["imageUrl"]

            await self.bot.pool.execute(
                "INSERT INTO roblox_templates (asset_id, image_url, item_name, extra) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (asset_id) DO UPDATE SET image_url = $2, item_name = $3, extra = $4::jsonb, cached_at = now() at time zone 'utc'",
                aid,
                image_url,
                extra["name"],
                json.dumps(extra),
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            self.bot.cached_roblox_templates[aid] = (image_url, extra, now)

            await self.self_send_asset(ctx, image_url, extra, cached_at=now)

    @roblox_group.command("game")
    @app_commands.describe(game="Roblox game name, place ID, universe ID, or URL.")
    async def roblox_game(self, ctx: Context, *, game: str) -> None:
        """Look up a Roblox game and its public stats."""
        async with ctx.typing():
            await self._roblox_game_lookup(ctx, game)

    async def _roblox_game_lookup(self, ctx: Context, game: str) -> None:
        universe_id = await self._roblox_game_universe_id(game)
        result = await self._roblox_json(
            "GET",
            "https://games.roblox.com/v1/games",
            params={"universeIds": universe_id},
        )
        entries = result.get("data", []) if isinstance(result, dict) else []
        game_data = next((entry for entry in entries if isinstance(entry, dict)), None)
        if not isinstance(game_data, dict) or not game_data.get("id"):
            raise commands.BadArgument(f"Could not find the Roblox game `{game}`.")

        images, votes, social_links, maturity, gamepasses = await asyncio.gather(
            self._roblox_game_images(universe_id),
            self._roblox_game_votes(universe_id),
            self._roblox_game_social_links(universe_id),
            self._roblox_game_maturity(universe_id),
            self._roblox_gamepass_totals(universe_id),
        )
        icon_url, thumbnail_url = images
        root_place_id = game_data.get("rootPlaceId")
        try:
            root_place_id = int(root_place_id) if root_place_id else universe_id
        except (TypeError, ValueError):
            root_place_id = universe_id
        game_url = f"https://www.roblox.com/games/{root_place_id}/"

        def number(value: Any) -> str | None:
            try:
                return f"{int(value):,}" if value is not None else None
            except (TypeError, ValueError):
                return None

        creator = game_data.get("creator")
        if not isinstance(creator, dict):
            creator = {}
        creator_name = _roblox_text(
            creator.get("name") or creator.get("displayName") or "Unknown", 180
        )
        creator_id = creator.get("id")
        creator_type = str(creator.get("type") or "").casefold()
        if creator_id:
            try:
                creator_id = int(creator_id)
                creator_path = (
                    f"groups/{creator_id}/"
                    if "group" in creator_type or "community" in creator_type
                    else f"users/{creator_id}/profile"
                )
                creator_display = (
                    f"[{creator_name}](https://www.roblox.com/{creator_path})"
                )
            except (TypeError, ValueError):
                creator_display = creator_name
        else:
            creator_display = creator_name

        likes, dislikes = votes
        fields = [f"**Developer:** {creator_display}"]
        if likes is not None or dislikes is not None:
            likes_text = f"{likes:,}" if likes is not None else "Unknown"
            dislikes_text = f"{dislikes:,}" if dislikes is not None else "Unknown"
            fields.append(f"**Likes:** {likes_text} · **Dislikes:** {dislikes_text}")
        for label, key in (
            ("Visits", "visits"),
            ("Active players", "playing"),
            ("Server size", "maxPlayers"),
            ("Favourites", "favoritedCount"),
        ):
            value = number(game_data.get(key))
            if value is not None:
                fields.append(f"**{label}:** {value}")

        price = game_data.get("price")
        try:
            play_cost = "Free" if price in (None, 0, "0") else f"{int(price):,} Robux"
        except (TypeError, ValueError):
            play_cost = _roblox_text(price, 80) if price else "Free"
        if play_cost != "Free":
            fields.append(f"**Play cost:** {play_cost}")

        for label, value in (
            ("Genre", game_data.get("genre") or game_data.get("genreDescription")),
            ("Maturity", maturity),
            ("Created", _roblox_date(game_data.get("created"))),
            ("Last updated", _roblox_date(game_data.get("updated"))),
        ):
            if value:
                fields.append(f"**{label}:** {_roblox_text(value, 180)}")

        if gamepasses is not None:
            pass_count, pass_total = gamepasses
            fields.append(
                f"**Game passes:** {pass_count:,} · **Listed total:** {pass_total:,} Robux"
            )
        if social_links:
            fields.append(f"**Social links:** {' · '.join(social_links)}")

        view = self._roblox_view(
            title=str(game_data.get("name") or "Roblox game"),
            url=game_url,
            description=str(game_data.get("description") or "No description."),
            sections=["\n".join(fields)],
            media_url=icon_url,
            thumbnail=True,
            gallery_url=thumbnail_url,
            strip_title_emojis=True,
            footer=f"Universe ID: {universe_id} · Data from Roblox",
            buttons=[
                discord.ui.Button(
                    label="Open game", style=discord.ButtonStyle.link, url=game_url
                )
            ],
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @roblox_group.command("group")
    @app_commands.describe(group="Roblox group ID, group URL, or group name.")
    async def roblox_group_lookup(self, ctx: Context, *, group: str) -> None:
        """Look up a Roblox group by ID, URL, or name."""
        async with ctx.typing():
            await self._roblox_group_lookup(ctx, group)

    async def _roblox_group_lookup(self, ctx: Context, group: str) -> None:
        """Look up a Roblox group."""
        group_id = self._roblox_id(
            group,
            r"roblox\.com/groups/(\d+)",
            r"roblox\.com/communities/(\d+)",
        )
        if group_id is not None:
            data = await self._roblox_json(
                "GET", f"https://groups.roblox.com/v1/groups/{group_id}"
            )
        else:
            search = await self._roblox_json(
                "GET",
                "https://groups.roblox.com/v1/groups/search",
                params={"keyword": group[:50], "limit": 10},
            )
            candidates = search.get("data", []) if isinstance(search, dict) else []
            data = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict)
                    and str(item.get("name", "")).casefold() == group.casefold()
                ),
                candidates[0] if candidates else None,
            )
            group_id = (
                int(data["id"]) if isinstance(data, dict) and data.get("id") else None
            )
            if group_id is not None:
                data = await self._roblox_json(
                    "GET", f"https://groups.roblox.com/v1/groups/{group_id}"
                )
        if not isinstance(data, dict) or not data.get("id"):
            raise commands.BadArgument(f"Could not find the Roblox group `{group}`.")

        group_id = int(data["id"])
        icon_data = await self._roblox_json(
            "GET",
            "https://thumbnails.roblox.com/v1/groups/icons",
            params={"groupIds": group_id, "size": "420x420", "format": "Png"},
        )
        icons = icon_data.get("data", []) if isinstance(icon_data, dict) else []
        icon = icons[0].get("imageUrl") if icons else None
        owner = data.get("owner") or {}
        shout = data.get("shout") or {}
        owner_name = _roblox_text(
            owner.get("username") or owner.get("displayName") or "Unknown", 180
        )
        sections = [
            "\n".join(
                (
                    f"**Group ID:** `{group_id}`",
                    f"**Members:** {int(data.get('memberCount') or 0):,}",
                    f"**Owner:** {owner_name}",
                )
            )
        ]
        if shout.get("body"):
            sections.append(f"**Shout:**\n{_roblox_text(shout['body'], 1_000)}")
        created = data.get("created")
        if created:
            sections.append(f"**Created:** {_roblox_text(created, 100)}")
        group_url = f"https://www.roblox.com/groups/{group_id}/"
        view = self._roblox_view(
            title=str(data.get("name") or "Roblox group"),
            url=group_url,
            description=str(data.get("description") or "No description."),
            sections=sections,
            media_url=str(icon) if icon else None,
            thumbnail=True,
            buttons=[
                discord.ui.Button(
                    label="Open group", style=discord.ButtonStyle.link, url=group_url
                )
            ],
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @roblox_group.command(
        "item",
        extras={
            "usage": (
                "<item> [-creator <name> -roblox -limited -offsale -onsale "
                "-type <id/name>]"
            )
        },
    )
    @app_commands.describe(
        item="Roblox item ID, URL, or name. Text flags: -creator, -limited, -offsale, -onsale, -type, -roblox."
    )
    async def roblox_item(self, ctx: Context, *, item: str) -> None:
        """Look up a Roblox item with optional creator, sale, limited, and type filters.

        -# -creator <name>  Only match items by this creator.
        -# -roblox           Use Roblox as the creator filter.
        -# -limited          Only match limited or collectible items.
        -# -offsale          Only match items that are off sale.
        -# -onsale           Only match items currently on sale.
        -# -type <id/name>   Filter by Roblox asset type, such as `41` or `hair`.
        """
        async with ctx.typing():
            query, filters = _parse_roblox_item_query(item)
            await self._roblox_item(ctx, query, filters=filters)

    async def _roblox_item_resale_data(
        self, item_id: int, catalog: dict[str, Any], economy: dict[str, Any]
    ) -> dict[str, Any]:
        collectible_id = catalog.get("collectibleItemId") or economy.get(
            "CollectibleItemId"
        )
        if collectible_id:
            url = (
                "https://apis.roblox.com/marketplace-sales/v1/item/"
                f"{collectible_id}/resale-data"
            )
        else:
            url = f"https://economy.roblox.com/v1/assets/{item_id}/resale-data"
        resale = await self._roblox_optional_json("GET", url)
        return resale if isinstance(resale, dict) else {}

    async def _roblox_item_reseller_count(
        self, catalog: dict[str, Any], *, max_pages: int = 20
    ) -> int | None:
        """Count current official reseller listings for a collectible item."""
        count, _ = await self._roblox_item_resellers(catalog, max_pages=max_pages)
        return count

    async def _roblox_item_resellers(
        self, catalog: dict[str, Any], *, max_pages: int = 20
    ) -> tuple[int | None, int | None]:
        """Return the count and lowest current price for official resellers."""
        collectible_id = catalog.get("collectibleItemId")
        if not collectible_id:
            return None, None
        count = 0
        lowest_price: int | None = None
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._roblox_optional_json(
                "GET",
                f"https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers",
                params=params,
            )
            if not isinstance(data, dict):
                return None, None
            rows = data.get("data")
            if not isinstance(rows, list):
                return None, None
            count += len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                price = _roblox_price(
                    row.get("priceInRobux", row.get("price", row.get("priceInRobux")))
                )
                if price is not None and price > 0:
                    lowest_price = (
                        price if lowest_price is None else min(lowest_price, price)
                    )
            next_cursor = data.get("nextPageCursor") or data.get("next_cursor")
            if not next_cursor:
                return count, lowest_price
            next_cursor = str(next_cursor)
            if next_cursor == cursor:
                return count, lowest_price
            cursor = next_cursor
        # Avoid presenting a partial count as an exact total.
        return None, lowest_price

    @staticmethod
    def _roblox_item_sale_fields(
        catalog: dict[str, Any],
        economy: dict[str, Any],
        resale: dict[str, Any],
        reseller_count: int | None = None,
        reseller_lowest_price: int | None = None,
    ) -> tuple[list[str], bool]:
        restrictions = [
            str(value).casefold() for value in (catalog.get("itemRestrictions") or [])
        ]
        limited = bool(
            economy.get("IsLimited")
            or economy.get("IsLimitedUnique")
            or any(
                value in {"limited", "limitedunique", "collectible"}
                for value in restrictions
            )
        )
        status = (
            str(catalog.get("priceStatus") or "")
            .replace("_", "")
            .replace(" ", "")
            .casefold()
        )
        original = _roblox_price(catalog.get("price"))
        if original is None:
            original = _roblox_price(economy.get("PriceInRobux"))
        lowest = _roblox_price(catalog.get("lowestPrice"))
        if lowest is None or lowest <= 0:
            lowest = _roblox_price(catalog.get("lowestResalePrice"))
        fields: list[str] = []
        is_free = status == "free" or (
            not limited and original == 0 and economy.get("IsForSale") is not False
        )
        is_off_sale = status in {"offsale", "notforsale", "notapplicable"} or (
            economy.get("IsForSale") is False
        )
        if limited:
            if original is not None:
                fields.append(f"**Original price:** {original:,} Robux")
            current_price = reseller_lowest_price or lowest
            if current_price is not None and current_price > 0:
                fields.append(f"**Price:** {current_price:,} Robux")
            elif is_free:
                fields.append("**Price:** Free")
            else:
                fields.append("**Price:** No current sellers.")
        elif is_free:
            fields.append("**Price:** Free")
        elif is_off_sale:
            fields.append("**Price:** Off-sale")
        elif status == "noresellers":
            fields.append("**Price:** No resellers")
        elif original is not None:
            fields.append(f"**Price:** {original:,} Robux")

        rap = _roblox_price(resale.get("recentAveragePrice"))
        if limited and rap is not None:
            fields.append(f"**RAP:** {rap:,} Robux")

        # Roblox does not publish Rolimon's community Value metric. Do not
        # substitute RAP or the lowest listing and present it as Value.
        if reseller_count is not None:
            fields.append(f"**Resellers:** {reseller_count:,}")
        return fields, limited

    @staticmethod
    def _roblox_item_favourite_count(
        catalog: dict[str, Any], economy: dict[str, Any]
    ) -> int | None:
        for source in (catalog, economy):
            for key in (
                "favoriteCount",
                "favoritedCount",
                "FavoriteCount",
                "FavoritedCount",
            ):
                value = _roblox_price(source.get(key))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _roblox_item_matches_filters(
        item: dict[str, Any],
        filters: dict[str, Any],
        *,
        economy: dict[str, Any] | None = None,
    ) -> bool:
        economy = economy or {}
        creator_filter = filters.get("creator")
        if creator_filter:
            creator_name = str(item.get("creatorName") or "")
            creator_id = str(item.get("creatorTargetId") or "")
            if str(creator_filter).casefold() == "roblox":
                if creator_id != "1" and creator_name.casefold() != "roblox":
                    return False
            elif creator_name.casefold() != str(creator_filter).casefold():
                return False

        type_id = filters.get("type_id")
        if type_id is not None:
            item_type = item.get("assetType", item.get("assetTypeId"))
            try:
                if int(str(item_type)) != int(type_id):
                    return False
            except (TypeError, ValueError):
                return False

        restrictions = {
            str(value).casefold() for value in (item.get("itemRestrictions") or [])
        }
        limited = bool(
            item.get("isLimited")
            or item.get("isLimitedUnique")
            or economy.get("IsLimited")
            or economy.get("IsLimitedUnique")
            or restrictions.intersection({"limited", "limitedunique", "collectible"})
        )
        if filters.get("limited") and not limited:
            return False

        status = str(item.get("priceStatus") or "").replace("_", "").replace(" ", "")
        off_sale = status in {"offsale", "notforsale", "noresellers", "notapplicable"}
        off_sale = off_sale or item.get("isOffSale") is True
        off_sale = off_sale or economy.get("IsForSale") is False
        current_listing = _roblox_price(item.get("lowestPrice"))
        if current_listing is None or current_listing <= 0:
            current_listing = _roblox_price(item.get("lowestResalePrice"))
        if current_listing is not None and current_listing > 0:
            off_sale = False
        if filters.get("offsale") and not off_sale:
            return False
        if filters.get("onsale") and off_sale:
            return False
        return True

    async def _roblox_item(
        self,
        ctx: Context,
        item: str,
        *,
        filters: dict[str, Any] | None = None,
    ) -> None:
        """Look up a Roblox catalog item."""
        filters = filters or {}
        item_id = self._roblox_id(item, r"roblox\.com/(?:catalog|library)/(\d+)")
        if item_id is None:
            search_params: dict[str, Any] = {
                "Keyword": item[:100],
                "Category": "All",
                "Limit": 30,
                "SortType": "Relevance",
            }
            if filters.get("creator"):
                search_params["CreatorName"] = str(filters["creator"])
            if filters.get("type_id") is not None:
                search_params["AssetTypes"] = str(filters["type_id"])
            if filters.get("offsale") or filters.get("limited"):
                search_params["IncludeNotForSale"] = "true"
            elif filters.get("onsale"):
                search_params["IncludeNotForSale"] = "false"
            search = await self._roblox_json(
                "GET",
                "https://catalog.roblox.com/v2/search/items/details",
                params=search_params,
            )
            candidates = search.get("data", []) if isinstance(search, dict) else []
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and str(candidate.get("name", "")).casefold() == item.casefold()
                    and self._roblox_item_matches_filters(candidate, filters)
                ),
                next(
                    (
                        candidate
                        for candidate in candidates
                        if isinstance(candidate, dict)
                        and self._roblox_item_matches_filters(candidate, filters)
                    ),
                    None,
                ),
            )
            item_id = (
                int(selected["id"])
                if isinstance(selected, dict) and selected.get("id")
                else None
            )
        if item_id is None:
            raise commands.BadArgument("Could not find that Roblox catalog item.")
        data = await self._roblox_json(
            "GET", f"https://economy.roblox.com/v2/assets/{item_id}/details"
        )
        if not isinstance(data, dict) or not data.get("Name"):
            raise commands.BadArgument(f"Could not find Roblox item `{item_id}`.")
        catalog_data = await self._roblox_optional_json(
            "GET",
            f"https://catalog.roblox.com/v1/catalog/items/{item_id}/details",
            params={"itemType": "Asset"},
        )
        catalog = catalog_data if isinstance(catalog_data, dict) else {}
        if filters and not self._roblox_item_matches_filters(
            catalog, filters, economy=data
        ):
            raise commands.BadArgument(
                "That Roblox item does not match the filters you provided."
            )
        creator = data.get("Creator") or {}
        creator_name = _roblox_text(
            catalog.get("creatorName") or creator.get("Name") or "Unknown", 180
        )
        creator_id = catalog.get("creatorTargetId") or creator.get("Id")
        creator_type = catalog.get("creatorType") or creator.get("CreatorType")
        thumb_data = await self._roblox_json(
            "GET",
            "https://thumbnails.roblox.com/v1/assets",
            params={"assetIds": item_id, "size": "420x420", "format": "Png"},
        )
        thumbs = thumb_data.get("data", []) if isinstance(thumb_data, dict) else []
        image_url = (
            str(thumbs[0]["imageUrl"]) if thumbs and thumbs[0].get("imageUrl") else None
        )
        if creator_id:
            creator_url = (
                f"https://www.roblox.com/groups/{creator_id}/"
                if creator_type == "Group"
                else f"https://www.roblox.com/users/{creator_id}/profile"
            )
            creator_name = f"[{_roblox_text(creator_name, 180)}]({creator_url})"
            if str(creator_id) == "1":
                creator_name += " (Official)"
        sections = [
            "\n".join(
                (
                    f"**Creator:** {creator_name}",
                    f"**Asset type:** {_roblox_text(data.get('AssetTypeId') or 'Unknown', 80)}",
                    f"**Item ID:** `{item_id}`",
                )
            )
        ]
        limited = bool(
            data.get("IsLimited")
            or data.get("IsLimitedUnique")
            or any(
                str(value).casefold() in {"limited", "limitedunique", "collectible"}
                for value in (catalog.get("itemRestrictions") or [])
            )
        )
        if limited:
            resale, reseller_data = await asyncio.gather(
                self._roblox_item_resale_data(item_id, catalog, data),
                self._roblox_item_resellers(catalog),
            )
            reseller_count, reseller_lowest_price = reseller_data
        else:
            resale, reseller_count, reseller_lowest_price = {}, None, None
        sale_fields, _ = self._roblox_item_sale_fields(
            catalog, data, resale, reseller_count, reseller_lowest_price
        )
        if sale_fields:
            sections.append("\n".join(sale_fields))
        favourite_count = self._roblox_item_favourite_count(catalog, data)
        if favourite_count is not None:
            sections.append(f"**Favourites:** {favourite_count:,}")
        sales = data.get("Sales")
        try:
            sales_count = int(sales) if sales is not None else 0
        except (TypeError, ValueError):
            sales_count = 0
        if sales_count > 0:
            sections.append(f"**Sales:** {sales_count:,}")
        created_text = _roblox_date(data.get("Created") or data.get("created"))
        item_url = f"https://www.roblox.com/catalog/{item_id}/"
        view = self._roblox_view(
            title=str(data.get("Name")),
            url=item_url,
            description=str(data.get("Description") or "No description."),
            sections=sections,
            media_url=image_url,
            thumbnail=True,
            footer=(
                f"Created at: {created_text} · Data from Roblox"
                if created_text
                else "Data from Roblox"
            ),
            buttons=[
                discord.ui.Button(
                    label="Open item", style=discord.ButtonStyle.link, url=item_url
                )
            ],
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @roblox_group.command("outfits")
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_outfits(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox user to view."
        ),
    ) -> None:
        """Browse a Roblox user's saved custom outfits."""
        async with ctx.typing():
            view = await self._roblox_outfits(ctx, username)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _roblox_outfits(
        self, ctx: Context, username: Optional[str]
    ) -> RobloxOutfitPaginator:
        """Browse a Roblox user's saved custom outfits."""
        profile = await self._roblox_user(username, ctx)
        user_id = int(profile["id"])
        data = await self._roblox_json(
            "GET",
            f"https://avatar.roblox.com/v2/avatar/users/{user_id}/outfits",
            params={"isEditable": "true", "itemsPerPage": 50},
        )
        outfits = [
            outfit
            for outfit in (data.get("data", []) if isinstance(data, dict) else [])
            if isinstance(outfit, dict)
            and outfit.get("id")
            and outfit.get("isEditable") is True
            and str(outfit.get("outfitType") or "Avatar") == "Avatar"
        ]
        if not outfits:
            raise commands.BadArgument(
                f"{profile.get('name', 'This user')} has no saved custom outfits."
            )
        view = RobloxOutfitPaginator(ctx, self, profile, outfits[:50])
        await view.load_current()
        return view

    async def _roblox_inventory_items(self, user_id: int) -> list[dict[str, Any]]:
        # ``can-view-inventory`` is viewer-sensitive and can report false for
        # inventories that the public inventory endpoints still expose. Treat
        # the actual category and item responses as authoritative instead.
        try:
            categories = await self._roblox_json(
                "GET", f"https://inventory.roblox.com/v1/users/{user_id}/categories"
            )
        except commands.BadArgument:
            # A private inventory may reject the category request. Continue
            # with the known public asset types so the final result can still
            # distinguish a false visibility response from available items.
            categories = {}
        asset_type_ids = {
            int(item["id"])
            for category in (
                categories.get("categories", []) if isinstance(categories, dict) else []
            )
            if isinstance(category, dict)
            for item in (category.get("items", []) or [])
            if isinstance(item, dict)
            and item.get("type") == "AssetType"
            and str(item.get("id", "")).isdigit()
        }
        if not asset_type_ids:
            asset_type_ids = {
                2,
                8,
                11,
                12,
                13,
                18,
                19,
                24,
                41,
                42,
                43,
                44,
                45,
                46,
                47,
                61,
                64,
                65,
                66,
                67,
                68,
                69,
                70,
                71,
                72,
            }

        semaphore = asyncio.Semaphore(8)

        async def fetch_type(asset_type_id: int) -> list[dict[str, Any]]:
            collected: list[dict[str, Any]] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(20):
                params: dict[str, Any] = {
                    "sortOrder": "Desc",
                    "limit": 50,
                }
                if cursor:
                    params["cursor"] = cursor
                async with semaphore:
                    try:
                        data = await self._roblox_json(
                            "GET",
                            f"https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type_id}",
                            params=params,
                        )
                    except commands.CommandError:
                        break
                items = data.get("data", []) if isinstance(data, dict) else []
                collected.extend(
                    item
                    for item in items
                    if isinstance(item, dict)
                    and (item.get("assetId") or item.get("id"))
                )
                next_cursor = (
                    data.get("nextPageCursor") if isinstance(data, dict) else None
                )
                if not next_cursor:
                    break
                next_cursor = str(next_cursor)
                if next_cursor in seen_cursors or next_cursor == cursor:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            return collected

        results = await asyncio.gather(
            *(fetch_type(asset_type_id) for asset_type_id in sorted(asset_type_ids))
        )
        unique: dict[int, dict[str, Any]] = {}
        for items in results:
            for item in items:
                try:
                    asset_id = int(str(item.get("assetId") or item.get("id")))
                except (TypeError, ValueError):
                    continue
                unique.setdefault(asset_id, item)
        return sorted(
            unique.values(),
            key=lambda item: str(item.get("created") or item.get("updated") or ""),
            reverse=True,
        )

    @roblox_group.command("inventory")
    @app_commands.describe(username="Roblox username, user ID, or profile URL.")
    async def roblox_inventory(
        self,
        ctx: Context,
        *,
        username: Optional[str] = commands.param(
            default=None, description="Roblox user to view."
        ),
    ) -> None:
        """List a Roblox user's recent public inventory items."""
        async with ctx.typing():
            profile = await self._roblox_user(username, ctx)
            user_id = int(profile["id"])
            items = await self._roblox_inventory_items(user_id)
            if not items:
                raise commands.BadArgument(
                    "This inventory is private, empty, or Roblox did not return any public items."
                )
            view = RobloxInventoryPaginator(ctx, self, profile, items)
            await view.load_page()
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

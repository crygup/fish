"""Recent Mudae character claims.

The Mudae character card is emitted once, then edited when somebody claims it.
This module contains the small parser and Components V2 paginator used by the
Mudae cog.  Keeping the parser independent from the event listener makes it
possible to test the transition logic with raw gateway fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import discord

from .wishes import (
    FOOTER_KAKERA_RE,
    KAKERA_RE,
    NON_SPAWN_MARKERS_RE,
    PAGINATION_FOOTER_RE,
    _extract_series,
    parse_mudae_embed,
)

if TYPE_CHECKING:
    from extensions.context import Context


CLAIMANT_RE = re.compile(
    r"\b(?:belongs\s+to|claimed(?:\s+by)?)\s*[:\-]?\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MudaeSpawn:
    """The identifying data shared by the before/after card versions."""

    character: str
    series: str | None
    kakera: int


@dataclass(frozen=True, slots=True)
class RecentClaim:
    """One persisted Mudae claim."""

    id: int
    channel_id: int
    character_name: str
    claiming_username: str
    claiming_user_id: int
    claimed_at: datetime


def _value(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _embed_parts(embed: object) -> tuple[str | None, str, str]:
    author = _value(embed, "author") or {}
    character = _value(author, "name")
    description = _value(embed, "description", "") or ""
    footer = _value(embed, "footer") or {}
    footer_text = _value(footer, "text", "") or ""
    return (
        character.strip() if isinstance(character, str) and character.strip() else None,
        description if isinstance(description, str) else "",
        footer_text.strip() if isinstance(footer_text, str) else "",
    )


def parse_mudae_spawn(
    embed: object, *, allow_claimed: bool = False
) -> MudaeSpawn | None:
    """Parse a valid Mudae character card.

    ``parse_mudae_embed`` is intentionally strict and rejects claimed cards,
    which is exactly what the existing wish listener needs.  Recent claims
    need to parse the *after* side of an edit as well, so this helper keeps the
    same non-spawn/kakera validation while optionally allowing a claimed
    footer.  It never treats profile/list/ranking embeds as spawns.
    """

    if not allow_claimed:
        parsed = parse_mudae_embed(embed)
        if parsed is None or parsed.kakera is None:
            return None
        return MudaeSpawn(parsed.character, parsed.series, int(parsed.kakera))

    character, description, footer = _embed_parts(embed)
    if character is None:
        return None
    if NON_SPAWN_MARKERS_RE.search(description):
        return None
    if footer and PAGINATION_FOOTER_RE.fullmatch(footer):
        return None

    match = KAKERA_RE.search(description)
    if match:
        kakera_text = match.group(1) or match.group(2)
        kakera = int(kakera_text.replace(",", ""))
    else:
        footer_match = FOOTER_KAKERA_RE.search(footer)
        kakera = int(footer_match.group(1).replace(",", "")) if footer_match else None
    if kakera is None:
        return None

    return MudaeSpawn(character, _extract_series(description, footer), kakera)


def claiming_username(embed: object) -> str | None:
    """Extract the claimant name when Mudae exposes it in the footer."""

    _character, _description, footer = _embed_parts(embed)
    if not footer:
        return None
    match = CLAIMANT_RE.search(footer)
    if match is None:
        return None
    # Only remove wrappers around the username.  ``remove_markdown`` would
    # also strip underscores inside legitimate Discord usernames.
    name = match.group("name").strip()
    # A custom footer may include a trailing separator or markdown link; keep
    # only the visible username and avoid storing a giant arbitrary footer.
    name = name.split("\n", 1)[0].strip(" `*_~")
    return name[:256] or None


def _reaction_key(reaction: object) -> tuple[object, str | None, int]:
    emoji = _value(reaction, "emoji")
    emoji_id = _value(emoji, "id")
    emoji_name = _value(emoji, "name")
    count = _value(reaction, "count", 0)
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    return emoji_id, str(emoji_name) if emoji_name is not None else None, count


def reaction_count(message: object) -> int:
    """Return the total reaction count from a cached or fixture message."""

    reactions = _value(message, "reactions", ()) or ()
    total = 0
    for reaction in reactions:
        _emoji_id, _emoji_name, count = _reaction_key(reaction)
        total += count
    return total


def claim_transition(
    before: object, after: object
) -> tuple[MudaeSpawn, MudaeSpawn] | None:
    """Return the spawn pair only when an unclaimed card became claimed.

    Both sides are parsed as character cards, while the before side is not
    classified from a particular footer layout: servers can customize Mudae's
    footer with ``$setfooter``.  A claim signal must appear on the after side
    (a new reaction or recognizable claimant footer), and an existing reaction
    or claimant on the before side suppresses duplicates.
    """

    before_embed = next(iter(_value(before, "embeds", ()) or ()), None)
    after_embed = next(iter(_value(after, "embeds", ()) or ()), None)
    if before_embed is None or after_embed is None:
        return None
    old = parse_mudae_spawn(before_embed, allow_claimed=True)
    if old is None:
        return None
    new = parse_mudae_spawn(after_embed, allow_claimed=True)
    if new is None or new.character.casefold() != old.character.casefold():
        return None

    # A cached before message can still include a reaction object with a zero
    # count.  Compare both total counts and emoji/count signatures so a claim
    # is detected when Mudae's gateway payload only includes the changed one.
    old_reactions = tuple(
        _reaction_key(r) for r in (_value(before, "reactions", ()) or ())
    )
    new_reactions = tuple(
        _reaction_key(r) for r in (_value(after, "reactions", ()) or ())
    )
    reaction_added = (
        reaction_count(after) > reaction_count(before) or old_reactions != new_reactions
    )
    footer_claim = claiming_username(after_embed) is not None
    if not reaction_added and not footer_claim:
        return None
    # When the before side was already claimed but had a custom footer, its
    # reactions are the remaining reliable signal.  Reject it regardless of
    # the after footer so a later edit cannot create a duplicate claim.
    if reaction_count(before) > 0 or claiming_username(before_embed) is not None:
        return None
    return old, new


def _safe_colour(ctx: Context) -> discord.Colour | int:
    value = getattr(ctx, "embedcolor", None)
    if value is None:
        bot = getattr(ctx, "bot", None)
        value = getattr(bot, "embedcolor", discord.Colour.blurple())
    return value


class RecentClaimsView(discord.ui.LayoutView):
    """Paginate recent claims, ten entries per page."""

    PAGE_SIZE = 10

    def __init__(
        self,
        ctx: Context,
        claims: Iterable[RecentClaim],
        *,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.claims = tuple(claims)
        self.page = 0
        self.message: discord.Message | None = None
        self._buttons: list[discord.ui.Button] = []
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.claims) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return discord.utils.format_dt(value, "R")

    def _render(self) -> None:
        self.clear_items()
        self.page %= self.page_count
        start = self.page * self.PAGE_SIZE
        page_claims = self.claims[start : start + self.PAGE_SIZE]
        lines = ["## Recent Mudae claims", ""]
        if page_claims:
            for claim in page_claims:
                character = discord.utils.escape_markdown(claim.character_name)
                username = discord.utils.escape_markdown(
                    claim.claiming_username or "Unknown"
                )
                if claim.claiming_user_id > 1:
                    claimant = f"{username} (`{claim.claiming_user_id}`)"
                else:
                    claimant = "Unknown user"
                lines.append(
                    f"- **{character}** · {claimant} · "
                    f"{self._timestamp(claim.claimed_at)} · <#{claim.channel_id}>"
                )
        else:
            lines.append("No recent claims recorded.")
        lines.extend(("", f"-# Page {self.page + 1}/{self.page_count}"))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=_safe_colour(self.ctx),
            )
        )

        previous = discord.ui.Button(
            label="<",
            style=discord.ButtonStyle.secondary,
            disabled=self.page_count <= 1,
        )
        next_button = discord.ui.Button(
            label=">",
            style=discord.ButtonStyle.secondary,
            disabled=self.page_count <= 1,
        )

        async def previous_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page - 1) % self.page_count
            await self._edit_from_interaction(interaction)

        async def next_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page + 1) % self.page_count
            await self._edit_from_interaction(interaction)

        previous.callback = previous_callback
        next_button.callback = next_callback
        self._buttons = [previous, next_button]
        self.add_item(discord.ui.ActionRow(previous, next_button))

    async def _edit_from_interaction(self, interaction: discord.Interaction) -> None:
        self._render()
        if interaction.response.is_done():
            if self.message is not None:
                await self.message.edit(view=self)
            return
        await interaction.response.edit_message(view=self)

    async def start(self, *, ephemeral: bool = False) -> None:
        self.message = await self.ctx.send(view=self, ephemeral=ephemeral)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this list can use its controls.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        for button in self._buttons:
            button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


__all__ = [
    "MudaeSpawn",
    "RecentClaim",
    "RecentClaimsView",
    "claim_transition",
    "claiming_username",
    "parse_mudae_spawn",
    "reaction_count",
]

from __future__ import annotations

import asyncio
import math
from itertools import combinations
from typing import TYPE_CHECKING, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils.emojis import sp, spB, spG, spO, spP, spT, spU, spY

if TYPE_CHECKING:
    from extensions.context import Context

TESTING = False  # set to False for production Mudae
MudaeID = 432610292342587392

SPHERE_MAP: dict[int, str] = {
    sp.id: "red",  # type: ignore
    spB.id: "blue",  # type: ignore
    spT.id: "teal",  # type: ignore
    spG.id: "green",  # type: ignore
    spY.id: "yellow",  # type: ignore
    spO.id: "orange",  # type: ignore
}

OQ_SPHERE_MAP: dict[int, str] = {
    sp.id: "red",  # type: ignore
    spP.id: "purple",  # type: ignore
    spB.id: "blue",  # type: ignore
    spT.id: "teal",  # type: ignore
    spG.id: "green",  # type: ignore
    spY.id: "yellow",  # type: ignore
    spO.id: "orange",  # type: ignore
}

GRID_SIZE = 5
CENTER = 12
OQ_TARGET_TOTAL = 4
OQ_PURPLES_BEFORE_RED = 3
OQ_MAX_CLICKS = 7
OQ_INITIAL_MOVES = (6, 16, 8, 18)
OQ_CLUE_VALUES = {
    "blue": 0,
    "teal": 1,
    "green": 2,
    "yellow": 3,
    "orange": 4,
}


def _to_rc(idx: int) -> Tuple[int, int]:
    """Convert linear index to (row, col)."""
    return divmod(idx, GRID_SIZE)


def _from_rc(row: int, col: int) -> int:
    """Convert (row, col) to linear index."""
    return row * GRID_SIZE + col


def _oq_neighbors(position: int) -> Tuple[int, ...]:
    row, column = _to_rc(position)
    neighbors = []
    for row_offset in range(-1, 2):
        for column_offset in range(-1, 2):
            if row_offset == 0 and column_offset == 0:
                continue
            neighbor_row = row + row_offset
            neighbor_column = column + column_offset
            if 0 <= neighbor_row < GRID_SIZE and 0 <= neighbor_column < GRID_SIZE:
                neighbors.append(_from_rc(neighbor_row, neighbor_column))
    return tuple(neighbors)


def _same_row(a: int, b: int) -> bool:
    return a // GRID_SIZE == b // GRID_SIZE


def _same_col(a: int, b: int) -> bool:
    return a % GRID_SIZE == b % GRID_SIZE


def _same_diag(a: int, b: int) -> bool:
    ar, ac = _to_rc(a)
    br, bc = _to_rc(b)
    return abs(ar - br) == abs(ac - bc)


def _adjacent(a: int, b: int) -> bool:
    ar, ac = _to_rc(a)
    br, bc = _to_rc(b)
    return max(abs(ar - br), abs(ac - bc)) == 1


OQ_NEIGHBORS = tuple(
    _oq_neighbors(position) for position in range(GRID_SIZE * GRID_SIZE)
)


def _oq_layouts(revealed: dict[int, str]) -> list[frozenset[int]]:
    """Return every four-target layout compatible with the revealed OQ clues."""
    targets = {
        position for position, color in revealed.items() if color in {"purple", "red"}
    }
    if len(targets) > OQ_TARGET_TOTAL:
        return []
    if sum(color == "red" for color in revealed.values()) > 1:
        return []

    remaining = OQ_TARGET_TOTAL - len(targets)
    candidates = [
        position
        for position in range(GRID_SIZE * GRID_SIZE)
        if position not in revealed
    ]
    if remaining > len(candidates):
        return []

    clues = [
        (position, OQ_CLUE_VALUES[color])
        for position, color in revealed.items()
        if color in OQ_CLUE_VALUES
    ]
    layouts: list[frozenset[int]] = []
    for selected in combinations(candidates, remaining):
        layout = frozenset(targets.union(selected))
        if all(
            sum(neighbor in layout for neighbor in OQ_NEIGHBORS[position]) == value
            for position, value in clues
        ):
            layouts.append(layout)
    return layouts


def _oq_outcome(layout: frozenset[int], position: int) -> str | int:
    if position in layout:
        return "target"
    return sum(neighbor in layout for neighbor in OQ_NEIGHBORS[position])


def _oq_best_clicks(revealed: dict[int, str]) -> list[int]:
    """Return up to four equally ranked next clicks for an OQ board."""
    if not revealed:
        return list(OQ_INITIAL_MOVES)

    targets = {
        position for position, color in revealed.items() if color in {"purple", "red"}
    }
    purple_count = sum(color == "purple" for color in revealed.values())
    clicks_used = sum(color != "red" for color in revealed.values())
    if (
        len(targets) >= OQ_TARGET_TOTAL
        or "red" in revealed.values()
        or purple_count >= OQ_PURPLES_BEFORE_RED
        or clicks_used >= OQ_MAX_CLICKS
    ):
        return []

    layouts = _oq_layouts(revealed)
    if not layouts:
        return []

    unknown = [
        position
        for position in range(GRID_SIZE * GRID_SIZE)
        if position not in revealed
    ]
    frequencies = {
        position: sum(position in layout for layout in layouts) for position in unknown
    }
    guaranteed = [
        position
        for position, frequency in frequencies.items()
        if frequency == len(layouts)
    ]
    if guaranteed:
        return sorted(
            guaranteed,
            key=lambda position: (-len(OQ_NEIGHBORS[position]), position),
        )[:4]

    ranked: list[Tuple[int, float, float, float]] = []
    for position in unknown:
        outcomes: dict[str | int, int] = {}
        for layout in layouts:
            outcome = _oq_outcome(layout, position)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        entropy = 0.0
        for count in outcomes.values():
            probability = count / len(layouts)
            entropy -= probability * math.log2(probability)

        target_chance = frequencies[position] / len(layouts)
        score = entropy + target_chance * 1.5
        ranked.append((position, score, target_chance, entropy))

    best_score = max(score for _, score, _, _ in ranked)
    tied = [entry for entry in ranked if math.isclose(entry[1], best_score)]
    best_target_chance = max(target_chance for _, _, target_chance, _ in tied)
    return [
        position
        for position, _, _, _ in sorted(
            (entry for entry in tied if math.isclose(entry[2], best_target_chance)),
            key=lambda entry: (-entry[3], entry[0]),
        )[:4]
    ]


def _possible_red_positions(revealed: dict[int, str]) -> Set[int]:
    """Given {position: color} for revealed spheres, return the set of
    positions where the red sphere could still be."""
    candidates: Set[int] = set(range(GRID_SIZE * GRID_SIZE))
    candidates.discard(CENTER)

    for pos, color in revealed.items():
        if color == "red":
            return {pos}  # found it
        elif color == "orange":
            candidates &= {p for p in candidates if _adjacent(p, pos)}
        elif color == "yellow":
            candidates &= {p for p in candidates if _same_diag(p, pos)}
        elif color == "green":
            candidates &= {
                p for p in candidates if _same_row(p, pos) or _same_col(p, pos)
            }
        elif color == "teal":
            candidates &= {
                p
                for p in candidates
                if _same_row(p, pos) or _same_col(p, pos) or _same_diag(p, pos)
            }
        elif color == "blue":
            candidates -= {
                p
                for p in candidates
                if _same_row(p, pos) or _same_col(p, pos) or _same_diag(p, pos)
            }

    # Remove already-revealed positions
    candidates -= set(revealed)
    return candidates


def _best_next_click(revealed: dict[int, str]) -> Optional[int]:
    """Pick the unrevealed position that maximally narrows red possibilities,
    or after red is found, maximizes score from remaining high-value spheres."""
    max_counts = {"orange": 2, "yellow": 3, "green": 4}
    remaining: dict[str, int] = {}
    for color, count in max_counts.items():
        remaining[color] = count - sum(1 for c in revealed.values() if c == color)

    unrevealed = [
        p for p in range(GRID_SIZE * GRID_SIZE) if p not in revealed and p != CENTER
    ]
    if not unrevealed:
        return None

    red_pos = next((p for p, c in revealed.items() if c == "red"), None)

    if red_pos is not None:
        # Red found, score unrevealed cells by their likely color value
        # Priority: orange (adjacent) > yellow (diag, not adj) > green (row/col, not diag) > teal (row/col/diag) > blue (none)

        def _score(pos: int) -> int:
            adj = _adjacent(pos, red_pos)
            diag = _same_diag(pos, red_pos)
            rowcol = _same_row(pos, red_pos) or _same_col(pos, red_pos)

            if adj and remaining.get("orange", 0) > 0:
                return 50  # likely orange
            if diag and not adj and remaining.get("yellow", 0) > 0:
                return 40  # likely yellow
            if rowcol and not diag and remaining.get("green", 0) > 0:
                return 30  # likely green
            if rowcol or diag:
                return 20  # teal
            return 10  # blue

        return max(unrevealed, key=_score)

    # Red not yet found, original minimax logic
    if not revealed:
        return 16  # optimal starting move: row 4, column 2
    candidates = _possible_red_positions(revealed)
    if not candidates:
        return None
    if len(candidates) == 1:
        return next(iter(candidates))

    unrevealed_candidates = [p for p in unrevealed if p in candidates]
    if not unrevealed_candidates:
        return None

    possible_colors = ["teal", "blue"]
    for color, count in remaining.items():
        if count > 0:
            possible_colors.append(color)
    if sum(1 for c in revealed.values() if c == "red") == 0:
        possible_colors.append("red")

    best_pos = None
    best_worst = len(candidates)

    for pos in unrevealed_candidates:
        max_remaining = 0
        for test_color in possible_colors:
            test = {**revealed, pos: test_color}
            remaining_count = len(_possible_red_positions(test))
            max_remaining = max(max_remaining, remaining_count)
        if max_remaining < best_worst:
            best_worst = max_remaining
            best_pos = pos

    return best_pos


class SphereView(discord.ui.View):
    """5x5 button grid showing sphere chest state."""

    message: Optional[discord.Message]

    def __init__(
        self, ctx: Context, revealed: dict[int, str], recommendation: Optional[int]
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.revealed = revealed
        self.recommendation = recommendation
        self.message = None
        self._build()

    async def _recommendation_click(self, interaction: discord.Interaction) -> None:
        if self.recommendation is None:
            await interaction.response.defer()
            return
        row, col = _to_rc(self.recommendation)
        await interaction.response.send_message(
            f"Click button at **row {row + 1}, column {col + 1}** on the Mudae message above.",
            ephemeral=True,
        )

    def _build(self) -> None:
        self.clear_items()
        emoji_map = {
            "red": sp,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        hidden = spU
        for idx in range(GRID_SIZE * GRID_SIZE):
            row = idx // GRID_SIZE
            if idx in self.revealed:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji=emoji_map[self.revealed[idx]],
                    disabled=True,
                    row=row,
                )
            elif idx == self.recommendation:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=hidden,
                    disabled=False,
                    row=row,
                )
                btn.callback = self._recommendation_click
            else:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    emoji=hidden,
                    disabled=True,
                    row=row,
                )
                btn.callback = self._recommendation_click
            if not btn.callback:
                btn.callback = self._recommendation_click
            self.add_item(btn)


class SphereQView(discord.ui.View):
    """5x5 OQ grid showing revealed colors and recommended clicks."""

    message: Optional[discord.Message]

    def __init__(
        self, ctx: Context, revealed: dict[int, str], recommendations: list[int]
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.revealed = revealed
        self.recommendations = recommendations
        self.message = None
        self._build()

    async def _send_location(
        self, interaction: discord.Interaction, position: int
    ) -> None:
        row, column = _to_rc(position)
        await interaction.response.send_message(
            f"Click button at **row {row + 1}, column {column + 1}** "
            "on the Mudae message above.",
            ephemeral=True,
        )

    def _recommendation_callback(self, position: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._send_location(interaction, position)

        return callback

    def _build(self) -> None:
        self.clear_items()
        emoji_map = {
            "red": sp,
            "purple": spP,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        recommended = set(self.recommendations)
        for position in range(GRID_SIZE * GRID_SIZE):
            row = position // GRID_SIZE
            if position in self.revealed:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji=emoji_map[self.revealed[position]],
                    disabled=True,
                    row=row,
                )
            elif position in recommended:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=spU,
                    disabled=False,
                    row=row,
                )
                button.callback = self._recommendation_callback(position)
            else:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    emoji=spU,
                    disabled=True,
                    row=row,
                )
            self.add_item(button)


OC_TEXT = "You can click **5** times on the buttons below"
OQ_TEXT = "You can click **7** times on the buttons below"


class SphereCog(Cog):
    """Mudae sphere chest solver."""

    def _parse_sphere_components(self, components: list) -> Optional[dict[int, str]]:
        """Parse revealed spheres from raw component data (gateway event)."""
        revealed: dict[int, str] = {}
        idx = 0
        found_any = False
        for action_row in components:
            for child in action_row.get("components", []):
                found_any = True
                if child.get("emoji"):
                    emoji_id = int(child["emoji"]["id"])
                    color = SPHERE_MAP.get(emoji_id)
                    if color:
                        revealed[idx] = color
                idx += 1
        return revealed if found_any else None

    def _parse_sphere_message(
        self, message: discord.Message
    ) -> Optional[dict[int, str]]:
        """Parse a Mudae sphere chest message from its discord.Message components."""
        revealed: dict[int, str] = {}
        idx = 0
        found_any = False
        for action_row in message.components:
            if not isinstance(action_row, discord.ActionRow):
                continue
            for child in action_row.children:
                if not isinstance(child, discord.Button):
                    continue
                found_any = True
                if child.emoji and child.emoji.id:
                    color = SPHERE_MAP.get(child.emoji.id)
                    if color:
                        revealed[idx] = color
                idx += 1
        return revealed if found_any else None

    def _parse_oq_components(self, components: list) -> Optional[dict[int, str]]:
        """Parse an OQ board from raw gateway component data."""
        revealed: dict[int, str] = {}
        position = 0
        found_any = False
        for action_row in components:
            for child in action_row.get("components", []):
                found_any = True
                emoji = child.get("emoji")
                if emoji and emoji.get("id"):
                    color = OQ_SPHERE_MAP.get(int(emoji["id"]))
                    if color:
                        revealed[position] = color
                position += 1
        return revealed if found_any else None

    def _parse_oq_message(self, message: discord.Message) -> Optional[dict[int, str]]:
        """Parse an OQ board from a cached Discord message."""
        revealed: dict[int, str] = {}
        position = 0
        found_any = False
        for action_row in message.components:
            if not isinstance(action_row, discord.ActionRow):
                continue
            for child in action_row.children:
                if not isinstance(child, discord.Button):
                    continue
                found_any = True
                if child.emoji and child.emoji.id:
                    color = OQ_SPHERE_MAP.get(child.emoji.id)
                    if color:
                        revealed[position] = color
                position += 1
        return revealed if found_any else None

    @commands.hybrid_command(name="oc", aliases=("sphere",))
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def sphere(self, ctx: Context):
        """Show the best next move for a Mudae sphere chest game."""
        if not self.bot.user:
            raise commands.BadArgument("Bot not fully loaded, please wait.")
        target_ids = [self.bot.user.id, MudaeID]
        mudae_msg: Optional[discord.Message] = None

        async for msg in ctx.channel.history(limit=8):
            if msg.author.id not in target_ids or not msg.components:
                continue
            if msg.author.bot and OC_TEXT in msg.content:
                mudae_msg = msg
                break

        if not mudae_msg:
            raise commands.BadArgument(
                "No sphere chest found. Run `$oc` or `fish simsphere` first, then try again."
            )

        revealed = self._parse_sphere_message(mudae_msg)
        if revealed is None:
            raise commands.BadArgument(
                "Could not parse the sphere chest grid from the message."
            )

        await self._show_sphere(ctx, revealed, mudae_msg)

    async def _show_sphere(
        self, ctx: Context, revealed: dict[int, str], mudae_msg: discord.Message
    ):

        recommendation = _best_next_click(revealed)
        view = SphereView(ctx, revealed, recommendation)
        view.message = await ctx.send(view=view)

        while True:
            try:
                event = await self.bot.wait_for(
                    "raw_message_edit",
                    check=lambda e: e.message_id == mudae_msg.id,
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                break

            components = event.data.get("components", [])
            if not components:
                continue
            new_revealed = self._parse_sphere_components(components)  # type: ignore[arg-type]
            if new_revealed is None or new_revealed == view.revealed:
                continue

            if len(new_revealed) >= 5:
                view.revealed = new_revealed
                view.recommendation = None
                view._build()
                try:
                    await view.message.edit(view=view)
                except discord.HTTPException:
                    pass
                break

            view.revealed = new_revealed
            view.recommendation = _best_next_click(new_revealed)
            view._build()
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                break

    @commands.hybrid_command(name="oq", aliases=("sphereq",))
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def sphereq(self, ctx: Context):
        """Show the best next moves for a Mudae OQ sphere game."""
        if not self.bot.user:
            raise commands.BadArgument("Bot not fully loaded, please wait.")

        target_ids = [self.bot.user.id, MudaeID]
        mudae_msg: Optional[discord.Message] = None
        async for message in ctx.channel.history(limit=8):
            if message.author.id not in target_ids or not message.components:
                continue
            if message.author.bot and OQ_TEXT in message.content:
                mudae_msg = message
                break

        if not mudae_msg:
            raise commands.BadArgument(
                "No OQ sphere game found. Run `$oq` first, then try again."
            )

        revealed = self._parse_oq_message(mudae_msg)
        if revealed is None:
            raise commands.BadArgument("Could not parse the OQ sphere grid.")

        await self._show_sphereq(ctx, revealed, mudae_msg)

    async def _show_sphereq(
        self, ctx: Context, revealed: dict[int, str], mudae_msg: discord.Message
    ) -> None:
        recommendations = _oq_best_clicks(revealed)
        view = SphereQView(ctx, revealed, recommendations)
        view.message = await ctx.send(view=view)

        while True:
            try:
                event = await self.bot.wait_for(
                    "raw_message_edit",
                    check=lambda payload: payload.message_id == mudae_msg.id,
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                break

            components = event.data.get("components", [])
            if not components:
                continue
            new_revealed = self._parse_oq_components(
                components  # type: ignore[arg-type]
            )
            if new_revealed is None or new_revealed == view.revealed:
                continue

            view.revealed = new_revealed
            view.recommendations = _oq_best_clicks(new_revealed)
            view._build()
            if view.message is None:
                break
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                break

            targets = sum(color in {"purple", "red"} for color in new_revealed.values())
            clicks_used = sum(color != "red" for color in new_revealed.values())
            if (
                "red" in new_revealed.values()
                or targets >= OQ_TARGET_TOTAL
                or clicks_used >= OQ_MAX_CLICKS
            ):
                break

    @commands.command(name="simoc")
    async def simoc(self, ctx: Context):
        """Generate a random sphere chest for testing"""
        import random

        candidates = [i for i in range(GRID_SIZE * GRID_SIZE) if i != CENTER]
        red_pos = random.choice(candidates)
        layout: dict[int, str] = {red_pos: "red"}

        orange_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout and _adjacent(i, red_pos)
        ]
        for pos in random.sample(orange_candidates, min(2, len(orange_candidates))):
            layout[pos] = "orange"

        yellow_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout and _same_diag(i, red_pos) and not _adjacent(i, red_pos)
        ]
        for pos in random.sample(yellow_candidates, min(3, len(yellow_candidates))):
            layout[pos] = "yellow"

        green_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout
            and (_same_row(i, red_pos) or _same_col(i, red_pos))
            and not _same_diag(i, red_pos)
        ]
        for pos in random.sample(green_candidates, min(4, len(green_candidates))):
            layout[pos] = "green"

        for i in range(GRID_SIZE * GRID_SIZE):
            if i not in layout:
                if (
                    _same_row(i, red_pos)
                    or _same_col(i, red_pos)
                    or _same_diag(i, red_pos)
                ):
                    layout[i] = "teal"
                else:
                    layout[i] = "blue"

        view = SimSphereView(ctx, layout)
        await ctx.send(OC_TEXT + "\nRun `fish sphere` for help.", view=view)


class SimSphereView(discord.ui.View):
    def __init__(self, ctx: Context, layout: dict[int, str]):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.layout = layout
        self.revealed: dict[int, str] = {}
        self.clicks = 0
        self._emoji_map = {
            "red": sp,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        self._hidden = spU
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for idx in range(GRID_SIZE * GRID_SIZE):
            row = idx // GRID_SIZE
            if idx in self.revealed:
                color = self.revealed[idx]
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    emoji=self._emoji_map[color],
                    disabled=True,
                    row=row,
                )
            else:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    emoji=self._hidden,
                    disabled=self.clicks >= 5,
                    row=row,
                )
                btn.callback = self._make_callback(idx)
            self.add_item(btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.defer()
                return
            self.revealed[idx] = self.layout[idx]
            self.clicks += 1
            self._build()
            await interaction.response.edit_message(view=self)

        return callback

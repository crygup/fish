from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_or_fetch_user

if TYPE_CHECKING:
    from extensions.context import Context


CUSTOM_EMOJI_RE = re.compile(r"^<a?:([A-Za-z0-9_~]+):(\d+)>$")


def _safe_name(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or "Unknown"))
    )


def _emoji_text(row: Any, bot: Any) -> str:
    name = str(row["emoji_name"] or "")
    if bool(row["unicode"]):
        return name
    emoji_id = row["emoji_id"]
    if emoji_id is None:
        return f":{_safe_name(name)}:"
    custom = bot.get_emoji(int(emoji_id))
    if custom is not None:
        return str(custom)
    return f"<:{name}:{int(emoji_id)}>"


class ReactionStatsView(discord.ui.LayoutView):
    def __init__(
        self, ctx: Context, title: str, sections: list[tuple[str, str]]
    ) -> None:
        super().__init__(timeout=300)
        children: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(title)]
        for heading, content in sections:
            children.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"### {heading}\n{content}"),
                )
            )
        self.add_item(discord.ui.Container(*children, accent_color=ctx.bot.embedcolor))


class ReactionStats(Cog):
    """Opt-in reaction history and Components V2 reaction statistics."""

    async def _resolve_emoji_filter(
        self, ctx: Context, value: str
    ) -> tuple[str, int | None, bool] | None:
        match = CUSTOM_EMOJI_RE.fullmatch(value)
        if match:
            return match.group(1), int(match.group(2)), False

        if emoji_lib.is_emoji(value):
            return value, None, True

        if ctx.guild is not None:
            for candidate in ctx.guild.emojis:
                if candidate.name and candidate.name.casefold() == value.casefold():
                    return candidate.name, candidate.id, False
        for candidate in self.bot.emojis:
            if candidate.name and candidate.name.casefold() == value.casefold():
                return candidate.name, candidate.id, False
        return None

    async def _resolve_user(
        self, ctx: Context, value: str
    ) -> discord.User | discord.Member:
        if ctx.guild is not None:
            try:
                return await commands.MemberConverter().convert(ctx, value)
            except commands.CommandError:
                pass
        try:
            return await commands.UserConverter().convert(ctx, value)
        except commands.CommandError as exc:
            raise commands.BadArgument(
                f"I could not find a user for `{value}`."
            ) from exc

    async def _resolve_guild(self, ctx: Context, value: str) -> discord.Guild | None:
        try:
            return await commands.GuildConverter().convert(ctx, value)
        except commands.CommandError:
            return None

    async def _parse_reaction_arguments(
        self, ctx: Context, arguments: str | None
    ) -> tuple[
        bool,
        discord.User | discord.Member | None,
        discord.Guild | None,
        tuple[str, int | None, bool] | None,
    ]:
        try:
            tokens = shlex.split(arguments or "")
        except ValueError as exc:
            raise commands.BadArgument(
                "I could not parse those reaction arguments."
            ) from exc

        global_scope = False
        user: discord.User | discord.Member | None = None
        guild: discord.Guild | None = None
        emoji_filter: tuple[str, int | None, bool] | None = None
        unknown: list[str] = []

        for token in tokens:
            if token.casefold() == "global":
                global_scope = True
                continue
            parsed_emoji = await self._resolve_emoji_filter(ctx, token)
            if parsed_emoji is not None:
                if emoji_filter is not None:
                    raise commands.BadArgument("Please provide only one emoji.")
                emoji_filter = parsed_emoji
                continue

            mention_or_id = bool(re.fullmatch(r"<@!?\d+>", token)) or token.isdigit()
            if mention_or_id and user is None:
                # Prefer a known guild for numeric IDs, otherwise let the user
                # converter handle it. Mentions are always users.
                if not token.startswith("<@"):
                    guild_candidate = await self._resolve_guild(ctx, token)
                    if guild_candidate is not None:
                        guild = guild_candidate
                        continue
                user = await self._resolve_user(ctx, token)
                continue

            guild_candidate = await self._resolve_guild(ctx, token)
            if guild_candidate is not None:
                if guild is not None:
                    raise commands.BadArgument("Please provide only one server.")
                guild = guild_candidate
                continue

            if user is None:
                user = await self._resolve_user(ctx, token)
                continue
            unknown.append(token)

        if unknown:
            raise commands.BadArgument(f"I did not understand `{unknown[0]}`.")
        if not global_scope and guild is None:
            guild = ctx.guild
        if not global_scope and guild is None:
            raise commands.BadArgument("Choose a server or use `global` in DMs.")
        return global_scope, user, guild, emoji_filter

    @staticmethod
    def _scope_where(
        *,
        global_scope: bool,
        guild: discord.Guild | None,
        emoji_filter: tuple[str, int | None, bool] | None = None,
        start_index: int = 1,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        values: list[object] = []
        if not global_scope and guild is not None:
            values.append(guild.id)
            clauses.append(f"guild_id = ${start_index + len(values) - 1}")
        if emoji_filter is not None:
            name, emoji_id, is_unicode = emoji_filter
            values.append(name)
            name_index = start_index + len(values) - 1
            values.append(is_unicode)
            unicode_index = start_index + len(values) - 1
            if emoji_id is None:
                clauses.append(
                    f"emoji_name = ${name_index} AND unicode = ${unicode_index}"
                )
            else:
                values.append(emoji_id)
                id_index = start_index + len(values) - 1
                clauses.append(
                    f"emoji_name = ${name_index} AND emoji_id = ${id_index} "
                    f"AND unicode = ${unicode_index}"
                )
        return (" AND ".join(clauses) or "TRUE"), values

    async def _user_lines(self, rows: list[Any], id_key: str, count_key: str) -> str:
        lines: list[str] = []
        for row in rows:
            user = await get_or_fetch_user(self.bot, int(row[id_key]))
            name = _safe_name(getattr(user, "name", None) or row[id_key])
            lines.append(f"**{name}** · {int(row[count_key]):,}")
        return "\n".join(lines) or "None"

    def _emoji_filter_text(self, emoji_filter: tuple[str, int | None, bool]) -> str:
        name, emoji_id, is_unicode = emoji_filter
        return _emoji_text(
            {
                "emoji_name": name,
                "emoji_id": emoji_id,
                "unicode": is_unicode,
            },
            self.bot,
        )

    async def _emoji_lines(self, rows: list[Any]) -> str:
        return (
            "\n".join(
                f"{_emoji_text(row, self.bot)} · {int(row['total']):,}" for row in rows
            )
            or "None"
        )

    async def _send_reaction_stats(
        self,
        ctx: Context,
        *,
        global_scope: bool,
        user: discord.User | discord.Member | None,
        guild: discord.Guild | None,
        emoji_filter: tuple[str, int | None, bool] | None,
    ) -> None:
        scope, scope_values = self._scope_where(
            global_scope=global_scope,
            guild=guild,
            emoji_filter=emoji_filter,
        )
        sections: list[tuple[str, str]] = []
        scope_label = "Global" if global_scope else (guild.name if guild else "Server")
        title_label = (
            _safe_name(getattr(user, "name", user.id))
            if user is not None
            else scope_label
        )
        emoji_prefix = (
            f"{self._emoji_filter_text(emoji_filter)} "
            if emoji_filter is not None
            else ""
        )

        if user is not None:
            user_index = len(scope_values) + 1
            given_scope = f"{scope} AND giver_id = ${user_index}"
            received_scope = f"{scope} AND receiver_id = ${user_index}"
            given_args = [*scope_values, user.id]
            received_args = [*scope_values, user.id]
            if emoji_filter is None:
                given_emojis = await self.bot.pool.fetch(
                    "SELECT emoji_name, emoji_id, unicode, COUNT(*) AS total "
                    f"FROM reaction_logs WHERE {given_scope} "
                    "GROUP BY emoji_name, emoji_id, unicode ORDER BY total DESC LIMIT 3",
                    *given_args,
                )
                received_emojis = await self.bot.pool.fetch(
                    "SELECT emoji_name, emoji_id, unicode, COUNT(*) AS total "
                    f"FROM reaction_logs WHERE {received_scope} "
                    "GROUP BY emoji_name, emoji_id, unicode ORDER BY total DESC LIMIT 3",
                    *received_args,
                )
            given_users = await self.bot.pool.fetch(
                "SELECT receiver_id, COUNT(*) AS total FROM reaction_logs "
                f"WHERE {given_scope} GROUP BY receiver_id ORDER BY total DESC LIMIT 3",
                *given_args,
            )
            received_users = await self.bot.pool.fetch(
                "SELECT giver_id, COUNT(*) AS total FROM reaction_logs "
                f"WHERE {received_scope} GROUP BY giver_id ORDER BY total DESC LIMIT 3",
                *received_args,
            )
            if emoji_filter is not None:
                sections.extend(
                    (
                        (
                            "Givers",
                            await self._user_lines(received_users, "giver_id", "total"),
                        ),
                        (
                            "Receivers",
                            await self._user_lines(given_users, "receiver_id", "total"),
                        ),
                    )
                )
            else:
                sections.extend(
                    (
                        (
                            "Top received reactions",
                            await self._emoji_lines(received_emojis),
                        ),
                        ("Top given reactions", await self._emoji_lines(given_emojis)),
                        (
                            "Users they reacted to",
                            await self._user_lines(given_users, "receiver_id", "total"),
                        ),
                        (
                            "Users who reacted to them",
                            await self._user_lines(received_users, "giver_id", "total"),
                        ),
                    )
                )
            title = f"## {emoji_prefix}Reaction stats · {title_label}"
        else:
            if emoji_filter is not None:
                givers = await self.bot.pool.fetch(
                    "SELECT giver_id, COUNT(*) AS total FROM reaction_logs "
                    f"WHERE {scope} GROUP BY giver_id ORDER BY total DESC LIMIT 3",
                    *scope_values,
                )
                receivers = await self.bot.pool.fetch(
                    "SELECT receiver_id, COUNT(*) AS total FROM reaction_logs "
                    f"WHERE {scope} GROUP BY receiver_id ORDER BY total DESC LIMIT 3",
                    *scope_values,
                )
                sections.extend(
                    (
                        ("Givers", await self._user_lines(givers, "giver_id", "total")),
                        (
                            "Receivers",
                            await self._user_lines(receivers, "receiver_id", "total"),
                        ),
                    )
                )
            else:
                emoji_rows = await self.bot.pool.fetch(
                    "SELECT emoji_name, emoji_id, unicode, COUNT(*) AS total "
                    f"FROM reaction_logs WHERE {scope} "
                    "GROUP BY emoji_name, emoji_id, unicode ORDER BY total DESC LIMIT 3",
                    *scope_values,
                )
                givers = await self.bot.pool.fetch(
                    "SELECT giver_id, COUNT(*) AS total FROM reaction_logs "
                    f"WHERE {scope} GROUP BY giver_id ORDER BY total DESC LIMIT 3",
                    *scope_values,
                )
                receivers = await self.bot.pool.fetch(
                    "SELECT receiver_id, COUNT(*) AS total FROM reaction_logs "
                    f"WHERE {scope} GROUP BY receiver_id ORDER BY total DESC LIMIT 3",
                    *scope_values,
                )
                sections.extend(
                    (
                        ("Top emojis", await self._emoji_lines(emoji_rows)),
                        (
                            "Top givers",
                            await self._user_lines(givers, "giver_id", "total"),
                        ),
                        (
                            "Top receivers",
                            await self._user_lines(receivers, "receiver_id", "total"),
                        ),
                    )
                )
            title = f"## {emoji_prefix}Reaction stats · {title_label}"

        if not any(content != "None" for _, content in sections):
            sections = [
                ("No data", "No opted-in reaction data was found for those filters.")
            ]
        else:
            sections = [
                (heading, content) for heading, content in sections if content != "None"
            ]
        await ctx.send(
            view=ReactionStatsView(ctx, title, sections),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="reactions",
        aliases=("reaction",),
        extras={"usage": "[global] [user] [server] [emoji]"},
    )
    @app_commands.describe(
        arguments=(
            "Optional global, user, server, and emoji filters in any order. "
            "Global overrides a server filter."
        )
    )
    async def reactions(self, ctx: Context, *, arguments: str | None = None) -> None:
        """Shows reactions for a server and/or a user/emoji globally."""
        async with ctx.typing():
            global_scope, user, guild, emoji_filter = (
                await self._parse_reaction_arguments(ctx, arguments)
            )
            await self._send_reaction_stats(
                ctx,
                global_scope=global_scope,
                user=user,
                guild=guild,
                emoji_filter=emoji_filter,
            )

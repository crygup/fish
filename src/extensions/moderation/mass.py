from __future__ import annotations

import asyncio
import datetime
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, cast

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from core.views import AuthorView
from utils import time as time_utils

if TYPE_CHECKING:
    from extensions.context import GuildContext


MassTarget = discord.Member | discord.User

MASS_ACTION_PERMISSIONS: dict[str, str] = {
    "ban": "ban_members",
    "unban": "ban_members",
    "nick": "manage_nicknames",
    "unnick": "manage_nicknames",
    "kick": "kick_members",
}

MASS_USAGE = (
    "[-bot/-bots] [-created <duration>] [-joined <duration>] "
    "[-nopfp/-no_pfp] "
    "[-username_contains <text>] [-display_contains <text>] "
    "[-nick_contains <text>] [-count <int>] [-role <role>] [-noroles] "
    "[-timedout] [-online] [-offline] [-dnd] [-idle] "
    "[-before <id>] [-after <id>] [-users <mentions/ids...>]"
)

MASS_FLAGS_HELP = """
        -# -bot/-bots        Only match bot accounts.
        -# -created          Only match accounts created within a duration, such as 7d.
        -# -joined           Only match members who joined within a duration, such as 7d.
        -# -nopfp/-no_pfp    Only match accounts without a profile picture.
        -# -username_contains Match part of a username.
        -# -display_contains  Match part of a display name.
        -# -nick_contains     Match part of a server nickname.
        -# -count             Stop after this many matches.
        -# -role              Only match members with this role.
        -# -noroles           Only match members with no extra roles.
        -# -timedout          Only match members who are timed out.
        -# -online/-offline   Match the corresponding presence status.
        -# -dnd/-idle         Match the corresponding presence status.
        -# -before/-after     Match IDs before or after this Discord snowflake.
        -# -users              Only match the listed mentions or user IDs.
        """


class MassFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    """Filters shared by the mass moderation commands."""

    bot: bool = commands.flag(
        default=False, description="Only match bot accounts.", aliases=["bots"]
    )
    created: str | None = commands.flag(
        default=None,
        description="Only match accounts created within this duration, such as 7d.",
    )
    joined: str | None = commands.flag(
        default=None,
        description="Only match members who joined within this duration, such as 7d.",
    )
    nopfp: bool = commands.flag(
        default=False,
        description="Only match accounts without a profile picture.",
        aliases=["no_pfp"],
    )
    username_contains: str | None = commands.flag(
        default=None, description="Match part of a username."
    )
    display_contains: str | None = commands.flag(
        default=None, description="Match part of a display name."
    )
    nick_contains: str | None = commands.flag(
        default=None, description="Match part of a server nickname."
    )
    count: int | None = commands.flag(
        default=None, description="Maximum number of matching users to change."
    )
    role: discord.Role | None = commands.flag(
        default=None, description="Only match members who have this role."
    )
    noroles: bool = commands.flag(
        default=False, description="Only match members with no extra roles."
    )
    timedout: bool = commands.flag(
        default=False, description="Only match members who are timed out."
    )
    online: bool = commands.flag(
        default=False, description="Only match members whose status is online."
    )
    offline: bool = commands.flag(
        default=False, description="Only match members whose status is offline."
    )
    dnd: bool = commands.flag(
        default=False, description="Only match members whose status is do not disturb."
    )
    idle: bool = commands.flag(
        default=False, description="Only match members whose status is idle."
    )
    before: int | None = commands.flag(
        default=None, description="Only match IDs before this Discord snowflake."
    )
    after: int | None = commands.flag(
        default=None, description="Only match IDs after this Discord snowflake."
    )
    users: str | None = commands.flag(
        default=None,
        description="Only match these users. Separate mentions or IDs with spaces.",
    )

    @classmethod
    def parse_flags(cls, argument: str, *, ignore_extra: bool = True):
        # FlagConverter normally expects a value after boolean flags. Move bare
        # booleans to the end so `-bot -online` works naturally in text commands.
        boolean_flags = "bot|bots|nopfp|no_pfp|noroles|timedout|online|offline|dnd|idle"
        bare_boolean = (
            rf"(?<!\S)--?(?P<flag>{boolean_flags})" rf"(?=\s*(?:--?\w+(?=\s|$)|$))"
        )
        bare_flags = re.findall(bare_boolean, argument, flags=re.IGNORECASE)
        if bare_flags:
            argument = re.sub(bare_boolean, "", argument, flags=re.IGNORECASE).strip()
            argument = " ".join(
                part
                for part in (argument, *(f"-{flag} true" for flag in bare_flags))
                if part
            )

        parsed = super().parse_flags(argument, ignore_extra=ignore_extra)
        return {
            name: [value.strip() for value in values] for name, values in parsed.items()
        }


@dataclass(slots=True)
class MassChange:
    target: MassTarget
    previous_nick: str | None = None


def _mass_reason(ctx: GuildContext, action: str, *, undo: bool = False) -> str:
    suffix = " undo" if undo else ""
    return (
        f"Mass {action}{suffix} automated by {ctx.author} " f"(ID: {ctx.author.id})"
    )[:512]


class MassOperationView(AuthorView):
    """Controls for a running mass action and its reversible changes."""

    def __init__(
        self,
        cog: Mass,
        ctx: GuildContext,
        action: str,
        changes: list[MassChange],
    ) -> None:
        super().__init__(ctx=ctx, timeout=600)
        self.cog = cog
        self.action = action
        self.changes = changes
        self.running = True
        self.cancel_requested = asyncio.Event()
        self.undo_lock = asyncio.Lock()
        self.message: discord.Message | None = None
        if action == "kick":
            self.undo_button.disabled = True

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not self.running:
            await interaction.response.send_message(
                "This mass action has already finished.", ephemeral=True
            )
            return
        self.cancel_requested.set()
        self.cancel_button.disabled = True
        await interaction.response.edit_message(view=self)

    async def undo(self, interaction: discord.Interaction) -> None:
        if self.running:
            await interaction.response.send_message(
                "Please wait until the mass action finishes before undoing it.",
                ephemeral=True,
            )
            return
        if not self.changes:
            await interaction.response.send_message(
                "There are no completed changes to undo.", ephemeral=True
            )
            return

        async with self.undo_lock:
            if self.undo_button.disabled:
                await interaction.response.send_message(
                    "This action has already been undone.", ephemeral=True
                )
                return
            self.undo_button.disabled = True
            await interaction.response.edit_message(view=self)
            succeeded, failed = await self.cog._undo_mass_changes(
                cast("GuildContext", self.ctx), self.action, self.changes
            )
            if self.message:
                await self.message.edit(
                    content=(
                        f"Mass {self.action} undo complete: **{succeeded}** restored"
                        + (f", **{failed}** failed." if failed else ".")
                    ),
                    view=self,
                )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.cancel(interaction)

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary)
    async def undo_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.undo(interaction)

    async def on_timeout(self) -> None:
        if self.running:
            self.cancel_requested.set()
        self.cancel_button.disabled = True
        self.undo_button.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Mass(Cog):
    """Bulk moderation commands with a confirmation and reversible operation view."""

    @staticmethod
    def _ensure_mass_permissions(ctx: GuildContext, action: str) -> None:
        """Require Manage Server and the permission for the selected action.

        The decorators protect normal text and application invocations. This
        runtime check is kept as a second layer because mass operations are
        destructive and can also be reached through shared command helpers.
        """
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage()

        required = MASS_ACTION_PERMISSIONS[action]
        author_permissions = getattr(ctx.author, "guild_permissions", None)
        missing = [
            permission
            for permission in ("manage_guild", required)
            if author_permissions is None or not getattr(author_permissions, permission)
        ]
        if missing:
            raise commands.MissingPermissions(missing)

        me = guild.me
        bot_permissions = me.guild_permissions if me is not None else None
        if bot_permissions is None or not getattr(bot_permissions, required):
            raise commands.BotMissingPermissions([required])

    @staticmethod
    def _member_is_manageable(ctx: GuildContext, member: discord.Member) -> bool:
        """Return whether both the invoker and bot may act on ``member``."""
        guild = ctx.guild
        me = guild.me
        if me is None:
            return False

        actor = (
            ctx.author
            if isinstance(ctx.author, discord.Member)
            else guild.get_member(ctx.author.id)
        )
        if actor is None:
            return False
        if member.id in {actor.id, me.id, guild.owner_id}:
            return False
        if member.top_role >= me.top_role:
            return False
        if actor.id != guild.owner_id and member.top_role >= actor.top_role:
            return False
        return True

    async def _parse_age_limit(self, value: str | None) -> float | None:
        if not value:
            return None
        now = discord.utils.utcnow()
        try:
            target = time_utils.ShortTime(value, now=now).dt
        except (commands.BadArgument, ValueError, TypeError) as error:
            raise commands.BadArgument(
                f"Invalid duration `{value}`. Try values such as `30m`, `7d`, or `2w`."
            ) from error
        seconds = (target - now).total_seconds()
        if seconds <= 0:
            raise commands.BadArgument("Mass action durations must be in the future.")
        return seconds

    @staticmethod
    def _has_profile_picture(target: MassTarget) -> bool:
        return bool(
            getattr(target, "avatar", None) is not None
            or getattr(target, "guild_avatar", None) is not None
        )

    async def _validate_flags(
        self,
        ctx: GuildContext,
        flags: MassFlags,
        action: str,
    ) -> tuple[float | None, float | None]:
        if flags.count is not None and flags.count < 1:
            raise commands.BadArgument("`-count` must be at least 1.")
        if flags.role is not None and flags.role.guild.id != ctx.guild.id:
            raise commands.BadArgument("The `-role` must belong to this server.")
        if action == "unban":
            unsupported: list[str] = []
            if flags.joined:
                unsupported.append("joined")
            if flags.display_contains:
                unsupported.append("display_contains")
            if flags.nick_contains:
                unsupported.append("nick_contains")
            if flags.role:
                unsupported.append("role")
            if flags.noroles:
                unsupported.append("noroles")
            if flags.timedout:
                unsupported.append("timedout")
            if flags.online or flags.offline or flags.dnd or flags.idle:
                unsupported.append("status")
            if unsupported:
                names = ", ".join(f"`-{name}`" for name in unsupported)
                raise commands.BadArgument(
                    f"{names} cannot be used with mass unban because banned users "
                    "do not have member data."
                )
        created = await self._parse_age_limit(flags.created)
        joined = await self._parse_age_limit(flags.joined)
        return created, joined

    @staticmethod
    def _status_filters(flags: MassFlags) -> set[discord.Status]:
        statuses: set[discord.Status] = set()
        if flags.online:
            statuses.add(discord.Status.online)
        if flags.offline:
            statuses.add(discord.Status.offline)
        if flags.dnd:
            statuses.add(discord.Status.dnd)
        if flags.idle:
            statuses.add(discord.Status.idle)
        return statuses

    def _matches(
        self,
        target: MassTarget,
        flags: MassFlags,
        *,
        created_age: float | None,
        joined_age: float | None,
        selected_user_ids: set[int] | None,
        now: datetime.datetime,
    ) -> bool:
        if selected_user_ids is not None and target.id not in selected_user_ids:
            return False
        if flags.bot and not target.bot:
            return False
        if flags.nopfp and self._has_profile_picture(target):
            return False
        if (
            flags.username_contains
            and flags.username_contains.casefold() not in target.name.casefold()
        ):
            return False
        if flags.before is not None and target.id >= flags.before:
            return False
        if flags.after is not None and target.id <= flags.after:
            return False

        if created_age is not None:
            created_at = target.created_at
            if (now - created_at).total_seconds() > created_age:
                return False

        if isinstance(target, discord.User):
            return True

        if (
            flags.display_contains
            and flags.display_contains.casefold() not in target.display_name.casefold()
        ):
            return False
        if flags.nick_contains and (
            not target.nick
            or flags.nick_contains.casefold() not in target.nick.casefold()
        ):
            return False
        if flags.role is not None and flags.role not in target.roles:
            return False
        if flags.noroles and len(target.roles) > 1:
            return False
        if flags.timedout and not target.is_timed_out():
            return False
        statuses = self._status_filters(flags)
        if statuses and target.status not in statuses:
            return False
        if joined_age is not None:
            if (
                target.joined_at is None
                or (now - target.joined_at).total_seconds() > joined_age
            ):
                return False
        return True

    async def _resolve_user_filter(
        self, ctx: GuildContext, users: str | None
    ) -> set[int] | None:
        if not users:
            return None
        try:
            values = shlex.split(users)
        except ValueError as error:
            raise commands.BadArgument(
                "The `-users` value has invalid quoting."
            ) from error
        if not values:
            raise commands.BadArgument(
                "Provide at least one mention or ID for `-users`."
            )

        converter = commands.UserConverter()
        selected_ids: set[int] = set()
        for value in values:
            match = re.fullmatch(r"(?:<@!?(\d+)>|(\d+))", value)
            if match:
                selected_ids.add(int(match.group(1) or match.group(2)))
                continue
            if ctx.message is None:
                raise commands.BadArgument(
                    f"`{value}` is not a user mention or Discord user ID."
                )
            try:
                user = await converter.convert(ctx, value)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    f"I could not find a Discord user from `{value}`."
                ) from error
            selected_ids.add(user.id)
        return selected_ids

    async def _find_targets(
        self,
        ctx: GuildContext,
        action: str,
        flags: MassFlags,
    ) -> list[MassTarget]:
        created_age, joined_age = await self._validate_flags(ctx, flags, action)
        now = discord.utils.utcnow()
        selected_user_ids = await self._resolve_user_filter(ctx, flags.users)

        if action == "unban":
            try:
                targets = [entry.user async for entry in ctx.guild.bans(limit=None)]
            except discord.HTTPException as error:
                raise commands.BadArgument(
                    "I could not read this server's ban list."
                ) from error
        else:
            # A member chunk gives the command the same complete list regardless
            # of which members have recently been seen by the bot.
            try:
                targets = [
                    member async for member in ctx.guild.fetch_members(limit=None)
                ]
            except discord.HTTPException:
                targets = list(ctx.guild.members)

        matches = [
            target
            for target in targets
            if self._matches(
                target,
                flags,
                created_age=created_age,
                joined_age=joined_age,
                selected_user_ids=selected_user_ids,
                now=now,
            )
        ]

        if action == "unnick":
            matches = [
                target
                for target in matches
                if isinstance(target, discord.Member) and target.nick
            ]
        elif action == "nick":
            matches = [
                target for target in matches if isinstance(target, discord.Member)
            ]

        if action in {"ban", "kick", "nick", "unnick"}:
            matches = [
                target
                for target in matches
                if isinstance(target, discord.Member)
                and self._member_is_manageable(ctx, target)
            ]

        if flags.count is not None:
            matches = matches[: flags.count]
        return cast(list[MassTarget], matches)

    async def _apply_mass_change(
        self,
        ctx: GuildContext,
        action: str,
        target: MassTarget,
        nickname: str | None,
    ) -> MassChange:
        reason = _mass_reason(ctx, action)
        previous_nick = target.nick if isinstance(target, discord.Member) else None
        if action == "ban":
            await ctx.guild.ban(target, reason=reason, delete_message_seconds=0)
        elif action == "unban":
            await ctx.guild.unban(target, reason=reason)
        elif action == "kick":
            if not isinstance(target, discord.Member):
                raise commands.BadArgument("Mass kick can only target current members.")
            await ctx.guild.kick(target, reason=reason)
        elif action == "nick":
            if not isinstance(target, discord.Member):
                raise commands.BadArgument("Mass nick can only target current members.")
            await target.edit(nick=nickname, reason=reason)
        elif action == "unnick":
            if not isinstance(target, discord.Member):
                raise commands.BadArgument(
                    "Mass unnick can only target current members."
                )
            await target.edit(nick=None, reason=reason)
        else:
            raise RuntimeError(f"Unknown mass action: {action}")
        return MassChange(target=target, previous_nick=previous_nick)

    async def _undo_mass_changes(
        self,
        ctx: GuildContext,
        action: str,
        changes: Iterable[MassChange],
    ) -> tuple[int, int]:
        if action == "kick":
            return 0, len(list(changes))
        succeeded = failed = 0
        reason = _mass_reason(ctx, action, undo=True)
        for change in reversed(list(changes)):
            try:
                if action == "ban":
                    await ctx.guild.unban(change.target, reason=reason)
                elif action == "unban":
                    await ctx.guild.ban(
                        change.target, reason=reason, delete_message_seconds=0
                    )
                elif action in {"nick", "unnick"} and isinstance(
                    change.target, discord.Member
                ):
                    await change.target.edit(nick=change.previous_nick, reason=reason)
                else:
                    raise RuntimeError("Unsupported undo target")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
            else:
                succeeded += 1
        return succeeded, failed

    async def _run_mass(
        self,
        ctx: GuildContext,
        action: str,
        targets: list[MassTarget],
        nickname: str | None = None,
    ) -> None:
        action_label = {
            "ban": "ban",
            "unban": "unban",
            "nick": "change nicknames for",
            "unnick": "remove nicknames from",
            "kick": "kick",
        }[action]
        view = MassOperationView(self, ctx, action, [])
        view.message = await ctx.send(
            f"Mass {action_label} started for **{len(targets):,}** users. "
            "You can cancel the operation below.",
            view=view,
        )
        succeeded = failed = 0
        for index, target in enumerate(targets, start=1):
            if view.cancel_requested.is_set():
                break
            # Re-check hierarchy immediately before each change. Roles can
            # change after the preview, so the initial target list is not a
            # sufficient safety boundary for a destructive operation.
            if isinstance(target, discord.Member) and not self._member_is_manageable(
                ctx, target
            ):
                failed += 1
                continue
            try:
                change = await self._apply_mass_change(ctx, action, target, nickname)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
            else:
                view.changes.append(change)
                succeeded += 1

            if index == 1 or index % 5 == 0 or index == len(targets):
                try:
                    await view.message.edit(
                        content=(
                            f"Mass {action_label} in progress: **{index:,}/{len(targets):,}** "
                            f"processed • **{succeeded:,}** changed"
                            + (f" • **{failed:,}** failed" if failed else "")
                            + (
                                " • cancellation requested"
                                if view.cancel_requested.is_set()
                                else "."
                            )
                        ),
                        view=view,
                    )
                except discord.HTTPException:
                    pass

        view.running = False
        view.cancel_button.disabled = True
        if action != "kick" and not view.changes:
            view.undo_button.disabled = True
        status = "cancelled" if view.cancel_requested.is_set() else "complete"
        if view.message:
            await view.message.edit(
                content=(
                    f"Mass {action_label} {status}: **{succeeded:,}** changed"
                    + (f", **{failed:,}** failed" if failed else ".")
                ),
                view=view,
            )
        ctx.bot.dispatch(
            "logger_fishie_moderation",
            ctx.guild,
            f"Mass {action} by Fishie",
            (
                f"Fishie mass {action} finished with {succeeded:,} changed and "
                f"{failed:,} failed."
            ),
            None,
            ctx.author,
            f"Fishie mass {action} command",
        )

    async def _mass_command(
        self,
        ctx: GuildContext,
        action: str,
        flags: MassFlags,
        nickname: str | None = None,
    ) -> None:
        self._ensure_mass_permissions(ctx, action)
        if action == "nick" and not nickname:
            raise commands.BadArgument("Mass nick needs the nickname to apply.")
        if nickname is not None and len(nickname) > 32:
            raise commands.BadArgument("Nicknames cannot be longer than 32 characters.")

        async with ctx.typing():
            targets = await self._find_targets(ctx, action, flags)
        if not targets:
            await ctx.send("No matching users could be acted on.")
            return

        preview = (
            f"This will {action} **{len(targets):,}** matching user"
            f"{'s' if len(targets) != 1 else ''}"
            + (
                " with nickname **"
                + discord.utils.escape_mentions(discord.utils.escape_markdown(nickname))
                + "**"
                if nickname
                else ""
            )
            + ". Continue?"
        )
        confirmed = await ctx.prompt(preview, confirm_label="Yes", cancel_label="No")
        if not confirmed:
            await ctx.send("Mass action cancelled.")
            return
        await self._run_mass(ctx, action, targets, nickname)

    @commands.hybrid_group(
        name="mass",
        invoke_without_command=True,  # pyright: ignore[reportCallIssue]
        extras={"usage": "<ban|unban|nick|unnick|kick> " + MASS_USAGE},
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mass(self, ctx: GuildContext) -> None:
        """Bulk ban, unban, nickname, unnickname, or kick matching members.

        Use one of the subcommands and its filters to preview matching members
        before confirming the action.
        """
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Use `mass ban`, `mass unban`, `mass nick`, `mass unnick`, or `mass kick` "
                "with filters."
            )

    @mass.command(
        name="ban",
        description="Preview and ban matching members.",
        extras={"usage": MASS_USAGE},
        help="Ban every matching member.\n" + MASS_FLAGS_HELP,
    )
    @commands.has_guild_permissions(manage_guild=True, ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    @commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mass_ban(self, ctx: GuildContext, *, flags: MassFlags) -> None:
        """Ban every matching member after showing a confirmation count."""
        await self._mass_command(ctx, "ban", flags)

    @mass.command(
        name="unban",
        description="Preview and unban matching users.",
        extras={"usage": MASS_USAGE},
        help="Unban every matching user.\n" + MASS_FLAGS_HELP,
    )
    @commands.has_guild_permissions(manage_guild=True, ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    @commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mass_unban(self, ctx: GuildContext, *, flags: MassFlags) -> None:
        """Unban every matching user after showing a confirmation count."""
        await self._mass_command(ctx, "unban", flags)

    @mass.command(
        name="nick",
        description="Preview and set a nickname for matching members.",
        extras={"usage": f"<nickname> {MASS_USAGE}"},
        help="Set the same nickname for every matching member.\n" + MASS_FLAGS_HELP,
    )
    @commands.has_guild_permissions(manage_guild=True, manage_nicknames=True)
    @commands.bot_has_guild_permissions(manage_nicknames=True)
    @commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(nickname="The nickname to apply to every matching member.")
    async def mass_nick(
        self, ctx: GuildContext, nickname: str, *, flags: MassFlags
    ) -> None:
        """Set the same nickname for every matching member."""
        await self._mass_command(ctx, "nick", flags, nickname)

    @mass.command(
        name="unnick",
        description="Preview and remove nicknames from matching members.",
        extras={"usage": MASS_USAGE},
        help="Remove nicknames from every matching member.\n" + MASS_FLAGS_HELP,
    )
    @commands.has_guild_permissions(manage_guild=True, manage_nicknames=True)
    @commands.bot_has_guild_permissions(manage_nicknames=True)
    @commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mass_unnick(self, ctx: GuildContext, *, flags: MassFlags) -> None:
        """Remove nicknames from every matching member."""
        await self._mass_command(ctx, "unnick", flags)

    @mass.command(
        name="kick",
        description="Preview and kick matching members.",
        extras={"usage": MASS_USAGE},
        help="Kick every matching member.\n" + MASS_FLAGS_HELP,
    )
    @commands.has_guild_permissions(manage_guild=True, kick_members=True)
    @commands.bot_has_guild_permissions(kick_members=True)
    @commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True, kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mass_kick(self, ctx: GuildContext, *, flags: MassFlags) -> None:
        """Kick every matching member after showing a confirmation count."""
        await self._mass_command(ctx, "kick", flags)

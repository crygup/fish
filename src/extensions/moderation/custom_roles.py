from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from extensions.context import GuildContext


CUSTOM_ROLE_MODERATION_PERMISSIONS = frozenset(
    {
        "administrator",
        "ban_members",
        "kick_members",
        "moderate_members",
        "manage_channels",
        "manage_guild",
        "manage_messages",
        "manage_roles",
        "manage_webhooks",
        "manage_nicknames",
        "manage_threads",
        "manage_events",
        "mention_everyone",
        "view_audit_log",
        "move_members",
        "mute_members",
        "deafen_members",
    }
)
CUSTOM_EMOJI_RE = re.compile(
    r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{1,32}):(?P<id>[0-9]{15,20})>$"
)


def _safe_text(value: object, limit: int = 1_000) -> str:
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _role_dangerous_permissions(role: discord.Role) -> list[str]:
    return [
        name.replace("_", " ").title()
        for name, enabled in role.permissions
        if enabled and name in CUSTOM_ROLE_MODERATION_PERMISSIONS
    ]


def _parse_colour(value: str | None) -> discord.Colour:
    if value is None or not value.strip():
        return discord.Colour.default()
    raw = value.strip().casefold()
    if raw in {"default", "none", "clear", "reset"}:
        return discord.Colour.default()
    try:
        if raw.startswith("#") or raw.startswith("0x"):
            return discord.Colour.from_str(raw)
        if re.fullmatch(r"[0-9a-f]{6}", raw):
            return discord.Colour.from_str(f"#{raw}")
        colour_factory = getattr(discord.Colour, raw.replace("-", "_"), None)
        if callable(colour_factory):
            colour = colour_factory()
            if isinstance(colour, discord.Colour):
                return colour
    except (TypeError, ValueError):
        pass
    raise commands.BadArgument(
        "Colour must be a Discord colour name or a hex value such as `#5865F2`."
    )


class CustomRoles(Cog):
    """Manage server roles that can be tied to members and boost status."""

    async def _role_record(self, guild_id: int, role_id: int) -> Any | None:
        return await self.bot.pool.fetchrow(
            """
            SELECT role_id, booster_only, emoji, created_by
            FROM custom_roles
            WHERE guild_id = $1 AND role_id = $2
            """,
            guild_id,
            role_id,
        )

    async def _require_custom_role(self, ctx: GuildContext, role: discord.Role) -> Any:
        if role.guild.id != ctx.guild.id:
            raise commands.BadArgument("That role must belong to this server.")
        record = await self._role_record(ctx.guild.id, role.id)
        if record is None:
            raise commands.BadArgument(
                "That role is not registered as a custom role. Use `custom-role create`."
            )
        return record

    def _ensure_role_manageable(self, ctx: GuildContext, role: discord.Role) -> None:
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            raise commands.BotMissingPermissions(["manage_roles"])
        if role.is_default():
            raise commands.BadArgument("The everyone role cannot be a custom role.")
        if role.managed:
            raise commands.BadArgument(
                "Managed integration roles cannot be custom roles."
            )
        if role >= me.top_role:
            raise commands.BadArgument(
                "I cannot manage a role at or above my highest role."
            )
        if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            raise commands.BadArgument(
                "You cannot manage a role at or above your highest role."
            )

    def _ensure_member_manageable(
        self, ctx: GuildContext, member: discord.Member
    ) -> None:
        guild = ctx.guild
        me = guild.me
        if me is None:
            raise commands.BadArgument("I could not resolve my member in this server.")
        if member.id == guild.owner_id and member.id != ctx.author.id:
            raise commands.BadArgument("The server owner cannot be targeted.")
        if member.id != ctx.author.id and ctx.author.id != guild.owner_id:
            if member.top_role >= ctx.author.top_role:
                raise commands.BadArgument(
                    "You cannot manage someone with a higher or equal role."
                )
        if member.top_role >= me.top_role and member.id != me.id:
            raise commands.BadArgument(
                "I cannot manage someone with a higher or equal role."
            )

    def _ensure_actor_can_grant(self, ctx: GuildContext, role: discord.Role) -> None:
        if (
            ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.administrator
        ):
            return
        missing = [
            name.replace("_", " ").title()
            for name, enabled in role.permissions
            if (
                enabled
                and name in CUSTOM_ROLE_MODERATION_PERMISSIONS
                and not getattr(ctx.author.guild_permissions, name)
            )
        ]
        if missing:
            raise commands.BadArgument(
                "You do not have the permissions granted by this role: "
                + ", ".join(missing)
                + "."
            )

    def _ensure_safe_assignment_target(
        self, ctx: GuildContext, role: discord.Role, member: discord.Member
    ) -> None:
        if _role_dangerous_permissions(role) and member.id != ctx.author.id:
            raise commands.BadArgument(
                "Roles with moderation permissions can only be given to yourself."
            )

    async def _confirm_role_permissions(
        self, ctx: GuildContext, role: discord.Role, *, action: str
    ) -> bool:
        permissions = _role_dangerous_permissions(role)
        if not permissions:
            return True
        message = await ctx.prompt(
            (
                f"The role **{_safe_text(role.name)}** grants moderation permissions: "
                f"{', '.join(permissions)}. Do you want to {action} it?"
            ),
            confirm_label="Yes",
            cancel_label="No",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return message is not None

    async def _resolve_role(self, ctx: GuildContext, value: str) -> discord.Role:
        try:
            return await commands.RoleConverter().convert(ctx, value)
        except commands.BadArgument as error:
            raise commands.BadArgument(
                f"I could not find the role `{value}`."
            ) from error

    async def _resolve_clear_target(
        self, ctx: GuildContext, value: str
    ) -> discord.Role | discord.Member | discord.User:
        try:
            return await self._resolve_role(ctx, value)
        except commands.BadArgument:
            pass
        try:
            return await commands.MemberConverter().convert(ctx, value)
        except commands.BadArgument:
            try:
                return await commands.UserConverter().convert(ctx, value)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    f"I could not find a role or user matching `{value}`."
                ) from error

    async def _role_icon(
        self, ctx: GuildContext, value: str | None
    ) -> tuple[str | bytes | None, str | None]:
        if value is None or not value.strip():
            return None, None
        raw = value.strip()
        if raw.casefold() in {"none", "clear", "reset"}:
            return None, None
        match = CUSTOM_EMOJI_RE.fullmatch(raw)
        if match:
            emoji = discord.PartialEmoji(
                name=match.group("name"),
                id=int(match.group("id")),
                animated=bool(match.group("animated")),
            )
            try:
                async with self.bot.session.get(str(emoji.url)) as response:
                    if response.status != 200:
                        raise commands.BadArgument(
                            "I could not download that custom emoji."
                        )
                    data = await response.read()
            except commands.BadArgument:
                raise
            except Exception as error:
                raise commands.BadArgument(
                    "I could not download that custom emoji."
                ) from error
            if len(data) > 256 * 1024:
                raise commands.BadArgument("That emoji is too large for a role icon.")
            return data, raw
        if len(raw) > 100:
            raise commands.BadArgument("That emoji is too long for a role icon.")
        return raw, raw

    async def _store_assignment(
        self, ctx: GuildContext, role: discord.Role, member_id: int
    ) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO custom_role_assignments
                (guild_id, role_id, user_id, assigned_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, role_id, user_id)
            DO UPDATE SET assigned_by = EXCLUDED.assigned_by,
                          assigned_at = now()
            """,
            ctx.guild.id,
            role.id,
            member_id,
            ctx.author.id,
        )

    async def _assign_role(
        self,
        ctx: GuildContext,
        role: discord.Role,
        member: discord.Member,
        record: Any,
    ) -> bool:
        self._ensure_role_manageable(ctx, role)
        self._ensure_member_manageable(ctx, member)
        self._ensure_actor_can_grant(ctx, role)
        await self._store_assignment(ctx, role, member.id)
        if record["booster_only"] and member.premium_since is None:
            if role in member.roles:
                await member.remove_roles(
                    role,
                    reason=f"Custom role boost requirement managed by {ctx.author} ({ctx.author.id})",
                )
            return False
        if role not in member.roles:
            await member.add_roles(
                role,
                reason=f"Custom role assigned by {ctx.author} ({ctx.author.id})",
            )
        return True

    async def _sync_role_assignments(
        self, guild: discord.Guild, role: discord.Role, booster_only: bool
    ) -> None:
        me = guild.me
        if me is None or role.managed or role >= me.top_role:
            return
        if _role_dangerous_permissions(role):
            return
        rows = await self.bot.pool.fetch(
            "SELECT user_id FROM custom_role_assignments "
            "WHERE guild_id = $1 AND role_id = $2",
            guild.id,
            role.id,
        )
        for row in rows:
            member = guild.get_member(int(row["user_id"]))
            if member is None:
                continue
            should_have = not booster_only or member.premium_since is not None
            try:
                if should_have and role not in member.roles:
                    await member.add_roles(role, reason="Custom role boost status sync")
                elif not should_have and role in member.roles:
                    await member.remove_roles(
                        role, reason="Custom role boost status sync"
                    )
            except (discord.Forbidden, discord.HTTPException):
                continue

    @commands.hybrid_group(
        name="custom-role",
        aliases=(
            "boosterole",
            "booster-role",
            "customrole",
            "user-role",
            "userrole",
        ),
        fallback="help",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def custom_role(self, ctx: GuildContext) -> None:
        """Create and manage persistent member and booster roles."""
        await ctx.send(
            "Use `custom-role create`, `assign`, `rename`, `booster`, `emoji`, "
            "`color`, `unassign`, `delete`, or `clear`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="create")
    @app_commands.describe(
        name="Name for the custom role.",
        role="Existing role to register instead of creating one.",
        color="Role colour such as #5865F2 or blue.",
        booster="Only assign this role while the member is boosting.",
        user="Member to assign and tie to this role.",
    )
    async def custom_role_create(
        self,
        ctx: GuildContext,
        name: str,
        role: discord.Role | None = None,
        color: str | None = None,
        booster: bool = False,
        user: discord.Member | None = None,
    ) -> None:
        """Create or register a custom role and optionally assign it."""
        name = name.strip()
        if not name or len(name) > 100:
            raise commands.BadArgument(
                "Role names must be between 1 and 100 characters."
            )
        colour = _parse_colour(color)
        if role is None:
            role = await ctx.guild.create_role(
                name=name,
                colour=colour,
                reason=f"Custom role created by {ctx.author} ({ctx.author.id})",
            )
        else:
            self._ensure_role_manageable(ctx, role)
            if await self._role_record(ctx.guild.id, role.id) is not None:
                raise commands.BadArgument("That role is already a custom role.")
            if role.name != name or color is not None:
                await role.edit(
                    name=name,
                    colour=colour,
                    reason=f"Custom role configured by {ctx.author} ({ctx.author.id})",
                )
        if user is not None:
            self._ensure_role_manageable(ctx, role)
            self._ensure_member_manageable(ctx, user)
            self._ensure_actor_can_grant(ctx, role)
            self._ensure_safe_assignment_target(ctx, role, user)
        await self.bot.pool.execute(
            """
            INSERT INTO custom_roles (guild_id, role_id, created_by, booster_only)
            VALUES ($1, $2, $3, $4)
            """,
            ctx.guild.id,
            role.id,
            ctx.author.id,
            booster,
        )
        if user is not None:
            if not await self._confirm_role_permissions(
                ctx, role, action=f"assign it to {_safe_text(user)}"
            ):
                result = " (assignment cancelled)"
            else:
                assigned = await self._assign_role(
                    ctx, role, user, {"booster_only": booster}
                )
                result = (
                    f" and assigned to **{_safe_text(user)}**"
                    if assigned
                    else " and saved for the member, but not assigned because they are not boosting"
                )
        else:
            result = ""
        await ctx.send(
            f"Created custom role **{_safe_text(role.name)}**{result}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="assign", aliases=("give",))
    @app_commands.describe(
        role="Registered custom role to assign.",
        user="Member to assign the role to.",
    )
    async def custom_role_assign(
        self, ctx: GuildContext, role: discord.Role, user: discord.Member
    ) -> None:
        """Assign a custom role to a member."""
        record = await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        self._ensure_member_manageable(ctx, user)
        self._ensure_actor_can_grant(ctx, role)
        self._ensure_safe_assignment_target(ctx, role, user)
        if not await self._confirm_role_permissions(
            ctx, role, action=f"assign it to {_safe_text(user)}"
        ):
            return
        assigned = await self._assign_role(ctx, role, user, record)
        message = (
            f"Assigned **{_safe_text(role.name)}** to **{_safe_text(user)}**."
            if assigned
            else f"Saved the assignment, but **{_safe_text(user)}** is not currently boosting."
        )
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    @custom_role.command(name="rename")
    @app_commands.describe(role="Registered custom role.", new_name="New role name.")
    async def custom_role_rename(
        self, ctx: GuildContext, role: discord.Role, new_name: str
    ) -> None:
        """Rename a custom role."""
        await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        new_name = new_name.strip()
        if not new_name or len(new_name) > 100:
            raise commands.BadArgument(
                "Role names must be between 1 and 100 characters."
            )
        await role.edit(
            name=new_name,
            reason=f"Custom role renamed by {ctx.author} ({ctx.author.id})",
        )
        await ctx.send(
            f"Renamed the custom role to **{_safe_text(new_name)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="booster")
    @app_commands.describe(role="Registered custom role to toggle.")
    async def custom_role_booster(self, ctx: GuildContext, role: discord.Role) -> None:
        """Toggle whether a custom role follows a member's boost status."""
        record = await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        booster_only = not bool(record["booster_only"])
        await self.bot.pool.execute(
            "UPDATE custom_roles SET booster_only = $1 WHERE guild_id = $2 AND role_id = $3",
            booster_only,
            ctx.guild.id,
            role.id,
        )
        await self._sync_role_assignments(ctx.guild, role, booster_only)
        status = (
            "now only assigned while members boost"
            if booster_only
            else "no longer tied to boost status"
        )
        await ctx.send(
            f"**{_safe_text(role.name)}** is {status}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="emoji")
    @app_commands.describe(
        role="Registered custom role.",
        emoji="Unicode or custom emoji. Leave blank to clear the role icon.",
    )
    async def custom_role_emoji(
        self, ctx: GuildContext, role: discord.Role, emoji: str | None = None
    ) -> None:
        """Set or clear a custom role icon."""
        await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        icon, stored = await self._role_icon(ctx, emoji)
        try:
            await role.edit(
                display_icon=icon,
                reason=f"Custom role icon changed by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as error:
            raise commands.BadArgument(
                "Discord could not set that role icon. The server may not support role icons."
            ) from error
        await self.bot.pool.execute(
            "UPDATE custom_roles SET emoji = $1 WHERE guild_id = $2 AND role_id = $3",
            stored,
            ctx.guild.id,
            role.id,
        )
        await ctx.send(
            (
                "Cleared the custom role icon."
                if stored is None
                else f"Set the custom role icon to {stored}."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="color", aliases=("colour",))
    @app_commands.describe(
        role="Registered custom role.",
        colour="Colour name or hex value. Leave blank to clear the colour.",
    )
    async def custom_role_color(
        self, ctx: GuildContext, role: discord.Role, colour: str | None = None
    ) -> None:
        """Set or clear a custom role colour."""
        await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        value = _parse_colour(colour)
        await role.edit(
            colour=value,
            reason=f"Custom role colour changed by {ctx.author} ({ctx.author.id})",
        )
        await ctx.send(
            (
                "Cleared the custom role colour."
                if colour is None
                or colour.strip().casefold() in {"none", "clear", "reset", "default"}
                else f"Updated the custom role colour to `{colour.strip()}`."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="unassign", aliases=("remove",))
    @app_commands.describe(
        role="Registered custom role.",
        user="Member to remove the role from.",
    )
    async def custom_role_unassign(
        self, ctx: GuildContext, role: discord.Role, user: discord.Member
    ) -> None:
        """Remove a custom role assignment from a member."""
        await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        self._ensure_member_manageable(ctx, user)
        if role in user.roles:
            await user.remove_roles(
                role,
                reason=f"Custom role removed by {ctx.author} ({ctx.author.id})",
            )
        await self.bot.pool.execute(
            "DELETE FROM custom_role_assignments "
            "WHERE guild_id = $1 AND role_id = $2 AND user_id = $3",
            ctx.guild.id,
            role.id,
            user.id,
        )
        await ctx.send(
            f"Removed **{_safe_text(role.name)}** from **{_safe_text(user)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="delete")
    @app_commands.describe(role="Registered custom role to delete.")
    async def custom_role_delete(self, ctx: GuildContext, role: discord.Role) -> None:
        """Delete a custom role and its saved assignments."""
        await self._require_custom_role(ctx, role)
        self._ensure_role_manageable(ctx, role)
        confirmed = await ctx.prompt(
            f"Delete custom role **{_safe_text(role.name)}** and its saved assignments?",
            confirm_label="Yes",
            cancel_label="No",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if confirmed is None:
            return
        await role.delete(
            reason=f"Custom role deleted by {ctx.author} ({ctx.author.id})"
        )
        await self.bot.pool.execute(
            "DELETE FROM custom_role_assignments WHERE guild_id = $1 AND role_id = $2",
            ctx.guild.id,
            role.id,
        )
        await self.bot.pool.execute(
            "DELETE FROM custom_roles WHERE guild_id = $1 AND role_id = $2",
            ctx.guild.id,
            role.id,
        )
        await ctx.send(
            f"Deleted custom role **{_safe_text(role.name)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @custom_role.command(name="clear")
    @app_commands.describe(target="A registered custom role or a user.")
    async def custom_role_clear(self, ctx: GuildContext, *, target: str) -> None:
        """Clear all assignments from a role or all custom roles from a user."""
        resolved = await self._resolve_clear_target(ctx, target)
        if isinstance(resolved, discord.Role):
            await self._require_custom_role(ctx, resolved)
            self._ensure_role_manageable(ctx, resolved)
            rows = await self.bot.pool.fetch(
                "SELECT user_id FROM custom_role_assignments "
                "WHERE guild_id = $1 AND role_id = $2",
                ctx.guild.id,
                resolved.id,
            )
            count = len(rows)
            description = f"Remove **{count:,}** saved assignment(s) for **{_safe_text(resolved.name)}**?"
            confirmed = await ctx.prompt(
                description,
                confirm_label="Yes",
                cancel_label="No",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if confirmed is None:
                return
            removed = 0
            for row in rows:
                member = ctx.guild.get_member(int(row["user_id"]))
                if member is None or resolved not in member.roles:
                    continue
                try:
                    await member.remove_roles(
                        resolved,
                        reason=f"Custom role assignments cleared by {ctx.author} ({ctx.author.id})",
                    )
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
            await self.bot.pool.execute(
                "DELETE FROM custom_role_assignments WHERE guild_id = $1 AND role_id = $2",
                ctx.guild.id,
                resolved.id,
            )
            await ctx.send(
                f"Cleared **{count:,}** assignment(s) from **{_safe_text(resolved.name)}** "
                f"and removed the role from **{removed:,}** current member(s).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        user_id = resolved.id
        rows = await self.bot.pool.fetch(
            "SELECT role_id FROM custom_role_assignments "
            "WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id,
            user_id,
        )
        confirmed = await ctx.prompt(
            f"Clear **{len(rows):,}** saved custom role assignment(s) from **{_safe_text(resolved)}**?",
            confirm_label="Yes",
            cancel_label="No",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if confirmed is None:
            return
        member = ctx.guild.get_member(user_id)
        removed = 0
        for row in rows:
            role = ctx.guild.get_role(int(row["role_id"]))
            if role is None:
                continue
            try:
                self._ensure_role_manageable(ctx, role)
            except commands.BadArgument:
                continue
            if member is not None and role in member.roles:
                try:
                    await member.remove_roles(
                        role,
                        reason=f"Custom role assignments cleared by {ctx.author} ({ctx.author.id})",
                    )
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
        await self.bot.pool.execute(
            "DELETE FROM custom_role_assignments WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id,
            user_id,
        )
        await ctx.send(
            f"Cleared **{len(rows):,}** custom role assignment(s) from **{_safe_text(resolved)}** "
            f"and removed **{removed:,}** role(s) currently held.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener("on_member_join")
    async def custom_role_member_join(self, member: discord.Member) -> None:
        rows = await self.bot.pool.fetch(
            """
            SELECT a.role_id, r.booster_only
            FROM custom_role_assignments AS a
            JOIN custom_roles AS r
              ON r.guild_id = a.guild_id AND r.role_id = a.role_id
            WHERE a.guild_id = $1 AND a.user_id = $2
            """,
            member.guild.id,
            member.id,
        )
        me = member.guild.me
        if me is None:
            return
        for row in rows:
            role = member.guild.get_role(int(row["role_id"]))
            if (
                role is None
                or role.managed
                or role >= me.top_role
                or _role_dangerous_permissions(role)
                or (row["booster_only"] and member.premium_since is None)
            ):
                continue
            try:
                if role not in member.roles:
                    await member.add_roles(role, reason="Restore saved custom role")
            except (discord.Forbidden, discord.HTTPException):
                continue

    @commands.Cog.listener("on_member_update")
    async def custom_role_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.premium_since == after.premium_since:
            return
        rows = await self.bot.pool.fetch(
            """
            SELECT a.role_id, r.booster_only
            FROM custom_role_assignments AS a
            JOIN custom_roles AS r
              ON r.guild_id = a.guild_id AND r.role_id = a.role_id
            WHERE a.guild_id = $1 AND a.user_id = $2 AND r.booster_only = TRUE
            """,
            after.guild.id,
            after.id,
        )
        me = after.guild.me
        if me is None:
            return
        for row in rows:
            role = after.guild.get_role(int(row["role_id"]))
            if role is None or role.managed or role >= me.top_role:
                continue
            try:
                if after.premium_since is None and role in after.roles:
                    await after.remove_roles(role, reason="Member stopped boosting")
                elif (
                    after.premium_since is not None
                    and role not in after.roles
                    and not _role_dangerous_permissions(role)
                ):
                    await after.add_roles(role, reason="Member started boosting")
            except (discord.Forbidden, discord.HTTPException):
                continue

    @commands.Cog.listener("on_guild_role_delete")
    async def custom_role_deleted(self, role: discord.Role) -> None:
        await self.bot.pool.execute(
            "DELETE FROM custom_role_assignments WHERE guild_id = $1 AND role_id = $2",
            role.guild.id,
            role.id,
        )
        await self.bot.pool.execute(
            "DELETE FROM custom_roles WHERE guild_id = $1 AND role_id = $2",
            role.guild.id,
            role.id,
        )

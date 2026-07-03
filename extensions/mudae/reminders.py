from __future__ import annotations

import asyncio
import datetime
import re
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from extensions.context import Context

MudaeID = 432610292342587392

SERVER_ITEMS = ("claim",)
USER_ITEMS = ("rolls", "rt", "daily", "dk", "p", "vote", "spheres")
ITEMS = SERVER_ITEMS + USER_ITEMS
ALIASES = {"oh": "spheres", "oc": "spheres", "oq": "spheres", "ot": "spheres"}

PATTERNS: dict[str, re.Pattern] = {
    "claim": re.compile(r"claim (?:reset is in|for another) \*{2}(\d+)\*{2} min"),
    "rolls": re.compile(r"rolls reset in \*{2}(\d+)\*{2} min"),
    "rt": re.compile(r"(?:The )?cooldown of \$rt is not over\. Time left: \*{2}(\d+)h (\d+)\*{2} min"),
    "daily": re.compile(r"daily reset in \*{2}(\d+)h (\d+)\*{2} min"),
    "dk": re.compile(r"dk in \*{2}(\d+)h (\d+)\*{2} min"),
    "p": re.compile(r"next \$p: \*{2}(?:(\d+)h )?(\d+)\*{2} min"),
}

READY_PATTERNS: dict[str, re.Pattern] = {
    "claim": re.compile(r"you .+ claim right now"),
    "rt": re.compile(r"\$rt is available"),
    "vote": re.compile(r"You may vote right now"),
    "rolls": re.compile(r"You have \*{2}\d+\*{2} rolls left"),
}

def parse_tu(content: str) -> dict[str, Optional[float]]:
    """Parse a Mudae $tu response and return {item: minutes_until_reset}.

    Returns None for items not found in the output.
    Returns 0.0 for items that are "available right now".
    """
    result: dict[str, Optional[float]] = {}

    # Check ready-now items first
    for item, pat in READY_PATTERNS.items():
        if pat.search(content):
            result[item] = 0.0

    # Parse timed items
    for item, pat in PATTERNS.items():
        m = pat.search(content)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                h = int(groups[0]) if groups[0] else 0
                result[item] = h * 60 + int(groups[1])
            elif len(groups) == 1:
                result[item] = int(groups[0])

    if "rolls" not in result:
        rolls_pat = re.compile(r"You have \*{2}(\d+)\*{2} rolls left")
        if rolls_pat.search(content):
            result["rolls"] = 0.0

    if "spheres" not in result:
        oh_pat = re.compile(r"(\d+)h (\d+)\*{2} min before the refill")
        m = oh_pat.search(content)
        if m:
            result["spheres"] = int(m.group(1)) * 60 + int(m.group(2))

    return result


class MudaeReminders(Cog):
    """Mudae timer reminder subscriptions."""

    async def cog_load(self) -> None:
        self._dispatch_task = self.bot.loop.create_task(self._dispatch_loop())

    def cog_unload(self) -> None:
        self._dispatch_task.cancel()

    async def _dispatch_loop(self) -> None:
        """Poll mudae_timers and fire pings when timers expire."""
        while not self.bot.is_closed():
            try:
                await self._check_timers()
            except Exception as e:
                self.bot.logger.error(
                    f"Mudae timer dispatch error: {type(e).__name__}: {e}"
                )
                await self.bot.log_error(e)
            await asyncio.sleep(30)

    async def _check_timers(self) -> None:
        rows = await self.bot.pool.fetch(
            "DELETE FROM mudae_timers WHERE expires_at <= now() at time zone 'utc' RETURNING guild_id, item"
        )
        for row in rows:
            guild_id = row["guild_id"]
            item = row["item"]
            users = await self._get_subscribed_users(guild_id, item)
            if not users:
                self.bot.logger.info(
                    f"Mudae timer `{item}` expired in guild {guild_id} but no subscribers remain."
                )
                await self.bot.pool.execute(
                    "DELETE FROM mudae_subs WHERE guild_id = $1 AND item = $2",
                    guild_id, item,
                )
                continue

            guild = self.bot.get_guild(guild_id)
            if not guild:
                self.bot.logger.info(
                    f"Mudae timer `{item}` expired in guild {guild_id} but guild not found."
                )
                continue
            channel = await self._get_channel(guild_id)
            channel_mention = channel.mention # type: ignore[union-attr]

            dm_users = []
            ping_users = []
            for uid in users:
                if await self._consented(uid):
                    dm_users.append(uid)
                else:
                    ping_users.append(uid)

            if ping_users and channel:
                mentions = " ".join(f"<@{uid}>" for uid in ping_users)
                try:
                    await channel.send(f"{mentions} `{item}` timer has reset!")
                    self.bot.logger.info(
                        f"Mudae timer `{item}` fired in guild {guild_id} ({guild.name}), "
                        f"pinged {len(ping_users)} user(s) in {channel}"
                    )
                except discord.HTTPException as e:
                    self.bot.logger.warning(
                        f"Mudae timer `{item}` failed to send in guild {guild_id}: {e}"
                    )
            elif ping_users:
                self.bot.logger.warning(
                    f"Mudae timer `{item}` expired in guild {guild_id}: "
                    f"{len(ping_users)} user(s) to ping but no channel set"
                )

            sent = 0
            for uid in dm_users:
                user = self.bot.get_user(uid)
                if user:
                    try:
                        await user.send(
                            f"`{item}` timer has reset in **{guild.name}**! "
                            f"{channel_mention if channel else ''}"
                        )
                        sent += 1
                    except discord.HTTPException:
                        pass
            if dm_users:
                self.bot.logger.info(
                    f"Mudae timer `{item}` DM'd {sent}/{len(dm_users)} consented user(s) in guild {guild_id}"
                )

            await self.bot.pool.execute(
                "DELETE FROM mudae_subs WHERE guild_id = $1 AND item = $2",
                guild_id, item,
            )

    async def _get_subscribed_users(
        self, guild_id: int, item: str
    ) -> List[int]:
        row = await self.bot.pool.fetchrow(
            "SELECT user_ids FROM mudae_subs WHERE guild_id = $1 AND item = $2",
            guild_id, item,
        )
        return row["user_ids"] if row else []

    async def _subscribe(self, guild_id: int, item: str, user_id: int) -> bool:
        sql = """
        INSERT INTO mudae_subs (guild_id, item, user_ids)
        VALUES ($1, $2, ARRAY[$3]::bigint[])
        ON CONFLICT (guild_id, item)
        DO UPDATE SET user_ids = ARRAY(
            SELECT DISTINCT unnest(mudae_subs.user_ids || ARRAY[$3]::bigint[])
        )
        """
        await self.bot.pool.execute(sql, guild_id, item, user_id)
        return True

    async def _unsubscribe(self, guild_id: int, item: str, user_id: int) -> bool:
        sql = """
        UPDATE mudae_subs
        SET user_ids = ARRAY(SELECT unnest(user_ids) EXCEPT SELECT $3)
        WHERE guild_id = $1 AND item = $2
        """
        await self.bot.pool.execute(sql, guild_id, item, user_id)
        # Clean up empty rows
        await self.bot.pool.execute(
            "DELETE FROM mudae_subs WHERE guild_id = $1 AND item = $2 AND cardinality(user_ids) = 0",
            guild_id, item,
        )
        return True

    async def _get_timer(self, guild_id: int, item: str) -> Optional[datetime.datetime]:
        row = await self.bot.pool.fetchrow(
            "SELECT expires_at FROM mudae_timers WHERE guild_id = $1 AND item = $2",
            guild_id, item,
        )
        return row["expires_at"] if row else None

    async def _set_timer(self, guild_id: int, item: str, expires_at: datetime.datetime):
        await self.bot.pool.execute(
            "INSERT INTO mudae_timers (guild_id, item, expires_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, item) DO UPDATE SET expires_at = $3",
            guild_id, item, expires_at,
        )


    @commands.group(name="mudae", invoke_without_command=True, aliases=("m",))
    async def mudae_group(self, ctx: Context):
        """Mudae integration commands."""
        await ctx.send_help(ctx.command)

    @mudae_group.command(name="sub", aliases=["subscribe"])
    async def mudae_sub(self, ctx: Context, item: str = "all"):
        """Subscribe to pings when a Mudae timer resets.

        Items: claim, rolls, rt, daily, dk, p, vote, oh, oc
        Use `all` to subscribe to everything.
        """
        if not ctx.guild:
            raise commands.BadArgument("This command can only be used in a server.")

        has_channel = await self._get_channel(ctx.guild.id) is not None
        if not has_channel:
            await ctx.send(
                f"No Mudae ping channel is set in this server. "
                f"Admins can set one with `{ctx.get_prefix}mudae channel`. "
                f"You can also get pings via DM with `{ctx.get_prefix}mudae consent`."
            )

        item = item.lower()
        item = ALIASES.get(item, item)  # resolve aliases like oh->spheres
        if item not in ITEMS and item != "all":
            raise commands.BadArgument(
                f"Unknown item `{item}`. Valid items: {', '.join(ITEMS)}, all"
            )
        items = list(ITEMS) if item == "all" else [item]
        need_tu = []
        reused = []

        for it in items:
            existing = await self._get_timer(ctx.guild.id, it)
            if existing and existing > discord.utils.utcnow():
                await self._subscribe(ctx.guild.id, it, ctx.author.id)
                reused.append((it, existing))
            else:
                need_tu.append(it)

        if not need_tu:
            lines = [f"Subscribed {ctx.author.mention} to:"]
            for it, expires in reused:
                lines.append(f"`{it}` — {discord.utils.format_dt(expires, 'R')}")
            await ctx.send("\n".join(lines))
            return

        tu_msg: Optional[discord.Message] = None
        async for msg in ctx.channel.history(limit=10):
            if msg.author.id == MudaeID and msg.content:
                if any(pat.search(msg.content) for pat in PATTERNS.values()) or \
                   any(pat.search(msg.content) for pat in READY_PATTERNS.values()):
                    tu_msg = msg
                    break

        if not tu_msg:
            server_missing = [i for i in need_tu if i in SERVER_ITEMS]
            user_missing = [i for i in need_tu if i in USER_ITEMS]
            parts = []
            if server_missing:
                parts.append(f"No stored timer for {', '.join(f'`{i}`' for i in server_missing)}. An admin can run `$tu` then `fish mudae parse` to set it.")
            if user_missing:
                parts.append(f"{', '.join(f'`{i}`' for i in user_missing)} requires your own `$tu` output. Run `$tu` first, then `fish mudae sub`.")
            await ctx.send("\n".join(parts))
            return

        timers = parse_tu(tu_msg.content)
        subscribed = []
        available = []
        not_found = []

        for it in need_tu:
            timer_minutes = timers.get(it)
            if timer_minutes is None:
                not_found.append(it)
                continue
            if timer_minutes == 0:
                await self._subscribe(ctx.guild.id, it, ctx.author.id)
                available.append(it)
                continue

            expires_at = tu_msg.created_at + datetime.timedelta(minutes=timer_minutes)
            await self._set_timer(ctx.guild.id, it, expires_at)
            await self._subscribe(ctx.guild.id, it, ctx.author.id)
            subscribed.append((it, max(discord.utils.utcnow(), expires_at)))

        lines = []
        if reused:
            lines.append("Reused stored timers:")
            for it, expires in reused:
                lines.append(f"`{it}` — {discord.utils.format_dt(expires, 'R')}")
        if subscribed:
            if lines:
                lines.append("")
            lines.append("New from `$tu`:")
            for it, expires in subscribed:
                lines.append(f"`{it}` — {discord.utils.format_dt(expires, 'R')}")
        if available:
            lines.append(f"Subscribed (available now, run `$tu` after use for timer): {', '.join(f'`{a}`' for a in available)}")
        if not_found:
            lines.append(f"Not found in `$tu` output: {', '.join(f'`{n}`' for n in not_found)}")

        if not lines:
            await ctx.send("No active timers found.")
        else:
            await ctx.send("\n".join(lines))

    @mudae_group.command(name="unsub", aliases=["unsubscribe"])
    async def mudae_unsub(self, ctx: Context, item: str = "all"):
        """Unsubscribe from Mudae timer pings. Use `all` to unsubscribe from everything."""
        if not ctx.guild:
            raise commands.BadArgument("This command can only be used in a server.")

        item = item.lower()
        if item == "all":
            for it in ITEMS:
                await self._unsubscribe(ctx.guild.id, it, ctx.author.id)
            await ctx.send("Unsubscribed from all Mudae timer reminders.", ephemeral=True)
            return

        item = ALIASES.get(item, item)
        if item not in ITEMS:
            raise commands.BadArgument(
                f"Unknown item `{item}`. Valid items: {', '.join(ITEMS)}, all"
            )

        await self._unsubscribe(ctx.guild.id, item, ctx.author.id)
        await ctx.send(f"Unsubscribed from `{item}` reminders.", ephemeral=True)

    async def _get_channel(self, guild_id: int) -> Optional[discord.abc.Messageable]:
        row = await self.bot.pool.fetchrow(
            "SELECT channel_id FROM mudae_channels WHERE guild_id = $1", guild_id
        )
        if not row:
            return None
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None
        return guild.get_channel(row["channel_id"])  # type: ignore[return-value]

    async def _consented(self, user_id: int) -> bool:
        return user_id in self.bot.cached_mudae_consent

    @mudae_group.command(name="channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def mudae_channel(self, ctx: Context, channel: discord.TextChannel):
        """Set the channel where Mudae timer pings are sent."""
        if not ctx.guild:
            return
        await self.bot.pool.execute(
            "INSERT INTO mudae_channels (guild_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2",
            ctx.guild.id, channel.id,
        )
        await ctx.send(f"Mudae pings will be sent to {channel.mention}.")

    @mudae_group.command(name="consent")
    async def mudae_consent(self, ctx: Context):
        """Receive Mudae timer pings via DM instead of in-server pings."""
        try:
            await ctx.author.send("This is a test DM from Fishie's Mudae timer system. You'll receive timer pings here instead of in the server channel.")
        except discord.HTTPException:
            await ctx.send("I couldn't DM you. Please enable DMs from server members and try again.", ephemeral=True)
            return

        await self.bot.pool.execute(
            "INSERT INTO mudae_dm_consent (user_id, consented) VALUES ($1, TRUE) "
            "ON CONFLICT (user_id) DO UPDATE SET consented = TRUE",
            ctx.author.id,
        )
        self.bot.cached_mudae_consent.add(ctx.author.id)
        await ctx.send("Done! You'll now receive Mudae timer pings via DM instead of in-server.", ephemeral=True)

    @mudae_group.command(name="unconsent")
    async def mudae_unconsent(self, ctx: Context):
        """Revoke DM consent for Mudae timer pings."""
        await self.bot.pool.execute(
            "UPDATE mudae_dm_consent SET consented = FALSE WHERE user_id = $1",
            ctx.author.id,
        )
        self.bot.cached_mudae_consent.discard(ctx.author.id)
        await ctx.send("DM consent revoked. You'll no longer receive Mudae timer pings via DM.", ephemeral=True)

    @mudae_group.command(name="reset", hidden=True)
    async def mudae_reset(self, ctx: Context):
        """Owner-only: wipe all Mudae timer data."""
        if not await self.bot.is_owner(ctx.author):
            raise commands.BadArgument("This command is owner-only.")
        await self.bot.pool.execute("DELETE FROM mudae_timers")
        await self.bot.pool.execute("DELETE FROM mudae_subs")
        await self.bot.pool.execute("DELETE FROM mudae_channels")
        await self.bot.pool.execute("DELETE FROM mudae_dm_consent")
        self.bot.cached_mudae_consent.clear()
        await ctx.send("All Mudae timer data has been wiped.")

    @commands.has_guild_permissions(manage_guild=True)
    @mudae_group.command(name="parse")
    async def mudae_parse(self, ctx: Context):
        """Manually parse the most recent $tu message and update timers."""
        if not ctx.guild:
            raise commands.BadArgument("This command can only be used in a server.")

        tu_msg: Optional[discord.Message] = None
        async for msg in ctx.channel.history(limit=10):
            if msg.author.id == MudaeID and msg.content:
                if any(pat.search(msg.content) for pat in PATTERNS.values()) or \
                   any(pat.search(msg.content) for pat in READY_PATTERNS.values()):
                    tu_msg = msg
                    break

        if not tu_msg:
            raise commands.BadArgument("No recent `$tu` output found in this channel.")

        timers = parse_tu(tu_msg.content)
        lines = []
        for item, minutes in sorted(timers.items()):
            if minutes is None:
                lines.append(f"`{item}`: not found")
            elif minutes == 0:
                lines.append(f"`{item}`: available now")
            else:
                expires_at = tu_msg.created_at + datetime.timedelta(minutes=minutes)
                await self._set_timer(ctx.guild.id, item, expires_at)
                lines.append(
                    f"`{item}`: {discord.utils.format_dt(expires_at, 'R')} "
                    f"({minutes:.0f} min)"
                )

        await ctx.send("\n".join(lines) if lines else "No timers found.")


    @mudae_group.command(name="timers", aliases=["list"])
    async def mudae_timers(self, ctx: Context):
        """Show remaining time for all subscribed Mudae timers in this server."""
        if not ctx.guild:
            raise commands.BadArgument("This command can only be used in a server.")

        rows = await self.bot.pool.fetch(
            "SELECT item, expires_at, user_ids FROM mudae_subs "
            "JOIN mudae_timers USING (guild_id, item) "
            "WHERE guild_id = $1 ORDER BY expires_at",
            ctx.guild.id,
        )

        if not rows:
            await ctx.send("No active Mudae timer subscriptions in this server.")
            return

        lines = []
        for row in rows:
            item = row["item"]
            expires = row["expires_at"]
            count = len(row["user_ids"])
            lines.append(
                f"`{item}` — {discord.utils.format_dt(expires, 'R')} "
                f"({count} subscriber{'s' if count != 1 else ''})"
            )

        await ctx.send("\n".join(lines))

from __future__ import annotations

import asyncio
import datetime
import textwrap
from typing import TYPE_CHECKING, Annotated, Any, Optional

import asyncpg
import discord
from discord.ext import commands

from utils import FieldPageSource, Pager, PGTimer, plural, timer as timer_module

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class ReminderCommands(CogBase):
    @commands.group(
        name="remind",
        aliases=("timer", "remindme", "reminder"),
        usage="<when>",
        invoke_without_command=True,
    )
    async def reminder_set(
        self,
        ctx: Context,
        *,
        when: Annotated[
            timer_module.FriendlyTimeResult,
            timer_module.UserFriendlyTime(commands.clean_content, default="…"),
        ],
    ):
        """Reminds you about something."""

        timer = await self.create_timer(
            when.dt,
            "reminder",
            ctx.author.id,
            ctx.channel.id,
            when.arg,
            created=ctx.message.created_at,
            message_id=ctx.message.id,
        )
        delta = timer_module.human_timedelta(when.dt, source=timer.created_at)
        await ctx.send(f"Okay, reminding {ctx.author.mention} in {delta}: {when.arg}")

    @reminder_set.command(name="list", ignore_extra=False)
    async def reminder_list(self, ctx: Context):
        """Shows the 10 latest currently running reminders."""
        query = """SELECT id, expires, extra #>> '{args,2}'
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires;
                """

        records = await ctx.db.fetch(query, str(ctx.author.id))

        if len(records) == 0:
            return await ctx.send("No reminders.")

        entries = [
            (
                f"{_id}: {timer_module.format_relative(expires)}",
                textwrap.shorten(message, width=512),
            )
            for _id, expires, message in records
        ]

        p = FieldPageSource(entries, per_page=10)
        p.embed.title = f"Reminders for {ctx.author}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    @reminder_set.command(
        name="delete", aliases=["remove", "cancel"], ignore_extra=False
    )
    async def reminder_delete(self, ctx: Context, *, id: int):
        """Deletes a reminder by its ID.
        To get a reminder ID, use the reminder list command.
        You must own the reminder to delete it, obviously.
        """

        query = """DELETE FROM reminders
                   WHERE id=$1
                   AND event = 'reminder'
                   AND extra #>> '{args,0}' = $2;
                """

        status = await ctx.db.execute(query, id, str(ctx.author.id))
        if status == "DELETE 0":
            return await ctx.send("Could not delete any reminders with that ID.")

        # if the current timer is being deleted
        if self._current_timer and self._current_timer.id == id:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        await ctx.send("Successfully deleted reminder.")

    @reminder_set.command(name="clear", ignore_extra=False)
    async def reminder_clear(self, ctx: Context):
        """Clears all reminders you have set."""

        # For UX purposes this has to be two queries.

        query = """SELECT COUNT(*)
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1;
                """

        author_id = str(ctx.author.id)
        total: asyncpg.Record = await ctx.db.fetchrow(query, author_id)
        total = total[0]
        if total == 0:
            return await ctx.send("You do not have any reminders to delete.")

        confirm = await ctx.prompt(
            f"Are you sure you want to delete {plural(total):reminder}?"
        )
        if not confirm:
            return await ctx.send("Aborting", ephemeral=True)

        query = """DELETE FROM reminders WHERE event = 'reminder' AND extra #>> '{args,0}' = $1;"""
        await ctx.db.execute(query, author_id)

        # Check if the current timer is the one being cleared and cancel it if so
        if self._current_timer and self._current_timer.author_id == ctx.author.id:
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        await ctx.send(f"Successfully deleted {plural(total):reminder}.")

    @commands.Cog.listener()
    async def on_reminder_timer_complete(self, timer: PGTimer):
        author_id, channel_id, message = timer.args

        try:
            channel: discord.VoiceChannel | discord.TextChannel | discord.Thread = (
                self.bot.get_channel(channel_id)
                or (await self.bot.fetch_channel(channel_id))
            )  # type: ignore
        except discord.HTTPException:
            return

        message_id = timer.kwargs.get("message_id")
        msg = f"<@{author_id}>, reminder from {timer.human_delta}: {message}"

        try:
            reference = await channel.fetch_message(message_id) if message_id else None
        except:
            reference = None

        try:
            await channel.send(msg, reference=reference)  # type:ignore
        except discord.HTTPException:
            return

    @commands.command(name="reminders", ignore_extra=False)
    async def reminders(self, ctx: Context):
        """Shows the 10 latest currently running reminders.

        Alias for remind list"""
        query = """SELECT id, expires, extra #>> '{args,2}'
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires;
                """

        records = await ctx.db.fetch(query, str(ctx.author.id))

        if len(records) == 0:
            return await ctx.send("No reminders.")

        entries = [
            (
                f"{_id}: {timer_module.format_relative(expires)}",
                textwrap.shorten(message, width=512),
            )
            for _id, expires, message in records
        ]

        p = FieldPageSource(entries, per_page=10)
        p.embed.title = f"Reminders for {ctx.author}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

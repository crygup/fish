from __future__ import annotations
import asyncio
import datetime

import textwrap
from typing import TYPE_CHECKING, Annotated, Any, Optional

import asyncpg
import discord
from discord.ext import commands

from utils import (
    FieldPageSource,
    Pager,
    PGTimer,
    plural,
    timer as timer_module,
    BaseCog,
    CHECK,
)

if TYPE_CHECKING:
    from cogs.context import Context
    from bot import Bot


class MaybeAcquire:
    def __init__(
        self, connection: Optional[asyncpg.Connection], *, pool: asyncpg.Pool
    ) -> None:
        self.connection: Optional[asyncpg.Connection] = connection
        self.pool: asyncpg.Pool = pool
        self._cleanup: bool = False

    async def __aenter__(self) -> asyncpg.Connection:
        if self.connection is None:
            self._cleanup = True
            self._connection = c = await self.pool.acquire()
            return c  # type: ignore
        return self.connection

    async def __aexit__(self, *args) -> None:
        if self._cleanup:
            await self.pool.release(self._connection)


class ReminderCommands(BaseCog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        self._have_data = asyncio.Event()
        self._current_timer: Optional[PGTimer] = None
        self._task = bot.loop.create_task(self.dispatch_timers())

    async def cog_unload(self):
        self._task.cancel()

    async def create_timer(
        self, when: datetime.datetime, event: str, /, *args: Any, **kwargs: Any
    ) -> PGTimer:
        r"""Creates a timer.
        Parameters
        -----------
        when: datetime.datetime
            When the timer should fire.
        event: str
            The name of the event to trigger.
            Will transform to 'on_{event}_timer_complete'.
        \*args
            Arguments to pass to the event
        \*\*kwargs
            Keyword arguments to pass to the event
        connection: asyncpg.Connection
            Special keyword-only argument to use a specific connection
            for the DB request.
        created: datetime.datetime
            Special keyword-only argument to use as the creation time.
            Should make the timedeltas a bit more consistent.
        Note
        ------
        Arguments and keyword arguments must be JSON serialisable.
        Returns
        --------
        :class:`Timer`
        """

        pool = self.bot.pool

        try:
            now = kwargs.pop("created")
        except KeyError:
            now = discord.utils.utcnow()

        # Remove timezone information since the database does not deal with it
        when = when.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)

        timer = PGTimer.temporary(
            event=event, args=args, kwargs=kwargs, expires=when, created=now
        )
        delta = (when - now).total_seconds()
        if delta <= 60:
            # a shortcut for small timers
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        query = """INSERT INTO reminders (event, extra, expires, created)
                   VALUES ($1, $2::jsonb, $3, $4)
                   RETURNING id;
                """

        row = await pool.fetchrow(
            query, event, {"args": args, "kwargs": kwargs}, when, now
        )

        if row is None:
            raise TypeError("Failed")

        timer.id = row[0]

        # only set the data check if it can be waited on
        if delta <= (86400 * 40):  # 40 days
            self._have_data.set()

        # check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    async def get_active_timer(
        self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7
    ) -> Optional[PGTimer]:
        query = "SELECT * FROM reminders WHERE expires < (CURRENT_DATE + $1::interval) ORDER BY expires LIMIT 1;"
        con = connection or self.bot.pool

        record = await con.fetchrow(query, datetime.timedelta(days=days))
        return PGTimer(record=record) if record else None

    async def short_timer_optimisation(self, seconds: float, timer: PGTimer) -> None:
        await asyncio.sleep(seconds)
        event_name = f"{timer.event}_timer_complete"
        self.bot.dispatch(event_name, timer)

    async def wait_for_active_timers(
        self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7
    ) -> PGTimer:
        async with MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            timer = await self.get_active_timer(connection=con, days=days)
            if timer is not None:
                self._have_data.set()
                return timer

            self._have_data.clear()
            self._current_timer = None
            await self._have_data.wait()

            # At this point we always have data
            return await self.get_active_timer(connection=con, days=days)  # type: ignore

    async def call_timer(self, timer: PGTimer) -> None:
        # delete the timer
        query = "DELETE FROM reminders WHERE id=$1;"
        await self.bot.pool.execute(query, timer.id)

        # dispatch the event
        event_name = f"{timer.event}_timer_complete"
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self) -> None:
        try:
            while not self.bot.is_closed():
                # can only asyncio.sleep for up to ~48 days reliably
                # so we're gonna cap it off at 40 days
                # see: http://bugs.python.org/issue20493
                timer = self._current_timer = await self.wait_for_active_timers(days=40)
                now = datetime.datetime.utcnow()

                if timer.expires >= now:
                    to_sleep = (timer.expires - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.call_timer(timer)
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/reminder.py#L301-L331
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
        total = await ctx.db.fetchrow(query, author_id)

        if total is None:
            return await ctx.send("You do not have any reminders to delete.")

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

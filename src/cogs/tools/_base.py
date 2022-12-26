from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING, Any, Optional

import asyncpg
import discord
from discord.ext.commands import Cog

from utils import PGTimer

if TYPE_CHECKING:
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
            return c
        return self.connection

    async def __aexit__(self, *args) -> None:
        if self._cleanup:
            await self.pool.release(self._connection)


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        self.currently_downloading: list[str] = []
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

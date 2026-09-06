from __future__ import annotations

import asyncio
import datetime
import random
import re
import textwrap
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, Sequence, cast

import asyncpg

# TODO: replace with ZoneInfo when upgrading to 3.9
import dateutil.tz
import discord
from dateutil.zoneinfo import get_zonefile_instance
from discord import app_commands
from discord.ext import commands
from typing_extensions import Annotated

from core import Cog
from core.handoff import is_legacy_instance
from utils import FieldPageSource, Pager, cache, formats, fuzzy, time
from utils.timezone_locations import OfflineLocationResolver

if TYPE_CHECKING:
    from typing_extensions import Self

    from core.bot import Fishie
    from extensions.context import Context


class MaybeAcquire:
    def __init__(
        self, connection: Optional[asyncpg.Connection], *, pool: asyncpg.Pool
    ) -> None:
        self._connection: Optional[asyncpg.Connection] = connection
        self.pool: asyncpg.Pool = pool
        self._cleanup: bool = False

    async def __aenter__(self) -> asyncpg.Connection:
        if self._connection is None:
            self._cleanup = True
            self._connection = c = await self.pool.acquire()  # type: ignore
            return c  # type: ignore

        return self._connection

    async def __aexit__(self, *args) -> None:
        if self._cleanup:
            await self.pool.release(self._connection)  # type: ignore


class TimeZone(NamedTuple):
    label: str
    key: str

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> TimeZone:
        assert isinstance(ctx.cog, Reminder)
        timezones = ctx.cog.resolve_timezones(argument)
        if not timezones:
            raise commands.BadArgument(f"Could not find timezone for {argument!r}")
        if len(timezones) == 1:
            return timezones[0]
        return await choose_timezone(ctx, timezones)

    def to_choice(self) -> app_commands.Choice[str]:
        return app_commands.Choice(name=self.label[:100], value=self.key)


_REMINDER_JSON_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _reminder_json_filters(kwargs: Mapping[str, Any], *, start: int = 2) -> list[str]:
    """Build safe JSON-path predicates for reminder lookup operations.

    PostgreSQL does not accept a bind parameter for an identifier embedded in
    a JSON path expression, so these predicates necessarily include the key in
    the SQL text.  Reminder keys are internal values, but validate them before
    interpolation so a future caller cannot turn this helper into SQL
    injection.  Values remain normal bind parameters in the caller.
    """

    predicates: list[str] = []
    for index, key in enumerate(kwargs, start=start):
        if not isinstance(key, str) or _REMINDER_JSON_KEY_RE.fullmatch(key) is None:
            raise ValueError(
                "Reminder lookup keys must contain only letters, numbers, and underscores."
            )
        predicates.append(f"extra #>> ARRAY['kwargs', '{key}'] = ${index}")
    return predicates


class TimeZoneChoiceButton(discord.ui.Button["TimeZoneDisambiguatorView"]):
    def __init__(self, timezone: TimeZone, *, row: int) -> None:
        super().__init__(label=timezone.label[:80], row=row)
        self.timezone = timezone

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.selected = self.timezone
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        self.view.stop()


class TimeZonePageButton(discord.ui.Button["TimeZoneDisambiguatorView"]):
    def __init__(self, *, direction: int, disabled: bool) -> None:
        label = "Previous" if direction < 0 else "Next"
        emoji = (
            "\N{BLACK LEFT-POINTING TRIANGLE}"
            if direction < 0
            else "\N{BLACK RIGHT-POINTING TRIANGLE}"
        )
        super().__init__(label=label, emoji=emoji, row=4, disabled=disabled)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.page += self.direction
        self.view.render()
        await interaction.response.edit_message(
            content=self.view.content, view=self.view
        )


class TimeZoneDisambiguatorView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(self, ctx: Context, timezones: list[TimeZone]) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.timezones = timezones
        self.page = 0
        self.selected: Optional[TimeZone] = None
        self.message: Optional[discord.Message] = None
        self.render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.timezones) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    @property
    def content(self) -> str:
        suffix = (
            f" (page {self.page + 1}/{self.page_count})" if self.page_count > 1 else ""
        )
        return f"That location uses multiple timezones. Which one did you mean?{suffix}"

    def render(self) -> None:
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        for index, timezone in enumerate(
            self.timezones[start : start + self.PAGE_SIZE]
        ):
            self.add_item(TimeZoneChoiceButton(timezone, row=index // 5))
        if self.page_count > 1:
            self.add_item(TimeZonePageButton(direction=-1, disabled=self.page == 0))
            self.add_item(
                TimeZonePageButton(
                    direction=1, disabled=self.page == self.page_count - 1
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This timezone selection is not for you.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass


async def choose_timezone(ctx: Context, timezones: list[TimeZone]) -> TimeZone:
    view = TimeZoneDisambiguatorView(ctx, timezones)
    view.message = await ctx.send(view.content, view=view, ephemeral=True)
    await view.wait()
    if view.selected is None:
        raise commands.BadArgument("Timezone selection timed out.")
    return view.selected


class Timer:
    __slots__ = ("args", "kwargs", "event", "id", "created_at", "expires", "timezone")

    def __init__(self, *, record: asyncpg.Record):
        self.id: int = record["id"]

        extra = record["extra"]
        self.args: Sequence[Any] = extra.get("args", [])
        self.kwargs: dict[str, Any] = extra.get("kwargs", {})
        self.event: str = record["event"]
        # PostgreSQL returns timezone-aware UTC values for reminder timestamps.
        self.created_at: datetime.datetime = record["created"]
        self.expires: datetime.datetime = record["expires"]
        self.timezone: str = record["timezone"]

    @classmethod
    def temporary(
        cls,
        *,
        expires: datetime.datetime,
        created: datetime.datetime,
        event: str,
        args: Sequence[Any],
        kwargs: dict[str, Any],
        timezone: str,
    ) -> Self:
        pseudo = {
            "id": None,
            "extra": {"args": args, "kwargs": kwargs},
            "event": event,
            "created": created,
            "expires": expires,
            "timezone": timezone,
        }
        return cls(record=pseudo)  # type: ignore

    def __eq__(self, other: object) -> bool:
        try:
            return self.id == other.id  # type: ignore
        except AttributeError:
            return False

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def human_delta(self) -> str:
        # Discord formatting needs an aware datetime even though reminder
        # scheduling deliberately uses naive UTC internally.
        created_at = self.created_at.replace(tzinfo=datetime.timezone.utc)
        return discord.utils.format_dt(created_at, "R")

    @property
    def author_id(self) -> Optional[int]:
        if self.args:
            return int(self.args[0])
        return None

    def __repr__(self) -> str:
        return f"<Timer created={self.created_at} expires={self.expires} event={self.event}>"


UTC_OFFSET_RE = re.compile(
    r"^(?:UTC|GMT)\s*(?P<sign>[+-])\s*(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$",
    re.IGNORECASE,
)


def parse_utc_offset(argument: str) -> Optional[TimeZone]:
    match = UTC_OFFSET_RE.fullmatch(argument.strip())
    if match is None:
        return None

    hours = int(match["hours"])
    minutes = int(match["minutes"] or 0)
    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        return None

    sign = match["sign"]
    key = f"UTC{sign}{hours:02d}:{minutes:02d}"
    return TimeZone(label=key, key=key)


class Reminder(Cog):
    """Reminders to do something."""

    DEFAULT_TIMEZONES = (
        ("Eastern Time", "America/New_York"),
        ("Central Time", "America/Chicago"),
        ("Mountain Time", "America/Denver"),
        ("Pacific Time", "America/Los_Angeles"),
        ("India", "Asia/Kolkata"),
        ("Istanbul", "Europe/Istanbul"),
        ("Moscow", "Europe/Moscow"),
        ("London", "Europe/London"),
        ("Paris", "Europe/Paris"),
        ("Madrid", "Europe/Madrid"),
        ("Berlin", "Europe/Berlin"),
        ("Athens", "Europe/Athens"),
        ("Kyiv", "Europe/Kyiv"),
        ("Rome", "Europe/Rome"),
        ("Amsterdam", "Europe/Amsterdam"),
        ("Warsaw", "Europe/Warsaw"),
        ("Toronto", "America/Toronto"),
        ("Brisbane", "Australia/Brisbane"),
        ("Sydney", "Australia/Sydney"),
        ("S\N{LATIN SMALL LETTER A WITH TILDE}o Paulo", "America/Sao_Paulo"),
        ("Tokyo", "Asia/Tokyo"),
        ("Shanghai", "Asia/Shanghai"),
    )

    def __init__(self, bot: Fishie):
        self.bot: Fishie = bot
        self._have_data = asyncio.Event()
        self._current_timer: Optional[Timer] = None
        self._task: asyncio.Task[None] | None = None
        if not is_legacy_instance(bot):
            self._task = bot.loop.create_task(self.dispatch_timers())
        self.valid_timezones: set[str] = set(get_zonefile_instance().zones)
        self._timezone_aliases: dict[str, str] = {
            "Eastern Time": "America/New_York",
            "Central Time": "America/Chicago",
            "Mountain Time": "America/Denver",
            "Pacific Time": "America/Los_Angeles",
            # (Unfortunately) special case American timezone abbreviations
            "EST": "America/New_York",
            "CST": "America/Chicago",
            "MST": "America/Denver",
            "PST": "America/Los_Angeles",
            "EDT": "America/New_York",
            "CDT": "America/Chicago",
            "MDT": "America/Denver",
            "PDT": "America/Los_Angeles",
            "AKST": "America/Anchorage",
            "AKDT": "America/Anchorage",
            "HST": "Pacific/Honolulu",
            "GMT": "UTC",
            "BST": "Europe/London",
            "CET": "Europe/Paris",
            "CEST": "Europe/Paris",
            "EET": "Europe/Athens",
            "EEST": "Europe/Athens",
            "AEST": "Australia/Sydney",
            "AEDT": "Australia/Sydney",
            "ACST": "Australia/Adelaide",
            "ACDT": "Australia/Adelaide",
            "AWST": "Australia/Perth",
            "JST": "Asia/Tokyo",
            "KST": "Asia/Seoul",
            "NZST": "Pacific/Auckland",
            "NZDT": "Pacific/Auckland",
        }
        self._default_timezones = [
            app_commands.Choice(name=label, value=key)
            for label, key in self.DEFAULT_TIMEZONES
        ]
        self._location_resolver = OfflineLocationResolver()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\N{ALARM CLOCK}")

    @staticmethod
    def _interaction_requires_author_dm(interaction: discord.Interaction) -> bool:
        """Return whether a slash reminder cannot be delivered to its source.

        User-installed application commands can be invoked from a private or
        group DM that Fishie is not a member of.  Discord still delivers the
        interaction, but the interaction follow-up webhook cannot post back to
        that channel.  A normal DM with Fishie is messageable and should keep
        using the interaction channel.
        """

        if interaction.guild_id is not None:
            return False

        channel = interaction.channel
        if channel is None:
            return True

        # Group DMs are never messageable by an app that is not a participant.
        if isinstance(channel, discord.GroupChannel):
            return True

        if isinstance(channel, discord.DMChannel):
            # Discord may omit recipients in interaction payloads.  In that
            # case we cannot prove this is Fishie's DM, so use the author's DM
            # rather than risking an invalid follow-up.
            recipient = channel.recipient
            return recipient is None or recipient.id != interaction.user.id

        # Unknown private-channel implementations (or partial channels) are
        # safest to treat as inaccessible and route through the author's DM.
        return True

    async def _author_dm(self, author_id: int) -> Optional[discord.DMChannel]:
        """Resolve the author's DM channel for reminders.

        ``get_user`` is usually populated, but reminders survive restarts, so
        fall back to ``fetch_user`` before creating the DM channel.
        """

        user = self.bot.get_user(author_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(author_id)
            except discord.HTTPException:
                return None

        try:
            return await user.create_dm()
        except discord.HTTPException:
            return None

    def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()

    @cache.cache()
    async def get_timezone(self, user_id: int, /) -> Optional[str]:
        query = "SELECT timezone from user_settings WHERE user_id = $1;"
        record = await self.bot.pool.fetchrow(query, user_id)
        return record["timezone"] if record else None

    async def get_tzinfo(self, user_id: int, /) -> datetime.tzinfo:
        tz = await self.get_timezone(user_id)
        if tz is None:
            return datetime.timezone.utc
        return dateutil.tz.gettz(tz) or datetime.timezone.utc

    def resolve_timezones(self, query: str) -> list[TimeZone]:
        offset = parse_utc_offset(query)
        if offset is not None:
            return [offset]

        query_folded = query.strip().casefold()
        aliases = {
            label.casefold(): TimeZone(label=label, key=key)
            for label, key in self._timezone_aliases.items()
        }
        alias = aliases.get(query_folded)
        if alias is not None:
            return [alias]

        timezone_names = {
            timezone.casefold(): timezone for timezone in self.valid_timezones
        }
        timezone = timezone_names.get(query_folded)
        if timezone is not None:
            return [TimeZone(label=timezone, key=timezone)]

        return [
            TimeZone(label=match.label, key=match.timezone)
            for match in self._location_resolver.resolve(query, limit=100)
        ]

    def find_timezones(self, query: str) -> list[TimeZone]:
        exact = self.resolve_timezones(query)
        if exact:
            return exact

        if "/" in query:
            return [
                TimeZone(key=timezone, label=timezone)
                for timezone in fuzzy.finder(query, self.valid_timezones)
            ]

        locations = self._location_resolver.search(query)
        if locations:
            return [
                TimeZone(label=match.label, key=match.timezone) for match in locations
            ]

        keys = fuzzy.finder(query, self._timezone_aliases.keys())
        return [
            TimeZone(label=label, key=self._timezone_aliases[label]) for label in keys
        ]

    async def get_active_timer(
        self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7
    ) -> Optional[Timer]:
        query = """
            SELECT * FROM reminders
            ORDER BY expires
            LIMIT 1;
        """
        con = connection or self.bot.pool

        record = await con.fetchrow(query)
        return Timer(record=record) if record else None

    async def wait_for_active_timers(
        self, *, connection: Optional[asyncpg.Connection] = None, days: int = 7
    ) -> Timer:
        async with MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            while True:
                # Clear before querying so an insert racing with the query always
                # leaves the event set and cannot strand the dispatcher.
                self._have_data.clear()
                timer = await self.get_active_timer(connection=con, days=days)
                if timer is not None:
                    return timer
                self._current_timer = None
                try:
                    await asyncio.wait_for(self._have_data.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    pass

    async def call_timer(self, timer: Timer) -> None:
        # delete the timer
        query = "DELETE FROM reminders WHERE id=$1 RETURNING id;"
        deleted = await self.bot.pool.fetchval(query, timer.id)
        if deleted is None:
            return

        # dispatch the event
        event_name = f"{timer.event}_timer_complete"
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self) -> None:
        backoff = 1.0
        while not self.bot.is_closed():
            try:
                timer = self._current_timer = await self.wait_for_active_timers()
                now = discord.utils.utcnow()
                delay = max(0.0, (timer.expires - now).total_seconds())
                if delay:
                    self._have_data.clear()
                    try:
                        await asyncio.wait_for(
                            self._have_data.wait(), timeout=min(delay, 3600.0)
                        )
                    except asyncio.TimeoutError:
                        pass
                    if self._have_data.is_set() or delay > 3600.0:
                        continue
                await self.call_timer(timer)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.bot.logger.exception("Reminder dispatcher failed; retrying")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def short_timer_optimisation(self, seconds: float, timer: Timer) -> None:
        await asyncio.sleep(seconds)
        event_name = f"{timer.event}_timer_complete"
        self.bot.dispatch(event_name, timer)

    async def get_timer(self, event: str, /, **kwargs: Any) -> Optional[Timer]:
        r"""Gets a timer from the database.

        Note you cannot find a database by its expiry or creation time.

        Parameters
        -----------
        event: str
            The name of the event to search for.
        \*\*kwargs
            Keyword arguments to search for in the database.

        Returns
        --------
        Optional[:class:`Timer`]
            The timer if found, otherwise None.
        """

        filtered_clause = _reminder_json_filters(kwargs)
        query = f"SELECT * FROM reminders WHERE event = $1 AND {' AND '.join(filtered_clause)} LIMIT 1"
        record = await self.bot.pool.fetchrow(query, event, *kwargs.values())
        return Timer(record=record) if record else None

    async def delete_timer(self, event: str, /, **kwargs: Any) -> None:
        r"""Deletes a timer from the database.

        Note you cannot find a database by its expiry or creation time.

        Parameters
        -----------
        event: str
            The name of the event to search for.
        \*\*kwargs
            Keyword arguments to search for in the database.
        """

        filtered_clause = _reminder_json_filters(kwargs)
        query = f"DELETE FROM reminders WHERE event = $1 AND {' AND '.join(filtered_clause)} RETURNING id"
        record: Any = await self.bot.pool.fetchrow(query, event, *kwargs.values())

        # if the current timer is being deleted
        if (
            record is not None
            and self._current_timer
            and self._current_timer.id == record["id"]
        ):
            self._have_data.set()

    async def create_timer(
        self, when: datetime.datetime, event: str, /, *args: Any, **kwargs: Any
    ) -> Timer:
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
        timezone: str
            Special keyword-only argument to use as the timezone for the
            expiry time. This automatically adjusts the expiry time to be
            in the future, should it be in the past.

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

        timezone_name = kwargs.pop("timezone", "UTC")
        when = (
            when.astimezone(datetime.timezone.utc)
            if when.tzinfo is not None
            else when.replace(tzinfo=datetime.timezone.utc)
        )
        now = (
            now.astimezone(datetime.timezone.utc)
            if now.tzinfo is not None
            else now.replace(tzinfo=datetime.timezone.utc)
        )

        timer = Timer.temporary(
            event=event,
            args=args,
            kwargs=kwargs,
            expires=when,
            created=now,
            timezone=timezone_name,
        )
        query = """INSERT INTO reminders (event, extra, expires, created, timezone)
                   VALUES ($1, $2::jsonb, $3, $4, $5)
                   RETURNING id;
                """

        row = await pool.fetchrow(
            query, event, {"args": args, "kwargs": kwargs}, when, now, timezone_name
        )

        if row is None:
            raise commands.BadArgument("No results.")

        timer.id = row[0]

        self._have_data.set()

        return timer

    @commands.hybrid_group(
        name="remind", aliases=["timer", "reminder", "remindme"], usage="<when>"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder(
        self,
        ctx: Context,
        *,
        when: Annotated[
            time.FriendlyTimeResult,
            time.UserFriendlyTime(commands.clean_content, default="…"),
        ],
    ):
        """Reminds you of something after a certain amount of time

        The input can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:

        - "next thursday at 3pm do something funny"
        - "do the dishes tomorrow"
        - "in 3 days do the thing"
        - "2d unmute someone"

        Times are in UTC unless a timezone is specified
        using the "timezone set" command.
        """

        zone = await self.get_timezone(ctx.author.id)
        timer = await self.create_timer(
            when.dt,
            "reminder",
            ctx.author.id,
            ctx.channel.id,
            when.arg,
            created=ctx.message.created_at,
            message_id=ctx.message.id,
            timezone=zone or "UTC",
        )
        delta = time.human_timedelta(when.dt, source=timer.created_at)
        msg = f"Alright {ctx.author.mention}, in {delta}: {when.arg}"
        # 10% chance of advertising timezone support (temporarily...?)
        if zone is None and random.randint(0, 10) == 5:
            msg = f'{msg}\n\n\N{ELECTRIC LIGHT BULB} Did you know you can set your timezone with "{ctx.get_prefix}timezone set"?'

        await ctx.send(msg)

    @reminder.app_command.command(name="set")
    @app_commands.describe(
        when="When to be reminded of something.", text="What to be reminded of"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_set(
        self,
        interaction: discord.Interaction,
        when: app_commands.Transform[datetime.datetime, time.TimeTransformer],
        text: str = "…",
    ):
        """Sets a reminder to remind you of something at a specific time."""

        # A user-installed app can receive a slash command in a private/group
        # DM where Fishie is not a participant.  The interaction response can
        # be acknowledged, but its follow-up webhook cannot post there.  Route
        # both the acknowledgement and eventual reminder to the author's DM in
        # that case.
        route_to_dm = self._interaction_requires_author_dm(interaction)
        author_dm: Optional[discord.DMChannel] = None
        response_deferred = False
        if route_to_dm:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                    response_deferred = True
            except (discord.HTTPException, discord.InteractionResponded):
                # We still attempt the author's DM below.  This covers an
                # interaction that was already acknowledged by a wrapper.
                response_deferred = False
            author_dm = await self._author_dm(interaction.user.id)

        zone = await self.get_timezone(interaction.user.id)
        timer = await self.create_timer(
            when,
            "reminder",
            interaction.user.id,
            author_dm.id if author_dm is not None else (interaction.channel_id or 0),
            text,
            created=interaction.created_at,
            message_id=None,
            timezone=zone or "UTC",
        )
        delta = time.human_timedelta(when, source=timer.created_at)
        response = f"Alright {interaction.user.mention}, in {delta}: {text}"

        if not route_to_dm:
            await interaction.response.send_message(response)
            return

        delivered = False
        if author_dm is not None:
            try:
                await author_dm.send(response)
                delivered = True
            except discord.HTTPException:
                self.bot.logger.warning(
                    "Could not send reminder acknowledgement to user DM %s",
                    interaction.user.id,
                    exc_info=True,
                )

        # Remove the temporary deferred response from an inaccessible source
        # channel once the author's DM acknowledgement has been sent.  If DM
        # delivery failed, leave a best-effort ephemeral response instead.
        if delivered and response_deferred:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        elif not delivered:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(response, ephemeral=True)
            except (discord.HTTPException, discord.InteractionResponded):
                self.bot.logger.warning(
                    "Could not acknowledge reminder interaction for user %s",
                    interaction.user.id,
                    exc_info=True,
                )

    @reminder_set.error
    async def reminder_set_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, time.BadTimeTransform):
            response = str(error)
            if not self._interaction_requires_author_dm(interaction):
                await interaction.response.send_message(response, ephemeral=True)
                return

            # Transformers run before ``reminder_set`` itself, so the normal
            # callback cannot defer an interaction from a private/group DM.
            # Acknowledge it best-effort, then put the validation error in the
            # author's DM just like a successfully-created reminder.
            deferred = False
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                    deferred = True
            except (discord.HTTPException, discord.InteractionResponded):
                pass

            delivered = False
            author_dm = await self._author_dm(interaction.user.id)
            if author_dm is not None:
                try:
                    await author_dm.send(response)
                    delivered = True
                except discord.HTTPException:
                    self.bot.logger.warning(
                        "Could not send reminder validation error to user DM %s",
                        interaction.user.id,
                        exc_info=True,
                    )

            if delivered and deferred:
                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    pass
            elif not delivered:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            response, ephemeral=True
                        )
                except (discord.HTTPException, discord.InteractionResponded):
                    self.bot.logger.warning(
                        "Could not acknowledge reminder validation error for user %s",
                        interaction.user.id,
                        exc_info=True,
                    )

    @reminder.command(name="delete", aliases=["remove", "cancel"], ignore_extra=False)
    @app_commands.describe(id="ID of the reminder to delete.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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

        status = await ctx.bot.pool.execute(query, id, str(ctx.author.id))
        if status == "DELETE 0":
            return await ctx.send("Could not delete any reminders with that ID.")

        # if the current timer is being deleted
        if self._current_timer and self._current_timer.id == id:
            self._have_data.set()

        await ctx.send("Successfully deleted reminder.", ephemeral=True)

    @reminder.command(name="clear", ignore_extra=False)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_clear(self, ctx: Context):
        """Clears all reminders you have set."""

        # For UX purposes this has to be two queries.

        query = """SELECT COUNT(*)
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1;
        """

        author_id = str(ctx.author.id)
        total = int(await ctx.bot.pool.fetchval(query, author_id) or 0)
        if total == 0:
            return await ctx.send("You do not have any reminders to delete.")

        confirm = await ctx.prompt(
            f"Are you sure you want to delete {formats.plural(total):reminder}?"  # type: ignore
        )
        if not confirm:
            return await ctx.send("Aborting", ephemeral=True)

        query = """DELETE FROM reminders WHERE event = 'reminder' AND extra #>> '{args,0}' = $1;"""
        await ctx.bot.pool.execute(query, author_id)

        # Check if the current timer is the one being cleared and cancel it if so
        if self._current_timer and self._current_timer.author_id == ctx.author.id:
            self._have_data.set()

        await ctx.send(
            f"Successfully deleted {formats.plural(total):reminder}.",
            ephemeral=True,  # type: ignore
        )

    async def reminders_command(self, ctx: Context):
        query = """SELECT id, expires, extra #>> '{args,2}'
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires
                   LIMIT 10;
                """

        records = await ctx.bot.pool.fetch(query, str(ctx.author.id))

        if len(records) == 0:
            return await ctx.send("No currently running reminders.")

        entries = [
            (
                f"{_id}: {discord.utils.format_dt(expires, 'R')}",
                textwrap.shorten(message, width=512),
            )
            for _id, expires, message in records
        ]

        p = FieldPageSource(entries, per_page=10)
        p.embed.title = f"Reminders for {ctx.author}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    @reminder.command(name="list", ignore_extra=False)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reminder_list(self, ctx: Context):
        """Shows the 10 latest currently running reminders."""
        await self.reminders_command(ctx)

    @commands.command(name="reminders")
    async def reminders(self, ctx: Context):
        """Shows the 10 latest currently running reminders."""
        await self.reminders_command(ctx)

    async def _resolve_timezone_query(
        self, ctx: Context, query: str
    ) -> tuple[discord.User | discord.Member | None, TimeZone | None]:
        """Resolve a base ``timezone`` argument as a user or a location.

        Locations are resolved first so names such as ``Bulgaria`` and
        ``New York`` remain useful even when a server happens to have a member
        with a similar name.  Explicit mentions and numeric IDs are always
        treated as users.
        """

        query = query.strip()
        if not query:
            raise commands.BadArgument("Please provide a user or timezone.")

        is_user_reference = bool(re.fullmatch(r"<@!?\d+>|\d+", query))
        if not is_user_reference:
            matches = self.resolve_timezones(query)
            if matches:
                timezone = (
                    matches[0]
                    if len(matches) == 1
                    else await choose_timezone(ctx, matches)
                )
                return None, timezone

        try:
            user = await commands.UserConverter().convert(ctx, query)
        except commands.BadArgument as user_error:
            if is_user_reference:
                raise commands.BadArgument(
                    f"Could not find user {query!r}."
                ) from user_error

            # A user name can be supplied without a mention.  If it is not a
            # user either, return the same helpful error used by timezone set.
            matches = self.resolve_timezones(query)
            if matches:
                timezone = (
                    matches[0]
                    if len(matches) == 1
                    else await choose_timezone(ctx, matches)
                )
                return None, timezone
            raise commands.BadArgument(
                f"Could not find a user or timezone for {query!r}."
            ) from user_error
        return user, None

    async def _send_timezone_info(self, ctx: Context, tz: TimeZone) -> None:
        """Send the current time and UTC offset for a resolved timezone."""

        embed = discord.Embed(title=tz.key, colour=discord.Colour.blurple())
        dt = discord.utils.utcnow().astimezone(
            dateutil.tz.gettz(tz.key) or datetime.timezone.utc
        )
        current_time = dt.strftime("%Y-%m-%d %I:%M %p")
        embed.add_field(name="Current Time", value=current_time)

        offset = dt.utcoffset()
        if offset is not None:
            minutes, _ = divmod(int(offset.total_seconds()), 60)
            hours, minutes = divmod(minutes, 60)
            embed.add_field(name="UTC Offset", value=f"{hours:+03d}:{minutes:02d}")

        await ctx.send(embed=embed)

    async def _send_user_timezone(
        self, ctx: Context, user: discord.User | discord.Member
    ) -> None:
        """Send the current local time for a user's saved timezone."""

        tz = await self.get_timezone(user.id)
        if tz is None:
            prefix = getattr(ctx, "get_prefix", "") or ""
            setup_command = f"{prefix}timezone set <tz>"
            await ctx.send(
                f"{user} has not set their timezone. Use `{setup_command}` to set it.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        current_time = discord.utils.utcnow().astimezone(
            dateutil.tz.gettz(tz) or datetime.timezone.utc
        )
        formatted = current_time.strftime("%Y-%m-%d %I:%M %p")
        await ctx.send(
            f"The current time for {user} is {formatted} ({tz}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cast(Any, commands.hybrid_group)(
        name="timezone",
        aliases=("time",),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="A user, timezone, UTC offset, city, state, or country."
    )
    async def timezone(self, ctx: Context, *, query: str | None = None):
        """Show a timezone's current time or a user's current local time."""

        if not query:
            await self._send_user_timezone(ctx, ctx.author)
            return

        user, tz = await self._resolve_timezone_query(ctx, query)
        if user is not None:
            await self._send_user_timezone(ctx, user)
        elif tz is not None:
            await self._send_timezone_info(ctx, tz)

    @timezone.command(name="set")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(tz="A timezone, UTC offset, city, state, or country.")
    async def timezone_set(self, ctx: Context, *, tz: TimeZone):
        """Sets your timezone.

        This is used to convert times to your local timezone when
        using the reminder command and other miscellaneous commands
        such as tempblock, tempmute, etc.
        """

        pool = self.bot.pool
        await pool.execute(
            """INSERT INTO user_settings (user_id, timezone)
               VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET timezone = $2;
            """,
            ctx.author.id,
            tz.key,
        )

        self.get_timezone.invalidate(self, ctx.author.id)
        await ctx.send(
            f"Your timezone has been set to {tz.key}.",
            ephemeral=True,
            delete_after=10,
        )

    @timezone.command(name="info")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(tz="A timezone, UTC offset, city, state, or country.")
    async def timezone_info(self, ctx: Context, *, tz: TimeZone):
        """Retrieves info about a timezone."""

        await self._send_timezone_info(ctx, tz)

    @timezone.autocomplete("query")
    @timezone_set.autocomplete("tz")
    @timezone_info.autocomplete("tz")
    async def timezone_set_autocomplete(
        self, _, argument: str
    ) -> list[app_commands.Choice[str]]:
        if not argument:
            return self._default_timezones
        matches = self.find_timezones(argument)
        return [tz.to_choice() for tz in matches[:25]]

    @timezone.command(name="get")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        user="The member to get the timezone of. Defaults to yourself."
    )
    async def timezone_get(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows the timezone of a user."""
        self_query = user.id == ctx.author.id
        tz = await self.get_timezone(user.id)
        if tz is None:
            return await ctx.send(f"{user} has not set their timezone.")

        time = (
            discord.utils.utcnow()
            .astimezone(dateutil.tz.gettz(tz))
            .strftime("%Y-%m-%d %I:%M %p")
        )
        if self_query:
            msg = await ctx.send(
                f"Your timezone is {tz!r}. The current time is {time}."
            )
            await asyncio.sleep(5)
            await msg.edit(content=f"Your current time is {time}.")
        else:
            await ctx.send(f"The current time for {user} is {time}.")

    @timezone.command(name="clear")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def timezone_clear(self, ctx: Context):
        """Clears your timezone."""
        await ctx.bot.pool.execute(
            "UPDATE user_settings SET timezone = 'UTC' WHERE user_id=$1", ctx.author.id
        )
        self.get_timezone.invalidate(self, ctx.author.id)
        await ctx.send(
            "Your timezone has been cleared and reset to UTC.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_reminder_timer_complete(self, timer: Timer):
        if is_legacy_instance(self.bot):
            return
        author_id, channel_id, message = timer.args

        msg = f"<@{author_id}>, reminder from {timer.human_delta}: {message}"

        # Prefer the original channel so text reminders can reply to the
        # message that created them.  A slash command invoked in a DM that
        # does not include Fishie stores the author's DM as its destination;
        # older timers may still contain the inaccessible source channel, so
        # gracefully fall back to the author's DM whenever resolution or
        # delivery fails.
        channel: discord.abc.Messageable | None = None
        try:
            candidate = self.bot.get_channel(channel_id)
            if candidate is None:
                candidate = await self.bot.fetch_channel(channel_id)
            if isinstance(candidate, discord.abc.Messageable):
                channel = candidate
        except discord.HTTPException:
            channel = None

        message_id = timer.kwargs.get("message_id")
        if channel is not None:
            try:
                if message_id:
                    try:
                        source_message = await channel.fetch_message(message_id)
                        await source_message.reply(msg)
                        return
                    except discord.HTTPException:
                        pass

                await channel.send(msg)
                return
            except (discord.HTTPException, AttributeError):
                pass

        # The bot may no longer be able to access the source DM (or the
        # original guild channel may have disappeared).  Delivering directly
        # to the author is reliable across restarts and private-channel
        # contexts.
        author_dm = await self._author_dm(author_id)
        if author_dm is None:
            self.bot.logger.warning(
                "Could not deliver reminder %s to channel %s or user DM %s",
                timer.id,
                channel_id,
                author_id,
            )
            return
        try:
            await author_dm.send(msg)
        except discord.HTTPException:
            self.bot.logger.warning(
                "Could not deliver reminder %s to user DM %s",
                timer.id,
                author_id,
                exc_info=True,
            )


async def setup(bot):
    await bot.add_cog(Reminder(bot))

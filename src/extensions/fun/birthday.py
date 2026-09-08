"""Birthday storage and flexible, explicit month/day parsing."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import discord
from dateutil import tz
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


@dataclass(frozen=True)
class Birthday:
    month: int
    day: int
    year: int | None = None

    def label(self) -> str:
        suffix = (
            "th"
            if 10 < self.day % 100 < 14
            else {1: "st", 2: "nd", 3: "rd"}.get(self.day % 10, "th")
        )
        result = f"{calendar.month_name[self.month]} {self.day}{suffix}"
        return result if self.year is None else f"{result}, {self.year}"

    def next_date(self, today: date) -> date:
        # February 29 remains February 29: find the next actual leap-day birthday.
        for year in range(today.year, today.year + 9):
            try:
                candidate = date(year, self.month, self.day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
        raise ValueError("No upcoming birthday found.")


async def birthday_timestamp(
    pool: Any,
    birthday: Birthday,
    viewer_id: int,
    owner_id: int,
    *,
    now: datetime | None = None,
) -> datetime:
    """Use the viewer's zone, then the owner's, for birthday midnight."""
    rows = await pool.fetch(
        "SELECT user_id, timezone FROM user_settings WHERE user_id = ANY($1::BIGINT[])",
        list(dict.fromkeys((viewer_id, owner_id))),
    )
    zones = {int(row["user_id"]): row["timezone"] for row in rows}
    selected_zone = timezone.utc
    for user_id in (viewer_id, owner_id):
        name = zones.get(user_id)
        if name and (resolved := tz.gettz(name)) is not None:
            selected_zone = resolved
            break
    local_now = (now or datetime.now(timezone.utc)).astimezone(selected_zone)
    upcoming = birthday.next_date(local_now.date())
    return datetime(upcoming.year, upcoming.month, upcoming.day, tzinfo=selected_zone)


def parse_birthday(value: str, *, today: date | None = None) -> Birthday:
    today = today or datetime.now(timezone.utc).date()
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value.strip().lower())
    parts = re.split(r"[\s,./-]+", cleaned)
    parts = [part for part in parts if part]
    if len(parts) not in (2, 3):
        raise ValueError(
            "Provide a month and day, optionally with a year (for example August 18th or 8/18/1998)."
        )
    months = {
        name.lower(): i
        for i in range(1, 13)
        for name in (calendar.month_name[i], calendar.month_abbr[i])
    }
    months["sept"] = 9
    named = [i for i, part in enumerate(parts) if part in months]
    year_text: str | None = None
    if named:
        if len(named) != 1:
            raise ValueError("Provide one month and one day.")
        index = named[0]
        month = months[parts[index]]
        numbers = [part for i, part in enumerate(parts) if i != index]
        if any(not part.isdigit() for part in numbers):
            raise ValueError("The day and year must be numbers.")
        if len(numbers) == 2 and (len(numbers[0]) == 4 or int(numbers[0]) > 31):
            year_text, day_text = numbers
        else:
            day_text = numbers[0]
            year_text = numbers[1] if len(numbers) == 2 else None
        day = int(day_text)
    else:
        if any(not part.isdigit() for part in parts):
            raise ValueError("Use a month name or a numeric date such as 8/18.")
        if len(parts) == 3 and len(parts[0]) == 4:
            year_text, month_text, day_text = parts
            month, day = int(month_text), int(day_text)
        else:
            month, day = int(parts[0]), int(parts[1])
            if month > 12 and day <= 12:
                month, day = day, month
            year_text = parts[2] if len(parts) == 3 else None
    year = int(year_text) if year_text is not None else None
    if year is not None and year_text is not None:
        if len(year_text) <= 2:
            year += 2000 if year <= today.year % 100 else 1900
        elif len(year_text) != 4:
            raise ValueError("Use a two-digit or four-digit year.")
    try:
        actual = date(year if year is not None else 2000, month, day)
    except ValueError as exc:
        raise ValueError("That is not a valid calendar date.") from exc
    if year is not None and actual > today:
        raise ValueError("Your birth date cannot be in the future.")
    return Birthday(month, day, year)


class BirthdayCommands:
    bot: Fishie

    @tasks.loop(minutes=5)
    async def birthday_reward_loop(self) -> None:
        await self.bot.pool.execute(
            "DELETE FROM reward_cooldowns WHERE eligible_at <= now()"
        )
        rows = await self.bot.pool.fetch("""
            SELECT b.user_id FROM user_birthdays b
            LEFT JOIN birthday_rewards r USING (user_id)
            LEFT JOIN user_settings s USING (user_id)
            WHERE COALESCE(s.tracking_enabled, true)
              AND COALESCE(s.currency_tracking_enabled, true)
              AND (r.last_awarded_on IS NULL OR
                   r.last_awarded_on + INTERVAL '1 year' <= (now() AT TIME ZONE 'UTC')::date + 1)
            """)
        for row in rows:
            try:
                await self.bot.currency.award_birthday(int(row["user_id"]))
            except Exception:
                self.bot.logger.exception(
                    "Could not award birthday Coins to user %s", row["user_id"]
                )

    @birthday_reward_loop.before_loop
    async def before_birthday_rewards(self) -> None:
        await self.bot.wait_until_ready()

    @cast(Any, commands.group)(name="birthday", invoke_without_command=True)
    async def birthday(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show your birthday or another user's next birthday."""
        row = await self.bot.pool.fetchrow(
            "SELECT month, day, year FROM user_birthdays WHERE user_id = $1", user.id
        )
        if row is None:
            await ctx.send(
                f"{discord.utils.escape_markdown(user.display_name)} has not set a birthday.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        birthday = Birthday(row["month"], row["day"], row["year"])
        stamp = await birthday_timestamp(
            self.bot.pool, birthday, ctx.author.id, user.id
        )
        await ctx.send(
            f"{discord.utils.escape_markdown(user.display_name)}'s birthday is {discord.utils.format_dt(stamp, 'R')} ({discord.utils.format_dt(stamp, 'D')}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @birthday.command(name="set")
    async def birthday_set(self, ctx: Context, *, time: str) -> None:
        """Set your birthday using a month and day, with an optional year."""
        try:
            birthday = parse_birthday(time)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        if (
            await ctx.prompt(
                f"Set your birthday as **{birthday.label()}**?",
                confirm_label="Set birthday",
            )
            is None
        ):
            return
        await self.bot.pool.execute(
            "INSERT INTO user_birthdays (user_id, month, day, year) VALUES ($1, $2, $3, $4) ON CONFLICT (user_id) DO UPDATE SET month = EXCLUDED.month, day = EXCLUDED.day, year = EXCLUDED.year",
            ctx.author.id,
            birthday.month,
            birthday.day,
            birthday.year,
        )
        await ctx.send(f"Your birthday is now set to **{birthday.label()}**.")

    @birthday.command(name="clear", aliases=["remove"])
    async def birthday_clear(self, ctx: Context) -> None:
        """Remove your saved birthday."""
        await self.bot.pool.execute(
            "DELETE FROM user_birthdays WHERE user_id = $1", ctx.author.id
        )
        await ctx.send("Your saved birthday has been removed.")

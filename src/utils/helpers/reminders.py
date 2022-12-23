from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

import parsedatetime
from discord.ext import commands

from ..vars.errors import InvalidDateProvided

if TYPE_CHECKING:
    from cogs.context import Context


class Reminder:
    def __init__(self, data: Dict[Any, Any]) -> None:
        self.member_id: int = data["member_id"]
        self.guild_id: int = data["guild_id"]
        self.message_url: str = data["message_url"]
        self.remind_text: Optional[str] = data["remind_text"]
        self.start: datetime.datetime = data["start"]
        self.end: datetime.datetime = data["end_time"]


class TimeConverter(commands.Converter):
    calender = parsedatetime.Calendar(version=parsedatetime.VERSION_CONTEXT_STYLE)

    async def convert(self, ctx: Context, argument: str) -> datetime.datetime:
        now = ctx.message.created_at
        cal = parsedatetime.Calendar()

        results = cal.parseDT(argument, tzinfo=datetime.timezone.utc)

        dt = results[0]
        status = results[1]

        if status == 0:
            raise InvalidDateProvided(
                'Invalid time provided, try "next thursday" or "3 days".'
            )

        if dt < now:
            raise InvalidDateProvided("This time is in the past.")

        return dt

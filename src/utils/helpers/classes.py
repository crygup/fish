from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional, Sequence, TypeAlias, Union

import asyncpg
import discord
from discord.ext import commands

from ..vars import BlankException
from . import timer as timer_module
from .functions import can_execute_action, get_or_fetch_member

if TYPE_CHECKING:
    from typing_extensions import Self

    from cogs.context import Context


class PGTimer:
    __slots__ = ("args", "kwargs", "event", "id", "created_at", "expires")

    def __init__(self, *, record: asyncpg.Record):
        self.id: int = record["id"]

        extra = record["extra"]
        self.args: Sequence[Any] = extra.get("args", [])
        self.kwargs: dict[str, Any] = extra.get("kwargs", {})
        self.event: str = record["event"]
        self.created_at: datetime.datetime = record["created"]
        self.expires: datetime.datetime = record["expires"]

    @classmethod
    def temporary(
        cls,
        *,
        expires: datetime.datetime,
        created: datetime.datetime,
        event: str,
        args: Sequence[Any],
        kwargs: dict[str, Any],
    ) -> Self:
        pseudo = {
            "id": None,
            "extra": {"args": args, "kwargs": kwargs},
            "event": event,
            "created": created,
            "expires": expires,
        }
        return cls(record=pseudo)

    def __eq__(self, other: object) -> bool:
        try:
            return self.id == other.id  # type: ignore
        except AttributeError:
            return False

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def human_delta(self) -> str:
        return timer_module.format_relative(self.created_at)

    @property
    def author_id(self) -> Optional[int]:
        if self.args:
            return int(self.args[0])
        return None

    def __repr__(self) -> str:
        return f"<Timer created={self.created_at} expires={self.expires} event={self.event}>"


class ActionReason(commands.Converter):
    async def convert(self, ctx: Context, argument: str):
        ret = f"{ctx.author} (ID: {ctx.author.id}): {argument}"

        if len(ret) > 512:
            reason_max = 512 - len(ret) + len(argument)
            raise BlankException(f"Reason is too long ({len(argument)}/{reason_max})")
        return ret


class MemberID(commands.Converter):
    async def convert(self, ctx: Context, argument: str):
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            try:
                member_id = int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(
                    f"{argument} is not a valid member or member ID."
                ) from None
            else:
                m = await get_or_fetch_member(ctx.guild, member_id)
                if m is None:
                    # hackban case
                    return type(
                        "_Hackban",
                        (),
                        {"id": member_id, "__str__": lambda s: f"Member ID {s.id}"},
                    )()

        if not can_execute_action(ctx, ctx.author, m):
            raise commands.BadArgument(
                "You cannot do this action on this user due to role hierarchy."
            )
        return m


@dataclass
class RockPaperScissors:
    choice: Union[Literal["rock"], Literal["paper"], Literal["scissors"]]

    def __lt__(self, other: RockPaperScissors) -> bool:
        if self.choice == "rock":
            if other.choice == "scissors":
                return True

        elif self.choice == "paper":
            if other.choice == "rock":
                return True

        elif self.choice == "scissors":
            if other.choice == "paper":
                return True

        return False

    def __gt__(self, other: RockPaperScissors) -> bool:
        if self.choice == "rock":
            if other.choice == "paper":
                return True

        elif self.choice == "paper":
            if other.choice == "scissors":
                return True

        elif self.choice == "scissors":
            if other.choice == "rock":
                return True

        return False

    def __str__(self) -> str:
        return self.choice


class Timer:
    def __init__(self):
        self._start: Optional[float] = None
        self._end: Optional[float] = None

    def start(self):
        self._start = time.perf_counter()

    def stop(self):
        self._end = time.perf_counter()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def __int__(self):
        return round(self.time)

    def __float__(self):
        return self.time

    def __str__(self):
        return str(self.time)

    def __repr__(self):
        return f"<Timer time={self.time}>"

    @property
    def time(self):
        if self._end is None:
            raise ValueError("Timer has not been ended.")

        if self._start is None:
            raise ValueError("Timer has not been starter.")

        return self._end - self._start


@dataclass()
class GoogleImageData:
    image_url: str
    url: str
    snippet: str
    query: str
    author: discord.User | discord.Member


@dataclass()
class SpotifySearchData:
    track: str
    album: str
    artist: str

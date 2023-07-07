from __future__ import annotations

import re
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from extensions.context import Context


class URLConverter(commands.Converter[str]):
    async def convert(self, ctx: Context, argument: str) -> str:  # type: ignore
        if not re.match(r"^https?://", argument):
            argument = f"http://{argument}"

        return argument

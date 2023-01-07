from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

import discord

from ._base import CogBase
from .functions import *
from discord.ext import commands
from utils import BlankException, run_process

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class Video(CogBase):
    @commands.group(name="video", invoke_without_command=True)
    async def video(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @video.command(name="get-audio", aliases=("ga", "get_audio"))
    async def get_audio(
        self, ctx: Context, attachment: Union[Optional[discord.Attachment], str]
    ):
        if attachment is None:
            raise BlankException(
                "Please provide a video, either upload one or provide a direct url."
            )

        url = attachment if isinstance(attachment, str) else attachment.url

        async with ctx.session.get(url) as resp:
            content_type = resp.headers.get("Content-Type")
            if (
                not content_type
                or content_type
                and not content_type.startswith("video/")
            ):
                raise BlankException("Please provide an actual video.")

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands
from PIL import Image as PImage
from utils import Argument, ImageConverter, to_thread

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Manipulation(CogBase):
    @to_thread
    def resize_method(self, image: BytesIO, height: int, width: int) -> BytesIO:
        with PImage.open(image) as output:
            output_buffer = BytesIO()

            resized = output.resize((height, width))
            resized.save(output_buffer, "png")
            output_buffer.seek(0)

            return output_buffer

    @commands.command(name="resize")
    async def resize(
        self,
        ctx: Context,
        height: int,
        width: int,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Resizes an image"""
        if width > 2000 or height > 2000:
            raise ValueError("Width or height is too big, keep it under 2000px please.")

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await self.resize_method(new_image, width, height)
        file = discord.File(output, filename="resize.png")

        await ctx.send(file=file)

    @commands.command(name="caption", hidden=True, enabled=False)
    async def caption(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
        *,
        text: str = commands.parameter(displayed_default="<text>"),
    ):
        """Caption an image"""
        await ctx.send(text)

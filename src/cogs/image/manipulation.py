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
    def resize_method(
        self, image: BytesIO, width: Optional[int], height: Optional[int]
    ) -> BytesIO:
        with PImage.open(image) as output:
            output_buffer = BytesIO()

            height = height or output.height
            width = width or output.width
            resized = output.resize((width, height))
            resized.save(output_buffer, "png")
            output_buffer.seek(0)

            return output_buffer

    @commands.command(name="resize")
    async def resize(
        self,
        ctx: Context,
        width: Optional[int],
        height: Optional[int],
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Resizes an image"""
        if width and width > 2000 or height and height > 2000:
            raise ValueError("Width or height is too big, keep it under 2000px please.")

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await self.resize_method(new_image, height, width)
        file = discord.File(output, filename="resize.png")

        await ctx.send(file=file)

    @commands.command(name="caption")
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

from __future__ import annotations

import imghdr
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from PIL import Image, ImageOps

from utils import Argument, ImageConverter, to_thread

from ._base import CogBase
from .functions import add_images, gif_maker, text_to_image

if TYPE_CHECKING:
    from cogs.context import Context


class Manipulation(CogBase):
    @to_thread
    def resize_method(self, image: BytesIO, height: int, width: int) -> BytesIO:
        with Image.open(image) as output:
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

    @to_thread
    def layer_image(self, image: BytesIO, cover_file: str) -> BytesIO:
        with Image.open(image) as output:
            output_buffer = BytesIO()

            with Image.open(cover_file) as cover:
                resized_to_fit = cover.resize((output.width, output.height))
                output.paste(resized_to_fit, mask=resized_to_fit)

            output.save(output_buffer, "png")
            output_buffer.seek(0)

            return output_buffer

    @commands.command(name="debunked")
    async def debunked(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Adds a debunked image on another"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await self.layer_image(new_image, "src/files/assets/debunked.png")
        file = discord.File(output, filename="debunked.png")

        await ctx.send(file=file)

    @commands.command(name="gay")
    async def gay(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Adds a gay flag image on another"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await self.layer_image(new_image, "src/files/assets/gay.png")
        file = discord.File(output, filename="gay.png")

        await ctx.send(file=file)

    @to_thread
    def invert_method(self, image: BytesIO) -> BytesIO:
        with Image.open(image) as output:
            output_buffer = BytesIO()

            new_im = Image.new("RGB", (output.width, output.height))
            new_im.paste(output)

            inverted = ImageOps.invert(new_im)

            inverted.save(output_buffer, "png")
            output_buffer.seek(0)

            return output_buffer

    @commands.command(name="invert")
    async def invert(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Inverts the colors of an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await self.invert_method(new_image)
        file = discord.File(output, filename="inverted.png")

        await ctx.send(file=file)

    @to_thread
    def willslap_method(self, image: BytesIO, image2: BytesIO) -> BytesIO:
        with Image.open("src/files/assets/willslap.png") as output:
            output_buffer = BytesIO()

            new_im = Image.new("RGBA", (output.width, output.height))
            new_im.paste(output)

            with Image.open(image) as new_image:
                resized = new_image.resize((110, 110))
                new_im.paste(resized, (235, 100), mask=resized)

            with Image.open(image2) as new_image:
                resized = new_image.resize((135, 135))
                new_im.paste(resized, (570, 130), mask=resized)

            new_im.save(output_buffer, format="png")
            output_buffer.seek(0)
            return output_buffer

    @commands.command(name="willslap")
    async def willslap(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
        *,
        image2: Argument = commands.parameter(
            default=None, displayed_default="[image2=None]"
        ),
    ):
        """Slap someone will smith style"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        new_image2 = await ImageConverter().convert(ctx, image2)
        output = await self.willslap_method(new_image, new_image2)
        file = discord.File(output, filename="willslap.png")

        await ctx.send(file=file)

    @commands.command(name="caption")
    async def caption(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=commands.Author, displayed_default="[image=None]"
        ),
        *,
        text: str = commands.parameter(displayed_default="<text>"),
    ):
        """Captions an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        boxed = await text_to_image(text)
        gif = imghdr.what(new_image) == "gif"  # type: ignore
        asset = (
            await gif_maker(new_image, boxed)
            if gif
            else await add_images(new_image, boxed)
        )

        await ctx.send(
            file=discord.File(asset, filename=f"caption.{'gif' if gif else 'png'}")
        )

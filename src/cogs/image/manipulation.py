from __future__ import annotations

import imghdr
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from PIL import Image

from utils import Argument, ImageConverter, to_thread

from ._base import CogBase
from .functions import (
    add_images,
    gif_maker,
    invert_method,
    layer_image,
    resize_method,
    text_to_image,
    blur_method,
    kuwahara_method,
    sharpen_method,
    spread_method,
)

if TYPE_CHECKING:
    from cogs.context import Context


class Manipulation(CogBase):
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
        output = await resize_method(new_image, width, height)
        file = discord.File(output, filename="resize.png")

        await ctx.send(file=file)

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
        output = await layer_image(new_image, "src/files/assets/debunked.png")
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
        output = await layer_image(new_image, "src/files/assets/gay.png")
        file = discord.File(output, filename="gay.png")

        await ctx.send(file=file)

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
        output = await invert_method(new_image)
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

    @commands.command(name="blur")
    async def blur(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Blurs an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        asset = await blur_method(new_image)

        await ctx.send(file=discord.File(asset, filename=f"blur.png"))

    @commands.command(name="kuwahara", aliases=("paint",))
    async def kuwahara(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Kuwaharas an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        asset = await kuwahara_method(new_image)

        await ctx.send(file=discord.File(asset, filename=f"kuwahara.png"))

    @commands.command(name="sharpen")
    async def sharpen(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Sharpens an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        asset = await sharpen_method(new_image)

        await ctx.send(file=discord.File(asset, filename=f"kuwahara.png"))

    @commands.command(name="spread")
    async def spread(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Spreads an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        asset = await spread_method(new_image)

        await ctx.send(file=discord.File(asset, filename=f"kuwahara.png"))

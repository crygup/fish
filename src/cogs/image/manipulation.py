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
    sharpen_method,
    spread_method,
    willslap_method,
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
        output = await willslap_method(new_image, new_image2)
        file = discord.File(output, filename="willslap.png")

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
        gif = imghdr.what(asset) == "gif"  # type: ignore

        await ctx.send(
            file=discord.File(asset, filename=f"blur.{'gif' if gif else 'png'}")
        )

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
        gif = imghdr.what(asset) == "gif"  # type: ignore

        await ctx.send(
            file=discord.File(asset, filename=f"sharpen.{'gif' if gif else 'png'}")
        )

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
        gif = imghdr.what(asset) == "gif"  # type: ignore

        await ctx.send(
            file=discord.File(asset, filename=f"spread.{'gif' if gif else 'png'}")
        )

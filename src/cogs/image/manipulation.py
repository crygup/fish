from __future__ import annotations

import imghdr
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from utils import Argument, ImageConverter

from ._base import CogBase
from .functions import *  # it's bad practice to do this but it's just a timer saver innit

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
        gif: bool = imghdr.what(output) == "gif"  # type: ignore

        file = discord.File(output, filename=f"resize.{'gif' if gif else 'png'}")

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

        await ctx.send(file=discord.File(asset, filename=f"blur.png"))

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

        await ctx.send(file=discord.File(asset, filename=f"sharpen.png"))

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

        await ctx.send(file=discord.File(asset, filename=f"spread.png"))

    @commands.group(name="overlay", invoke_without_command=True)
    async def overlay(self, ctx: Context):
        await ctx.send_help(ctx.command)

    async def do_overlay(self, ctx: Context, image: Argument, mode: str):
        await ctx.trigger_typing()
        mode_path = {
            "debunked": "src/files/assets/debunked.png",
            "gay": "src/files/assets/gay.png",
            "trans": "src/files/assets/trans.png",
            "smg": "src/files/assets/smg.png",
            "uk": "src/files/assets/uk.png",
            "usa": "src/files/assets/usa.png",
            "ussr": "src/files/assets/ussr.png",
            "shotgun": "src/files/assets/shotgun.png",
            "pistol": "src/files/assets/pistol.png",
            "russia": "src/files/assets/russia.png",
        }
        new_image = await ImageConverter().convert(ctx, image)
        output = await layer_image(new_image, mode_path[mode])
        file = discord.File(output, filename=f"{mode}.png")

        await ctx.send(file=file)

    @overlay.command(name="debunked")
    async def debunked(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the debunked image on another"""
        await self.do_overlay(ctx, image, "debunked")

    @overlay.command(name="gay")
    async def gay(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the gay flag on an image"""
        await self.do_overlay(ctx, image, "gay")

    @overlay.command(name="trans")
    async def trans(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the trans flag on an image"""
        await self.do_overlay(ctx, image, "trans")

    @overlay.command(name="ussr")
    async def ussr(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the USSR flag on an image"""
        await self.do_overlay(ctx, image, "ussr")

    @overlay.command(name="russia")
    async def russia(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the Russia flag on an image"""
        await self.do_overlay(ctx, image, "russia")

    @overlay.command(name="usa")
    async def usa(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the USA flag on an image"""
        await self.do_overlay(ctx, image, "usa")

    @overlay.command(name="uk")
    async def uk(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays the UK flag on an image"""
        await self.do_overlay(ctx, image, "uk")

    @overlay.command(name="pistol")
    async def pistol(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays a pistol on an image"""
        await self.do_overlay(ctx, image, "pistol")

    @overlay.command(name="shotgun")
    async def shotgun(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays a shotgun on an image"""
        await self.do_overlay(ctx, image, "shotgun")

    @overlay.command(name="smg")
    async def smg(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Overlays an smg on an image"""
        await self.do_overlay(ctx, image, "smg")

    @commands.command(name="rotate")
    async def rotate(
        self,
        ctx: Context,
        degree: Optional[int] = 90,
        image: Argument = commands.parameter(
            default=None, displayed_default="[image=None]"
        ),
    ):
        """Rotates an image.

        Defaults to 90 degrees"""

        degree = degree or 90

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)
        output = await rotate_method(new_image, degree)
        file = discord.File(output, filename=f"rotated.png")

        await ctx.send(file=file)

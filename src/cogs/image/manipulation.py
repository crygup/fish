from __future__ import annotations
import time

from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from utils import Argument, ImageConverter, Timer, what

from ._base import CogBase
from .functions import *  # it's bad practice to do this but it's just a timer saver innit

if TYPE_CHECKING:
    from cogs.context import Context


class Manipulation(CogBase):
    async def do_image(self, ctx: Context, image: BytesIO, time_taken: float):
        filename = f"{ctx.command.name}.{what(image)}"
        file = discord.File(image, filename=filename)

        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_image(url=f"attachment://{filename}")

        width, height = await get_wh(image)
        size = await get_size(image)

        embed.set_footer(
            text=f"{width}x{height}, {size}, took {round(time_taken, 2)} seconds."
        )

        await ctx.send(file=file, embed=embed, check_ref=True)

    @commands.command(name="resize")
    async def resize(
        self,
        ctx: Context,
        height: int,
        width: int,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
    ):
        """Resizes an image"""
        if width > 2000 or height > 2000:
            raise ValueError("Width or height is too big, keep it under 2000px please.")

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await resize_method(new_image, width, height)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="invert")
    async def invert(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
    ):
        """Inverts the colors of an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await invert_method(new_image)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="willslap")
    async def willslap(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
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

        start = time.perf_counter()
        output = await willslap_method(new_image, new_image2)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="caption")
    async def caption(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
        *,
        text: str = commands.parameter(displayed_default="<text>"),
    ):
        """Captions an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        boxed = await text_to_image(text)
        gif = what(new_image) == "gif"
        output = (
            await gif_maker(new_image, boxed)
            if gif
            else await add_images(new_image, boxed)
        )
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="blur")
    async def blur(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
    ):
        """Blurs an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await blur_method(new_image)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="sharpen")
    async def sharpen(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
    ):
        """Sharpens an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await sharpen_method(new_image)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.command(name="spread")
    async def spread(
        self,
        ctx: Context,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
        ),
    ):
        """Spreads an image"""
        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await spread_method(new_image)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @commands.group(name="overlay", aliases=("o",), invoke_without_command=True)
    async def overlay(self, ctx: Context):
        """Overlay some images"""
        await ctx.send_help(ctx.command)

    async def do_overlay(self, ctx: Context, image: Argument, mode: str):
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

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await layer_image(new_image, mode_path[mode])
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

    @overlay.command(name="debunked")
    async def debunked(
        self,
        ctx: Context,
        *,
        image: Argument = commands.parameter(
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
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
            default=None, displayed_default="recent-media"
        ),
    ):
        """Rotates an image.

        Defaults to 90 degrees"""

        degree = degree or 90

        await ctx.trigger_typing()
        new_image = await ImageConverter().convert(ctx, image)

        start = time.perf_counter()
        output = await rotate_method(new_image, degree)
        end = time.perf_counter()

        await self.do_image(ctx=ctx, image=output, time_taken=end - start)

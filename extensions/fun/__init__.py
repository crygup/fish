from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING

import discord
import psutil
from discord.ext import commands
from discord import app_commands

from core import Cog
from utils import to_image, to_thread, MediaConverter
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

from .about import About
from .corn import Corn
from .helpers import RPSView, WTPView, dagpi

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context



@to_thread
def make_caption(img_bytes: bytes, caption_text: str) -> tuple[BytesIO, str]:
    src = Image.open(BytesIO(img_bytes))
    is_gif = src.format == "GIF" or getattr(src, "n_frames", 1) > 1

    if is_gif and getattr(src, "n_frames", 1) > 1:
        # caption every frame of animated GIF
        frames = []
        durations = []
        try:
            while True:
                frame = src.convert("RGBA")
                durations.append(src.info.get("duration", 100))
                frames.append(_caption_frame(frame, caption_text))
                src.seek(src.tell() + 1)
        except EOFError:
            pass

        buf = BytesIO()
        frames[0].save(
            buf, format="GIF", save_all=True,
            append_images=frames[1:], duration=durations,
            loop=0, disposal=2,
        )
        buf.seek(0)
        return buf, "captioned.gif"

    img = src.convert("RGBA")
    result = _caption_frame(img, caption_text)
    ext = "gif" if is_gif else "png"
    buf = BytesIO()
    result.save(buf, format="GIF" if is_gif else "PNG")
    buf.seek(0)
    return buf, f"captioned.{ext}"


def _caption_frame(img: Image.Image, caption_text: str) -> Image.Image:
    font_size = max(16, img.width // 10)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    padding = max(10, int(img.width * 0.04))
    max_w = img.width - padding * 2
    lines = []
    for word in caption_text.split():
        if lines and font.getbbox(lines[-1] + " " + word)[2] <= max_w:
            lines[-1] += " " + word
        else:
            lines.append(word)

    if not lines:
        lines = [caption_text]

    line_h = font.getbbox("Ag")[3]
    gap = max(2, line_h // 5)
    box_h = len(lines) * (line_h + gap) + padding * 2

    new_img = Image.new("RGBA", (img.width, img.height + box_h), (255, 255, 255, 255))
    new_img.paste(img, (0, box_h))

    draw = ImageDraw.Draw(new_img)
    y = padding
    for line in lines:
        w = font.getbbox(line)[2]
        draw.text(((new_img.width - w) // 2, y), line, fill="black", font=font)
        y += line_h + gap

    return new_img
class Fun(About, Corn):
    """Fun miscellaneous commands"""

    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self.process = psutil.Process()
        self.invite_url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
        )

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        """
        Play rock paper scissors against me!
        """
        await ctx.send(view=RPSView(ctx))

    @commands.command(name="monark")
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""

        await ctx.send(
            file=discord.File(
                rf"files/monark/monark{random.randint(1,3)}.png", "monark.png"
            )
        )

    @commands.command(name="merica", aliases=("cm",))
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    @commands.hybrid_command(name="caption")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def caption(self, ctx: Context, *, text: str):
        """Add a caption to an image."""

        converter = MediaConverter()
        image_url = ""
        caption_text = text

        # try first word(s) as image source, rest as caption
        parts = text.split(" ", 1)
        if len(parts) == 2:
            try:
                maybe_url = await converter.convert(ctx, parts[0])
                image_url = maybe_url
                caption_text = parts[1]
            except commands.BadArgument:
                pass

        # try first 2 words as image source
        if not image_url:
            parts = text.split(" ", 2)
            if len(parts) >= 2:
                try:
                    maybe_url = await converter.convert(ctx, " ".join(parts[:2]))
                    image_url = maybe_url
                    caption_text = " ".join(parts[2:])
                except commands.BadArgument:
                    pass

        # fallback: no image in text — use attachment/reply/recent
        if not image_url:
            try:
                image_url = await converter.convert(ctx, "")
            except commands.BadArgument:
                raise commands.BadArgument("No image found.")

        if not caption_text.strip():
            raise commands.BadArgument("You need to provide caption text.")
        async with ctx.typing():
            img_data = await to_image(ctx.session, image_url, bytes=True)
            buf, filename = await make_caption(img_data, caption_text)
            embed = discord.Embed(color=self.bot.embedcolor)
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(
                embed=embed,
                file=discord.File(buf, filename),
                reference=ctx.message.to_reference(fail_if_not_exists=False),
            )

    @commands.hybrid_command(name="invite", aliases=("join",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def invite(self, ctx: Context):
        """Sends a link to add me to a server."""

        await ctx.send(self.invite_url)

    @commands.hybrid_command(name="8ball")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def _8ball(
        self,
        ctx: Context,
        *,
        question: str = commands.param(
            displayed_name="question", description="What shall you ask?"
        ),
    ):
        """Ask the magic 8-ball a question.

        tony wanted this command"""
        answers = (
            "It is certain.",
            "It is decidedly so.",
            "Without a doubt.",
            "Yes definitely.",
            "You may rely on it.",
            "As I see it, yes.",
            "Most likely.",
            "Outlook good.",
            "Yes.",
            "Signs point to yes.",
            "Reply hazy, try again.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Concentrate and ask again.",
            "Don't count on it.",
            "My reply is no.",
            "My sources say no.",
            "Outlook not so good.",
            "Very doubtful.",
            "prolly",
            "prolly not",
            "cheese beast",
            "are you him?",
            "himmers bro",
            "ur mic is on btw",
            "u might be muted",
            "ur a beast",
            "ur him",
            "BANG",
            "sure man",
        )

        msg = random.choice(answers)
        if ctx.interaction:
            msg += f"\n-# {ctx.author.display_name} asked: *{question}*"

        await ctx.send(msg)

    @commands.command(name="wtp", hidden=True, enabled=False)
    async def wtp(self, ctx: Context):
        await ctx.typing()

        data = await dagpi(self.bot, ctx.message, "https://api.dagpi.xyz/data/wtp")

        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name="Who's that pokemon?")

        image = await to_image(ctx.session, data["question"])
        file = discord.File(fp=image, filename="pokemon.png")

        embed.set_image(url="attachment://pokemon.png")

        await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))

    @commands.hybrid_command(
        name="badapple",
        aliases=(
            "ba",
            "bad apple",
        ),
    )
    @commands.cooldown(1, 15, commands.BucketType.channel)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def badapple(self, ctx: Context):
        """Bad Apple!! feat.nomico"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    "files/videos/bad apple.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )

    @commands.command(
        name="quoteisifyouhaveaproblemwithmetextmeandifyoudonthavemynumberyoudontknowmewellenoughtohaveaproblemwithme",
        aliases=("QIIYHAPWMTMAIYDHMNYDKMWETHAPWM",),
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def QIIYHAPWMTMAIYDHMNYDKMWETHAPWM(self, ctx: Context):
        """if you have a problem with me text me and if you dont have my number you dont know me well enough to have a problem with me"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    "files/videos/QIIYHAPWMTMAIYDHMNYDKMWETHAPWM.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )


async def setup(bot: Fishie):
    await bot.add_cog(Fun(bot))

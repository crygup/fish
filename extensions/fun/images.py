from __future__ import annotations

import os
from io import BytesIO
from typing import TYPE_CHECKING, Optional

import discord
from discord import ui, MediaGalleryItem, app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from core import Cog
from utils import to_image, to_thread, MediaConverter

if TYPE_CHECKING:
    from extensions.context import Context


@to_thread
def make_caption(img_bytes: bytes, caption_text: str) -> tuple[BytesIO, str]:
    import subprocess, tempfile, json, os as _os

    try:
        src = Image.open(BytesIO(img_bytes))
    except Exception:
        tmp_path = tempfile.mktemp(suffix=".mp4")
        with open(tmp_path, "wb") as f:
            f.write(img_bytes)
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    tmp_path,
                ],
                capture_output=True,
                timeout=10,
                text=True,
            )
            info = json.loads(probe.stdout)
            vw = info["streams"][0]["width"]
            dummy = Image.new("RGBA", (vw, 1), (0, 0, 0, 0))
            overlay = _caption_frame(dummy, caption_text)
            overlay_path = tempfile.mktemp(suffix=".png")
            overlay.save(overlay_path)
            out_path = tempfile.mktemp(suffix=".mp4")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    tmp_path,
                    "-i",
                    overlay_path,
                    "-filter_complex",
                    "[0:v][1:v]overlay=0:0",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    out_path,
                ],
                capture_output=True,
                timeout=60,
            )
            with open(out_path, "rb") as f:
                result = f.read()
        finally:
            for p in (tmp_path, overlay_path, out_path):
                try:
                    _os.unlink(p)
                except:
                    pass
        return BytesIO(result), "captioned.mp4"

    is_gif = src.format == "GIF" or getattr(src, "n_frames", 1) > 1

    if is_gif and getattr(src, "n_frames", 1) > 1:
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
            buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
        )
        buf.seek(0)
        return buf, "captioned.gif"

    frame = _caption_frame(src.convert("RGBA"), caption_text)
    buf = BytesIO()
    frame.save(buf, format="PNG")
    buf.seek(0)
    return buf, "captioned.png"


def _caption_frame(img: Image.Image, caption_text: str) -> Image.Image:
    padding = max(10, int(img.width * 0.04))
    max_w = max(1, img.width - padding * 2)
    words = caption_text.split() or [""]

    def load_font(size: int):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size
            )
        except OSError:
            return ImageFont.load_default()

    font_size = max(12, min(96, img.width // 10))
    font = load_font(font_size)
    while font_size > 12 and any(font.getlength(word) > max_w for word in words):
        font_size -= 1
        font = load_font(font_size)

    wrapped_words = []
    for word in words:
        chunk = ""
        for character in word:
            candidate = chunk + character
            if chunk and font.getlength(candidate) > max_w:
                wrapped_words.append(chunk)
                chunk = character
            else:
                chunk = candidate
        wrapped_words.append(chunk)

    lines = []
    for word in wrapped_words:
        if lines and font.getlength(lines[-1] + " " + word) <= max_w:
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
        bbox = font.getbbox(line)
        w = bbox[2] - bbox[0]
        draw.text(
            ((new_img.width - w) // 2 - bbox[0], y),
            line,
            fill="black",
            font=font,
        )
        y += line_h + gap

    return new_img


@to_thread
def _speed_video(data: bytes, speed: float) -> bytes:
    import subprocess, tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(data)
        tmp_in = tmp.name
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_out = tmp.name
    af = f"atempo={speed}" if speed >= 0.5 else f"atempo=0.5,atempo={speed * 2}"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            tmp_in,
            "-filter_complex",
            f"[0:v]setpts={1/speed}*PTS[v];[0:a]{af}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            tmp_out,
        ],
        capture_output=True,
        timeout=60,
    )
    with open(tmp_out, "rb") as f:
        result = f.read()
    os.unlink(tmp_in)
    try:
        os.unlink(tmp_out)
    except:
        pass
    return result


class Images(Cog):
    """Image manipulation commands."""

    @commands.hybrid_command(name="caption")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        text="The caption text",
        media_url="An image/video URL or custom emoji",
        user="Use this user's avatar",
        attachment="Attach an image or video",
    )
    async def caption(
        self,
        ctx: Context,
        *,
        text: str = "",
        media_url: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ):
        """Add a caption to an image."""

        converter = MediaConverter()
        image_url = ""
        caption_text = text

        if media_url:
            try:
                image_url = await converter.convert(
                    ctx, media_url, include_message_media=False
                )
            except commands.BadArgument as error:
                raise commands.BadArgument("Invalid media URL or emoji.") from error
        elif user is not None:
            image_url = user.display_avatar.url
        elif attachment is not None:
            image_url = attachment.url

        if not image_url:
            parts = text.split(" ", 1)
            if len(parts) == 2:
                try:
                    maybe_url = await converter.convert(
                        ctx, parts[0], include_message_media=False
                    )
                    image_url = maybe_url
                    caption_text = parts[1]
                except commands.BadArgument:
                    pass

            if not image_url:
                parts = text.split(" ", 2)
                if len(parts) >= 2:
                    try:
                        maybe_url = await converter.convert(
                            ctx,
                            " ".join(parts[:2]),
                            include_message_media=False,
                        )
                        image_url = maybe_url
                        caption_text = " ".join(parts[2:])
                    except commands.BadArgument:
                        pass

        if not image_url:
            try:
                image_url = await converter.convert(ctx, "")
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "No image or video found. Attach one, reply to media, or use a media URL."
                ) from error

        async with ctx.typing():
            import time

            started = time.time()
            img_data = await to_image(ctx.session, image_url, bytes=True)
            buf, filename = await make_caption(img_data, caption_text)

            elapsed = time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"

            if filename.endswith(".mp4"):
                max_size = ctx.guild.filesize_limit if ctx.guild else 25 * 1024 * 1024
                buf.seek(0, 2)
                size = buf.tell()
                if size > 20 * 1024 * 1024:
                    import subprocess, tempfile

                    buf.seek(0)
                    with tempfile.NamedTemporaryFile(
                        suffix=".mp4", delete=False
                    ) as tmp:
                        tmp.write(buf.read())
                        tmp_path = tmp.name
                    out_path = tempfile.mktemp(suffix=".mp4")
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                tmp_path,
                                "-c:v",
                                "libx264",
                                "-crf",
                                "28",
                                "-preset",
                                "fast",
                                "-c:a",
                                "aac",
                                "-b:a",
                                "64k",
                                "-movflags",
                                "+faststart",
                                out_path,
                            ],
                            capture_output=True,
                            timeout=60,
                        )
                        with open(out_path, "rb") as f:
                            data = f.read()
                    finally:
                        os.unlink(tmp_path)
                        try:
                            os.unlink(out_path)
                        except:
                            pass
                    buf = BytesIO(data)
                buf.seek(0, 2)
                if buf.tell() > max_size:
                    buf.seek(0)
                    raise commands.BadArgument(
                        "The captioned video is too large for this server. Try a smaller video or use a server with a higher upload limit."
                    )
                buf.seek(0)
                gallery = ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
                container = ui.Container(
                    gallery,
                    ui.TextDisplay(info_text),
                    accent_color=self.bot.embedcolor,
                )
                view_type = type("CaptionVideoView", (ui.LayoutView,), {})
                v = view_type(timeout=None)
                v.add_item(container)
                await ctx.send(
                    file=discord.File(buf, filename),
                    view=v,
                    reference=ctx.message.to_reference(fail_if_not_exists=False),
                )
            else:
                embed = discord.Embed(color=self.bot.embedcolor)
                embed.description = info_text
                embed.set_image(url=f"attachment://{filename}")
                await ctx.send(
                    embed=embed,
                    file=discord.File(buf, filename),
                    reference=ctx.message.to_reference(fail_if_not_exists=False),
                )

    @commands.hybrid_command(name="speed")
    @app_commands.allowed_installs(guilds=True, users=True)
    async def speed(self, ctx: Context, *, input: str):
        """Change playback speed of a video or GIF. 2.0 = 2x, 0.5 = half speed."""

        # try parsing as "speed" first, fallback to "image speed"
        converter = MediaConverter()
        parts = input.split(" ", 1)
        speed_str = parts[0]
        image_str = ""

        try:
            speed_val = float(speed_str.strip())
        except ValueError:
            # first word isn't a number, treat it as an image source
            try:
                image_str = await converter.convert(ctx, speed_str)
            except commands.BadArgument:
                pass
            # remaining text might be the speed
            if len(parts) > 1:
                try:
                    speed_val = float(parts[1].strip())
                except ValueError:
                    raise commands.BadArgument(
                        "Speed must be a number like `2` or `0.5`."
                    )
            else:
                speed_val = 2.0

        if speed_val <= 0 or speed_val > 10:
            raise commands.BadArgument("Speed must be between 0.1 and 10.")

        if not image_str:
            try:
                image_str = await converter.convert(ctx, "")
            except commands.BadArgument:
                raise commands.BadArgument("No image or video found.")

        if not image_str or not isinstance(image_str, str):
            raise commands.BadArgument("Could not resolve an image or video source.")
        async with ctx.typing():
            import time as _time

            started = _time.time()
            img_data = await to_image(ctx.session, image_str, bytes=True)
            result = await _speed_video(img_data, speed_val)

            elapsed = _time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"

            max_size = ctx.guild.filesize_limit if ctx.guild else 25 * 1024 * 1024
            if len(result) > max_size:
                raise commands.BadArgument(
                    "The sped-up video is too large for this server."
                )

            buf = BytesIO(result)
            gallery = ui.MediaGallery(MediaGalleryItem(f"attachment://speed.mp4"))
            container = ui.Container(
                gallery,
                ui.TextDisplay(info_text),
                accent_color=self.bot.embedcolor,
            )
            view_type = type("SpeedView", (ui.LayoutView,), {})
            v = view_type(timeout=None)
            v.add_item(container)
            await ctx.send(
                file=discord.File(buf, "speed.mp4"),
                view=v,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
            )

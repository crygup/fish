from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional, cast

import discord
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from core import Cog
from utils import MediaConverter, to_image, to_thread

if TYPE_CHECKING:
    from extensions.context import Context

Image.MAX_IMAGE_PIXELS = 25_000_000


@to_thread
def make_caption(img_bytes: bytes, caption_text: str) -> tuple[BytesIO, str]:
    import json
    import os as _os
    import subprocess
    import tempfile

    try:
        src = Image.open(BytesIO(img_bytes))
    except UnidentifiedImageError:
        with tempfile.TemporaryDirectory(prefix="fishie-caption-") as temp_dir:
            tmp_path = _os.path.join(temp_dir, "input.mp4")
            overlay_path = _os.path.join(temp_dir, "overlay.png")
            out_path = _os.path.join(temp_dir, "output.mp4")
            with open(tmp_path, "wb") as f:
                f.write(img_bytes)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height:format=duration",
                    "-of",
                    "json",
                    tmp_path,
                ],
                capture_output=True,
                timeout=10,
                text=True,
                check=True,
            )
            info = json.loads(probe.stdout)
            vw = info["streams"][0]["width"]
            vh = info["streams"][0]["height"]
            duration = float(info.get("format", {}).get("duration") or 0)
            if vw > 4096 or vh > 4096 or duration > 60:
                raise ValueError("Videos are limited to 4096px and 60 seconds.")
            dummy = Image.new("RGBA", (vw, 1), (0, 0, 0, 0))
            overlay = _caption_frame(dummy, caption_text)
            overlay.save(overlay_path)
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
                    "-t",
                    "60",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    out_path,
                ],
                capture_output=True,
                timeout=60,
                check=True,
            )
            with open(out_path, "rb") as f:
                result = f.read()
        return BytesIO(result), "captioned.mp4"

    frame_pixels = src.width * src.height
    if frame_pixels > 25_000_000:
        raise ValueError("Images are limited to 25 million pixels per frame.")

    is_gif = src.format == "GIF" or getattr(src, "n_frames", 1) > 1

    if is_gif and getattr(src, "n_frames", 1) > 1:
        if frame_pixels * int(getattr(src, "n_frames", 1)) > 100_000_000:
            raise ValueError("Animated images are limited to 100 million total pixels.")
        frames = []
        durations = []
        try:
            while True:
                if len(frames) >= 300:
                    raise ValueError("Animated images are limited to 300 frames.")
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

    line_h = int(font.getbbox("Ag")[3])
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


def _atempo_filter(speed: float) -> str:
    remaining = speed
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


@to_thread
def _speed_video(data: bytes, speed: float) -> bytes:
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fishie-speed-") as temp_dir:
        tmp_in = os.path.join(temp_dir, "input.mp4")
        tmp_out = os.path.join(temp_dir, "output.mp4")
        with open(tmp_in, "wb") as tmp:
            tmp.write(data)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                tmp_in,
            ],
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
        )
        command = ["ffmpeg", "-y", "-i", tmp_in]
        if probe.stdout.strip():
            audio_filter = _atempo_filter(speed)
            command += [
                "-filter_complex",
                f"[0:v]setpts={1/speed}*PTS[v];[0:a]{audio_filter}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
            ]
        else:
            command += ["-filter:v", f"setpts={1/speed}*PTS", "-an"]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-movflags",
            "+faststart",
            tmp_out,
        ]
        subprocess.run(
            command,
            capture_output=True,
            timeout=60,
            check=True,
        )
        with open(tmp_out, "rb") as f:
            return f.read()


@to_thread
def _compress_video(data: bytes) -> bytes:
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fishie-compress-") as temp_dir:
        input_path = os.path.join(temp_dir, "input.mp4")
        output_path = os.path.join(temp_dir, "output.mp4")
        with open(input_path, "wb") as output:
            output.write(data)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
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
                output_path,
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        with open(output_path, "rb") as compressed:
            return compressed.read()


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

        if len(caption_text) > 1000:
            raise commands.BadArgument("Captions are limited to 1,000 characters.")

        async with self.bot.media_semaphore, ctx.typing():
            import time

            started = time.time()
            img_data = cast(bytes, await to_image(ctx.session, image_url, bytes=True))
            buf, filename = await make_caption(img_data, caption_text)

            elapsed = time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"

            if filename.endswith(".mp4"):
                max_size = ctx.guild.filesize_limit if ctx.guild else 25 * 1024 * 1024
                buf.seek(0, 2)
                size = buf.tell()
                if size > 20 * 1024 * 1024:
                    buf.seek(0)
                    data = await _compress_video(buf.read())
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

        if speed_val < 0.1 or speed_val > 10:
            raise commands.BadArgument("Speed must be between 0.1 and 10.")

        if not image_str:
            try:
                image_str = await converter.convert(ctx, "")
            except commands.BadArgument:
                raise commands.BadArgument("No image or video found.")

        if not image_str or not isinstance(image_str, str):
            raise commands.BadArgument("Could not resolve an image or video source.")
        async with self.bot.media_semaphore, ctx.typing():
            import time as _time

            started = _time.time()
            img_data = cast(bytes, await to_image(ctx.session, image_str, bytes=True))
            result = await _speed_video(img_data, speed_val)

            elapsed = _time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"

            max_size = ctx.guild.filesize_limit if ctx.guild else 25 * 1024 * 1024
            if len(result) > max_size:
                raise commands.BadArgument(
                    "The sped-up video is too large for this server."
                )

            buf = BytesIO(result)
            gallery = ui.MediaGallery(MediaGalleryItem("attachment://speed.mp4"))
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

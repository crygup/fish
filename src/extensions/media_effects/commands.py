from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import random as random_module
import re
import shlex
import time
from collections.abc import Mapping
from functools import lru_cache, wraps
from io import BytesIO
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, cast
from urllib.parse import urlsplit

import discord
import emoji as emoji_lib
import numpy as np
import pycountry
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands
from discord.http import Route
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

from core import Cog
from utils import (
    TEMP_MEDIA_MAX_BYTES,
    MediaConverter,
    SimplePages,
    TemporaryMediaError,
    fetch_public_bytes,
    to_thread,
    upload_temporary_media,
)
from utils.converters import TwemojiConverter
from utils.downloads import Downloader, is_downloadable_media_page
from utils.errors import DownloadError
from utils.rich_text import (
    draw_inline_tokens,
    inline_animation_duration,
    measure_inline_tokens,
    resolve_inline_images,
    text_font,
    wrap_inline_text,
)
from utils.unit_conversion import (
    ConversionError,
    convert_request,
    parse_conversion_expression,
)

from .audio_effects import (
    AudioEffect,
    audio_effect_catalog,
    audio_effect_choices,
    find_audio_effect,
)
from .fonts import font_names
from .image_assets import ImageAsset, image_asset_catalog
from .processing import (
    MAX_MEDIA_DURATION,
    PRIDE_FLAGS,
    AverageColor,
    EffectResult,
    NotPillowMedia,
    compress_media_to_size,
    convert_media,
    make_flag_asset,
    probe_media,
    render_average_colors,
    render_combine_effect,
    render_image_effect,
    render_overlay_effect,
    render_text_effect,
    render_video_effect,
)
from .video_assets import VideoAsset, video_asset_catalog

if TYPE_CHECKING:
    from core.bot import Fishie
    from extensions.context import Context

Image.MAX_IMAGE_PIXELS = 25_000_000

MEDIA_INPUT_DESCRIPTION = "User/Emoji/Media URL"
MEDIA_EFFECT_TIMEOUT = 60
MEDIA_APP_COMMAND_LIMIT = 7_600
MEDIA_APP_COMMAND_ROOT_OVERHEAD = 350
RANDOM_EFFECTS = (
    "blur",
    "brightness",
    "contrast",
    "deepfry",
    "distort",
    "exposure",
    "falsecolor",
    "fisheye",
    "flip",
    "glitch",
    "grain",
    "grayscale",
    "huerotate",
    "invert",
    "magik",
    "noise",
    "oilpaint",
    "parallax",
    "pixelate",
    "resize",
    "rotate",
    "saturation",
    "sepia",
    "sharpen",
    "swirl",
    "tint",
    "vignette",
    "watercolor",
    "zoom",
)
RANDOM_AUDIO_EFFECTS = (
    "adhd",
    "audiocompress",
    "audiodeepvoice",
    "audiodestroy",
    "audioecho",
    "audionightcore",
    "audiopitch",
    "audioreverb",
    "audioreverse",
    "audiosurround",
    "audiounderwater",
    "bassboost",
    "basslower",
    "soundeffect",
    "volume",
)
RANDOM_OVERLAY_EFFECTS = ("overlayflag", "overlay")
RANDOM_OVERLAY_SOURCE_KINDS = ("emoji", "user", "asset", "media", "flag")
OVERLAY_SOURCE_KIND_ALIASES = {
    "emoji": "emoji",
    "emojis": "emoji",
    "user": "user",
    "users": "user",
    "asset": "asset",
    "assets": "asset",
    "image": "asset",
    "images": "asset",
    "media": "media",
    "flag": "flag",
    "flags": "flag",
}
OVERLAY_RANDOM_WORDS = frozenset({"random", "rand"})
RANDOM_OVERLAY_POSITIONS = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)
PIPELINE_DISALLOWED_EFFECTS = frozenset({"enlarge"})
RANDOM_POSITIONAL_NUMBERS = {
    "at",
    "duration",
    "source_start",
    "source_stop",
    "start",
    "stop",
    "x",
    "y",
}
SPIN3D_SIZE = 512
SPIN3D_FRAMES = 37
SPIN3D_FRAME_DURATION = 20
SPIN3D_ANIMATED_SIZE = 384
SPIN3D_MAX_ANIMATION_DURATION = 10_000
SPIN3D_MAX_ANIMATION_FRAMES = 150
SPIN3D_MAX_DECODED_PIXELS = 150_000_000
GLOBE_SIZE = 384
GLOBE_ANIMATED_SIZE = 320
GLOBE_TEXTURE_SIZE = 512
SPIN3D_VALUE_FLAG_RE = re.compile(
    r"(?<!\S)--?(?P<name>tilt|t|zoom|z|speed|s)"
    r"(?:\s*=\s*|\s+)"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)",
    re.IGNORECASE,
)
SPIN3D_CLOCKWISE_FLAG_RE = re.compile(
    r"(?<!\S)--?(?:clockwise|c)(?=\s|$)",
    re.IGNORECASE,
)
SPIN3D_UNSET_VALUE_FLAG_RE = re.compile(
    r"(?<!\S)--?(?:tilt|t|zoom|z|speed|s)(?=\s|$)",
    re.IGNORECASE,
)


def _parse_overlay_selector(value: str) -> tuple[str, str | None] | None:
    """Parse an overlay source kind and optional name.

    The text overlay command accepts both the legacy ``random emoji`` form and
    the more descriptive ``emoji random``/``user name`` forms. A ``None``
    selector means that the kind should be chosen randomly.
    """
    try:
        tokens = shlex.split(value)
    except ValueError as error:
        raise commands.BadArgument(
            "The random overlay arguments contain an unmatched quote."
        ) from error
    if not tokens:
        return None
    first = tokens[0].casefold()
    if first in OVERLAY_RANDOM_WORDS:
        if len(tokens) == 1:
            return "all", None
        kind = OVERLAY_SOURCE_KIND_ALIASES.get(tokens[1].casefold())
        if kind is None:
            raise commands.BadArgument(
                "Random overlay must be emoji, user, or asset, media, or flag."
            )
        selector = " ".join(tokens[2:]).strip() or None
    else:
        kind = OVERLAY_SOURCE_KIND_ALIASES.get(first)
        if kind is None:
            return None
        selector = " ".join(tokens[1:]).strip() or None

    # ``image`` is an alias for the bundled asset pool. It is useful as a
    # readable spelling of ``asset random`` and should not require an asset
    # literally named "image".
    if kind == "asset" and selector is not None:
        normalized = selector.casefold()
        if normalized in OVERLAY_RANDOM_WORDS or normalized in {"image", "images"}:
            selector = None
    elif selector is not None and selector.casefold() in OVERLAY_RANDOM_WORDS:
        selector = None
    return kind, selector


def _random_overlay_kind(value: str) -> str | None:
    """Return an overlay source kind, or ``None`` for normal media."""
    parsed = _parse_overlay_selector(value)
    return parsed[0] if parsed is not None else None


def _random_overlay_label(value: object) -> str:
    """Return a readable name for a randomly selected emoji source."""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()

    text = str(value).strip()
    custom = re.fullmatch(r"<a?:(?P<name>[^:>]+):\d+>", text)
    if custom is not None:
        return custom.group("name")
    if text and not text.startswith(("http://", "https://")):
        demojized = emoji_lib.demojize(text)
        if demojized != text:
            return demojized.strip(":").replace("_", " ")
    return text


def _random_overlay_display(label: str) -> str:
    return f"overlay image ({label})"


def _random_overlay_note(label: str) -> str:
    return f"Applied: {_random_overlay_display(label)}"


@lru_cache(maxsize=1)
def _unicode_overlay_emojis() -> tuple[str, ...]:
    return tuple(
        value
        for value in emoji_lib.EMOJI_DATA
        if TwemojiConverter.is_unicode_emoji(value)
    )


GLOBE_SPEED_FLAG_RE = re.compile(
    r"(?<!\S)--?(?:speed|s)"
    r"(?:\s*=\s*|\s+)"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)",
    re.IGNORECASE,
)
GLOBE_CLOCKWISE_FLAG_RE = re.compile(
    r"(?<!\S)--?(?:clockwise|c)(?=\s|$)",
    re.IGNORECASE,
)
GLOBE_UNSET_SPEED_FLAG_RE = re.compile(
    r"(?<!\S)--?(?:speed|s)(?=\s|$)",
    re.IGNORECASE,
)
RESIZE_DIMENSIONS_RE = re.compile(r"^(?P<width>\d{1,4})[xX×](?P<height>\d{1,4})$")


def media_effect_timeout(callback: Any) -> Any:
    """Limit a complete media-effect command, including downloads, to 60 seconds."""

    @wraps(callback)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            async with asyncio.timeout(MEDIA_EFFECT_TIMEOUT):
                return await callback(*args, **kwargs)
        except TimeoutError as error:
            raise commands.BadArgument(
                "That effect took longer than 60 seconds. Try a shorter or smaller file."
            ) from error

    return wrapped


class _MediaAppCommandSizeTree:
    """Minimal tree interface needed to serialize a top-level app command."""

    class _Defaults:
        @staticmethod
        def _merge_to_array(_value: Any) -> list[int]:
            return []

    allowed_contexts = _Defaults()
    allowed_installs = _Defaults()


_MEDIA_APP_COMMAND_SIZE_TREE = _MediaAppCommandSizeTree()


class _RunProgress:
    """Post one unobtrusive progress message only when a run exceeds a minute."""

    def __init__(self, ctx: Context, total: int):
        self.ctx = ctx
        self.total = total
        self.step = 0
        self.label = "downloading media"
        self.changed = asyncio.Event()
        self.done = asyncio.Event()
        self.owner = asyncio.current_task()
        self.task = asyncio.create_task(self._report())
        self.task.add_done_callback(self._consume_result)
        if self.owner is not None:
            self.owner.add_done_callback(lambda _task: self.close())

    @staticmethod
    def _consume_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def update(self, step: int, label: str) -> None:
        self.step = step
        self.label = label
        self.changed.set()

    def close(self) -> None:
        self.done.set()
        self.changed.set()

    def _content(self) -> str:
        if self.step:
            return (
                f"Still working... Step **{self.step}/{self.total}**: "
                f"**{self.label}**"
            )
        return f"Still working... Preparing **{self.total}** expanded effect steps."

    async def _report(self) -> None:
        try:
            await asyncio.wait_for(self.done.wait(), timeout=60)
            return
        except TimeoutError:
            pass
        if self.owner is not None and self.owner.done():
            return

        interaction = getattr(self.ctx, "interaction", None)
        if interaction is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                message = await interaction.edit_original_response(
                    content=self._content()
                )
                self.ctx._previous_message = message
            except Exception:
                return

            last_content = self._content()
            while not self.done.is_set() and not (
                self.owner is not None and self.owner.done()
            ):
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=10)
                except TimeoutError:
                    pass
                if self.done.is_set():
                    break
                content = self._content()
                if content == last_content:
                    continue
                try:
                    message = await interaction.edit_original_response(content=content)
                    self.ctx._previous_message = message
                except Exception:
                    break
                last_content = content
            return

        try:
            message = await self.ctx.send_new(self._content())
        except Exception:
            return
        last_content = self._content()
        try:
            while not self.done.is_set() and not (
                self.owner is not None and self.owner.done()
            ):
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=10)
                except TimeoutError:
                    pass
                if self.done.is_set():
                    break
                content = self._content()
                if content != last_content:
                    try:
                        await message.edit(content=content)
                    except Exception:
                        break
                    last_content = content
        finally:
            try:
                await message.delete()
            except Exception:
                pass


def _random_effect_choices(count: int) -> list[str]:
    available = tuple(
        effect for effect in RANDOM_EFFECTS if effect not in PIPELINE_DISALLOWED_EFFECTS
    )
    if count > len(available):
        raise commands.BadArgument(
            "A pipeline can contain up to " f"{len(available)} unique random effects."
        )
    return random_module.SystemRandom().sample(available, count)


def _randomize_overlay_options(
    options: dict[str, Any],
    rng: random_module.Random | random_module.SystemRandom,
) -> None:
    """Choose a bounded overlay size and an in-canvas anchor position."""
    options["scale"] = round(rng.uniform(0.1, 1.0), 2)
    options.pop("size", None)
    options["position"] = rng.choice(RANDOM_OVERLAY_POSITIONS)
    # Named positions plus a scale no larger than the base keep every edge
    # inside the background. Random offsets would defeat that guarantee.
    options["x"] = 0
    options["y"] = 0
    options["stretch"] = False


def _randomize_effect_timing(
    effect: str,
    options: dict[str, Any],
    media_duration: float,
    rng: random_module.Random | random_module.SystemRandom,
) -> None:
    """Choose a valid partial-media window for a randomly selected effect."""
    bounds = EFFECT_NUMERIC_BOUNDS.get(effect, {})
    if "start" not in bounds or "stop" not in bounds or media_duration <= 0:
        return

    upper = min(media_duration, bounds["start"][1], bounds["stop"][1])
    if upper <= 0:
        return

    minimum_window = min(upper, max(0.05, upper * 0.1))
    start = round(rng.uniform(0, max(0.0, upper - minimum_window)), 3)
    options["start"] = start
    options.pop("duration", None)
    # Half of randomized effects continue to the end of the media. Omitting
    # stop is different from explicitly passing zero to timed renderers.
    if rng.random() < 0.5:
        options.pop("stop", None)
        return
    stop_minimum = min(upper, start + minimum_window)
    stop = rng.uniform(stop_minimum, upper) if stop_minimum < upper else upper
    options["stop"] = round(max(start + 0.001, stop), 3)


def _resolve_pipeline_random_effects(
    effects: list[tuple[str, dict[str, Any]]],
    *,
    allow_audio: bool = False,
    allow_visual: bool = True,
    media_duration: float = 0.0,
) -> list[tuple[str, str, dict[str, Any]]]:
    rng = random_module.SystemRandom()
    resolved: list[tuple[str, str, dict[str, Any]]] = []
    for effect, options in effects:
        if effect == "random":
            category = str(options.get("category", "all")).casefold()
            if category not in {"all", "visual", "audio", "overlay"}:
                raise commands.BadArgument(
                    "Random category must be all, visual, audio, or overlay."
                )
            if category == "audio":
                if not allow_audio:
                    raise commands.BadArgument(
                        "Random audio needs media with an audio track."
                    )
                pool = RANDOM_AUDIO_EFFECTS
            elif category == "overlay":
                if not allow_visual:
                    raise commands.BadArgument(
                        "Random overlay needs media with a video or image track."
                    )
                pool = RANDOM_OVERLAY_EFFECTS
            elif category == "visual":
                if not allow_visual:
                    raise commands.BadArgument(
                        "Random visual needs media with a video or image track."
                    )
                pool = RANDOM_EFFECTS
            else:
                if allow_visual and allow_audio:
                    pool = (*RANDOM_EFFECTS, *RANDOM_AUDIO_EFFECTS)
                elif allow_visual:
                    pool = RANDOM_EFFECTS
                elif allow_audio:
                    pool = RANDOM_AUDIO_EFFECTS
                else:
                    raise commands.BadArgument(
                        "No random effects are compatible with that media."
                    )
            pool = tuple(
                effect for effect in pool if effect not in PIPELINE_DISALLOWED_EFFECTS
            )
            if not pool:
                raise commands.BadArgument(
                    "No random effects are available for that category."
                )
            chosen = rng.choice(pool)
            chosen_options = {
                name: default
                for name, (_, _, default) in PIPELINE_EFFECTS[chosen][1].items()
            }
            no_random = bool(options.get("norandom"))
            full_random = bool(options.get("fullrandom"))
            if chosen == "soundeffect":
                chosen_options.update(
                    {
                        "effect": "random",
                        "at": 0.0,
                        "random_time": not no_random,
                        "pitch": 0.0,
                        "speed": 1.0,
                        "volume": 1.0,
                    }
                )
                if full_random:
                    chosen_options["random_time"] = True
                    chosen_options["pitch"] = round(rng.uniform(-12, 12), 2)
                    chosen_options["speed"] = round(rng.uniform(0.5, 2), 2)
                label_category = "" if category == "all" else f" {category}"
                resolved.append(
                    (chosen, f"random{label_category} ({chosen})", chosen_options)
                )
                continue
            if not no_random:
                for name, bounds in EFFECT_NUMERIC_BOUNDS.get(chosen, {}).items():
                    if name in RANDOM_POSITIONAL_NUMBERS:
                        continue
                    minimum, maximum, integer = bounds
                    value: float | int
                    if integer:
                        value = rng.randint(int(minimum), int(maximum))
                    else:
                        value = rng.uniform(minimum, maximum)
                        value = round(value, 2)
                    chosen_options[name] = value
            if full_random:
                for name, choices_for_option in EFFECT_CATEGORICAL_CHOICES.get(
                    chosen, {}
                ).items():
                    chosen_options[name] = rng.choice(choices_for_option)
                for name in PIPELINE_EFFECTS[chosen][2]:
                    chosen_options[name] = bool(rng.getrandbits(1))
                if chosen in {"audiooverlay", "soundeffect"}:
                    chosen_options["source_stop"] = 0.0
            if chosen in {"overlay", "overlayflag"} and (full_random or not no_random):
                _randomize_overlay_options(chosen_options, rng)
            explicit_timing = {
                name: options[name]
                for name in ("start", "stop", "duration")
                if name in options
            }
            if explicit_timing:
                chosen_options.update(explicit_timing)
            elif not no_random:
                _randomize_effect_timing(
                    chosen,
                    chosen_options,
                    media_duration,
                    rng,
                )
            selected_flag: str | None = None
            if category == "overlay":
                if chosen == "overlayflag":
                    selected_flag = rng.choice(
                        EFFECT_CATEGORICAL_CHOICES["overlayflag"]["flag"]
                    )
                    chosen_options["flag"] = selected_flag
                elif chosen == "overlay":
                    chosen_options["overlay"] = "random"
            label_category = "" if category == "all" else f" {category}"
            if (
                chosen == "overlayflag"
                and category == "overlay"
                and selected_flag is not None
            ):
                label = f"random{label_category} (overlayflag: {selected_flag})"
            else:
                label = f"random{label_category} ({chosen})"
            resolved.append((chosen, label, chosen_options))
        else:
            resolved.append((effect, effect, options))
    return resolved


CAPTION_GIF_FILTER = (
    "fps=10,scale=480:-1:flags=lanczos,"
    "split[s0][s1];"
    "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
    "[s1][p]paletteuse=dither=bayer:bayer_scale=5"
)


def _is_klipy_media_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return hostname in {"klipy.com", "www.klipy.com", "static.klipy.com"}


async def _finalize_pipeline_result(
    result: EffectResult,
    media_url: str,
) -> EffectResult:
    """Convert Klipy's working video to one compatible GIF after all effects."""
    if not _is_klipy_media_url(media_url) or result.filename.endswith(".gif"):
        return result
    converted = await convert_media(result.data, "gif", 1)
    filename = f"{result.filename.rsplit('.', 1)[0]}.gif"
    return EffectResult(converted.data, filename, converted.displayable)


async def refresh_discord_attachment_url(bot: Fishie, url: str) -> str:
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    if hostname not in {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }:
        return url
    original = url.split("?", 1)[0]
    try:
        response = await bot.http.request(
            Route("POST", "/attachments/refresh-urls"),
            json={"attachment_urls": [original]},
        )
        refreshed = response.get("refreshed_urls", [])
        if refreshed and refreshed[0].get("refreshed"):
            return str(refreshed[0]["refreshed"])
    except (discord.HTTPException, KeyError, TypeError):
        # Fresh signed links work without a refresh. If Discord rejects a
        # non-attachment URL, the bounded fetch still validates the original.
        pass
    return url


def _parse_spin3d_input(argument: str) -> tuple[str, float, float, float, bool]:
    tilt = 15.0
    zoom = 1.5
    speed = 1.0

    for match in SPIN3D_VALUE_FLAG_RE.finditer(argument):
        name = match.group("name").lower()
        value = float(match.group("value"))
        if name in {"tilt", "t"}:
            tilt = value
        elif name in {"zoom", "z"}:
            zoom = value
        else:
            speed = value

    media = SPIN3D_VALUE_FLAG_RE.sub(" ", argument)
    clockwise = bool(SPIN3D_CLOCKWISE_FLAG_RE.search(media))
    media = SPIN3D_CLOCKWISE_FLAG_RE.sub(" ", media)
    if SPIN3D_UNSET_VALUE_FLAG_RE.search(media):
        raise ValueError("The tilt, zoom, and speed flags require a number.")

    return " ".join(media.split()), tilt, zoom, speed, clockwise


def _parse_globe_input(argument: str) -> tuple[str, float, bool]:
    speed = 1.0
    speed_match = GLOBE_SPEED_FLAG_RE.search(argument)
    if speed_match is not None:
        speed = float(speed_match.group("value"))

    media = GLOBE_SPEED_FLAG_RE.sub(" ", argument)
    clockwise = bool(GLOBE_CLOCKWISE_FLAG_RE.search(media))
    media = GLOBE_CLOCKWISE_FLAG_RE.sub(" ", media)
    if GLOBE_UNSET_SPEED_FLAG_RE.search(media):
        raise ValueError("The speed flag requires a number.")
    return " ".join(media.split()), speed, clockwise


def _spin3d_timing(speed: float) -> tuple[int, int]:
    target_duration = SPIN3D_FRAMES * SPIN3D_FRAME_DURATION / speed
    frame_count = max(12, min(74, round(SPIN3D_FRAMES / speed)))
    frame_duration = max(10, round(target_duration / frame_count / 10) * 10)
    return frame_count, frame_duration


def _spin3d_animation_timing(total_duration: int) -> tuple[list[int], list[int]]:
    minimum_interval = 20 if total_duration <= 200 else 40
    capped_interval = math.ceil(total_duration / SPIN3D_MAX_ANIMATION_FRAMES / 10) * 10
    interval = max(minimum_interval, capped_interval)
    timestamps = list(range(0, total_duration, interval))
    durations = [
        max(10, min(interval, total_duration - timestamp)) for timestamp in timestamps
    ]
    return timestamps, durations


def _spin3d_animation_rotations(total_duration: int, speed: float) -> int:
    normal_rotation_duration = SPIN3D_FRAMES * SPIN3D_FRAME_DURATION
    requested_rotations = total_duration * speed / normal_rotation_duration
    return max(1, round(requested_rotations))


def _spin3d_source_timeline(opened: Image.Image) -> tuple[list[int], int]:
    frame_starts: list[int] = []
    total_duration = 0
    decoded_pixels = 0
    frame_pixels = opened.width * opened.height
    frame_total = getattr(opened, "n_frames", 1)

    for frame_index in range(frame_total):
        decoded_pixels += frame_pixels
        if decoded_pixels > SPIN3D_MAX_DECODED_PIXELS:
            raise ValueError("That GIF has too many frames or is too large.")

        opened.seek(frame_index)
        duration = max(20, int(opened.info.get("duration") or 100))
        remaining = SPIN3D_MAX_ANIMATION_DURATION - total_duration
        if remaining <= 0:
            break

        frame_starts.append(total_duration)
        total_duration += min(duration, remaining)

    opened.seek(0)
    return frame_starts, total_duration


def _perspective_coefficients(
    destination: list[tuple[float, float]],
    source: list[tuple[float, float]],
) -> tuple[float, ...]:
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        values.append(u)
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.append(v)
    try:
        coefficients = np.linalg.solve(
            np.asarray(matrix, dtype=float),
            np.asarray(values, dtype=float),
        )
    except np.linalg.LinAlgError as error:
        raise ValueError("That tilt produced an invalid perspective.") from error
    return tuple(float(value) for value in coefficients)


def _spin3d_frame(
    source: Image.Image,
    angle_degrees: float,
    tilt_degrees: float,
    zoom: float,
) -> Image.Image:
    size = source.width
    center = size / 2
    angle = math.radians(angle_degrees)
    tilt = math.radians(tilt_degrees)
    cosine_tilt = math.cos(tilt)
    if abs(cosine_tilt) < 1e-4:
        cosine_tilt = math.copysign(1e-4, cosine_tilt or 1)
    sine_tilt = math.sin(tilt)

    camera_distance = 4.0
    zoom_scale = 2 ** ((zoom - 1.5) / 2.5)
    radius = center * 0.93 * zoom_scale
    destination: list[tuple[float, float]] = []
    for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        tilted_y = y * cosine_tilt
        tilted_z = y * sine_tilt
        rotated_x = x * math.cos(angle) + tilted_z * math.sin(angle)
        rotated_z = -x * math.sin(angle) + tilted_z * math.cos(angle)
        perspective = camera_distance / (camera_distance - rotated_z)
        destination.append(
            (
                center + rotated_x * radius * perspective,
                center + tilted_y * radius * perspective,
            )
        )

    source_corners = [
        (0.0, 0.0),
        (float(size), 0.0),
        (float(size), float(size)),
        (0.0, float(size)),
    ]
    coefficients = _perspective_coefficients(destination, source_corners)
    frame = source.transform(
        (size, size),
        Image.Transform.PERSPECTIVE,
        coefficients,
        Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(destination, fill=255)
    frame.putalpha(ImageChops.multiply(frame.getchannel("A"), mask))
    return frame


def _encode_spin3d(
    frames: list[Image.Image],
    frame_duration: int | list[int] = SPIN3D_FRAME_DURATION,
) -> BytesIO:
    output = BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration,
        loop=0,
        disposal=2,
        optimize=False,
    )
    output.seek(0)
    return output


def _prepare_spin3d_source(
    image: Image.Image,
    size: int = SPIN3D_SIZE,
) -> Image.Image:
    image = ImageOps.contain(
        image,
        (size, size),
        Image.Resampling.LANCZOS,
    )
    source = Image.new(
        "RGBA",
        (size, size),
        (0, 0, 0, 0),
    )
    source.alpha_composite(
        image,
        (
            (size - image.width) // 2,
            (size - image.height) // 2,
        ),
    )
    return source


def _prepare_globe_texture(image: Image.Image) -> Image.Image:
    return image.convert("RGBA").resize(
        (GLOBE_TEXTURE_SIZE, GLOBE_TEXTURE_SIZE),
        Image.Resampling.LANCZOS,
    )


@lru_cache(maxsize=4)
def _globe_projection(
    size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    radius = size * 0.46
    coordinates = (np.arange(size, dtype=np.float32) + 0.5 - size / 2) / radius
    x, y = np.meshgrid(coordinates, coordinates)
    radius_squared = x * x + y * y
    mask = radius_squared <= 1
    z = np.sqrt(np.clip(1 - radius_squared, 0, 1))
    longitude = np.arctan2(x, z)
    latitude = np.arcsin(np.clip(-y, -1, 1))
    u = longitude / (2 * math.pi) + 0.5
    v = 0.5 - latitude / math.pi

    light_x, light_y, light_z = -0.35, -0.3, 0.89
    light_length = math.sqrt(light_x**2 + light_y**2 + light_z**2)
    light = (
        x * (light_x / light_length)
        + (-y) * (light_y / light_length)
        + z * (light_z / light_length)
    )
    shading = 0.52 + 0.48 * np.clip(light, 0, 1)
    edge_alpha = np.clip(
        (1 - np.sqrt(np.clip(radius_squared, 0, 1))) * radius,
        0,
        1,
    )
    return mask, u, v, shading, edge_alpha


def _globe_frame(
    texture: Image.Image,
    angle_degrees: float,
    size: int,
) -> Image.Image:
    mask, base_u, v, shading, edge_alpha = _globe_projection(size)
    pixels = np.asarray(texture.convert("RGBA"), dtype=np.float32)
    texture_height, texture_width = pixels.shape[:2]

    u_values = np.mod(base_u[mask] + angle_degrees / 360, 1)
    v_values = np.clip(v[mask], 0, 1)
    source_x = u_values * texture_width
    source_y = v_values * (texture_height - 1)
    x0 = np.floor(source_x).astype(np.int32) % texture_width
    x1 = (x0 + 1) % texture_width
    y0 = np.floor(source_y).astype(np.int32)
    y1 = np.minimum(y0 + 1, texture_height - 1)
    x_weight = (source_x - np.floor(source_x))[:, None]
    y_weight = (source_y - y0)[:, None]

    top = pixels[y0, x0] * (1 - x_weight) + pixels[y0, x1] * x_weight
    bottom = pixels[y1, x0] * (1 - x_weight) + pixels[y1, x1] * x_weight
    sampled = top * (1 - y_weight) + bottom * y_weight
    sampled[:, :3] *= shading[mask, None]
    sampled[:, 3] *= edge_alpha[mask]

    output = np.zeros((size, size, 4), dtype=np.uint8)
    output[mask] = np.clip(sampled, 0, 255).astype(np.uint8)
    return Image.fromarray(output, "RGBA")


def _render_animated_spin3d(
    opened: Image.Image,
    tilt: float,
    zoom: float,
    speed: float,
    clockwise: bool,
) -> tuple[list[Image.Image], list[int]]:
    frame_starts, total_duration = _spin3d_source_timeline(opened)
    timestamps, durations = _spin3d_animation_timing(total_duration)
    direction = -1 if clockwise else 1
    rotations = _spin3d_animation_rotations(total_duration, speed)
    source_index = 0
    prepared_index = -1
    prepared_source: Image.Image | None = None
    frames: list[Image.Image] = []

    for timestamp in timestamps:
        while (
            source_index + 1 < len(frame_starts)
            and frame_starts[source_index + 1] <= timestamp
        ):
            source_index += 1

        if source_index != prepared_index:
            opened.seek(source_index)
            prepared_source = _prepare_spin3d_source(
                opened.convert("RGBA"),
                SPIN3D_ANIMATED_SIZE,
            )
            prepared_index = source_index

        if prepared_source is None:
            raise ValueError("That GIF does not contain any usable frames.")

        angle = direction * timestamp * 360 * rotations / total_duration
        frames.append(_spin3d_frame(prepared_source, angle, tilt, zoom))

    return frames, durations


def _render_animated_globe(
    opened: Image.Image,
    speed: float,
    clockwise: bool,
) -> tuple[list[Image.Image], list[int]]:
    frame_starts, total_duration = _spin3d_source_timeline(opened)
    timestamps, durations = _spin3d_animation_timing(total_duration)
    rotations = _spin3d_animation_rotations(total_duration, speed)
    direction = -1 if clockwise else 1
    source_index = 0
    prepared_index = -1
    texture: Image.Image | None = None
    frames: list[Image.Image] = []

    for timestamp in timestamps:
        while (
            source_index + 1 < len(frame_starts)
            and frame_starts[source_index + 1] <= timestamp
        ):
            source_index += 1

        if source_index != prepared_index:
            opened.seek(source_index)
            texture = _prepare_globe_texture(opened.convert("RGBA"))
            prepared_index = source_index

        if texture is None:
            raise ValueError("That GIF does not contain any usable frames.")

        angle = direction * timestamp * 360 * rotations / total_duration
        frames.append(_globe_frame(texture, angle, GLOBE_ANIMATED_SIZE))

    return frames, durations


@to_thread
def make_spin3d(
    image_bytes: bytes,
    tilt: float,
    zoom: float,
    speed: float,
    clockwise: bool,
    max_bytes: int,
) -> BytesIO:
    try:
        opened = Image.open(BytesIO(image_bytes))
    except UnidentifiedImageError as error:
        raise ValueError("Spin3D currently requires an image or GIF.") from error

    if opened.width * opened.height > 25_000_000:
        raise ValueError("Images are limited to 25 million pixels.")

    frame_count = int(getattr(opened, "n_frames", 1))
    if getattr(opened, "is_animated", False) and frame_count > 1:
        frames, frame_duration = _render_animated_spin3d(
            opened,
            tilt,
            zoom,
            speed,
            clockwise,
        )
    else:
        opened.seek(0)
        image = ImageOps.exif_transpose(opened).convert("RGBA")
        source = _prepare_spin3d_source(image)
        frame_count, frame_duration = _spin3d_timing(speed)
        direction = -1 if clockwise else 1
        frames = [
            _spin3d_frame(
                source,
                direction * frame_index * 360 / frame_count,
                tilt,
                zoom,
            )
            for frame_index in range(frame_count)
        ]

    output = _encode_spin3d(frames, frame_duration)
    if output.getbuffer().nbytes <= max_bytes:
        return output

    for size in (448, 384, 320, 256):
        if size >= frames[0].width:
            continue
        resized = [
            frame.resize((size, size), Image.Resampling.LANCZOS) for frame in frames
        ]
        output = _encode_spin3d(resized, frame_duration)
        if output.getbuffer().nbytes <= max_bytes:
            return output

    raise ValueError("The Spin3D GIF is too large for this server.")


@to_thread
def make_globe(
    image_bytes: bytes,
    speed: float,
    clockwise: bool,
    max_bytes: int,
) -> BytesIO:
    try:
        opened = Image.open(BytesIO(image_bytes))
    except UnidentifiedImageError as error:
        raise ValueError("Globe currently requires an image or GIF.") from error

    if opened.width * opened.height > 25_000_000:
        raise ValueError("Images are limited to 25 million pixels.")

    frame_count = int(getattr(opened, "n_frames", 1))
    if getattr(opened, "is_animated", False) and frame_count > 1:
        frames, frame_duration = _render_animated_globe(
            opened,
            speed,
            clockwise,
        )
    else:
        opened.seek(0)
        texture = _prepare_globe_texture(ImageOps.exif_transpose(opened))
        frame_count, frame_duration = _spin3d_timing(speed)
        direction = -1 if clockwise else 1
        frames = [
            _globe_frame(
                texture,
                direction * frame_index * 360 / frame_count,
                GLOBE_SIZE,
            )
            for frame_index in range(frame_count)
        ]

    output = _encode_spin3d(frames, frame_duration)
    if output.getbuffer().nbytes <= max_bytes:
        return output

    for size in (320, 256, 192):
        if size >= frames[0].width:
            continue
        resized = [
            frame.resize((size, size), Image.Resampling.LANCZOS) for frame in frames
        ]
        output = _encode_spin3d(resized, frame_duration)
        if output.getbuffer().nbytes <= max_bytes:
            return output

    raise ValueError("The globe GIF is too large for this server.")


@to_thread
def make_caption(
    img_bytes: bytes,
    caption_text: str,
    inline_images: dict[str, bytes] | None = None,
    *,
    force_gif: bool = False,
) -> tuple[BytesIO, str]:
    import json
    import os as _os
    import subprocess
    import tempfile

    try:
        src = Image.open(BytesIO(img_bytes))
    except UnidentifiedImageError:
        with tempfile.TemporaryDirectory(prefix="fishie-caption-") as temp_dir:
            tmp_path = _os.path.join(temp_dir, "input.mp4")
            animated_caption = inline_animation_duration(inline_images or {})
            overlay_path = _os.path.join(
                temp_dir,
                "overlay.gif" if animated_caption else "overlay.png",
            )
            out_path = _os.path.join(temp_dir, "output.mp4")
            gif_path = _os.path.join(temp_dir, "output.gif")
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
            if animated_caption:
                frame_duration = 50
                overlay_frames = [
                    _caption_panel(
                        vw,
                        vh,
                        caption_text,
                        inline_images or {},
                        timestamp_ms,
                    )
                    for timestamp_ms in range(0, animated_caption, frame_duration)
                ]
                overlay_frames[0].save(
                    overlay_path,
                    format="GIF",
                    save_all=True,
                    append_images=overlay_frames[1:],
                    duration=frame_duration,
                    loop=0,
                    disposal=2,
                )
            else:
                overlay = _caption_panel(
                    vw,
                    vh,
                    caption_text,
                    inline_images or {},
                )
                overlay.save(overlay_path)
            caption_height = (
                overlay_frames[0].height if animated_caption else overlay.height
            )
            command = ["ffmpeg", "-y", "-i", tmp_path]
            if animated_caption:
                command.extend(["-stream_loop", "-1"])
            command.extend(
                [
                    "-i",
                    overlay_path,
                    "-filter_complex",
                    (
                        f"[0:v]pad=iw:ih+{caption_height}:0:{caption_height}:"
                        "color=white[base];"
                        "[base][1:v]overlay=0:0:eof_action=repeat[v]"
                    ),
                    "-map",
                    "[v]",
                    "-map",
                    "0:a?",
                    "-t",
                    str(min(60, duration) if duration > 0 else 60),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    out_path,
                ]
            )
            subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                check=True,
            )
            final_path = out_path
            filename = "captioned.mp4"
            if force_gif:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        out_path,
                        "-filter_complex",
                        CAPTION_GIF_FILTER,
                        gif_path,
                    ],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                final_path = gif_path
                filename = "captioned.gif"
            with open(final_path, "rb") as f:
                result = f.read()
        return BytesIO(result), filename

    frame_pixels = src.width * src.height
    if frame_pixels > 25_000_000:
        raise ValueError("Images are limited to 25 million pixels per frame.")

    is_gif = src.format == "GIF" or getattr(src, "n_frames", 1) > 1

    if is_gif and getattr(src, "n_frames", 1) > 1:
        if frame_pixels * int(getattr(src, "n_frames", 1)) > 100_000_000:
            raise ValueError("Animated images are limited to 100 million total pixels.")
        frames = []
        durations = []
        timestamp_ms = 0
        try:
            while True:
                if len(frames) >= 300:
                    raise ValueError("Animated images are limited to 300 frames.")
                frame = src.convert("RGBA")
                duration = max(20, int(src.info.get("duration") or 100))
                durations.append(duration)
                frames.append(
                    _caption_frame(
                        frame,
                        caption_text,
                        inline_images or {},
                        timestamp_ms,
                    )
                )
                timestamp_ms += duration
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

    animated_caption = inline_animation_duration(inline_images or {})
    if animated_caption:
        frame_duration = 50
        source = src.convert("RGBA")
        frames = [
            _caption_frame(
                source,
                caption_text,
                inline_images or {},
                timestamp_ms,
            )
            for timestamp_ms in range(0, animated_caption, frame_duration)
        ]
        buf = BytesIO()
        frames[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration,
            loop=0,
            disposal=2,
        )
        buf.seek(0)
        return buf, "captioned.gif"

    frame = _caption_frame(src.convert("RGBA"), caption_text, inline_images or {})
    buf = BytesIO()
    frame.save(buf, format="PNG")
    buf.seek(0)
    return buf, "captioned.png"


def _caption_frame(
    img: Image.Image,
    caption_text: str,
    inline_images: dict[str, bytes] | None = None,
    timestamp_ms: int = 0,
) -> Image.Image:
    panel = _caption_panel(
        img.width,
        img.height,
        caption_text,
        inline_images,
        timestamp_ms,
    )
    new_img = Image.new(
        "RGBA",
        (img.width, img.height + panel.height),
        (255, 255, 255, 255),
    )
    new_img.alpha_composite(panel, (0, 0))
    new_img.alpha_composite(img.convert("RGBA"), (0, panel.height))
    return new_img


def _caption_panel(
    width: int,
    media_height: int,
    caption_text: str,
    inline_images: dict[str, bytes] | None = None,
    timestamp_ms: int = 0,
) -> Image.Image:
    inline_images = inline_images or {}
    padding = max(8, int(width * 0.035))
    max_w = max(1, width - padding * 2)
    max_panel_height = max(
        48,
        min(
            round(max(1, media_height) * 0.45),
            round(width * 0.45),
        ),
    )

    def layout(size: int) -> tuple[Any, list[Any], int, int, int]:
        selected_font = text_font(caption_text, size)
        selected_lines = wrap_inline_text(
            caption_text,
            selected_font,
            size,
            inline_images,
            max_w,
        )
        font_box = selected_font.getbbox("Ag")
        selected_line_h = int(max(size, font_box[3] - font_box[1]))
        selected_gap = max(2, selected_line_h // 5)
        selected_height = (
            len(selected_lines) * selected_line_h
            + max(0, len(selected_lines) - 1) * selected_gap
            + padding * 2
        )
        return (
            selected_font,
            selected_lines,
            selected_line_h,
            selected_gap,
            selected_height,
        )

    minimum_size = 12
    maximum_size = max(minimum_size, min(96, width // 10))
    low = minimum_size
    high = maximum_size
    font, lines, line_h, gap, box_h = layout(minimum_size)
    while low <= high:
        candidate = (low + high) // 2
        candidate_layout = layout(candidate)
        if candidate_layout[-1] <= max_panel_height:
            font_size = candidate
            font, lines, line_h, gap, box_h = candidate_layout
            low = candidate + 1
        else:
            high = candidate - 1
    font_size = int(getattr(font, "size", minimum_size))

    maximum_lines = max(1, (max_panel_height - padding * 2 + gap) // (line_h + gap))
    if len(lines) > maximum_lines:
        lines = lines[:maximum_lines]
        ellipsis = "…"
        while lines[-1] and (
            measure_inline_tokens(lines[-1], font, font_size, inline_images)
            + font.getlength(ellipsis)
            > max_w
        ):
            lines[-1] = lines[-1][:-1]
        lines[-1] = [
            *lines[-1],
            *wrap_inline_text(ellipsis, font, font_size, inline_images, max_w)[0],
        ]
    box_h = len(lines) * line_h + max(0, len(lines) - 1) * gap + padding * 2
    if box_h % 2:
        box_h += 1

    panel = Image.new("RGBA", (width, box_h), (255, 255, 255, 255))

    y = padding
    for line in lines:
        line_width = measure_inline_tokens(line, font, font_size, inline_images)
        draw_inline_tokens(
            panel,
            line,
            (round((panel.width - line_width) / 2), y),
            font=font,
            image_size=font_size,
            assets=inline_images,
            timestamp_ms=timestamp_ms,
            fill="black",
        )
        y += line_h + gap

    return panel


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


def _playback_factor(speed: float) -> float:
    if speed == 0 or abs(speed) < 1 or abs(speed) > 5:
        raise ValueError("Speed must be from -5 to -1 or from 1 to 5.")
    return speed if speed > 0 else 1 / abs(speed)


def _speed_video_sync(
    data: bytes,
    speed: float,
    *,
    start: float = 0.0,
    stop: float = 0.0,
) -> bytes:
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
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                tmp_in,
            ],
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
        )
        try:
            metadata = json.loads(probe.stdout or "{}")
        except json.JSONDecodeError:
            metadata = {}
        streams = metadata.get("streams") or []
        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        if not has_video:
            if has_audio:
                return _speed_audio_sync(data, speed, start=start, stop=stop)
            raise ValueError("Speed requires video, GIF, or audio media.")

        factor = _playback_factor(speed)
        try:
            total_duration = float((metadata.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            total_duration = 0.0
        if not 0 <= start <= MAX_MEDIA_DURATION or not 0 <= stop <= MAX_MEDIA_DURATION:
            raise ValueError(
                f"Start and stop must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
            )
        end = stop or total_duration or None
        if end is not None and end <= start:
            raise ValueError("Stop must be after start.")
        if total_duration > 0:
            if start >= total_duration:
                raise ValueError("Start must be before the end of the media.")
            end = min(end or total_duration, total_duration)

        command = ["ffmpeg", "-y", "-i", tmp_in]
        full_media = start <= 0 and (
            end is None or total_duration <= 0 or end >= total_duration - 0.001
        )
        even_scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        if full_media:
            filters = [f"[0:v]setpts=(PTS-STARTPTS)/{factor:g},{even_scale}[v]"]
            if has_audio:
                filters.append(f"[0:a]{_atempo_filter(factor)}[a]")
        else:
            segments: list[tuple[float, float | None, bool]] = []
            if start > 0:
                segments.append((0.0, start, False))
            segments.append((start, end, True))
            if end is not None and (
                total_duration <= 0 or end < total_duration - 0.001
            ):
                segments.append((end, None, False))

            video_inputs = "".join(f"[video{index}]" for index in range(len(segments)))
            filters = [f"[0:v]split={len(segments)}{video_inputs}"]
            if has_audio:
                audio_inputs = "".join(
                    f"[audio{index}]" for index in range(len(segments))
                )
                filters.append(f"[0:a]asplit={len(segments)}{audio_inputs}")
            video_outputs: list[str] = []
            audio_outputs: list[str] = []
            for index, (segment_start, segment_end, changed) in enumerate(segments):
                trim = f"trim=start={segment_start:g}"
                atrim = f"atrim=start={segment_start:g}"
                if segment_end is not None:
                    trim += f":end={segment_end:g}"
                    atrim += f":end={segment_end:g}"
                video_pts = f"(PTS-STARTPTS)/{factor:g}" if changed else "PTS-STARTPTS"
                filters.append(
                    f"[video{index}]{trim},setpts={video_pts},{even_scale}[v{index}]"
                )
                video_outputs.append(f"[v{index}]")
                if has_audio:
                    audio_chain = f"[audio{index}]{atrim},asetpts=PTS-STARTPTS"
                    if changed:
                        audio_chain += f",{_atempo_filter(factor)}"
                    filters.append(f"{audio_chain}[a{index}]")
                    audio_outputs.append(f"[a{index}]")
            filters.append(
                f"{''.join(video_outputs)}concat=n={len(segments)}:v=1:a=0[v]"
            )
            if has_audio:
                filters.append(
                    f"{''.join(audio_outputs)}concat=n={len(segments)}:v=0:a=1[a]"
                )

        command.extend(["-filter_complex", ";".join(filters), "-map", "[v]"])
        if has_audio:
            command.extend(["-map", "[a]", "-c:a", "aac", "-b:a", "64k"])
        else:
            command.append("-an")
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            tmp_out,
        ]
        try:
            subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                check=True,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                "That speed change took longer than 30 seconds. "
                "Try a shorter or smaller file."
            ) from error
        except subprocess.CalledProcessError as error:
            lines = error.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = lines[-1] if lines else "ffmpeg could not process that file."
            raise ValueError(
                f"Could not change that media speed: {detail[:300]}"
            ) from error
        with open(tmp_out, "rb") as f:
            return f.read()


_speed_video = to_thread(_speed_video_sync)


def _speed_audio_sync(
    data: bytes,
    speed: float,
    *,
    start: float = 0.0,
    stop: float = 0.0,
) -> bytes:
    """Change the speed of an audio-only file while preserving untouched ranges."""
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fishie-speed-audio-") as temp_dir:
        tmp_in = os.path.join(temp_dir, "input.media")
        tmp_out = os.path.join(temp_dir, "output.mp3")
        with open(tmp_in, "wb") as tmp:
            tmp.write(data)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                tmp_in,
            ],
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
        )
        try:
            metadata = json.loads(probe.stdout or "{}")
        except json.JSONDecodeError:
            metadata = {}
        streams = metadata.get("streams") or []
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValueError("That file does not contain an audio track.")
        try:
            total_duration = float((metadata.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            total_duration = 0.0
        if not 0 <= start <= MAX_MEDIA_DURATION or not 0 <= stop <= MAX_MEDIA_DURATION:
            raise ValueError(
                f"Start and stop must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
            )
        end = stop or total_duration or None
        if end is not None and end <= start:
            raise ValueError("Stop must be after start.")
        if total_duration > 0:
            if start >= total_duration:
                raise ValueError("Start must be before the end of the media.")
            end = min(end or total_duration, total_duration)

        full_media = start <= 0 and (
            end is None or total_duration <= 0 or end >= total_duration - 0.001
        )
        command = ["ffmpeg", "-y", "-i", tmp_in]
        if full_media:
            command.extend(["-af", _atempo_filter(_playback_factor(speed)), "-vn"])
        else:
            factor = _playback_factor(speed)
            segments: list[tuple[float, float | None, bool]] = []
            if start > 0:
                segments.append((0.0, start, False))
            segments.append((start, end, True))
            if end is not None and (
                total_duration <= 0 or end < total_duration - 0.001
            ):
                segments.append((end, None, False))
            labels = "".join(f"[audio{index}]" for index in range(len(segments)))
            filters = [f"[0:a]asplit={len(segments)}{labels}"]
            outputs: list[str] = []
            for index, (segment_start, segment_end, changed) in enumerate(segments):
                trim = f"[audio{index}]atrim=start={segment_start:g}"
                if segment_end is not None:
                    trim += f":end={segment_end:g}"
                chain = f"{trim},asetpts=PTS-STARTPTS"
                if changed:
                    chain += f",{_atempo_filter(factor)}"
                chain += ",aresample=44100,aformat=sample_fmts=fltp"
                label = f"[segment{index}]"
                filters.append(f"{chain}{label}")
                outputs.append(label)
            filters.append(f"{''.join(outputs)}concat=n={len(outputs)}:v=0:a=1[a]")
            command.extend(["-filter_complex", ";".join(filters), "-map", "[a]"])
            command.append("-vn")
        command.extend(["-c:a", "libmp3lame", "-q:a", "2", tmp_out])
        try:
            subprocess.run(command, capture_output=True, timeout=30, check=True)
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                "That speed change took longer than 30 seconds. "
                "Try a shorter or smaller file."
            ) from error
        except subprocess.CalledProcessError as error:
            lines = error.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = lines[-1] if lines else "ffmpeg could not process that file."
            raise ValueError(
                f"Could not change that audio speed: {detail[:300]}"
            ) from error
        with open(tmp_out, "rb") as output:
            return output.read()


_speed_audio = to_thread(_speed_audio_sync)


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
            timeout=30,
            check=True,
        )
        with open(output_path, "rb") as compressed:
            return compressed.read()


def _parse_effect_flags(
    argument: str,
    *,
    values: dict[str, tuple[tuple[str, ...], Callable[[str], Any], Any]] | None = None,
    switches: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Parse lightweight text-command flags while leaving the media argument."""

    value_specs = dict(values or {})
    # Randomization controls are valid for every media-effect segment. Most
    # effects simply carry these through without changing their deterministic
    # renderer, while random/pipeline effects consume them. Keep the implicit
    # controls out of parsed options when they were not supplied so existing
    # command handlers retain their compact defaults.
    switch_specs = dict(switches or {})
    implicit_switches = {
        name for name in ("norandom", "fullrandom") if name not in switch_specs
    }
    switch_specs.setdefault("norandom", ("nr",))
    switch_specs.setdefault("fullrandom", ("fr",))
    existing_names = {
        name.casefold().lstrip("-")
        for canonical, (names, _, _) in value_specs.items()
        for name in (canonical, *names)
    }
    implicit_timing: set[str] = set()
    if "start" not in existing_names:
        value_specs["start"] = (("from",), float, 0.0)
        implicit_timing.add("start")
    if "stop" not in existing_names:
        value_specs["stop"] = (("end",), float, 0.0)
        implicit_timing.add("stop")
    aliases: dict[str, tuple[str, Callable[[str], Any]]] = {}
    for canonical, (names, converter, _) in value_specs.items():
        for name in (canonical, *names):
            aliases[name.casefold().lstrip("-")] = (canonical, converter)
    switch_aliases: dict[str, str] = {}
    for canonical, names in switch_specs.items():
        for name in (canonical, *names):
            switch_aliases[name.casefold().lstrip("-")] = canonical

    try:
        tokens = shlex.split(argument)
    except ValueError as error:
        raise commands.BadArgument(
            "The effect arguments contain an unmatched quote."
        ) from error

    options = {canonical: default for canonical, (_, _, default) in value_specs.items()}
    options.update({canonical: False for canonical in switch_specs})
    supplied_switches: set[str] = set()
    media: list[str] = []
    supplied_values: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-" and index + 1 < len(tokens):
            separated_name = tokens[index + 1].casefold().lstrip("-")
            if separated_name in aliases or separated_name in switch_aliases:
                index += 1
                token = f"-{tokens[index]}"
        if not token.startswith("-") or token == "-":
            media.append(token)
            index += 1
            continue

        raw_name, separator, inline_value = token.lstrip("-").partition("=")
        normalized = raw_name.casefold()
        if normalized in switch_aliases:
            options[switch_aliases[normalized]] = True
            supplied_switches.add(switch_aliases[normalized])
            index += 1
            continue
        spec = aliases.get(normalized)
        if spec is None:
            media.append(token)
            index += 1
            continue

        canonical, converter = spec
        if separator:
            raw_value = inline_value
        else:
            index += 1
            if index >= len(tokens):
                raise commands.BadArgument(f"The `{token}` flag requires a value.")
            raw_value = tokens[index]
        if canonical == "overlay" and not separator:
            # Overlay selectors can contain a category and a name, including
            # country names with spaces. Keep consuming non-flag tokens while
            # the combined value is still a valid selector.
            selector_tokens = [raw_value]
            while index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                candidate = " ".join((*selector_tokens, tokens[index + 1]))
                if _parse_overlay_selector(candidate) is None:
                    break
                selector_tokens.append(tokens[index + 1])
                index += 1
            raw_value = " ".join(selector_tokens)
        try:
            options[canonical] = converter(raw_value)
        except (TypeError, ValueError) as error:
            raise commands.BadArgument(
                f"The `{token}` flag has an invalid value."
            ) from error
        supplied_values.add(canonical)
        index += 1

    for name in implicit_timing - supplied_values:
        options.pop(name, None)
    for name in implicit_switches - supplied_switches:
        options.pop(name, None)
    return " ".join(media), options


def _parse_bool(value: str) -> bool:
    normalized = value.casefold().strip()
    if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    raise ValueError("Expected true or false.")


PipelineFlagValues = dict[str, tuple[tuple[str, ...], Callable[[str], Any], Any]]
PipelineFlagSwitches = dict[str, tuple[str, ...]]
PipelineEffectSpec = tuple[str, PipelineFlagValues, PipelineFlagSwitches]

AUDIO_TIMING_VALUES: PipelineFlagValues = {
    "start": (("from",), float, 0.0),
    "stop": (("end",), float, 0.0),
    "duration": (("length", "d"), float, 0.0),
}


def _audio_values(
    **values: tuple[tuple[str, ...], Callable[[str], Any], Any],
) -> PipelineFlagValues:
    return {**values, **AUDIO_TIMING_VALUES}


PIPELINE_EFFECTS: dict[str, PipelineEffectSpec] = {
    "invert": (
        "image",
        {},
        {
            "preserve_transparency": (
                "preserve-transparency",
                "preserve",
                "transparent",
                "keep-alpha",
                "pt",
            )
        },
    ),
    "flip": (
        "image",
        {"direction": (("dir", "d"), str, "horizontal")},
        {},
    ),
    "blur": (
        "image",
        {
            "radius": (("r", "strength"), float, 5.0),
            "blur_type": (("type", "mode", "t"), str, "gaussian"),
        },
        {},
    ),
    "deepfry": (
        "image",
        {"intensity": (("amount", "i"), float, 1.0)},
        {
            "preserve_transparency": (
                "preserve-transparency",
                "preserve",
                "transparent",
                "keep-alpha",
                "pt",
            )
        },
    ),
    "grayscale": (
        "image",
        {},
        {
            "preserve_transparency": (
                "preserve-transparency",
                "preserve",
                "transparent",
                "keep-alpha",
                "pt",
            )
        },
    ),
    "jpeg": ("image", {"quality": (("q",), int, 8)}, {}),
    "spin": (
        "image",
        {"speed": (("s",), float, 1.0)},
        {"clockwise": ("c",)},
    ),
    "magik": (
        "image",
        {"strength": (("amount", "s"), float, 20.0)},
        {},
    ),
    "gifmagik": (
        "image",
        {
            "strength": (("amount",), float, 20.0),
            "speed": (("s",), float, 1.0),
        },
        {},
    ),
    "cube": (
        "image",
        {"speed": (("s",), float, 1.0)},
        {"clockwise": ("c",)},
    ),
    "pyramid": (
        "image",
        {"speed": (("s",), float, 1.0)},
        {"clockwise": ("c",)},
    ),
    "crop": ("image", {"shape": (("s",), str, "circle")}, {}),
    "mirror": (
        "image",
        {"direction": (("dir", "d"), str, "bottom")},
        {},
    ),
    "swirl": (
        "image",
        {"strength": (("degrees", "s"), float, 180.0)},
        {},
    ),
    "gifswirl": (
        "image",
        {
            "strength": (("degrees",), float, 180.0),
            "speed": (("s",), float, 1.0),
        },
        {},
    ),
    "wiggle": (
        "image",
        {
            "amount": (("strength",), float, 8.0),
            "speed": (("s",), float, 1.0),
        },
        {},
    ),
    "fadein": (
        "image",
        {"duration": (("d",), float, 2.0)},
        {},
    ),
    "fadeout": (
        "image",
        {"duration": (("d",), float, 2.0)},
        {},
    ),
    "lag": (
        "image",
        {
            "amount": (("frames", "a"), int, 4),
            "method": (("type", "m"), str, "random"),
        },
        {"multi": ("multiple", "combine")},
    ),
    "shuffle": ("image", {}, {}),
    "tint": (
        "image",
        {
            "color": (("c",), str, "#5865f2"),
            "amount": (("strength", "a"), float, 0.35),
        },
        {},
    ),
    "implode": (
        "image",
        {"strength": (("amount", "s"), float, 0.5)},
        {},
    ),
    "explode": (
        "image",
        {"strength": (("amount", "s"), float, 0.5)},
        {},
    ),
    "sharpen": (
        "image",
        {"amount": (("strength", "a"), float, 2.0)},
        {},
    ),
    "legoify": ("image", {"size": (("block", "s"), int, 12)}, {}),
    "bounce": (
        "image",
        {
            "amount": (("height", "a"), float, 20.0),
            "speed": (("s",), float, 1.0),
        },
        {},
    ),
    "fisheye": (
        "image",
        {"strength": (("amount", "s"), float, 0.65)},
        {},
    ),
    "sepia": ("image", {"amount": (("strength", "a"), float, 1.0)}, {}),
    "pixelate": ("image", {"size": (("block", "s"), int, 12)}, {}),
    "slidein": (
        "image",
        {
            "direction": (("dir",), str, "left"),
            "duration": (("d",), float, 1.0),
        },
        {},
    ),
    "slideout": (
        "image",
        {
            "direction": (("dir",), str, "left"),
            "duration": (("d",), float, 1.0),
        },
        {},
    ),
    "vignette": (
        "image",
        {"amount": (("strength", "a"), float, 0.65)},
        {},
    ),
    "resize": (
        "image",
        {
            "scale": (("s",), float, 1.0),
            "ratio": (("aspect", "r"), str, ""),
            "size": (("dimensions", "dim"), str, ""),
        },
        {},
    ),
    "text": (
        "special",
        {
            "text": (("t", "content"), str, ""),
            "font": (("f",), str, "Roboto"),
            "size": (("fontsize", "font-size"), int, 48),
            "position": (("pos", "p"), str, "center"),
            "x": (("left",), int, -1),
            "y": (("top",), int, -1),
            "color": (("colour", "c"), str, "#ffffff"),
            "style": (("mode",), str, "outline"),
            "stroke_color": (("stroke-color", "outline-color"), str, "#000000"),
            "stroke_width": (("stroke-width", "outline-width"), int, 0),
            "shadow_color": (("shadow-color",), str, "#000000"),
            "background": (("background-color", "bg"), str, ""),
            "background_opacity": (("background-opacity", "bg-opacity"), int, 160),
            "opacity": (("alpha",), float, 1.0),
            "align": (("alignment",), str, "center"),
            "padding": (("pad",), int, 8),
        },
        {"bold": ("b",)},
    ),
    "combine": (
        "special",
        {
            "second": (("with", "media2", "second-media"), str, ""),
            "position": (("pos", "p"), str, "right"),
            "mode": (("size-mode", "sizing"), str, "resize"),
            "audio": (("audio-mode",), str, "mix"),
        },
        {},
    ),
    "distort": (
        "image",
        {"amount": (("strength", "a"), float, 0.25)},
        {},
    ),
    "grain": ("image", {"amount": (("strength", "a"), float, 20.0)}, {}),
    "rotate": ("image", {"degrees": (("angle", "d"), float, 90.0)}, {}),
    "noise": ("image", {"amount": (("strength", "a"), float, 20.0)}, {}),
    "brightness": ("image", {"amount": (("value", "a"), float, 1.0)}, {}),
    "contrast": ("image", {"amount": (("value", "a"), float, 1.0)}, {}),
    "saturation": ("image", {"amount": (("value", "a"), float, 1.0)}, {}),
    "exposure": ("image", {"stops": (("amount", "s"), float, 0.0)}, {}),
    "hallway": ("image", {"speed": (("s",), float, 1.0)}, {}),
    "parallax": ("image", {"speed": (("s",), float, 1.0)}, {}),
    "huerotate": ("image", {"degrees": (("angle", "d"), float, 180.0)}, {}),
    "zoom": (
        "image",
        {"amount": (("factor", "z"), float, 2.0)},
        {"forever": ("forever", "infinite", "loop")},
    ),
    "squishy": ("image", {"amount": (("strength", "a"), float, 0.18)}, {}),
    "glitch": ("image", {"amount": (("strength", "a"), float, 12.0)}, {}),
    "tremble": ("image", {"amount": (("strength", "a"), float, 8.0)}, {}),
    "quilt": ("image", {"tiles": (("grid", "t"), int, 3)}, {}),
    "removebars": ("image", {}, {}),
    "removecaption": ("image", {}, {}),
    "removeoutrotiktok": ("video", {}, {}),
    "removeoutroreels": ("video", {}, {}),
    "enlarge": ("image", {"amount": (("factor", "s"), float, 2.0)}, {}),
    "falsecolor": ("image", {}, {}),
    "watercolor": ("image", {}, {}),
    "oilpaint": ("image", {}, {}),
    "meme": ("special", {"text": (("t",), str, "")}, {}),
    "random": (
        "image",
        {"category": (("type",), str, "all")},
        {
            "norandom": ("nr",),
            "fullrandom": ("fr",),
        },
    ),
    "caption": ("special", {"text": (("t",), str, "")}, {}),
    "convert": ("special", {"format": (("f",), str, "mp4")}, {}),
    "globe": (
        "special",
        {"speed": (("s",), float, 1.0)},
        {"clockwise": ("c",)},
    ),
    "overlayflag": (
        "special",
        {
            "flag": (("f",), str, "pride"),
            "opacity": (("o",), float, 35.0),
        },
        {},
    ),
    "overlay": (
        "special",
        {
            "overlay": (("second", "o"), str, ""),
            "opacity": (("alpha",), float, 70.0),
            "scale": (("s",), float, 1.0),
            "size": (("dimensions", "dim"), str, ""),
            "position": (("pos", "p"), str, "center"),
            "x": (("left",), int, 0),
            "y": (("top",), int, 0),
            "start": (("from",), float, 0.0),
            "stop": (("end",), float, 0.0),
        },
        {
            "stretch": ("fill",),
            "extend": ("extend",),
            "no_audio": ("no-audio", "mute-audio"),
        },
    ),
    "speed": (
        "special",
        {
            "speed": (("s",), float, 2.0),
            "start": (("from",), float, 0.0),
            "stop": (("end",), float, 0.0),
        },
        {},
    ),
    "spin3d": (
        "special",
        {
            "tilt": (("t",), float, 15.0),
            "zoom": (("z",), float, 1.5),
            "speed": (("s",), float, 1.0),
        },
        {"clockwise": ("c",)},
    ),
    "reverse": ("video", {}, {}),
    "volume": (
        "video",
        _audio_values(volume=(("amount", "v"), float, 1.0)),
        {},
    ),
    "bassboost": (
        "video",
        _audio_values(gain=(("amount", "g"), float, 12.0)),
        {},
    ),
    "basslower": (
        "video",
        _audio_values(gain=(("amount", "g"), float, 12.0)),
        {},
    ),
    "audioreverse": ("video", _audio_values(), {}),
    "audioreverb": (
        "video",
        _audio_values(room=(("amount", "r"), float, 0.5)),
        {},
    ),
    "extract": ("video", {}, {}),
    "audioreplace": (
        "special",
        {"audio": (("second", "a"), str, "")},
        {},
    ),
    "audiooverlay": (
        "special",
        {
            "audio": (("second", "a"), str, ""),
            "at": (("time",), float, 0.0),
            "source_start": (("start", "source-start", "trim-start"), float, 0.0),
            "source_stop": (
                ("stop", "source-stop", "cutoff", "trim-stop"),
                float,
                0.0,
            ),
            "duration": (("length", "d"), float, 0.0),
            "volume": (("v",), float, 1.0),
            "pitch": (("semitones", "p"), float, 0.0),
        },
        {"random_time": ("random-time", "rt")},
    ),
    "soundeffect": (
        "special",
        {
            "effect": (("id", "name", "e"), str, "random"),
            "at": (("time",), float, 0.0),
            "source_start": (("start", "source-start", "trim-start"), float, 0.0),
            "source_stop": (
                ("stop", "source-stop", "cutoff", "trim-stop"),
                float,
                0.0,
            ),
            "duration": (("length", "d"), float, 0.0),
            "volume": (("v",), float, 1.0),
            "pitch": (("semitones", "p"), float, 0.0),
            "speed": (("tempo", "s"), float, 1.0),
            "random_time": (("random-time", "rt"), _parse_bool, True),
            "fade_in": (("fade-in", "fadein"), float, 0.0),
            "fade_out": (("fade-out", "fadeout"), float, 0.0),
        },
        {
            "loop": ("repeat",),
            "norandom": ("nr",),
            "fullrandom": ("fr",),
        },
    ),
    "adhd": ("video", {}, {}),
    "audiodestroy": (
        "video",
        _audio_values(amount=(("strength", "a"), int, 6)),
        {},
    ),
    "audiocompress": (
        "video",
        _audio_values(ratio=(("r",), float, 4.0)),
        {},
    ),
    "channelscombine": ("video", _audio_values(), {}),
    "audiopitch": (
        "video",
        _audio_values(semitones=(("amount", "s"), float, 3.0)),
        {},
    ),
    "audiounderwater": ("video", _audio_values(), {}),
    "audionightcore": ("video", _audio_values(), {}),
    "audiodeepvoice": ("video", _audio_values(), {}),
    "audiosurround": ("video", _audio_values(), {}),
    "audioecho": ("video", _audio_values(), {}),
}

PIPELINE_EFFECT_ALIASES = {
    "amagik": "gifmagik",
    "animatedmagik": "gifmagik",
    "gmagik": "gifmagik",
    "aswirl": "gifswirl",
    "animatedswirl": "gifswirl",
    "gswirl": "gifswirl",
    "fade-in": "fadein",
    "fade-out": "fadeout",
    "slide-in": "slidein",
    "slide-out": "slideout",
    "greyscale": "grayscale",
    "legofy": "legoify",
    "size": "resize",
    "addtext": "text",
    "rotation": "rotate",
    "bassreduce": "basslower",
    "audiochannelscombine": "channelscombine",
    "audiomono": "channelscombine",
    "sound-effect": "soundeffect",
    "sound_effect": "soundeffect",
    "sfx": "soundeffect",
    "audio-overlay": "audiooverlay",
    "audio_overlay": "audiooverlay",
    "hue": "huerotate",
    "hueshift": "huerotate",
    "remove-bars": "removebars",
    "remove-caption": "removecaption",
    "remove-outro-tiktok": "removeoutrotiktok",
    "remove-outro-reels": "removeoutroreels",
    "outro-tiktok": "removeoutrotiktok",
    "outro-reels": "removeoutroreels",
    "deep-voice": "audiodeepvoice",
    "underwater": "audiounderwater",
    "nightcore": "audionightcore",
    "deepvoice": "audiodeepvoice",
    "surround": "audiosurround",
    "echo": "audioecho",
    "pitch": "audiopitch",
    "overlayimage": "overlay",
    "overlayvideo": "overlay",
}

NumericBounds = tuple[float, float, bool]

# Numeric options are normalized once at the command boundary. The final bool
# marks integer-only values. Keeping the ranges here gives text commands, app
# commands, random pipelines, and API callers the same behavior.
EFFECT_NUMERIC_BOUNDS: dict[str, dict[str, NumericBounds]] = {
    "blur": {"radius": (0.1, 50, False)},
    "deepfry": {"intensity": (0.25, 3, False)},
    "jpeg": {"quality": (1, 50, True)},
    "spin": {"speed": (0.25, 4, False)},
    "magik": {"strength": (1, 80, False)},
    "gifmagik": {
        "strength": (1, 80, False),
        "speed": (0.25, 4, False),
    },
    "cube": {"speed": (0.25, 4, False)},
    "pyramid": {"speed": (0.25, 4, False)},
    "swirl": {"strength": (-720, 720, False)},
    "gifswirl": {
        "strength": (-720, 720, False),
        "speed": (0.25, 4, False),
    },
    "wiggle": {
        "amount": (1, 30, False),
        "speed": (0.25, 4, False),
    },
    "fadein": {"duration": (0.1, 10, False)},
    "fadeout": {"duration": (0.1, 10, False)},
    "lag": {"amount": (2, 12, True)},
    "tint": {"amount": (0, 1, False)},
    "implode": {"strength": (0, 1, False)},
    "explode": {"strength": (0, 1, False)},
    "sharpen": {"amount": (0, 5, False)},
    "legoify": {"size": (3, 64, True)},
    "bounce": {
        "amount": (1, 100, False),
        "speed": (0.25, 4, False),
    },
    "fisheye": {"strength": (0, 1, False)},
    "sepia": {"amount": (0, 1, False)},
    "pixelate": {"size": (2, 128, True)},
    "slidein": {"duration": (0.1, 10, False)},
    "slideout": {"duration": (0.1, 10, False)},
    "vignette": {"amount": (0, 1, False)},
    "resize": {"scale": (0.1, 4, False)},
    "text": {
        "size": (8, 512, True),
        "x": (-1, 4096, True),
        "y": (-1, 4096, True),
        "stroke_width": (0, 32, True),
        "background_opacity": (0, 255, True),
        "opacity": (0, 1, False),
        "padding": (0, 256, True),
    },
    "distort": {"amount": (-1, 1, False)},
    "grain": {"amount": (0, 100, False)},
    "rotate": {"degrees": (-3600, 3600, False)},
    "noise": {"amount": (0, 100, False)},
    "brightness": {"amount": (0, 4, False)},
    "contrast": {"amount": (0, 4, False)},
    "saturation": {"amount": (0, 4, False)},
    "exposure": {"stops": (-5, 5, False)},
    "hallway": {"speed": (0.25, 4, False)},
    "parallax": {"speed": (0.25, 4, False)},
    "huerotate": {"degrees": (-3600, 3600, False)},
    "zoom": {"amount": (1, 4, False)},
    "squishy": {"amount": (0, 1, False)},
    "glitch": {"amount": (0, 100, False)},
    "tremble": {"amount": (1, 30, False)},
    "quilt": {"tiles": (2, 12, True)},
    "enlarge": {"amount": (1, 4, False)},
    "globe": {"speed": (0.25, 3, False)},
    "overlayflag": {
        "opacity": (0, 100, False),
        "start": (0, MAX_MEDIA_DURATION, False),
        "stop": (0, MAX_MEDIA_DURATION, False),
    },
    "overlay": {
        "opacity": (0, 100, False),
        "scale": (0.05, 2, False),
        "x": (-4096, 4096, True),
        "y": (-4096, 4096, True),
        "start": (0, MAX_MEDIA_DURATION, False),
        "stop": (0, MAX_MEDIA_DURATION, False),
    },
    "speed": {
        "speed": (-5, 5, False),
        "start": (0, MAX_MEDIA_DURATION, False),
        "stop": (0, MAX_MEDIA_DURATION, False),
    },
    "spin3d": {
        "tilt": (-360, 360, False),
        "zoom": (-3, 3, False),
        "speed": (0.25, 3, False),
    },
    "volume": {"volume": (0, 10, False)},
    "bassboost": {"gain": (1, 30, False)},
    "basslower": {"gain": (1, 30, False)},
    "audioreverb": {"room": (0.1, 1, False)},
    "audiodestroy": {"amount": (2, 12, True)},
    "audiocompress": {"ratio": (1, 20, False)},
    "audiopitch": {"semitones": (-12, 12, False)},
    "audiooverlay": {
        "at": (0, MAX_MEDIA_DURATION, False),
        "source_start": (0, MAX_MEDIA_DURATION, False),
        "source_stop": (0, MAX_MEDIA_DURATION, False),
        "duration": (0, MAX_MEDIA_DURATION, False),
        "volume": (0, 5, False),
        "pitch": (-12, 12, False),
    },
    "soundeffect": {
        "at": (0, MAX_MEDIA_DURATION, False),
        "source_start": (0, MAX_MEDIA_DURATION, False),
        "source_stop": (0, MAX_MEDIA_DURATION, False),
        "duration": (0, MAX_MEDIA_DURATION, False),
        "volume": (0, 5, False),
        "pitch": (-12, 12, False),
        "speed": (0.25, 4, False),
        "fade_in": (0, 30, False),
        "fade_out": (0, 30, False),
    },
}

AUDIO_TIMED_EFFECTS = {
    "volume",
    "bassboost",
    "basslower",
    "audioreverse",
    "audioreverb",
    "audiodestroy",
    "audiocompress",
    "channelscombine",
    "audiopitch",
    "audiounderwater",
    "audionightcore",
    "audiodeepvoice",
    "audiosurround",
    "audioecho",
}
for _timed_effect in AUDIO_TIMED_EFFECTS:
    EFFECT_NUMERIC_BOUNDS.setdefault(_timed_effect, {}).update(
        {
            "start": (0, MAX_MEDIA_DURATION, False),
            "stop": (0, MAX_MEDIA_DURATION, False),
            "duration": (0, MAX_MEDIA_DURATION, False),
        }
    )

VIDEO_TIMED_VISUAL_EFFECTS = {
    effect for effect, (engine, _, _) in PIPELINE_EFFECTS.items() if engine == "image"
}
for _timed_effect in VIDEO_TIMED_VISUAL_EFFECTS:
    EFFECT_NUMERIC_BOUNDS.setdefault(_timed_effect, {}).update(
        {
            "start": (0, MAX_MEDIA_DURATION, False),
            "stop": (0, MAX_MEDIA_DURATION, False),
        }
    )

EFFECT_CATEGORICAL_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "blur": {"blur_type": ("gaussian", "box", "motion")},
    "flip": {"direction": ("horizontal", "vertical")},
    "mirror": {"direction": ("top", "bottom", "left", "right")},
    "crop": {"shape": ("circle", "triangle")},
    "slidein": {"direction": ("left", "right", "top", "bottom")},
    "slideout": {"direction": ("left", "right", "top", "bottom")},
    "lag": {"method": ("random", "freeze", "stutter", "drop", "jitter")},
    "overlayflag": {
        "flag": tuple(
            dict.fromkeys(
                name
                for name in PRIDE_FLAGS
                if name not in {"rainbow", "gay", "bi", "nb", "ace", "aro"}
            )
        )
    },
    "soundeffect": {
        "effect": tuple(str(effect.id) for effect in audio_effect_catalog())
    },
    "combine": {
        "position": ("top", "bottom", "left", "right"),
        "mode": ("resize", "stretch", "original"),
        "audio": ("mix", "first", "second", "none"),
    },
}


def _format_numeric(value: float | int) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


async def _sound_effect_autocomplete(
    _: discord.Interaction[Any],
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=effect.label[:100], value=str(effect.id))
        for effect in audio_effect_choices(current)[:24]
    ]
    if not current or "random".startswith(current.casefold()):
        choices.insert(0, app_commands.Choice(name="Random", value="random"))
    return choices[:25]


async def _font_autocomplete(
    _: discord.Interaction[Any],
    current: str,
) -> list[app_commands.Choice[str]]:
    query = current.casefold().strip()
    return [
        app_commands.Choice(name=name, value=name)
        for name in font_names()
        if not query or query in name.casefold()
    ][:25]


def _normalize_effect_options(
    effect: str,
    options: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    normalized = options.copy()
    adjustments: list[str] = []
    for name, (minimum, maximum, integer) in EFFECT_NUMERIC_BOUNDS.get(
        effect, {}
    ).items():
        if name not in normalized:
            continue
        original = normalized[name]
        try:
            numeric = float(original)
        except (TypeError, ValueError):
            continue
        clamped = min(max(numeric, minimum), maximum)
        value: float | int = int(round(clamped)) if integer else clamped
        normalized[name] = value
        if numeric != float(value):
            adjustments.append(
                f"Rounded {effect} {name} from {_format_numeric(numeric)} to "
                f"{_format_numeric(value)} (allowed "
                f"{_format_numeric(minimum)}–{_format_numeric(maximum)})"
            )
    for name in ("start", "stop"):
        if name not in normalized or name in EFFECT_NUMERIC_BOUNDS.get(effect, {}):
            continue
        numeric = float(normalized[name])
        clamped = min(max(numeric, 0), MAX_MEDIA_DURATION)
        normalized[name] = clamped
        if numeric != clamped:
            adjustments.append(
                f"Rounded {effect} {name} from {_format_numeric(numeric)} to "
                f"{_format_numeric(clamped)} (allowed 0–{_format_numeric(MAX_MEDIA_DURATION)})"
            )
    return normalized, adjustments


def _renderer_effect_options(
    effect: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    rendered = options.copy()
    if effect in {"overlay", "overlayflag"}:
        rendered["opacity"] = float(rendered.get("opacity", 100)) / 100
    return rendered


PIPELINE_QUALIFIED_EFFECTS: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {
    ("crop", "circle"): ("crop", {"shape": "circle"}),
    ("crop", "triangle"): ("crop", {"shape": "triangle"}),
    ("fade", "in"): ("fadein", {}),
    ("fade", "out"): ("fadeout", {}),
    ("mirror", "bottom"): ("mirror", {"direction": "bottom"}),
    ("mirror", "left"): ("mirror", {"direction": "left"}),
    ("mirror", "right"): ("mirror", {"direction": "right"}),
    ("mirror", "top"): ("mirror", {"direction": "top"}),
    ("overlay", "flag"): ("overlayflag", {}),
    ("overlay", "image"): ("overlay", {}),
    ("overlay", "video"): ("overlay", {}),
    ("bass", "boost"): ("bassboost", {}),
    ("bass", "lower"): ("basslower", {}),
    ("audio", "reverse"): ("audioreverse", {}),
    ("audio", "reverb"): ("audioreverb", {}),
    ("audio", "extract"): ("extract", {}),
    ("audio", "replace"): ("audioreplace", {}),
    ("audio", "destroy"): ("audiodestroy", {}),
    ("audio", "compress"): ("audiocompress", {}),
    ("audio", "channels-combine"): ("channelscombine", {}),
    ("audio", "pitch"): ("audiopitch", {}),
    ("audio", "underwater"): ("audiounderwater", {}),
    ("audio", "nightcore"): ("audionightcore", {}),
    ("audio", "deepvoice"): ("audiodeepvoice", {}),
    ("audio", "surround"): ("audiosurround", {}),
    ("audio", "echo"): ("audioecho", {}),
    ("audio", "adhd"): ("adhd", {}),
    ("audio", "overlay"): ("audiooverlay", {}),
    ("audio", "sound-effect"): ("soundeffect", {}),
    ("audio", "soundeffect"): ("soundeffect", {}),
    ("audio", "sfx"): ("soundeffect", {}),
    ("audio", "random"): ("random", {"category": "audio"}),
    ("overlay", "random"): ("random", {"category": "overlay"}),
    ("random", "audio"): ("random", {"category": "audio"}),
    ("random", "overlay"): ("random", {"category": "overlay"}),
    ("random", "visual"): ("random", {"category": "visual"}),
    ("remove", "bars"): ("removebars", {}),
    ("remove", "caption"): ("removecaption", {}),
    ("remove", "outro-tiktok"): ("removeoutrotiktok", {}),
    ("remove", "outro-reels"): ("removeoutroreels", {}),
}
PIPELINE_TRIPLE_EFFECTS: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {
    ("remove", "outro", "tiktok"): ("removeoutrotiktok", {}),
    ("remove", "outro", "reels"): ("removeoutroreels", {}),
}
MAX_PIPELINE_EFFECTS = 67
PIPELINE_REPEAT_RE = re.compile(r"^x(?P<count>\d+)$", re.IGNORECASE)
PIPELINE_EFFECT_REPEAT_RE = re.compile(
    r"^(?P<effect>[a-z][a-z0-9_-]*?)x(?P<count>\d+)$", re.IGNORECASE
)


def _pipeline_segment_name(value: str) -> str | None:
    normalized = value.casefold()
    normalized = PIPELINE_EFFECT_ALIASES.get(normalized, normalized)
    if normalized in PIPELINE_EFFECTS:
        return normalized
    return None


def _prepare_sound_effect_options(
    options: dict[str, Any],
    *,
    rng: random_module.Random | random_module.SystemRandom | None = None,
) -> dict[str, Any]:
    prepared = options.copy()
    if prepared.pop("norandom", False):
        prepared["random_time"] = False
    if prepared.pop("fullrandom", False):
        generator = rng or random_module.SystemRandom()
        prepared["pitch"] = round(generator.uniform(-12, 12), 2)
        prepared["speed"] = round(generator.uniform(0.5, 2), 2)
    return prepared


def _extract_sound_effect_selector(
    media: str,
    selector: str,
) -> tuple[str, str]:
    """Accept the sound ID/name before or after the primary media argument."""
    if selector.casefold() not in {"", "random", "rand"}:
        return media, selector
    try:
        tokens = shlex.split(media)
    except ValueError as error:
        raise commands.BadArgument(
            "The sound-effect arguments contain an unmatched quote."
        ) from error

    for index in range(len(tokens)):
        candidate = tokens[index]
        try:
            selected = find_audio_effect(candidate)
        except ValueError:
            continue
        del tokens[index]
        return " ".join(tokens), str(selected.id)
    return media, selector or "random"


def _select_pipeline_sound_effect(
    selector: str,
    used_ids: set[int],
) -> AudioEffect:
    if selector.casefold() in {"", "random", "rand"}:
        available = [
            effect for effect in audio_effect_catalog() if effect.id not in used_ids
        ]
        if not available:
            available = list(audio_effect_catalog())
        return random_module.SystemRandom().choice(available)
    return find_audio_effect(selector)


def _parse_effect_pipeline(
    argument: str,
) -> tuple[str, list[tuple[str, dict[str, Any]]], list[str]]:
    try:
        tokens = shlex.split(argument)
    except ValueError as error:
        raise commands.BadArgument(
            "The effect pipeline contains an unmatched quote."
        ) from error

    source_tokens: list[str] = []
    segments: list[tuple[str, list[str], dict[str, Any]]] = []
    current: tuple[str, list[str], dict[str, Any]] | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        repeat = PIPELINE_REPEAT_RE.fullmatch(token)
        if repeat is not None and current is not None:
            current[2]["__repeat__"] = int(repeat.group("count"))
            index += 1
            continue
        if (
            current is not None
            and current[0] == "soundeffect"
            and token.casefold() in {"random", "rand"}
        ):
            current[1].append(token)
            index += 1
            continue
        if (
            current is not None
            and current[0] == "overlay"
            and token.casefold() in {"random", "rand"}
        ):
            current[1].append(token)
            if (
                index + 1 < len(tokens)
                and tokens[index + 1].casefold() in OVERLAY_SOURCE_KIND_ALIASES
            ):
                current[1].append(tokens[index + 1])
                index += 1
            index += 1
            continue
        if (
            current is not None
            and current[0] in {"caption", "meme"}
            and token.casefold() == "text"
        ):
            # "text" is a common word in positional captions. Keep the
            # established `caption text here invert` syntax unambiguous.
            current[1].append(token)
            index += 1
            continue

        qualified: tuple[str, dict[str, Any]] | None = None
        if (
            not token.startswith("-")
            and index + 2 < len(tokens)
            and not tokens[index + 1].startswith("-")
            and not tokens[index + 2].startswith("-")
        ):
            qualified = PIPELINE_TRIPLE_EFFECTS.get(
                (
                    token.casefold(),
                    tokens[index + 1].casefold(),
                    tokens[index + 2].casefold(),
                )
            )
            if qualified is not None:
                effect, defaults = qualified
                current = (effect, [], defaults.copy())
                segments.append(current)
                index += 3
                continue
        if (
            not token.startswith("-")
            and index + 1 < len(tokens)
            and not tokens[index + 1].startswith("-")
        ):
            qualified = PIPELINE_QUALIFIED_EFFECTS.get(
                (token.casefold(), tokens[index + 1].casefold())
            )
        if qualified is not None:
            effect, defaults = qualified
            current = (effect, [], defaults.copy())
            segments.append(current)
            index += 2
            continue

        repeat_suffix = (
            PIPELINE_EFFECT_REPEAT_RE.fullmatch(token)
            if not token.startswith("-")
            else None
        )
        effect_token = repeat_suffix.group("effect") if repeat_suffix else token
        effect = (
            _pipeline_segment_name(effect_token)
            if not effect_token.startswith("-")
            else None
        )
        if effect is not None:
            defaults: dict[str, Any] = {}
            if repeat_suffix is not None:
                defaults["__repeat__"] = int(repeat_suffix.group("count"))
            current = (effect, [], defaults)
            segments.append(current)
        elif current is None:
            source_tokens.append(token)
        else:
            current[1].append(token)
        index += 1

    if not segments:
        raise commands.BadArgument(
            "Add at least one supported effect after the media input."
        )
    parsed: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []
    for effect, effect_tokens, qualified_defaults in segments:
        if effect in PIPELINE_DISALLOWED_EFFECTS:
            raise commands.BadArgument(
                f"`{effect}` cannot be used inside `run` or `random`. "
                f"Use `fish {effect}` by itself instead."
            )
        repeat_count = int(qualified_defaults.pop("__repeat__", 1))
        if not 1 <= repeat_count <= MAX_PIPELINE_EFFECTS:
            raise commands.BadArgument(
                f"Effect repetition must be between 1 and {MAX_PIPELINE_EFFECTS}."
            )
        _, values, switches = PIPELINE_EFFECTS[effect]
        remainder, options = _parse_effect_flags(
            " ".join(shlex.quote(token) for token in effect_tokens),
            values=values,
            switches=switches,
        )
        if effect == "overlayflag" and remainder:
            positional_media, positional_flag = _positional_flag(remainder)
            if positional_flag is not None and not positional_media:
                options["flag"] = positional_flag
                remainder = ""
        elif effect == "overlay" and remainder and not options["overlay"]:
            options["overlay"] = remainder
            remainder = ""
        elif (
            effect in {"caption", "meme", "text"} and remainder and not options["text"]
        ):
            options["text"] = remainder
            remainder = ""
        elif effect == "zoom" and remainder.casefold() in {"forever", "infinite"}:
            options["forever"] = True
            remainder = ""
        elif effect == "zoom" and remainder:
            try:
                options["amount"] = float(remainder)
                remainder = ""
            except ValueError:
                pass
        elif effect == "speed" and remainder:
            try:
                options["speed"] = float(remainder)
                remainder = ""
            except ValueError:
                pass
        elif effect == "resize" and RESIZE_DIMENSIONS_RE.fullmatch(remainder):
            options["size"] = remainder
            remainder = ""
        elif effect == "soundeffect" and remainder:
            if remainder.casefold() in {"random", "rand"}:
                options["effect"] = "random"
                remainder = ""
            else:
                try:
                    selected = find_audio_effect(remainder)
                except ValueError:
                    pass
                else:
                    options["effect"] = str(selected.id)
                    remainder = ""
        if remainder:
            skipped.append(f"{effect} (unrecognized argument: {remainder})")
            continue
        options.update(qualified_defaults)
        if effect == "soundeffect":
            options = _prepare_sound_effect_options(options)
        parsed.extend((effect, options.copy()) for _ in range(repeat_count))
        if len(parsed) > MAX_PIPELINE_EFFECTS:
            raise commands.BadArgument(
                "The expanded effect pipeline can contain up to "
                f"{MAX_PIPELINE_EFFECTS} steps."
            )
    return " ".join(source_tokens), parsed, skipped


COUNTRY_FLAG_ALIASES = {
    "usa": "us",
    "unitedstates": "us",
    "unitedstatesofamerica": "us",
    "america": "us",
    "uk": "gb",
    "unitedkingdom": "gb",
    "england": "gb",
    "southkorea": "kr",
    "northkorea": "kp",
    "russia": "ru",
    "vatican": "va",
    "vaticancity": "va",
    "palestine": "ps",
    "taiwan": "tw",
}
RESTRICTED_FLAG_USER_ID = 766953372309127168


def _country_flag_code(flag: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", flag.casefold())
    alias = COUNTRY_FLAG_ALIASES.get(normalized)
    if alias is not None:
        return alias
    if re.fullmatch(r"[a-z]{2}", normalized):
        return normalized
    try:
        country = pycountry.countries.lookup(flag.strip())
    except LookupError:
        return None
    code = str(getattr(country, "alpha_2", "")).casefold()
    return code if re.fullmatch(r"[a-z]{2}", code) else None


def _restricted_flag_media(source: str, media_url: str) -> bool:
    user_id = str(RESTRICTED_FLAG_USER_ID)
    normalized_source = source.strip()
    if re.fullmatch(rf"(?:<@!?)?{user_id}>?", normalized_source):
        return True
    path = urlsplit(media_url).path.casefold()
    return bool(
        re.search(rf"/avatars/{user_id}/", path)
        or re.search(rf"/guilds/\d+/users/{user_id}(?:/|$)", path)
    )


def _effect_name_from_qualified(qualified_name: str) -> str:
    qualified = qualified_name.casefold().split()
    leaf = qualified[-1]
    effect = PIPELINE_EFFECT_ALIASES.get(leaf, leaf)
    # Overflow slash groups use names such as ``effect-audio`` and
    # ``effect-audio-more``. Treat those as the audio branch when deriving
    # numeric descriptions, while keeping the text-command hierarchy intact.
    has_audio = "audio" in qualified or any(
        part.startswith("effect-audio") for part in qualified
    )
    if has_audio:
        effect = {
            "reverse": "audioreverse",
            "reverb": "audioreverb",
            "extract": "extract",
            "replace": "audioreplace",
            "overlay": "audiooverlay",
            "sound-effect": "soundeffect",
            "destroy": "audiodestroy",
            "compress": "audiocompress",
            "channels-combine": "channelscombine",
            "pitch": "audiopitch",
            "underwater": "audiounderwater",
            "nightcore": "audionightcore",
            "deepvoice": "audiodeepvoice",
            "surround": "audiosurround",
            "echo": "audioecho",
        }.get(leaf, effect)
    elif "overlay" in qualified:
        effect = {
            "flag": "overlayflag",
            "image": "overlay",
            "video": "overlay",
        }.get(leaf, "overlay")
    elif "bass" in qualified:
        effect = {"boost": "bassboost", "lower": "basslower"}.get(leaf, effect)
    elif "fade" in qualified:
        effect = {"in": "fadein", "out": "fadeout"}.get(leaf, effect)
    return effect


def _short_media_app_description(description: Any) -> str:
    """Keep slash descriptions to one concise action line."""
    short = str(description).splitlines()[0].strip()
    short = re.sub(r"\bUser/Emoji/Media URLs?\b", "media", short)
    short = short.replace("an media", "media").replace("a media", "media")
    short = re.sub(r"\s*\(media\)$", "", short)
    return short


def describe_media_parameters(command: Any) -> None:
    """Fill app-command parameter descriptions and include numeric ranges."""
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            describe_media_parameters(child)
        short_description = _short_media_app_description(command.description)
        if str(command.description) != short_description:
            command.description = short_description
        return
    if not isinstance(command, app_commands.Command):
        return

    effect = _effect_name_from_qualified(command.qualified_name)
    generic = {
        "effects": "Effects and flags in the order they should run",
        "media": MEDIA_INPUT_DESCRIPTION,
        "second": "Second User/Emoji/Media URL",
        "second_media": "Second User/Emoji/Media URL",
        "overlay_media": (
            "User/Emoji/Media URL or random [emoji|user|asset] to place on top"
        ),
        "attachment": "Attach the media to process",
        "overlay_attachment": "Attach the media to place on top",
        "text": "Text used by the effect",
        "position": "Where to place the overlay",
        "direction": "Direction used by the effect",
        "clockwise": "Rotate clockwise",
        "stretch": "Stretch the overlay to fill the background",
        "overlay_audio": "Mix audio from the overlay",
        "extend": "Extend output for a longer overlay video",
        "size": "Overlay dimensions such as 100x100, from 1 to 4096 pixels per side",
        "audio_media": "Audio or video URL to use",
        "audio_attachment": "Attach audio or video to use",
        "effect": "Effect ID, name, or random",
        "start": "Time where the effect begins",
        "stop": "Time where the effect stops, or 0 for the end",
        "duration": "Duration 0 to 600 seconds, or 0 for the remaining media",
        "at": "Time where the overlay begins",
        "source_start": "Time to begin reading the overlay source",
        "source_stop": "Time to stop reading the overlay source",
        "random_time": "Choose a random valid start time",
        "preserve_transparency": "Keep transparent areas transparent",
        "mode": "Resize, stretch, or keep the second item at original size",
        "audio": "Mix, first, second, or no audio",
    }
    descriptions: dict[str, str] = {}
    numeric = EFFECT_NUMERIC_BOUNDS.get(effect, {})
    for parameter in command.parameters:
        if parameter.name in numeric:
            minimum, maximum, _ = numeric[parameter.name]
            label = parameter.name.replace("_", " ").capitalize()
            descriptions[parameter.name] = (
                f"{label} from {_format_numeric(minimum)} to "
                f"{_format_numeric(maximum)}"
            )
        elif parameter.name in generic:
            descriptions[parameter.name] = generic[parameter.name]
        elif parameter.name.startswith("attachment"):
            descriptions[parameter.name] = "Attach media to process"
        elif not parameter.description or parameter.description in {
            "…",
            "No description provided",
        }:
            descriptions[parameter.name] = parameter.name.replace("_", " ").capitalize()
        if (
            effect in {"averagecolors", "average-colors"}
            and parameter.name == "attachment"
        ):
            descriptions[parameter.name] = "Attach a still image"
    for name, description in descriptions.items():
        internal_parameter = command._params.get(name)
        if internal_parameter is not None:
            internal_parameter.description = app_commands.locale_str(description)

    short_description = _short_media_app_description(command.description)
    if str(command.description) != short_description:
        command.description = short_description


def describe_text_numeric_ranges(command: Any) -> None:
    """Expose the same numeric limits in prefix-command help text."""
    ranges = EFFECT_NUMERIC_BOUNDS.get(
        _effect_name_from_qualified(command.qualified_name),
        {},
    )
    if not ranges or not command.help or "-# Numeric limits:" in command.help:
        return
    limits = "; ".join(
        f"-{name} {_format_numeric(minimum)} to {_format_numeric(maximum)}"
        for name, (minimum, maximum, _) in ranges.items()
    )
    command.help = f"{command.help.rstrip()}\n\n-# Numeric limits: {limits}."


def _positional_flag(media: str) -> tuple[str, str | None]:
    """Extract a trailing flag name from the text-only overlay syntax."""
    try:
        tokens = shlex.split(media)
    except ValueError:
        return media, None
    if not tokens:
        return media, None

    for start in range(len(tokens) - 1, -1, -1):
        candidate = " ".join(tokens[start:])
        if make_flag_asset(candidate) is not None or _country_flag_code(candidate):
            return " ".join(tokens[:start]), candidate
    return media, None


def _parse_overlay_text_argument(
    argument: str,
) -> tuple[str, str, dict[str, Any]]:
    media, options = _parse_effect_flags(
        argument,
        values={
            "overlay": (("second", "o"), str, ""),
            "opacity": (("alpha",), float, 70.0),
            "scale": (("s",), float, 1.0),
            "size": (("dimensions", "dim"), str, ""),
            "position": (("pos", "p"), str, "center"),
            "x": (("left",), int, 0),
            "y": (("top",), int, 0),
            "start": (("from",), float, 0.0),
            "stop": (("end",), float, 0.0),
        },
        switches={
            "stretch": ("fill",),
            "extend": ("longer",),
            "no_audio": ("no-audio", "mute-audio"),
        },
    )
    overlay_source = str(options.pop("overlay", "")).strip()
    try:
        positional = shlex.split(media)
    except ValueError as error:
        raise commands.BadArgument(
            "The overlay arguments contain an unmatched quote."
        ) from error

    if overlay_source:
        if len(positional) > 1:
            raise commands.BadArgument(
                "Provide one background media item before `-overlay`."
            )
        source = positional[0] if positional else ""
        return source, overlay_source, options

    if len(positional) >= 2:
        candidate = " ".join(positional[1:])
        # Prefer the explicit source grammar (`user name`, `flag Germany`,
        # `asset random`) over treating the second token as a bare URL.
        if _parse_overlay_selector(candidate) is not None:
            overlay_source = candidate
            positional = positional[:1]
        elif len(positional) > 2:
            raise commands.BadArgument(
                "Overlay accepts `<media> <user|media|emoji|asset|flag> "
                "[name|random]`, or a second media URL. Provide one "
                "background and one overlay media item."
            )
    if len(positional) > 2:
        raise commands.BadArgument(
            "Overlay accepts a background and one overlay media item."
        )
    if not overlay_source and len(positional) >= 2:
        source, overlay_source = positional[:2]
        positional = positional[:1]
    if len(positional) > 1:
        raise commands.BadArgument(
            "Overlay accepts a background and one overlay media item."
        )
    source = positional[0] if positional else ""
    return source, overlay_source, options


class Images(Cog):
    """Image manipulation commands."""

    def _remove_audio_overlay_app_command(self) -> None:
        """Keep the legacy text command without registering a duplicate app command."""
        audio_group = getattr(self.video_effects_audio, "app_command", None)
        if isinstance(audio_group, app_commands.Group):
            audio_group.remove_command("overlay")

    @staticmethod
    def _compact_audio_app_group(audio_group: app_commands.Group) -> None:
        """Keep the complete audio namespace below Discord's command-size limit."""

        descriptions = {
            "reverse": "Reverse audio.",
            "reverb": "Add reverb.",
            "extract": "Extract audio.",
            "replace": "Replace audio.",
            "sound-effect": "Mix a sound effect.",
            "destroy": "Degrade audio.",
            "compress": "Compress audio.",
            "channels-combine": "Combine channels.",
            "pitch": "Change pitch.",
            "underwater": "Add an underwater effect.",
            "nightcore": "Apply nightcore.",
            "deepvoice": "Lower voices.",
            "surround": "Widen stereo.",
            "echo": "Add an echo.",
        }
        numeric_parameters = {
            "amount",
            "at",
            "duration",
            "fade_in",
            "fade_out",
            "gain",
            "pitch",
            "ratio",
            "room",
            "semitones",
            "source_start",
            "source_stop",
            "speed",
            "start",
            "stop",
            "volume",
        }
        for child in audio_group.commands:
            if not isinstance(child, app_commands.Command):
                continue
            if child.name in descriptions:
                child.description = descriptions[child.name]
            for parameter in child.parameters:
                if parameter.name in numeric_parameters and " to " in str(
                    parameter.description
                ):
                    # The option name already labels the value. Keeping only
                    # the range saves space without hiding its valid limits.
                    compact = str(parameter.description).split(" from ")[-1]
                    child._params[parameter.name].description = app_commands.locale_str(
                        compact
                    )
                elif parameter.name == "attachment":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "File"
                    )
                elif parameter.name == "audio_attachment":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "Audio"
                    )
                elif parameter.name == "effect":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "Effect"
                    )
                elif parameter.name == "random_time":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "Random start"
                    )
                elif parameter.name == "loop":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "Loop to end"
                    )
                elif parameter.name == "full_random":
                    child._params[parameter.name].description = app_commands.locale_str(
                        "Randomize settings"
                    )

    @staticmethod
    def _media_app_command_size(command: Any) -> int:
        payload = command.to_dict(_MEDIA_APP_COMMAND_SIZE_TREE)
        return len(json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def _custom_emoji_values(bot: Any) -> list[Any]:
        source = getattr(bot, "custom_emojis", None)
        if source is None:
            return []
        values: list[Any] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, (list, tuple, set, frozenset)):
                for nested in value:
                    collect(nested)
            elif value is not None:
                values.append(value)

        if isinstance(source, Mapping):
            collect(source)
            return values
        for namespace in (source, type(source)):
            try:
                collect(vars(namespace))
            except TypeError:
                continue
        return values

    def _random_overlay_candidates(
        self,
        ctx: Context,
        kind: str,
    ) -> list[tuple[str, str, object]]:
        candidates: list[tuple[str, str, object]] = []
        seen: set[tuple[str, str]] = set()

        def add(candidate_kind: str, label: str, value: object) -> None:
            key = (candidate_kind, str(value))
            if key not in seen:
                seen.add(key)
                candidates.append((candidate_kind, label, value))

        if kind in {"all", "emoji"}:
            if ctx.guild is not None:
                for value in ctx.guild.emojis:
                    url = getattr(value, "url", None)
                    if url:
                        add("emoji", _random_overlay_label(value), str(url))

            for value in self._custom_emoji_values(ctx.bot):
                if isinstance(value, str):
                    if value.startswith(("http://", "https://")):
                        add("emoji", _random_overlay_label(value), value)
                        continue
                    for item in emoji_lib.emoji_list(value):
                        token = str(item["emoji"])
                        if TwemojiConverter.is_unicode_emoji(token):
                            add(
                                "emoji",
                                _random_overlay_label(token),
                                TwemojiConverter.png_url(token),
                            )
                    continue
                url = getattr(value, "url", None)
                if url:
                    add("emoji", _random_overlay_label(value), str(url))
                    continue
                name = getattr(value, "name", None)
                if isinstance(name, str) and TwemojiConverter.is_unicode_emoji(name):
                    add(
                        "emoji",
                        _random_overlay_label(name),
                        TwemojiConverter.png_url(name),
                    )

            for token in _unicode_overlay_emojis():
                add(
                    "emoji",
                    _random_overlay_label(token),
                    TwemojiConverter.png_url(token),
                )

        if kind in {"all", "user"}:
            users: list[discord.abc.User]
            if ctx.guild is not None:
                users = list(ctx.guild.members)
            else:
                users = [ctx.author]
                if ctx.bot.user is not None:
                    users.append(ctx.bot.user)
            if not users:
                users = [ctx.author]
            for user in users:
                avatar = getattr(getattr(user, "display_avatar", None), "url", None)
                if avatar:
                    add(
                        "user",
                        str(getattr(user, "name", None) or user),
                        str(avatar),
                    )

        if kind in {"all", "asset", "media"}:
            for asset in image_asset_catalog():
                add("asset", asset.display_name, asset)
            for asset in video_asset_catalog():
                add("asset", asset.display_name, asset)

        if kind in {"all", "flag"}:
            # Keep aliases out of the random pool so equivalent pride flags
            # do not crowd out country flags or the canonical names.
            pride_aliases = {"rainbow", "gay", "bi", "nb", "ace", "aro"}
            for name in PRIDE_FLAGS:
                if name not in pride_aliases:
                    add("flag", name, name)
            for country in pycountry.countries:
                name = str(getattr(country, "name", "")).strip()
                code = str(getattr(country, "alpha_2", "")).casefold()
                if name and re.fullmatch(r"[a-z]{2}", code):
                    add("flag", name, code)

        return candidates

    async def _random_overlay_data(
        self,
        ctx: Context,
        source: str,
    ) -> tuple[bytes, str]:
        parsed = _parse_overlay_selector(source)
        if parsed is None:
            raise commands.BadArgument("That is not a valid overlay source.")
        kind, selector = parsed

        # `media <url>` is an explicit media source, while `media random`
        # shares the bundled image/video asset pool below.
        if kind == "media" and selector:
            try:
                media_url = await self._resolve_effect_media(
                    ctx,
                    selector,
                    scan_messages=False,
                )
                return await self._fetch_effect_media(ctx, media_url), (
                    f"media: {selector}"
                )
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "That media overlay could not be loaded."
                ) from error

        rng = random_module.SystemRandom()
        if kind == "all":
            candidate_pools = {
                candidate_kind: self._random_overlay_candidates(ctx, candidate_kind)
                for candidate_kind in ("emoji", "user", "asset", "flag")
            }
            available_kinds = [
                candidate_kind
                for candidate_kind, pool in candidate_pools.items()
                if pool
            ]
            if not available_kinds:
                raise commands.BadArgument("No random overlay sources are available.")
            kind = rng.choice(available_kinds)
            candidates = candidate_pools[kind]
        else:
            candidates = self._random_overlay_candidates(ctx, kind)

        if selector and kind == "flag":
            # Resolve named countries and two-letter codes directly so aliases
            # such as `uk` and multi-word names do not depend on candidate
            # labels being present in the random pool.
            data = await self._flag_data(ctx, selector)
            return data, f"flag: {selector}"

        if selector and kind == "user":
            # User selectors can be a username, display name, mention, or ID.
            query = selector.strip().casefold()
            if query.startswith("@") and not query.startswith("<@"):
                query = query[1:]
            query_id = re.fullmatch(r"<@!?(\d+)>|(\d+)", query)
            wanted_id = (
                next((group for group in query_id.groups() if group), None)
                if query_id
                else None
            )
            users: list[discord.abc.User]
            if ctx.guild is not None:
                users = list(ctx.guild.members)
            else:
                users = [ctx.author]
                if ctx.bot.user is not None:
                    users.append(ctx.bot.user)
            selected_user = next(
                (
                    user
                    for user in users
                    if (
                        (wanted_id is not None and str(user.id) == wanted_id)
                        or str(getattr(user, "name", "")).casefold() == query
                        or str(getattr(user, "display_name", "")).casefold() == query
                        or str(getattr(user, "global_name", "")).casefold() == query
                    )
                ),
                None,
            )
            if selected_user is None:
                raise commands.BadArgument(
                    f"No user named `{selector}` is available for an overlay."
                )
            avatar = getattr(
                getattr(selected_user, "display_avatar", None),
                "url",
                None,
            )
            if not avatar:
                raise commands.BadArgument("That user does not have an avatar.")
            candidates = [
                (
                    "user",
                    str(getattr(selected_user, "name", None) or selected_user),
                    str(avatar),
                )
            ]
        elif selector:
            normalized_query = selector.casefold().strip()
            query_labels = {
                normalized_query,
                _random_overlay_label(selector).casefold(),
            }
            compact_queries = {
                re.sub(r"[^a-z0-9]+", "", wanted) for wanted in query_labels
            }

            def matches(candidate: tuple[str, str, object]) -> bool:
                candidate_kind, label, value = candidate
                values = [label, str(value)]
                if candidate_kind == "asset":
                    for attribute in ("name", "display_name", "category"):
                        attribute_value = getattr(value, attribute, None)
                        if attribute_value:
                            values.append(str(attribute_value))
                for candidate_value in values:
                    folded = candidate_value.casefold().strip()
                    if folded in query_labels:
                        return True
                    if re.sub(r"[^a-z0-9]+", "", folded) in compact_queries:
                        return True
                return False

            candidates = [candidate for candidate in candidates if matches(candidate)]
            if not candidates:
                raise commands.BadArgument(
                    f"No {kind} overlay named `{selector}` is available."
                )
        if not candidates:
            raise commands.BadArgument(f"No random {kind} overlays are available.")

        last_error: Exception | None = None
        for _ in range(min(12, max(1, len(candidates)))):
            candidate_kind, label, value = rng.choice(candidates)
            try:
                if candidate_kind == "asset":
                    assert isinstance(value, (ImageAsset, VideoAsset))
                    # Bundled assets are small and local, so read them directly
                    # instead of spending time creating a worker-thread task.
                    if value.path.stat().st_size > 10 * 1024 * 1024:
                        raise commands.BadArgument("The bundled asset was too large.")
                    data = value.path.read_bytes()
                elif candidate_kind == "flag":
                    data = await self._flag_data(ctx, str(value))
                else:
                    response = await fetch_public_bytes(
                        ctx.session,
                        str(value),
                        max_bytes=10 * 1024 * 1024,
                        allowed_content_prefixes=("image/",),
                        allowed_hosts=(
                            "cdn.discord.com",
                            "cdn.discordapp.com",
                            "cdn.discordapp.net",
                            "media.discordapp.net",
                            "raw.githubusercontent.com",
                        ),
                    )
                    data = response.data
                return data, f"{candidate_kind}: {label}"
            except Exception as error:
                last_error = error
        raise commands.BadArgument(
            "A random overlay source could not be fetched."
        ) from last_error

    @staticmethod
    def _copy_media_app_group(
        source: app_commands.Group, name: str
    ) -> app_commands.Group:
        return app_commands.Group(
            name=name,
            description=source.description,
            allowed_contexts=source.allowed_contexts,
            allowed_installs=source.allowed_installs,
            guild_only=source.guild_only,
            nsfw=source.nsfw,
            default_permissions=source.default_permissions,
            extras=dict(source.extras),
        )

    def _rebalance_media_app_group(
        self,
        source: app_commands.Group,
        children: list[Any],
        names: set[str],
        base_name: str,
    ) -> None:
        """Split a hybrid app-command group into payload-safe top-level groups."""
        for child in list(source.commands):
            source.remove_command(child.name)

        chunks: list[list[Any]] = []
        current: list[Any] = []
        current_size = MEDIA_APP_COMMAND_ROOT_OVERHEAD
        for child in children:
            child_size = self._media_app_command_size(child) + 1
            if current and current_size + child_size > MEDIA_APP_COMMAND_LIMIT:
                chunks.append(current)
                current = []
                current_size = MEDIA_APP_COMMAND_ROOT_OVERHEAD
            current.append(child)
            current_size += child_size
        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks):
            if index == 0:
                target = source
            else:
                name = self._next_media_app_group_name(
                    names,
                    f"{base_name}-more",
                )
                target = self._copy_media_app_group(source, name)
                self.__cog_app_commands__.append(target)
            for child in chunk:
                child.parent = None
                target.add_command(child)

    @staticmethod
    def _next_media_app_group_name(names: set[str], base: str) -> str:
        if base not in names:
            names.add(base)
            return base
        index = 2
        while f"{base}-{index}" in names:
            index += 1
        name = f"{base}-{index}"
        names.add(name)
        return name

    def _merge_media_app_groups(self, groups: list[app_commands.Group]) -> None:
        """Pack compatible overflow groups into the fewest numbered groups."""
        while len(groups) > 1:
            best: tuple[int, int, int] | None = None
            for left_index, left in enumerate(groups[:-1]):
                left_names = {child.name for child in left.commands}
                for right_index in range(left_index + 1, len(groups)):
                    right = groups[right_index]
                    if left_names & {child.name for child in right.commands}:
                        continue
                    if len(left.commands) + len(right.commands) > 25:
                        continue

                    moved: list[Any] = []
                    combined_size = MEDIA_APP_COMMAND_LIMIT + 1
                    try:
                        for child in list(right.commands):
                            right.remove_command(child.name)
                            child.parent = None
                            moved.append(child)
                            left.add_command(child)
                        combined_size = self._media_app_command_size(left)
                    finally:
                        for child in moved:
                            left.remove_command(child.name)
                            child.parent = None
                            right.add_command(child)

                    if combined_size <= MEDIA_APP_COMMAND_LIMIT and (
                        best is None or combined_size > best[0]
                    ):
                        best = (combined_size, left_index, right_index)

            if best is None:
                return

            _, left_index, right_index = best
            left = groups[left_index]
            right = groups.pop(right_index)
            for child in list(right.commands):
                right.remove_command(child.name)
                child.parent = None
                left.add_command(child)
            if right in self.__cog_app_commands__:
                self.__cog_app_commands__.remove(right)

    def _rebalance_effect_app_commands(self) -> None:
        """Keep media-effect slash payloads below Discord's 8,000-byte limit.

        Hybrid commands remain in their existing text-command groups. Only the
        application-command objects are moved into numbered overflow groups,
        so prefix command names and behavior do not change.
        """
        if getattr(self, "_media_app_commands_rebalanced", False):
            return
        self._media_app_commands_rebalanced = True

        hybrid_roots: list[app_commands.Group] = []
        for command in self.__cog_commands__:
            if command.parent is not None:
                continue
            app_command = getattr(command, "app_command", None)
            if isinstance(app_command, app_commands.Group):
                hybrid_roots.append(app_command)
        hybrid_roots.sort(key=lambda group: (group.name != "effect", group.name))
        initial_app_command_ids = {id(command) for command in self.__cog_app_commands__}
        names = {command.name for command in hybrid_roots} | {
            command.name for command in self.__cog_app_commands__
        }

        for source in hybrid_roots:
            audio_group = next(
                (
                    child
                    for child in source.commands
                    if isinstance(child, app_commands.Group) and child.name == "audio"
                ),
                None,
            )
            if audio_group is not None:
                self._compact_audio_app_group(audio_group)

            for child in list(source.commands):
                if not isinstance(child, app_commands.Group):
                    continue
                if self._media_app_command_size(child) <= MEDIA_APP_COMMAND_LIMIT:
                    continue

                # A nested group that is larger than Discord's limit cannot be
                # registered as-is. Keep the audio namespace intact whenever
                # its compact form fits, and only flatten other oversized
                # groups as a last resort.
                source.remove_command(child.name)
                nested_children = list(child.commands)
                overflow_name = self._next_media_app_group_name(
                    names,
                    f"{source.name}-{child.name}",
                )
                overflow = self._copy_media_app_group(source, overflow_name)
                self._rebalance_media_app_group(
                    overflow,
                    nested_children,
                    names,
                    overflow_name,
                )
                if overflow not in self.__cog_app_commands__:
                    self.__cog_app_commands__.append(overflow)

            children = list(source.commands)
            if source.name == "effect" and audio_group is not None:
                # Put the audio namespace in the primary effect group. It is
                # intentionally kept ahead of visual commands so the splitter
                # can move those commands to effect-2, effect-3, and so on.
                direct_priority = [
                    child
                    for child in children
                    if child is not audio_group and child.name == "adhd"
                ]
                children = [audio_group, *direct_priority] + [
                    child
                    for child in children
                    if child is not audio_group and child not in direct_priority
                ]
            if self._media_app_command_size(source) > MEDIA_APP_COMMAND_LIMIT:
                self._rebalance_media_app_group(
                    source,
                    children,
                    names,
                    source.name,
                )

        generated_groups = [
            command
            for command in self.__cog_app_commands__
            if id(command) not in initial_app_command_ids
            and isinstance(command, app_commands.Group)
        ]
        self._merge_media_app_groups(generated_groups)
        all_media_groups = [*hybrid_roots, *generated_groups]
        for index, group in enumerate(all_media_groups):
            group.name = "effect" if index == 0 else f"effect-{index + 1}"

    async def _convert_effect_source(
        self,
        ctx: Context,
        source: str,
        *,
        include_message_media: bool = True,
    ) -> str:
        try:
            return await MediaConverter().convert(
                ctx,
                source,
                include_message_media=include_message_media,
            )
        except commands.BadArgument:
            if source and is_downloadable_media_page(source):
                return source
            raise

    async def _resolve_effect_media(
        self,
        ctx: Context,
        source: str = "",
        attachment: discord.Attachment | None = None,
        *,
        attachment_index: int = 0,
        scan_messages: bool = True,
    ) -> str:
        if attachment is not None:
            return attachment.url
        if source:
            try:
                return await self._convert_effect_source(
                    ctx,
                    source,
                    include_message_media=False,
                )
            except commands.BadArgument as error:
                if is_downloadable_media_page(source):
                    return source
                raise commands.BadArgument(
                    "That media URL is not supported."
                ) from error
        if len(ctx.message.attachments) > attachment_index:
            candidate = MediaConverter._attachment_url(
                ctx.message.attachments[attachment_index]
            )
            if candidate:
                return candidate
        if scan_messages:
            try:
                return await self._convert_effect_source(ctx, "")
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "No media found. Attach a file, reply to media, or provide a URL."
                ) from error
        raise commands.BadArgument("A second media file or URL is required.")

    async def _fetch_effect_media(self, ctx: Context, url: str) -> bytes:
        if is_downloadable_media_page(url):
            try:
                data, _ = await Downloader(
                    ctx,
                    url,
                    format="mp4",
                    hidden=True,
                ).download_for_processing(timeout=MEDIA_EFFECT_TIMEOUT)
            except DownloadError as error:
                raise commands.BadArgument(str(error)) from error
            return data
        url = await refresh_discord_attachment_url(self.bot, url)
        result = await fetch_public_bytes(
            ctx.session,
            url,
            max_bytes=50 * 1024 * 1024,
            allowed_content_prefixes=("image/", "video/", "audio/"),
        )
        return result.data

    async def _send_effect_result(
        self,
        ctx: Context,
        result: EffectResult,
        *,
        started: float,
        note: str = "",
    ) -> None:
        max_size = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )
        compressed = False
        if len(result.data) > max_size:
            original_result = result
            try:
                result = await compress_media_to_size(
                    result.data,
                    result.filename,
                    TEMP_MEDIA_MAX_BYTES,
                )
            except ValueError:
                result = original_result
            compressed = result.data != original_result.data
        info_lines = [
            f"-# Invoked by {ctx.author.mention}",
            f"-# Took {time.monotonic() - started:.1f}s",
        ]
        if note:
            info_lines.append(f"-# {note}")
        if compressed:
            info_lines.append("-# Compressed to fit the server upload limit")
        info_text = "\n".join(info_lines)
        filename = result.filename.replace("/", "_").replace("\\", "_")
        hosted_url: str | None = None
        if len(result.data) > max_size:
            try:
                hosted_url = await upload_temporary_media(
                    self.bot,
                    result.data,
                    filename,
                    content_type=mimetypes.guess_type(filename)[0],
                )
            except TemporaryMediaError as error:
                raise commands.BadArgument(
                    "The result is too large for Discord and could not be hosted "
                    "temporarily. Try fewer effects, a shorter video, or a smaller source."
                ) from error
            info_text = (
                f"{info_text}\n"
                "-# Discord's upload limit was exceeded. This link expires in 30 minutes."
            )
        if hosted_url is not None:
            container_items: list[ui.Item[Any]]
            if result.displayable:
                container_items = [
                    ui.MediaGallery(MediaGalleryItem(hosted_url)),
                    ui.TextDisplay(info_text),
                ]
            else:
                container_items = [
                    ui.TextDisplay(
                        f"[Open the generated file]({hosted_url})\n{info_text}"
                    )
                ]
            container = ui.Container(*container_items, accent_color=self.bot.embedcolor)
            view_type = type("HostedMediaEffectView", (ui.LayoutView,), {})
            view = view_type(timeout=None)
            view.add_item(container)
            await ctx.send(
                view=view,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
            )
            return
        if not result.displayable:
            await ctx.send(
                content=info_text,
                file=discord.File(BytesIO(result.data), filename),
                reference=ctx.message.to_reference(fail_if_not_exists=False),
            )
            return
        media_item: ui.Item[Any]
        media_item = ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
        container = ui.Container(
            media_item,
            ui.TextDisplay(info_text),
            accent_color=self.bot.embedcolor,
        )
        view_type = type("MediaEffectView", (ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        await ctx.send(
            file=discord.File(BytesIO(result.data), filename),
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )

    async def _send_average_colors_result(
        self,
        ctx: Context,
        colors: list[AverageColor],
        *,
        started: float,
    ) -> None:
        rows = ["## Average colors"]
        for i, color in enumerate(colors, start=1):
            red, green, blue = color.rgb
            hex_value = f"#{red:02X}{green:02X}{blue:02X}"
            rows.append(
                f"**{hex_value}** `rgb({red}, {green}, {blue})` "
                f"**{color.percentage:.1f}%**"
            )
        rows.extend(
            (
                "",
                f"-# Invoked by {ctx.author.mention}",
                f"-# Took {time.monotonic() - started:.1f}s",
            )
        )
        container = ui.Container(
            ui.TextDisplay("\n".join(rows)), accent_color=self.bot.embedcolor
        )
        view_type = type("AverageColorsView", (ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        await ctx.send(
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )

    @media_effect_timeout
    async def _average_colors_effect(
        self,
        ctx: Context,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
    ) -> None:
        media_url = await self._resolve_effect_media(ctx, source, attachment)
        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            media_data = await self._fetch_effect_media(ctx, media_url)
            try:
                _, colors = await render_average_colors(media_data)
            except NotPillowMedia as error:
                raise commands.BadArgument(
                    "Average colors only supports still images, not GIFs or videos."
                ) from error
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            await self._send_average_colors_result(
                ctx,
                colors,
                started=started,
            )

    async def _send_effect_results(
        self,
        ctx: Context,
        results: list[EffectResult],
        *,
        started: float,
    ) -> None:
        if not results:
            raise commands.BadArgument("No files were converted.")
        max_size = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )
        fitted_results: list[EffectResult] = []
        for result in results:
            if len(result.data) > max_size:
                try:
                    result = await compress_media_to_size(
                        result.data,
                        result.filename,
                        max_size,
                    )
                except ValueError:
                    pass
            fitted_results.append(result)
        results = fitted_results
        hosted_urls: dict[int, str] = {}
        for index, result in enumerate(results):
            if len(result.data) <= max_size:
                continue
            filename = result.filename.replace("/", "_").replace("\\", "_")
            try:
                hosted_urls[index] = await upload_temporary_media(
                    self.bot,
                    result.data,
                    filename,
                    content_type=mimetypes.guess_type(filename)[0],
                )
            except TemporaryMediaError as error:
                raise commands.BadArgument(
                    "At least one converted file is too large for Discord and "
                    "could not be hosted temporarily."
                ) from error
        filenames = [
            result.filename.replace("/", "_").replace("\\", "_") for result in results
        ]
        items: list[ui.Item[Any]] = []
        files: list[discord.File] = []
        for index, (result, filename) in enumerate(zip(results, filenames)):
            hosted_url = hosted_urls.get(index)
            if hosted_url is not None and result.displayable:
                items.append(ui.MediaGallery(MediaGalleryItem(hosted_url)))
            elif result.displayable:
                items.append(
                    ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
                )
                files.append(discord.File(BytesIO(result.data), filename))
            elif hosted_url is not None:
                items.append(ui.TextDisplay(f"[Open the generated file]({hosted_url})"))
            else:
                items.append(ui.File(f"attachment://{filename}"))
                files.append(discord.File(BytesIO(result.data), filename))
        items.append(
            ui.TextDisplay(
                f"-# Invoked by {ctx.author.mention}\n"
                f"-# Took {time.monotonic() - started:.1f}s\n"
                "-# Oversized files are hosted for 30 minutes"
                if hosted_urls
                else f"-# Took {time.monotonic() - started:.1f}s"
            )
        )
        container = ui.Container(*items, accent_color=self.bot.embedcolor)
        view_type = type("BulkMediaEffectView", (ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        await ctx.send(
            files=files,
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )

    @media_effect_timeout
    async def _apply_image_effect(
        self,
        ctx: Context,
        effect: str,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
        second_source: str = "",
        second_attachment: discord.Attachment | None = None,
        note: str = "",
        **options: Any,
    ) -> None:
        media_url = await self._resolve_effect_media(ctx, source, attachment)
        flag_name = str(options.pop("_flag_name", "")).strip()
        if flag_name:
            self._validate_flag_media(flag_name, source, media_url)
        second_url = ""
        second_data: bytes | None = None
        random_overlay_label: str | None = None
        if second_source or second_attachment is not None:
            if (
                second_attachment is None
                and _random_overlay_kind(second_source) is not None
            ):
                second_data, random_overlay_label = await self._random_overlay_data(
                    ctx,
                    second_source,
                )
            else:
                second_url = await self._resolve_effect_media(
                    ctx,
                    second_source,
                    second_attachment,
                    attachment_index=1,
                    scan_messages=False,
                )
        elif (
            effect == "overlay"
            and "overlay_data" not in options
            and len(ctx.message.attachments) > 1
        ):
            second_url = await self._resolve_effect_media(
                ctx,
                attachment_index=1,
                scan_messages=False,
            )

        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            media_data = await self._fetch_effect_media(ctx, media_url)
            supplied_overlay_data = options.pop("overlay_data", None)
            if second_data is not None:
                supplied_overlay_data = second_data
            elif second_url:
                supplied_overlay_data = await self._fetch_effect_media(ctx, second_url)
            if effect == "overlay" and bool(options.pop("fullrandom", False)):
                _randomize_overlay_options(options, random_module.SystemRandom())
            options, adjustments = _normalize_effect_options(effect, options)
            renderer_options = _renderer_effect_options(effect, options)
            try:
                if effect == "overlay":
                    if not isinstance(supplied_overlay_data, bytes):
                        raise ValueError("A second image or video is required.")
                    result = await render_overlay_effect(
                        media_data,
                        supplied_overlay_data,
                        **renderer_options,
                    )
                else:
                    result = await render_image_effect(
                        media_data,
                        effect,
                        **renderer_options,
                    )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            random_note = (
                _random_overlay_note(random_overlay_label)
                if random_overlay_label is not None
                else ""
            )
            combined_note = " | ".join(
                part for part in (note, random_note, "; ".join(adjustments)) if part
            )
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note=combined_note,
            )

    @media_effect_timeout
    async def _apply_video_effect(
        self,
        ctx: Context,
        effect: str,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
        second_source: str = "",
        second_attachment: discord.Attachment | None = None,
        second_data: bytes | None = None,
        note: str = "",
        **options: Any,
    ) -> None:
        media_url = await self._resolve_effect_media(ctx, source, attachment)
        second_url = ""
        random_overlay_label: str | None = None
        if second_source or second_attachment is not None:
            if (
                second_attachment is None
                and _random_overlay_kind(second_source) is not None
            ):
                second_data, random_overlay_label = await self._random_overlay_data(
                    ctx,
                    second_source,
                )
            else:
                second_url = await self._resolve_effect_media(
                    ctx,
                    second_source,
                    second_attachment,
                    attachment_index=1,
                    scan_messages=False,
                )
        elif (
            effect in {"overlay", "audioreplace", "audiooverlay"}
            and len(ctx.message.attachments) > 1
        ):
            second_url = await self._resolve_effect_media(
                ctx,
                attachment_index=1,
                scan_messages=False,
            )

        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            media_data = await self._fetch_effect_media(ctx, media_url)
            if second_data is None and second_url:
                second_data = await self._fetch_effect_media(ctx, second_url)
            options, adjustments = _normalize_effect_options(effect, options)
            renderer_options = _renderer_effect_options(effect, options)
            try:
                if effect == "overlay":
                    if not isinstance(second_data, bytes):
                        raise ValueError("A second image or video is required.")
                    result = await render_overlay_effect(
                        media_data,
                        second_data,
                        **renderer_options,
                    )
                else:
                    result = await render_video_effect(
                        media_data,
                        effect,
                        second_data=second_data,
                        **renderer_options,
                    )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            random_note = (
                _random_overlay_note(random_overlay_label)
                if random_overlay_label is not None
                else ""
            )
            combined_note = " | ".join(
                part for part in (note, random_note, "; ".join(adjustments)) if part
            )
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note=combined_note,
            )

    @media_effect_timeout
    async def _apply_text_effect(
        self,
        ctx: Context,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
        **options: Any,
    ) -> None:
        text = str(options.get("text", "")).strip()
        if not text:
            raise commands.BadArgument('Add text with `-text "your text"`.')
        media_url = await self._resolve_effect_media(ctx, source, attachment)
        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            media_data = await self._fetch_effect_media(ctx, media_url)
            options["inline_images"] = await resolve_inline_images(ctx.session, [text])
            options, adjustments = _normalize_effect_options("text", options)
            try:
                result = await render_text_effect(media_data, **options)
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note="; ".join(adjustments),
            )

    @media_effect_timeout
    async def _apply_combine_effect(
        self,
        ctx: Context,
        *,
        source: str = "",
        second_source: str = "",
        attachment: discord.Attachment | None = None,
        second_attachment: discord.Attachment | None = None,
        **options: Any,
    ) -> None:
        first_url = await self._resolve_effect_media(ctx, source, attachment)
        if not second_source and second_attachment is None:
            if len(ctx.message.attachments) > 1:
                second_attachment = ctx.message.attachments[1]
            else:
                raise commands.BadArgument(
                    "Add a second User/Emoji/Media URL with `-second`, or attach "
                    "two files."
                )
        second_url = await self._resolve_effect_media(
            ctx,
            second_source,
            second_attachment,
            attachment_index=1,
            scan_messages=False,
        )
        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            first_data, second_data = await asyncio.gather(
                self._fetch_effect_media(ctx, first_url),
                self._fetch_effect_media(ctx, second_url),
            )
            options, adjustments = _normalize_effect_options("combine", options)
            try:
                result = await render_combine_effect(
                    first_data,
                    second_data,
                    **options,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note="; ".join(adjustments),
            )

    @media_effect_timeout
    async def _convert_effect(
        self,
        ctx: Context,
        output_format: str,
        *,
        source: str = "",
        attachments: list[discord.Attachment] | None = None,
    ) -> None:
        selected = list(attachments or [])
        if not selected:
            selected = list(ctx.message.attachments[:5])
        if len(selected) > 5:
            raise commands.BadArgument("You can convert up to 5 files at a time.")

        urls: list[str] = []
        if source:
            urls.append(await self._resolve_effect_media(ctx, source))
        for attachment in selected:
            candidate = MediaConverter._attachment_url(attachment)
            if candidate:
                urls.append(candidate)
        urls = list(dict.fromkeys(urls))[:5]
        if not urls:
            urls.append(await self._resolve_effect_media(ctx))

        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            results: list[EffectResult] = []
            for index, url in enumerate(urls, start=1):
                data = await self._fetch_effect_media(ctx, url)
                try:
                    results.append(await convert_media(data, output_format, index))
                except ValueError as error:
                    raise commands.BadArgument(
                        f"File {index} could not be converted: {error}"
                    ) from error
            await self._send_effect_results(ctx, results, started=started)

    async def _flag_data(self, ctx: Context, flag: str) -> bytes:
        local = make_flag_asset(flag)
        if local is not None:
            return local
        country = _country_flag_code(flag)
        if country is None:
            raise commands.BadArgument(
                "Unknown flag. Use a country name, two-letter country code, "
                "pride flag, or pirate."
            )
        try:
            response = await fetch_public_bytes(
                ctx.session,
                f"https://flagcdn.com/w640/{country}.png",
                max_bytes=2 * 1024 * 1024,
                allowed_content_prefixes=("image/",),
                allowed_hosts=("flagcdn.com",),
            )
        except commands.BadArgument as error:
            raise commands.BadArgument(
                "That country flag could not be found."
            ) from error
        return response.data

    @staticmethod
    def _validate_flag_media(flag: str, source: str, media_url: str) -> None:
        if _country_flag_code(flag) == "il" and _restricted_flag_media(
            source,
            media_url,
        ):
            raise commands.BadArgument("no")

    @media_effect_timeout
    async def _globe_effect(self, ctx: Context, *, argument: str = "") -> None:
        try:
            media_argument, speed, clockwise = _parse_globe_input(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error

        normalized, adjustments = _normalize_effect_options(
            "globe",
            {"speed": speed},
        )
        speed = float(normalized["speed"])
        try:
            media_url = await self._convert_effect_source(ctx, media_argument)
        except commands.BadArgument as error:
            raise commands.BadArgument(
                "No image found. Attach one, reply to an image, or provide a URL."
            ) from error

        async with self.bot.media_semaphore, ctx.typing():
            import time

            started = time.time()
            image_data = await self._fetch_effect_media(ctx, media_url)
            try:
                output = await make_globe(
                    image_data,
                    speed,
                    clockwise,
                    TEMP_MEDIA_MAX_BYTES,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            await self._send_effect_result(
                ctx,
                EffectResult(output.getvalue(), "globe.gif"),
                started=started,
                note="; ".join(adjustments),
            )

    @media_effect_timeout
    async def _spin3d_effect(self, ctx: Context, *, argument: str = "") -> None:
        try:
            media_argument, tilt, zoom, speed, clockwise = _parse_spin3d_input(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error

        normalized, adjustments = _normalize_effect_options(
            "spin3d",
            {"tilt": tilt, "zoom": zoom, "speed": speed},
        )
        tilt = float(normalized["tilt"])
        zoom = float(normalized["zoom"])
        speed = float(normalized["speed"])
        try:
            media_url = await self._convert_effect_source(ctx, media_argument)
        except commands.BadArgument as error:
            raise commands.BadArgument(
                "No image found. Attach one, reply to an image, or provide a URL."
            ) from error

        async with self.bot.media_semaphore, ctx.typing():
            import time

            started = time.time()
            image_data = await self._fetch_effect_media(ctx, media_url)
            try:
                output = await make_spin3d(
                    image_data,
                    tilt,
                    zoom,
                    speed,
                    clockwise,
                    TEMP_MEDIA_MAX_BYTES,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            await self._send_effect_result(
                ctx,
                EffectResult(output.getvalue(), "spin3d.gif"),
                started=started,
                note="; ".join(adjustments),
            )

    @media_effect_timeout
    async def _caption_effect(
        self,
        ctx: Context,
        *,
        text: str = "",
        media: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ):
        """Add a caption to a User/Emoji/Media URL."""

        image_url = ""
        caption_text = text

        if media:
            try:
                image_url = await self._convert_effect_source(
                    ctx, media, include_message_media=False
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
                    maybe_url = await self._convert_effect_source(
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
                        maybe_url = await self._convert_effect_source(
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
                image_url = await self._convert_effect_source(ctx, "")
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "No image or video found. Attach one, reply to media, or use a media URL."
                ) from error

        if len(caption_text) > 1000:
            raise commands.BadArgument("Captions are limited to 1,000 characters.")

        async with self.bot.media_semaphore, ctx.typing():
            import time

            started = time.time()
            img_data = await self._fetch_effect_media(ctx, image_url)
            inline_images = await resolve_inline_images(
                ctx.session,
                [caption_text],
            )
            buf, filename = await make_caption(
                img_data,
                caption_text,
                inline_images,
                force_gif=_is_klipy_media_url(image_url),
            )

            if filename.endswith(".mp4"):
                buf.seek(0, 2)
                size = buf.tell()
                if size > 20 * 1024 * 1024:
                    buf.seek(0)
                    data = await _compress_video(buf.read())
                    buf = BytesIO(data)
                buf.seek(0)

            await self._send_effect_result(
                ctx,
                EffectResult(buf.read(), filename),
                started=started,
            )

    @media_effect_timeout
    async def _speed_effect(self, ctx: Context, *, input: str) -> None:
        media, options = _parse_effect_flags(
            input,
            values=PIPELINE_EFFECTS["speed"][1],
        )
        try:
            tokens = shlex.split(media)
        except ValueError as error:
            raise commands.BadArgument(
                "The speed arguments contain an unmatched quote."
            ) from error

        speed_val = float(options["speed"])
        numeric_index: int | None = None
        for candidate_index in (0, len(tokens) - 1):
            if not tokens or candidate_index < 0:
                continue
            candidate = tokens[candidate_index]
            if candidate.isdecimal() and len(candidate) >= 15:
                continue
            try:
                speed_val = float(candidate)
            except ValueError:
                continue
            numeric_index = candidate_index
            break
        if numeric_index is not None:
            tokens.pop(numeric_index)
        image_str = " ".join(tokens)

        normalized, adjustments = _normalize_effect_options(
            "speed",
            {
                "speed": speed_val,
                "start": options["start"],
                "stop": options["stop"],
            },
        )
        speed_val = float(normalized["speed"])
        start = float(normalized["start"])
        stop = float(normalized["stop"])
        try:
            _playback_factor(speed_val)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error

        if image_str:
            try:
                image_str = await self._convert_effect_source(ctx, image_str)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "That media URL, user, or emoji is not supported."
                ) from error
        else:
            try:
                image_str = await self._convert_effect_source(ctx, "")
            except commands.BadArgument:
                raise commands.BadArgument("No image, video, or audio found.")

        if not image_str or not isinstance(image_str, str):
            raise commands.BadArgument(
                "Could not resolve an image, video, or audio source."
            )
        async with self.bot.media_semaphore, ctx.typing():
            started = time.monotonic()
            img_data = await self._fetch_effect_media(ctx, image_str)
            try:
                probe = await probe_media(img_data)
                if probe.has_video:
                    result = EffectResult(
                        await _speed_video(
                            img_data,
                            speed_val,
                            start=start,
                            stop=stop,
                        ),
                        "speed.mp4",
                    )
                elif probe.has_audio:
                    result = EffectResult(
                        await _speed_audio(
                            img_data,
                            speed_val,
                            start=start,
                            stop=stop,
                        ),
                        "speed.mp3",
                        displayable=False,
                    )
                else:
                    raise ValueError("Speed requires video, GIF, or audio media.")
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note="; ".join(adjustments),
            )

    @commands.command(
        name="globe",
        aliases=("sphere3d", "imageglobe"),
        extras={"usage": "<media> [-speed 1 -clockwise]"},
    )
    async def globe(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap a User/Emoji/Media URL around a rotating globe.

        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        await self._globe_effect(ctx, argument=argument)

    @commands.command(
        name="spin3d",
        aliases=("spin3", "3dspin"),
        extras={"usage": "<media> [-tilt 15 -zoom 1.5 -speed 1 -clockwise]"},
    )
    async def spin3d(self, ctx: Context, *, argument: str = "") -> None:
        """Spin a User/Emoji/Media URL in 3D space.

        -# -tilt         Change the vertical tilt. Defaults to 15.
        -# -zoom         Change the media zoom. Defaults to 1.5.
        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        await self._spin3d_effect(ctx, argument=argument)

    @commands.command(
        name="caption",
        extras={"usage": "<text> [media]"},
    )
    async def caption(
        self,
        ctx: Context,
        *,
        text: str = "",
        media: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add a caption to a User/Emoji/Media URL."""
        await self._caption_effect(
            ctx,
            text=text,
            media=media,
            user=user,
            attachment=attachment,
        )

    @commands.command(
        name="speed",
        extras={"usage": "<media> <speed> [-start 0 -stop 0]"},
    )
    async def speed(self, ctx: Context, *, input: str = "") -> None:
        """Change a User/Emoji/Media URL from -5 to -1 (slower) or 1 to 5 (faster)."""
        await self._speed_effect(ctx, input=input)

    @commands.command(
        name="invert",
        extras={"usage": "<media> [-preserve-transparency]"},
    )
    async def invert(self, ctx: Context, *, argument: str = "") -> None:
        """Invert the colors of a User/Emoji/Media URL.

        -# -preserve-transparency    Keep transparent backgrounds transparent.
        """
        media, options = _parse_effect_flags(
            argument,
            switches={
                "preserve_transparency": (
                    "preserve-transparency",
                    "preserve",
                    "transparent",
                    "keep-alpha",
                    "pt",
                )
            },
        )
        await self._apply_image_effect(ctx, "invert", source=media, **options)

    @commands.command(
        name="spin",
        extras={"usage": "<media> [-speed 1 -clockwise]"},
    )
    async def spin(self, ctx: Context, *, argument: str = "") -> None:
        """Rotate a User/Emoji/Media URL in a flat animated loop.

        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"speed": (("s",), float, 1.0)},
            switches={"clockwise": ("c",)},
        )
        await self._apply_image_effect(ctx, "spin", source=media, **options)

    @commands.command(
        name="magik",
        extras={"usage": "<media> [-strength 20]"},
    )
    async def magik(self, ctx: Context, *, argument: str = "") -> None:
        """Distort a User/Emoji/Media URL with a liquid effect.

        -# -strength    Change the distortion strength. Defaults to 20.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"strength": (("amount", "s"), float, 20.0)},
        )
        await self._apply_image_effect(ctx, "magik", source=media, **options)

    @commands.command(
        name="amagik",
        aliases=("animatedmagik", "gmagik", "gifmagik"),
        extras={"usage": "<media> [-strength 20 -speed 1]"},
    )
    async def amagik(self, ctx: Context, *, argument: str = "") -> None:
        """Animate a liquid distortion over a User/Emoji/Media URL.

        -# -strength    Change the distortion strength. Defaults to 20.
        -# -speed       Change the animation speed. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "strength": (("amount",), float, 20.0),
                "speed": (("s",), float, 1.0),
            },
        )
        await self._apply_image_effect(ctx, "gifmagik", source=media, **options)

    @commands.command(
        name="flip",
        extras={"usage": "<media> [-horizontal | -vertical]"},
    )
    async def flip(self, ctx: Context, *, argument: str = "") -> None:
        """Flip a User/Emoji/Media URL horizontally or vertically.

        -# -horizontal    Flip the media horizontally. This is the default.
        -# -vertical      Flip the media vertically.
        """
        media, options = _parse_effect_flags(
            argument,
            switches={
                "vertical": ("v", "ver"),
                "horizontal": ("h", "hor"),
            },
        )
        direction = "vertical" if options["vertical"] else "horizontal"
        await self._apply_image_effect(
            ctx,
            "flip",
            source=media,
            direction=direction,
        )

    @commands.command(
        name="cube",
        extras={"usage": "<media> [-speed 1 -clockwise]"},
    )
    async def cube(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap a User/Emoji/Media URL around a rotating cube.

        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"speed": (("s",), float, 1.0)},
            switches={"clockwise": ("c",)},
        )
        await self._apply_image_effect(ctx, "cube", source=media, **options)

    @commands.command(
        name="pyramid",
        extras={"usage": "<media> [-speed 1 -clockwise]"},
    )
    async def pyramid(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap a User/Emoji/Media URL around a rotating pyramid.

        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"speed": (("s",), float, 1.0)},
            switches={"clockwise": ("c",)},
        )
        await self._apply_image_effect(ctx, "pyramid", source=media, **options)

    @commands.command(
        name="blur",
        extras={"usage": "<media> [-radius 5 -type gaussian]"},
    )
    async def blur(self, ctx: Context, *, argument: str = "") -> None:
        """Blur a User/Emoji/Media URL.

        -# -radius    Change the blur strength. Defaults to 5.
        -# -type      Choose gaussian, box, or motion blur.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "radius": (("r", "strength"), float, 5.0),
                "blur_type": (("type", "mode", "t"), str, "gaussian"),
            },
        )
        await self._apply_image_effect(ctx, "blur", source=media, **options)

    @commands.command(
        name="deepfry",
        extras={"usage": "<media> [-intensity 1] [-preserve-transparency]"},
    )
    async def deepfry(self, ctx: Context, *, argument: str = "") -> None:
        """Deep-fry a User/Emoji/Media URL.

        -# -intensity                Change the effect intensity. Defaults to 1.
        -# -preserve-transparency    Keep transparent backgrounds transparent.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"intensity": (("amount", "i"), float, 1.0)},
            switches={
                "preserve_transparency": (
                    "preserve-transparency",
                    "preserve",
                    "transparent",
                    "keep-alpha",
                    "pt",
                )
            },
        )
        await self._apply_image_effect(ctx, "deepfry", source=media, **options)

    @commands.command(
        name="grayscale",
        aliases=("greyscale",),
        extras={"usage": "<media> [-preserve-transparency]"},
    )
    async def grayscale(self, ctx: Context, *, argument: str = "") -> None:
        """Convert a User/Emoji/Media URL to grayscale.

        -# -preserve-transparency    Keep transparent backgrounds transparent.
        """
        media, options = _parse_effect_flags(
            argument,
            switches={
                "preserve_transparency": (
                    "preserve-transparency",
                    "preserve",
                    "transparent",
                    "keep-alpha",
                    "pt",
                )
            },
        )
        await self._apply_image_effect(ctx, "grayscale", source=media, **options)

    @commands.command(
        name="jpeg",
        aliases=("needsmorejpeg",),
        extras={"usage": "<media> [-quality 8]"},
    )
    async def jpeg(self, ctx: Context, *, argument: str = "") -> None:
        """Make a User/Emoji/Media URL look heavily JPEG-compressed.

        -# -quality    Change the JPEG quality from 1 to 50. Defaults to 8.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"quality": (("q",), int, 8)},
        )
        await self._apply_image_effect(ctx, "jpeg", source=media, **options)

    @commands.command(
        name="swirl",
        extras={"usage": "<media> [-strength 180]"},
    )
    async def swirl(self, ctx: Context, *, argument: str = "") -> None:
        """Swirl a User/Emoji/Media URL.

        -# -strength    Change the swirl in degrees. Defaults to 180.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"strength": (("degrees", "s"), float, 180.0)},
        )
        await self._apply_image_effect(ctx, "swirl", source=media, **options)

    @commands.command(
        name="aswirl",
        aliases=("animatedswirl", "gifswirl", "gswirl"),
        extras={"usage": "<media> [-strength 180 -speed 1]"},
    )
    async def aswirl(self, ctx: Context, *, argument: str = "") -> None:
        """Wind up a swirling distortion across a User/Emoji/Media URL.

        -# -strength    Change the swirl in degrees. Defaults to 180.
        -# -speed       Change the animation speed. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "strength": (("degrees",), float, 180.0),
                "speed": (("s",), float, 1.0),
            },
        )
        await self._apply_image_effect(ctx, "gifswirl", source=media, **options)

    @commands.command(name="hallway", extras={"usage": "<media>"})
    async def hallway(self, ctx: Context, *, argument: str = "") -> None:
        """Animate a User/Emoji/Media URL into a hallway effect."""
        await self._parsed_effect(ctx, "hallway", argument)

    @commands.command(name="parallax", extras={"usage": "<media> [-speed 1]"})
    async def parallax(self, ctx: Context, *, argument: str = "") -> None:
        """Add a moving parallax effect to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx, "parallax", argument, values={"speed": (("s",), float, 1.0)}
        )

    @commands.command(
        name="huerotate",
        aliases=("hue", "hueshift"),
        extras={"usage": "<media> [-degrees 180]"},
    )
    async def huerotate(self, ctx: Context, *, argument: str = "") -> None:
        """Rotate the colors of a User/Emoji/Media URL.

        -# -degrees    Change the hue rotation. Defaults to 180.
        """
        await self._parsed_effect(
            ctx,
            "huerotate",
            argument,
            values={"degrees": (("angle", "d"), float, 180.0)},
        )

    @commands.command(name="zoom", extras={"usage": "<media> [amount|forever]"})
    async def zoom(self, ctx: Context, *, argument: str = "") -> None:
        """Zoom a User/Emoji/Media URL, or loop the zoom forever.

        -# -amount     Change the zoom factor. Defaults to 2.
        -# -forever    Keep the zoom looping back and forth.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"amount": (("factor", "z"), float, 2.0)},
            switches={"forever": ("infinite", "loop")},
        )
        lowered_media = media.casefold()
        if lowered_media in {"forever", "infinite"}:
            media = ""
            options["forever"] = True
        elif lowered_media.startswith(("forever ", "infinite ")):
            media = media.split(" ", 1)[1]
            options["forever"] = True
        elif lowered_media.endswith((" forever", " infinite")):
            media = media.rsplit(" ", 1)[0]
            options["forever"] = True
        else:
            parts = media.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    options["amount"] = float(parts[1])
                except ValueError:
                    pass
                else:
                    media = parts[0]
        await self._apply_image_effect(ctx, "zoom", source=media, **options)

    @commands.command(name="squishy", extras={"usage": "<media> [-amount 0.18]"})
    async def squishy(self, ctx: Context, *, argument: str = "") -> None:
        """Make a User/Emoji/Media URL pulse and squash."""
        await self._parsed_effect(
            ctx,
            "squishy",
            argument,
            values={"amount": (("strength", "a"), float, 0.18)},
        )

    @commands.command(name="glitch", extras={"usage": "<media> [-amount 12]"})
    async def glitch(self, ctx: Context, *, argument: str = "") -> None:
        """Add a glitch effect to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx, "glitch", argument, values={"amount": (("strength", "a"), float, 12.0)}
        )

    @commands.command(name="tremble", extras={"usage": "<media> [-amount 8]"})
    async def tremble(self, ctx: Context, *, argument: str = "") -> None:
        """Make a User/Emoji/Media URL tremble."""
        await self._parsed_effect(
            ctx, "tremble", argument, values={"amount": (("strength", "a"), float, 8.0)}
        )

    @commands.command(name="quilt", extras={"usage": "<media> [-tiles 3]"})
    async def quilt(self, ctx: Context, *, argument: str = "") -> None:
        """Turn a User/Emoji/Media URL into a tiled quilt."""
        await self._parsed_effect(
            ctx, "quilt", argument, values={"tiles": (("grid", "t"), int, 3)}
        )

    @commands.command(
        name="removebars", aliases=("remove-bars",), extras={"usage": "<media>"}
    )
    async def remove_bars(self, ctx: Context, *, media: str = "") -> None:
        """Remove black bars from a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "removebars", source=media)

    @commands.command(
        name="removecaption", aliases=("remove-caption",), extras={"usage": "<media>"}
    )
    async def remove_caption(self, ctx: Context, *, media: str = "") -> None:
        """Remove caption bars from a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "removecaption", source=media)

    @commands.group(name="remove", invoke_without_command=True)
    async def remove_group(self, ctx: Context) -> None:
        """Remove bars or captions from a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @remove_group.command(name="bars", extras={"usage": "<media>"})
    async def remove_group_bars(self, ctx: Context, *, media: str = "") -> None:
        """Remove black bars from a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "removebars", source=media)

    @remove_group.command(name="caption", extras={"usage": "<media>"})
    async def remove_group_caption(self, ctx: Context, *, media: str = "") -> None:
        """Remove caption bars from a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "removecaption", source=media)

    @remove_group.group(name="outro", invoke_without_command=True)
    async def remove_group_outro(self, ctx: Context) -> None:
        """Remove a recognized social-media outro from a video."""
        await ctx.send_help(ctx.command)

    @remove_group_outro.command(name="tiktok", extras={"usage": "<media>"})
    async def remove_group_outro_tiktok(
        self,
        ctx: Context,
        *,
        media: str = "",
    ) -> None:
        """Detect and remove a TikTok outro from a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "removeoutrotiktok", source=media)

    @remove_group_outro.command(name="reels", extras={"usage": "<media>"})
    async def remove_group_outro_reels(
        self,
        ctx: Context,
        *,
        media: str = "",
    ) -> None:
        """Detect and remove a Reels outro from a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "removeoutroreels", source=media)

    @commands.command(
        name="enlarge",
        aliases=("e",),
        extras={"usage": "<media> [-amount 2]"},
    )
    async def enlarge(self, ctx: Context, *, argument: str = "") -> None:
        """Enlarge a User/Emoji/Media URL with a preset scale."""
        await self._parsed_effect(
            ctx, "enlarge", argument, values={"amount": (("factor", "s"), float, 2.0)}
        )

    @commands.command(name="falsecolor", extras={"usage": "<media>"})
    async def falsecolor(self, ctx: Context, *, media: str = "") -> None:
        """Apply a false-color palette to a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "falsecolor", source=media)

    @commands.command(name="watercolor", extras={"usage": "<media>"})
    async def watercolor(self, ctx: Context, *, media: str = "") -> None:
        """Give a User/Emoji/Media URL a watercolor look."""
        await self._apply_image_effect(ctx, "watercolor", source=media)

    @commands.command(name="oilpaint", extras={"usage": "<media>"})
    async def oilpaint(self, ctx: Context, *, media: str = "") -> None:
        """Give a User/Emoji/Media URL an oil-paint look."""
        await self._apply_image_effect(ctx, "oilpaint", source=media)

    @commands.command(name="random", extras={"usage": "<media>"})
    async def random_effect(self, ctx: Context, *, media: str = "") -> None:
        """Apply a random compatible effect to a User/Emoji/Media URL."""
        chosen = _random_effect_choices(1)[0]
        await self._apply_image_effect(
            ctx,
            chosen,
            source=media,
            note=f"Applied: random ({chosen})",
        )

    @commands.command(name="meme", extras={"usage": "<media> <text>"})
    async def meme(self, ctx: Context, *, argument: str = "") -> None:
        """Add impact-style top and bottom text to a User/Emoji/Media URL.

        -# -text       Text to place on the media. Use `|` to split top and bottom.
        """
        media, options = _parse_effect_flags(
            argument, values={"text": (("t",), str, "")}
        )
        if not options["text"]:
            parts = media.split(maxsplit=1)
            if len(parts) < 2:
                raise commands.BadArgument("Meme requires media followed by text.")
            candidate, text_value = parts
            if ctx.message.attachments and not candidate.startswith(
                ("http://", "https://", "<@", "<:", "<a:")
            ):
                options["text"] = media
                media = ""
            else:
                media, options["text"] = candidate, text_value
        await self._apply_image_effect(ctx, "meme", source=media, **options)

    @commands.command(
        name="wiggle",
        extras={"usage": "<media> [-amount 8 -speed 1]"},
    )
    async def wiggle(self, ctx: Context, *, argument: str = "") -> None:
        """Make a User/Emoji/Media URL wiggle.

        -# -amount    Change how far the media moves. Defaults to 8.
        -# -speed     Change the animation speed. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "amount": (("strength",), float, 8.0),
                "speed": (("s",), float, 1.0),
            },
        )
        await self._apply_image_effect(ctx, "wiggle", source=media, **options)

    async def _parsed_effect(
        self,
        ctx: Context,
        effect: str,
        argument: str,
        *,
        values: dict[str, tuple[tuple[str, ...], type, Any]] | None = None,
        switches: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        media, options = _parse_effect_flags(
            argument,
            values=values,
            switches=switches,
        )
        await self._apply_image_effect(ctx, effect, source=media, **options)

    @commands.command(
        name="lag",
        extras={
            "usage": (
                "<media> [-amount 4] [-method random|freeze|stutter|drop|jitter] "
                "[-multi]"
            )
        },
    )
    async def lag(self, ctx: Context, *, argument: str = "") -> None:
        """Make a User/Emoji/Media URL lag at randomized moments.

        -# -method    Pick random, freeze, stutter, drop, or jitter.
        -# -multi     Combine more than one lag method.
        """
        await self._parsed_effect(
            ctx,
            "lag",
            argument,
            values={
                "amount": (("a",), int, 4),
                "method": (("type", "m"), str, "random"),
            },
            switches={"multi": ("multiple", "combine")},
        )

    @commands.command(name="shuffle", extras={"usage": "<media>"})
    async def shuffle(self, ctx: Context, *, media: str = "") -> None:
        """Shuffle the frame order of a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "shuffle", source=media)

    @commands.command(
        name="average-colors",
        aliases=(
            "averagecolors",
            "avgcolors",
            "avgcs",
            "average-colours",
            "averagecolours",
            "avgcolours",
            "averagecolor",
            "avgcolor",
            "averagecolour",
            "avgcolour",
            "avgc",
        ),
        extras={"usage": "<media>"},
    )
    async def average_colors(self, ctx: Context, *, media: str = "") -> None:
        """Create a labeled palette from a still User/Emoji/Media URL image."""
        await self._average_colors_effect(ctx, source=media)

    @commands.command(
        name="tint",
        extras={"usage": "<media> [-color #5865f2 -amount 0.35]"},
    )
    async def tint(self, ctx: Context, *, argument: str = "") -> None:
        """Tint a User/Emoji/Media URL with a CSS color or hex value."""
        await self._parsed_effect(
            ctx,
            "tint",
            argument,
            values={
                "color": (("c",), str, "#5865f2"),
                "amount": (("strength", "a"), float, 0.35),
            },
        )

    @commands.command(name="implode", extras={"usage": "<media> [-strength 0.5]"})
    async def implode(self, ctx: Context, *, argument: str = "") -> None:
        """Pull a User/Emoji/Media URL inward toward its center."""
        await self._parsed_effect(
            ctx,
            "implode",
            argument,
            values={"strength": (("amount", "s"), float, 0.5)},
        )

    @commands.command(name="explode", extras={"usage": "<media> [-strength 0.5]"})
    async def explode(self, ctx: Context, *, argument: str = "") -> None:
        """Push a User/Emoji/Media URL outward from its center."""
        await self._parsed_effect(
            ctx,
            "explode",
            argument,
            values={"strength": (("amount", "s"), float, 0.5)},
        )

    @commands.command(name="sharpen", extras={"usage": "<media> [-amount 2]"})
    async def sharpen(self, ctx: Context, *, argument: str = "") -> None:
        """Sharpen a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "sharpen",
            argument,
            values={"amount": (("strength", "a"), float, 2.0)},
        )

    @commands.command(
        name="legoify",
        aliases=("legofy",),
        extras={"usage": "<media> [-size 12]"},
    )
    async def legoify(self, ctx: Context, *, argument: str = "") -> None:
        """Turn a User/Emoji/Media URL into a grid of Lego-like studs."""
        await self._parsed_effect(
            ctx,
            "legoify",
            argument,
            values={"size": (("block", "s"), int, 12)},
        )

    @commands.command(
        name="bounce",
        extras={"usage": "<media> [-amount 20 -speed 1]"},
    )
    async def bounce(self, ctx: Context, *, argument: str = "") -> None:
        """Bounce a User/Emoji/Media URL in an animated loop."""
        await self._parsed_effect(
            ctx,
            "bounce",
            argument,
            values={
                "amount": (("height", "a"), float, 20.0),
                "speed": (("s",), float, 1.0),
            },
        )

    @commands.command(name="fisheye", extras={"usage": "<media> [-strength 0.65]"})
    async def fisheye(self, ctx: Context, *, argument: str = "") -> None:
        """Apply a fisheye distortion to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "fisheye",
            argument,
            values={"strength": (("amount", "s"), float, 0.65)},
        )

    @commands.command(name="sepia", extras={"usage": "<media> [-amount 1]"})
    async def sepia(self, ctx: Context, *, argument: str = "") -> None:
        """Apply a sepia effect to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "sepia",
            argument,
            values={"amount": (("strength", "a"), float, 1.0)},
        )

    @commands.command(name="pixelate", extras={"usage": "<media> [-size 12]"})
    async def pixelate(self, ctx: Context, *, argument: str = "") -> None:
        """Pixelate a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "pixelate",
            argument,
            values={"size": (("block", "s"), int, 12)},
        )

    @commands.command(
        name="slidein",
        aliases=("slide-in",),
        extras={"usage": "<media> [-direction left -duration 1]"},
    )
    async def slide_in(self, ctx: Context, *, argument: str = "") -> None:
        """Slide a User/Emoji/Media URL into frame."""
        await self._parsed_effect(
            ctx,
            "slidein",
            argument,
            values={
                "direction": (("dir",), str, "left"),
                "duration": (("d",), float, 1.0),
            },
        )

    @commands.command(
        name="slideout",
        aliases=("slide-out",),
        extras={"usage": "<media> [-direction left -duration 1]"},
    )
    async def slide_out(self, ctx: Context, *, argument: str = "") -> None:
        """Slide a User/Emoji/Media URL out of frame."""
        await self._parsed_effect(
            ctx,
            "slideout",
            argument,
            values={
                "direction": (("dir",), str, "left"),
                "duration": (("d",), float, 1.0),
            },
        )

    @commands.command(name="vignette", extras={"usage": "<media> [-amount 0.65]"})
    async def vignette(self, ctx: Context, *, argument: str = "") -> None:
        """Darken the edges of a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "vignette",
            argument,
            values={"amount": (("strength", "a"), float, 0.65)},
        )

    @commands.command(
        name="resize",
        aliases=("size",),
        extras={"usage": "<media> [-scale 1 -ratio 16:9]"},
    )
    async def resize(self, ctx: Context, *, argument: str = "") -> None:
        """Resize a User/Emoji/Media URL.

        -# -scale  Scale both dimensions by a multiplier.
        -# -ratio  Crop to an aspect ratio such as 16:9.
        -# -size   Set an exact size such as 1280x720.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "scale": (("s",), float, 1.0),
                "ratio": (("aspect", "r"), str, ""),
                "size": (("dimensions", "dim"), str, ""),
            },
        )
        if not options["size"]:
            tokens = shlex.split(media)
            if tokens and RESIZE_DIMENSIONS_RE.fullmatch(tokens[-1]):
                options["size"] = tokens.pop()
                media = " ".join(tokens)
        await self._apply_image_effect(
            ctx,
            "resize",
            source=media,
            **options,
        )

    @commands.command(
        name="text-effect",
        aliases=("addtext",),
        extras={
            "usage": (
                '<media> -text "hello" [-font Roboto -size 48 -position center '
                "-color #ffffff -style outline -bold]"
            )
        },
    )
    async def text_effect(self, ctx: Context, *, argument: str = "") -> None:
        """Add styled Unicode and emoji text to a User/Emoji/Media URL.

        -# -text      Text to draw, including Unicode or Discord emoji.
        -# -font      One of the bundled font family names.
        -# -size      Font size from 8 to 512.
        -# -position  A named position such as center or bottom-right.
        -# -style     Use normal, outline, shadow, or box.
        -# -bold      Enable the bold font variation or bold rendering.
        """
        media, options = _parse_effect_flags(
            argument,
            values=PIPELINE_EFFECTS["text"][1],
            switches=PIPELINE_EFFECTS["text"][2],
        )
        if not options["text"] and "|" in media:
            media, text = media.split("|", 1)
            options["text"] = text.strip()
        await self._apply_text_effect(ctx, source=media.strip(), **options)

    @commands.command(
        name="combine",
        aliases=("joinmedia", "stackmedia"),
        extras={
            "usage": (
                "<media> -second <media> " "[-position right -mode resize -audio mix]"
            )
        },
    )
    async def combine_effect(self, ctx: Context, *, argument: str = "") -> None:
        """Combine two User/Emoji/Media URLs on one extended canvas.

        -# -second    The second item to place beside the first.
        -# -position  Place it at top, bottom, left, or right.
        -# -mode      Resize proportionally, stretch, or keep original size.
        -# -audio     Mix both sources, use first/second, or use none.
        """
        media, options = _parse_effect_flags(
            argument,
            values=PIPELINE_EFFECTS["combine"][1],
        )
        second_source = str(options.pop("second", "")).strip()
        await self._apply_combine_effect(
            ctx,
            source=media.strip(),
            second_source=second_source,
            **options,
        )

    @commands.command(name="distort", extras={"usage": "<media> [-amount 0.25]"})
    async def distort(self, ctx: Context, *, argument: str = "") -> None:
        """Shear and distort a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "distort",
            argument,
            values={"amount": (("strength", "a"), float, 0.25)},
        )

    @commands.command(name="grain", extras={"usage": "<media> [-amount 20]"})
    async def grain(self, ctx: Context, *, argument: str = "") -> None:
        """Add monochrome grain to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "grain",
            argument,
            values={"amount": (("strength", "a"), float, 20.0)},
        )

    @commands.command(
        name="rotate",
        aliases=("rotation",),
        extras={"usage": "<media> [-degrees 90]"},
    )
    async def rotate(self, ctx: Context, *, argument: str = "") -> None:
        """Rotate a User/Emoji/Media URL by a number of degrees."""
        await self._parsed_effect(
            ctx,
            "rotate",
            argument,
            values={"degrees": (("angle", "d"), float, 90.0)},
        )

    @commands.command(name="noise", extras={"usage": "<media> [-amount 20]"})
    async def noise(self, ctx: Context, *, argument: str = "") -> None:
        """Add colored noise to a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "noise",
            argument,
            values={"amount": (("strength", "a"), float, 20.0)},
        )

    @commands.command(name="brightness", extras={"usage": "<media> [-amount 1]"})
    async def brightness(self, ctx: Context, *, argument: str = "") -> None:
        """Adjust the brightness of a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "brightness",
            argument,
            values={"amount": (("value", "a"), float, 1.0)},
        )

    @commands.command(name="contrast", extras={"usage": "<media> [-amount 1]"})
    async def contrast(self, ctx: Context, *, argument: str = "") -> None:
        """Adjust the contrast of a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "contrast",
            argument,
            values={"amount": (("value", "a"), float, 1.0)},
        )

    @commands.command(name="saturation", extras={"usage": "<media> [-amount 1]"})
    async def saturation(self, ctx: Context, *, argument: str = "") -> None:
        """Adjust the saturation of a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "saturation",
            argument,
            values={"amount": (("value", "a"), float, 1.0)},
        )

    @commands.command(name="exposure", extras={"usage": "<media> [-stops 0]"})
    async def exposure(self, ctx: Context, *, argument: str = "") -> None:
        """Adjust the exposure of a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "exposure",
            argument,
            values={"stops": (("amount", "s"), float, 0.0)},
        )

    @commands.command(name="reverse", extras={"usage": "<media>"})
    async def reverse(self, ctx: Context, *, media: str = "") -> None:
        """Reverse a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "reverse", source=media)

    @commands.command(name="adhd", extras={"usage": "<media>"})
    async def adhd(self, ctx: Context, *, media: str = "") -> None:
        """Randomly speed up and slow down a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "adhd", source=media)

    @commands.command(
        name="convert",
        aliases=("covert",),
        extras={"usage": "<media> [-format mp4] | <amount> <unit> [to|into] <unit>"},
    )
    async def convert_command(self, ctx: Context, *, argument: str = "") -> None:
        """Convert media files, currencies, or measurements.

        Media conversion accepts a User/Emoji/Media URL and can use
        ``-format`` to choose another file format. Values can be converted
        between supported currencies, Robux, temperatures, distances, and
        time units, for example ``10 usd to pounds`` or ``5m into sec``.

        -# -format    Choose the output format for media. Defaults to MP4.
        """
        conversion = parse_conversion_expression(argument)
        if conversion is not None:
            try:
                async with ctx.typing():
                    result = await convert_request(ctx, conversion)
            except ConversionError as error:
                raise commands.BadArgument(str(error)) from error
            await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())
            return

        media, options = _parse_effect_flags(
            argument,
            values={"format": (("f",), str, "mp4")},
        )
        await self._convert_effect(ctx, str(options["format"]), source=media)

    @commands.command(
        name="volume",
        extras={"usage": "<media> [-volume 1 -start 0 -stop 0 -duration 0]"},
    )
    async def volume(self, ctx: Context, *, argument: str = "") -> None:
        """Change the volume of a User/Emoji/Media URL.

        -# -volume    Change the volume multiplier. Defaults to 1.
        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(volume=(("amount", "v"), float, 1.0)),
        )
        await self._apply_video_effect(ctx, "volume", source=media, **options)

    @commands.group(name="crop", invoke_without_command=True)
    async def crop_group(self, ctx: Context) -> None:
        """Crop a User/Emoji/Media URL into a shape."""
        await ctx.send_help(ctx.command)

    @crop_group.command(name="circle", extras={"usage": "<media>"})
    async def crop_group_circle(self, ctx: Context, *, media: str = "") -> None:
        """Crop a User/Emoji/Media URL into a circle."""
        await self._apply_image_effect(ctx, "crop", source=media, shape="circle")

    @crop_group.command(name="triangle", extras={"usage": "<media>"})
    async def crop_group_triangle(self, ctx: Context, *, media: str = "") -> None:
        """Crop a User/Emoji/Media URL into a triangle."""
        await self._apply_image_effect(ctx, "crop", source=media, shape="triangle")

    @commands.group(name="fade", invoke_without_command=True)
    async def fade_group(self, ctx: Context) -> None:
        """Fade a User/Emoji/Media URL in or out."""
        await ctx.send_help(ctx.command)

    @fade_group.command(
        name="in",
        extras={"usage": "<media> [-duration 1]"},
    )
    async def fade_group_in(self, ctx: Context, *, argument: str = "") -> None:
        """Fade a User/Emoji/Media URL in from transparent or black."""
        await self._parsed_effect(
            ctx,
            "fadein",
            argument,
            values={"duration": (("d",), float, 1.0)},
        )

    @fade_group.command(
        name="out",
        extras={"usage": "<media> [-duration 1]"},
    )
    async def fade_group_out(self, ctx: Context, *, argument: str = "") -> None:
        """Fade a User/Emoji/Media URL out to transparent or black."""
        await self._parsed_effect(
            ctx,
            "fadeout",
            argument,
            values={"duration": (("d",), float, 1.0)},
        )

    @commands.group(name="mirror", invoke_without_command=True)
    async def mirror_group(self, ctx: Context) -> None:
        """Mirror one side of a User/Emoji/Media URL onto the other."""
        await ctx.send_help(ctx.command)

    @mirror_group.command(name="bottom", extras={"usage": "<media>"})
    async def mirror_group_bottom(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the bottom half of a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="bottom")

    @mirror_group.command(name="right", extras={"usage": "<media>"})
    async def mirror_group_right(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the right half of a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="right")

    @mirror_group.command(name="left", extras={"usage": "<media>"})
    async def mirror_group_left(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the left half of a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="left")

    @mirror_group.command(name="top", extras={"usage": "<media>"})
    async def mirror_group_top(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the top half of a User/Emoji/Media URL."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="top")

    @commands.group(
        name="overlay",
        invoke_without_command=True,
        extras={
            "usage": (
                "<media> <user|media|emoji|asset|flag> [name|random] "
                "[-opacity 70 -scale 1 -size 100x100 -start 0 -stop 0 "
                "-position center -x 0 -y 0 -stretch -extend -no-audio -fr]"
            )
        },
    )
    async def overlay_group(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay one User/Emoji/Media URL on another.

        -# -fr           Randomize the overlay size and position within the background.
        """
        if not argument.strip():
            await ctx.send_help(ctx.command)
            return
        source, second_source, options = _parse_overlay_text_argument(argument)
        options["overlay_audio"] = not bool(options.pop("no_audio", False))
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=source,
            second_source=second_source,
            **options,
        )

    @overlay_group.command(
        name="flag",
        extras={"usage": "<media> [-flag pride -opacity 35]"},
    )
    async def overlay_group_flag(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a pride, country, or pirate flag on a User/Emoji/Media URL.

        -# -flag       Choose a pride flag, pirate flag, country name, or code.
        -# -opacity    Change the flag opacity from 0 to 100%. Defaults to 35.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "flag": (("f",), str, "pride"),
                "opacity": (("o",), float, 35.0),
            },
        )
        flag = str(options.pop("flag"))
        if flag == "pride" and not re.search(
            r"(?:^|\s)-{1,2}(?:flag|f)(?:[=\s]|$)", argument
        ):
            media, positional = _positional_flag(media)
            if positional is not None:
                flag = positional
        options["overlay_data"] = await self._flag_data(ctx, flag)
        options["_flag_name"] = flag
        options["stretch"] = True
        await self._apply_image_effect(ctx, "overlay", source=media, **options)

    @commands.group(name="bass", invoke_without_command=True)
    async def bass_group(self, ctx: Context) -> None:
        """Change the bass of a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @bass_group.command(
        name="boost",
        extras={"usage": "<media> [-gain 12 -start 0 -stop 0 -duration 0]"},
    )
    async def bass_group_boost(self, ctx: Context, *, argument: str = "") -> None:
        """Boost the bass of a User/Emoji/Media URL.

        -# -gain    Change the bass gain in decibels. Defaults to 12.
        -# -start   Start the effect at this time in seconds.
        -# -stop    Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(gain=(("amount", "g"), float, 12.0)),
        )
        await self._apply_video_effect(ctx, "bassboost", source=media, **options)

    @bass_group.command(
        name="lower",
        extras={"usage": "<media> [-gain 12 -start 0 -stop 0 -duration 0]"},
    )
    async def bass_group_lower(self, ctx: Context, *, argument: str = "") -> None:
        """Lower the bass of a User/Emoji/Media URL.

        -# -gain    Change the bass reduction in decibels. Defaults to 12.
        -# -start   Start the effect at this time in seconds.
        -# -stop    Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(gain=(("amount", "g"), float, 12.0)),
        )
        await self._apply_video_effect(ctx, "basslower", source=media, **options)

    @commands.command(
        name="sound-effects",
        aliases=("soundeffects", "sfx"),
    )
    async def sound_effects_catalog(self, ctx: Context) -> None:
        """Show every bundled sound effect and its ID."""
        entries = [
            (f"**{effect.display_name}**")
            for effect in audio_effect_catalog()  # sorted already here by id, no need for resorting.
        ]
        pager = SimplePages(entries, ctx=ctx, per_page=15)
        pager.embed.title = "Sound effects list"
        pager.embed.colour = ctx.bot.embedcolor
        await pager.start(ctx)

    @commands.group(name="audio", invoke_without_command=True)
    async def audio_group(self, ctx: Context) -> None:
        """Apply an audio effect to a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @audio_group.command(name="adhd", extras={"usage": "<media>"})
    async def audio_group_adhd(self, ctx: Context, *, media: str = "") -> None:
        """Randomly speed up and slow down a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "adhd", source=media)

    @audio_group.command(
        name="reverse",
        extras={"usage": "<media> [-start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_reverse(self, ctx: Context, *, argument: str = "") -> None:
        """Reverse the audio of a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audioreverse", source=media, **options)

    @audio_group.command(
        name="reverb",
        extras={"usage": "<media> [-room 0.5 -start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_reverb(self, ctx: Context, *, argument: str = "") -> None:
        """Add reverb to a User/Emoji/Media URL.

        -# -room    Change the reverb room size. Defaults to 0.5.
        -# -start   Start the effect at this time in seconds.
        -# -stop    Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(room=(("amount", "r"), float, 0.5)),
        )
        await self._apply_video_effect(ctx, "audioreverb", source=media, **options)

    @audio_group.command(name="extract", extras={"usage": "<media>"})
    async def audio_group_extract(self, ctx: Context, *, media: str = "") -> None:
        """Extract audio tracks from a User/Emoji/Media URL."""
        await self._apply_video_effect(ctx, "extract", source=media)

    @audio_group.command(
        name="replace",
        extras={"usage": "<media> -audio <media>"},
    )
    async def audio_group_replace(self, ctx: Context, *, argument: str = "") -> None:
        """Replace the audio of a User/Emoji/Media URL.

        -# -audio    The audio file that replaces the original audio.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"audio": (("second", "a"), str, "")},
        )
        second_source = str(options.pop("audio"))
        await self._apply_video_effect(
            ctx,
            "audioreplace",
            source=media,
            second_source=second_source,
        )

    @audio_group.command(
        name="overlay",
        extras={
            "usage": (
                "<media> -audio <media> [-at 0 -start 0 -stop 0 "
                "-duration 0 -volume 1 -pitch 0 -random-time]"
            )
        },
    )
    async def audio_group_overlay(self, ctx: Context, *, argument: str = "") -> None:
        """Mix audio into a User/Emoji/Media URL.

        -# -audio        Choose the audio or video source to mix in.
        -# -at           Place the inserted audio at this time in seconds.
        -# -start        Trim the start of the inserted audio.
        -# -stop         Stop the inserted audio at this source time.
        -# -duration     Limit how long the inserted audio plays.
        -# -volume       Change the inserted audio volume from 0 to 5.
        -# -pitch        Change the inserted audio by -12 to 12 semitones.
        -# -random-time  Choose a random placement in the main media.
        """
        media, options = _parse_effect_flags(
            argument,
            values=PIPELINE_EFFECTS["audiooverlay"][1],
            switches=PIPELINE_EFFECTS["audiooverlay"][2],
        )
        second_source = str(options.pop("audio"))
        await self._apply_video_effect(
            ctx,
            "audiooverlay",
            source=media,
            second_source=second_source,
            **options,
        )

    @audio_group.command(
        name="sound-effect",
        aliases=("soundeffect", "sfx"),
        extras={
            "usage": (
                "[media] [id|name|random] [-at 0 -start 0 -stop 0 "
                "-duration 0 -volume 1 -pitch 0 -speed 1 "
                "-random-time true -loop -fade-in 0 -fade-out 0 -fr]"
            )
        },
    )
    async def audio_group_sound_effect(
        self,
        ctx: Context,
        *,
        argument: str = "",
    ) -> None:
        """Mix a bundled sound effect into a User/Emoji/Media URL.

        -# -effect       Choose a catalog ID, name, or random.
        -# -at           Place the sound at this time in seconds.
        -# -start        Trim the start of the sound effect.
        -# -stop         Stop the sound effect at this source time.
        -# -duration     Limit how long the sound effect plays.
        -# -volume       Change the sound volume from 0 to 5.
        -# -pitch        Change the sound by -12 to 12 semitones.
        -# -speed        Change the sound speed from 0.25 to 4.
        -# -random-time  Randomize placement. Defaults to true. Use false to disable.
        -# -loop         Repeat the sound until the main media ends.
        -# -fade-in      Fade the sound in for this many seconds.
        -# -fade-out     Fade the sound out for this many seconds.
        -# -fr           Randomize the sound pitch and speed.
        """
        media, options = _parse_effect_flags(
            argument,
            values=PIPELINE_EFFECTS["soundeffect"][1],
            switches=PIPELINE_EFFECTS["soundeffect"][2],
        )
        selector = str(options.pop("effect", "random"))
        media, selector = _extract_sound_effect_selector(media, selector)
        options = _prepare_sound_effect_options(options)
        try:
            selected = find_audio_effect(selector)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error
        sound_data = await asyncio.to_thread(selected.path.read_bytes)
        await self._apply_video_effect(
            ctx,
            "soundeffect",
            source=media,
            second_data=sound_data,
            note=f"Sound effect: {selected.label}",
            **options,
        )

    @audio_group.command(
        name="destroy",
        extras={"usage": "<media> [-amount 6 -start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_destroy(self, ctx: Context, *, argument: str = "") -> None:
        """Destroy the audio quality of a User/Emoji/Media URL.

        -# -amount    Change how heavily the audio is destroyed. Defaults to 6.
        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(amount=(("strength", "a"), int, 6)),
        )
        await self._apply_video_effect(ctx, "audiodestroy", source=media, **options)

    @audio_group.command(
        name="compress",
        extras={"usage": "<media> [-ratio 4 -start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_compress(self, ctx: Context, *, argument: str = "") -> None:
        """Compress the audio of a User/Emoji/Media URL.

        -# -ratio    Change the compression ratio. Defaults to 4.
        -# -start    Start the effect at this time in seconds.
        -# -stop     Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(ratio=(("r",), float, 4.0)),
        )
        await self._apply_video_effect(ctx, "audiocompress", source=media, **options)

    @audio_group.command(
        name="channels-combine",
        aliases=("mono",),
        extras={"usage": "<media> [-start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_channels_combine(
        self, ctx: Context, *, argument: str = ""
    ) -> None:
        """Combine the audio channels of a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "channelscombine", source=media, **options)

    @audio_group.command(
        name="pitch",
        extras={"usage": "<media> [-semitones 3 -start 0 -stop 0 -duration 0]"},
    )
    async def audio_group_pitch(self, ctx: Context, *, argument: str = "") -> None:
        """Change the pitch of a User/Emoji/Media URL.

        -# -semitones  Change pitch from -12 to 12 semitones.
        -# -start      Start the effect at this time in seconds.
        -# -stop       Stop the effect at this time in seconds.
        -# -duration   Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(
            argument,
            values=_audio_values(semitones=(("amount", "s"), float, 3.0)),
        )
        await self._apply_video_effect(ctx, "audiopitch", source=media, **options)

    @audio_group.command(name="underwater", extras={"usage": "<media> [timing flags]"})
    async def audio_group_underwater(self, ctx: Context, *, argument: str = "") -> None:
        """Apply an underwater effect to a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audiounderwater", source=media, **options)

    @audio_group.command(name="nightcore", extras={"usage": "<media> [timing flags]"})
    async def audio_group_nightcore(self, ctx: Context, *, argument: str = "") -> None:
        """Apply a nightcore effect to a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audionightcore", source=media, **options)

    @audio_group.command(
        name="deepvoice",
        aliases=("deep-voice",),
        extras={"usage": "<media> [timing flags]"},
    )
    async def audio_group_deepvoice(self, ctx: Context, *, argument: str = "") -> None:
        """Lower the voice of a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audiodeepvoice", source=media, **options)

    @audio_group.command(name="surround", extras={"usage": "<media> [timing flags]"})
    async def audio_group_surround(self, ctx: Context, *, argument: str = "") -> None:
        """Widen the stereo field of a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audiosurround", source=media, **options)

    @audio_group.command(name="echo", extras={"usage": "<media> [timing flags]"})
    async def audio_group_echo(self, ctx: Context, *, argument: str = "") -> None:
        """Add an echo to a User/Emoji/Media URL.

        -# -start     Start the effect at this time in seconds.
        -# -stop      Stop the effect at this time in seconds.
        -# -duration  Apply the effect for this many seconds.
        """
        media, options = _parse_effect_flags(argument, values=_audio_values())
        await self._apply_video_effect(ctx, "audioecho", source=media, **options)

    async def _run_pipeline_special_effect(
        self,
        ctx: Context,
        current_data: bytes,
        effect: str,
        options: dict[str, Any],
    ) -> EffectResult:
        if effect == "globe":
            speed = float(options["speed"])
            if not 0.25 <= speed <= 3:
                raise ValueError("Speed must be between 0.25 and 3.")
            output = await make_globe(
                current_data,
                speed,
                bool(options["clockwise"]),
                TEMP_MEDIA_MAX_BYTES,
            )
            return EffectResult(output.getvalue(), "globe.gif")

        if effect == "spin3d":
            tilt = float(options["tilt"])
            zoom = float(options["zoom"])
            speed = float(options["speed"])
            if not -360 <= tilt <= 360:
                raise ValueError("Tilt must be between -360 and 360.")
            if not -3 <= zoom <= 3:
                raise ValueError("Zoom must be between -3 and 3.")
            if not 0.25 <= speed <= 3:
                raise ValueError("Speed must be between 0.25 and 3.")
            output = await make_spin3d(
                current_data,
                tilt,
                zoom,
                speed,
                bool(options["clockwise"]),
                TEMP_MEDIA_MAX_BYTES,
            )
            return EffectResult(output.getvalue(), "spin3d.gif")

        if effect == "caption":
            text = str(options["text"]).strip()
            if not text:
                raise ValueError('Caption requires text. Use `-text "your text"`.')
            if len(text) > 1000:
                raise ValueError("Captions are limited to 1,000 characters.")
            inline_images = await resolve_inline_images(ctx.session, [text])
            output, filename = await make_caption(
                current_data,
                text,
                inline_images,
            )
            return EffectResult(output.getvalue(), filename)

        if effect == "speed":
            speed = float(options["speed"])
            _playback_factor(speed)
            media_probe = await probe_media(current_data)
            start = float(options.get("start", 0))
            stop = float(options.get("stop", 0))
            if media_probe.has_video:
                return EffectResult(
                    await _speed_video(
                        current_data,
                        speed,
                        start=start,
                        stop=stop,
                    ),
                    "speed.mp4",
                )
            if media_probe.has_audio:
                return EffectResult(
                    await _speed_audio(
                        current_data,
                        speed,
                        start=start,
                        stop=stop,
                    ),
                    "speed.mp3",
                    displayable=False,
                )
            raise ValueError("Speed requires video, GIF, or audio media.")

        if effect == "meme":
            text = str(options.get("text", "")).strip()
            if not text:
                raise ValueError("Meme requires text.")
            return await render_image_effect(current_data, "meme", text=text)

        if effect == "convert":
            return await convert_media(
                current_data,
                str(options["format"]),
                1,
            )

        if effect == "overlayflag":
            overlay_data = await self._flag_data(ctx, str(options.pop("flag")))
            renderer_options = _renderer_effect_options(effect, options)
            # Explicit flag overlays fill the media by default. Randomized
            # overlays set this to false so their chosen scale and position
            # are respected instead of being overwritten here.
            renderer_options.setdefault("stretch", True)
            return await render_overlay_effect(
                current_data,
                overlay_data,
                **renderer_options,
            )

        if effect == "overlay":
            overlay_source = str(options.pop("overlay")).strip()
            if not overlay_source:
                raise ValueError(
                    "Overlay requires `-overlay` followed by a second "
                    "User/Emoji/Media URL."
                )
            overlay_data = options.pop("_overlay_data", None)
            if (
                not isinstance(overlay_data, bytes)
                and _random_overlay_kind(overlay_source) is not None
            ):
                overlay_data, _ = await self._random_overlay_data(ctx, overlay_source)
            elif not isinstance(overlay_data, bytes):
                overlay_url = await self._resolve_effect_media(
                    ctx,
                    overlay_source,
                    scan_messages=False,
                )
                overlay_data = await self._fetch_effect_media(ctx, overlay_url)
            renderer_options = _renderer_effect_options("overlay", options)
            renderer_options["overlay_audio"] = not bool(
                renderer_options.pop("no_audio", False)
            )
            return await render_overlay_effect(
                current_data,
                overlay_data,
                **renderer_options,
            )

        if effect == "audioreplace":
            audio_source = str(options.pop("audio")).strip()
            if not audio_source:
                raise ValueError(
                    "Audio replace requires `-audio` followed by a second "
                    "User/Emoji/Media URL."
                )
            audio_url = await self._resolve_effect_media(
                ctx,
                audio_source,
                scan_messages=False,
            )
            audio_data = await self._fetch_effect_media(ctx, audio_url)
            return await render_video_effect(
                current_data,
                effect,
                second_data=audio_data,
            )

        if effect == "audiooverlay":
            audio_source = str(options.pop("audio")).strip()
            if not audio_source:
                raise ValueError(
                    "Audio overlay requires `-audio` followed by another "
                    "User/Emoji/Media URL."
                )
            audio_url = await self._resolve_effect_media(
                ctx,
                audio_source,
                scan_messages=False,
            )
            audio_data = await self._fetch_effect_media(ctx, audio_url)
            return await render_video_effect(
                current_data,
                effect,
                second_data=audio_data,
                **options,
            )

        if effect == "soundeffect":
            try:
                selected = find_audio_effect(str(options.pop("effect", "random")))
            except ValueError as error:
                raise ValueError(str(error)) from error
            audio_data = await asyncio.to_thread(selected.path.read_bytes)
            return await render_video_effect(
                current_data,
                effect,
                second_data=audio_data,
                **options,
            )

        if effect == "text":
            text = str(options.get("text", "")).strip()
            if not text:
                raise ValueError("Text cannot be empty.")
            options["inline_images"] = await resolve_inline_images(ctx.session, [text])
            return await render_text_effect(current_data, **options)

        if effect == "combine":
            second_source = str(options.pop("second", "")).strip()
            if not second_source:
                raise ValueError(
                    "Combine requires `-second` followed by another "
                    "User/Emoji/Media URL."
                )
            second_url = await self._resolve_effect_media(
                ctx,
                second_source,
                scan_messages=False,
            )
            second_data = await self._fetch_effect_media(ctx, second_url)
            return await render_combine_effect(
                current_data,
                second_data,
                **options,
            )

        raise ValueError(f"{effect} is not supported in pipelines.")

    async def _run_effect_pipeline(
        self,
        ctx: Context,
        pipeline: str,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
    ) -> None:
        parsed_source, effects, skipped = _parse_effect_pipeline(pipeline)
        progress = _RunProgress(ctx, len(effects))
        media_url = await self._resolve_effect_media(
            ctx,
            source or parsed_source,
            attachment,
        )
        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            current_data = await self._fetch_effect_media(ctx, media_url)
            try:
                media_probe = await probe_media(current_data)
            except ValueError:
                media_probe = None
            result: EffectResult | None = None
            completed: list[str] = []
            adjustments: list[str] = []
            used_sound_effect_ids: set[int] = set()
            for step, (effect, label, options) in enumerate(
                _resolve_pipeline_random_effects(
                    effects,
                    allow_audio=bool(media_probe and media_probe.has_audio),
                    allow_visual=media_probe is None
                    or bool(getattr(media_probe, "has_video", True)),
                    media_duration=float(getattr(media_probe, "duration", 0.0)),
                ),
                start=1,
            ):
                progress.update(step, label)
                engine = PIPELINE_EFFECTS[effect][0]
                normalized_options, normalized_notes = _normalize_effect_options(
                    effect,
                    options,
                )
                if effect == "overlay" and bool(
                    normalized_options.pop("fullrandom", False)
                ):
                    _randomize_overlay_options(
                        normalized_options,
                        random_module.SystemRandom(),
                    )
                if effect == "overlayflag":
                    self._validate_flag_media(
                        str(normalized_options.get("flag", "")),
                        source or parsed_source,
                        media_url,
                    )
                if effect == "soundeffect":
                    try:
                        selected = _select_pipeline_sound_effect(
                            str(normalized_options.get("effect", "random")),
                            used_sound_effect_ids,
                        )
                    except ValueError as error:
                        skipped.append(f"{label} ({error})")
                        continue
                    used_sound_effect_ids.add(selected.id)
                    normalized_options["effect"] = str(selected.id)
                    label = f"{label}: {selected.display_name}"
                random_overlay_label: str | None = None
                try:
                    if effect == "overlay":
                        overlay_source = str(
                            normalized_options.get("overlay", "")
                        ).strip()
                        if _random_overlay_kind(overlay_source) is not None:
                            (
                                normalized_options["_overlay_data"],
                                random_overlay_label,
                            ) = await self._random_overlay_data(
                                ctx,
                                overlay_source,
                            )
                    async with asyncio.timeout(MEDIA_EFFECT_TIMEOUT):
                        if engine == "special":
                            result = await self._run_pipeline_special_effect(
                                ctx,
                                current_data,
                                effect,
                                normalized_options.copy(),
                            )
                        elif engine == "video":
                            result = await render_video_effect(
                                current_data,
                                effect,
                                **_renderer_effect_options(
                                    effect,
                                    normalized_options,
                                ),
                            )
                        else:
                            result = await render_image_effect(
                                current_data,
                                effect,
                                **_renderer_effect_options(
                                    effect,
                                    normalized_options,
                                ),
                            )
                except TimeoutError:
                    skipped.append(f"{label} (took longer than 60 seconds)")
                    continue
                except ValueError as error:
                    skipped.append(f"{label} ({error})")
                    continue
                current_data = result.data
                if random_overlay_label is not None:
                    label = _random_overlay_display(random_overlay_label)
                completed.append(label)
                adjustments.extend(normalized_notes)

            if result is None:
                detail = "; ".join(skipped) or "no compatible effects"
                raise commands.BadArgument(
                    f"None of the effects could be applied: {detail}"
                )
            result = await _finalize_pipeline_result(result, media_url)

            note_parts = [f"Applied: {', '.join(completed)}"]
            if skipped:
                note_parts.append(f"Skipped: {'; '.join(skipped)}")
            if adjustments:
                note_parts.append("; ".join(adjustments))
            progress.close()
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note=" | ".join(note_parts),
            )
        progress.close()

    @commands.command(
        name="run",
        aliases=("effectrun",),
        extras={
            "usage": ("<media> <effect> [effect flags] " "<effect> [effect flags] ...")
        },
    )
    async def run_effects(self, ctx: Context, *, pipeline: str) -> None:
        """Run several effects on a User/Emoji/Media URL.

        Each effect keeps its normal flags. Effects that cannot process the
        current result are skipped and listed under the finished file. Up to
        67 effects can run in one pipeline.
        """
        await self._run_effect_pipeline(ctx, pipeline)

    @app_commands.command(name="run")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        effects="Effects and flags in the order they should run",
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach the media to process",
    )
    async def run_effects_app(
        self,
        interaction: discord.Interaction["Fishie"],
        effects: str,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Run several effects on a User/Emoji/Media URL."""
        ctx = cast("Context", await self.bot.get_context(interaction))
        await self._run_effect_pipeline(
            ctx,
            effects,
            source=media or "",
            attachment=attachment,
        )

    async def _application_effect(
        self,
        ctx: Context,
        effect: str,
        media: str | None,
        attachment: discord.Attachment | None,
        **options: Any,
    ) -> None:
        await self._apply_image_effect(
            ctx,
            effect,
            source=media or "",
            attachment=attachment,
            **options,
        )

    @cast(Any, commands.hybrid_group)(
        name="effect-2",
        aliases=("effects-2", "effect2", "effects2"),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def effect_2(self, ctx: Context) -> None:
        """Apply additional effects to a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @effect_2.command(name="fade-in")
    async def effect_2_fade_in(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        duration: float = 1.0,
    ) -> None:
        """Fade a User/Emoji/Media URL in from transparent or black."""
        await self._application_effect(
            ctx, "fadein", media, attachment, duration=duration
        )

    @effect_2.command(name="fade-out")
    async def effect_2_fade_out(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        duration: float = 1.0,
    ) -> None:
        """Fade a User/Emoji/Media URL out to transparent or black."""
        await self._application_effect(
            ctx, "fadeout", media, attachment, duration=duration
        )

    @effect_2.command(name="lag")
    async def effect_2_lag(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: int = 4,
        method: Literal["random", "freeze", "stutter", "drop", "jitter"] = "random",
        multi: bool = False,
    ) -> None:
        """Make a User/Emoji/Media URL lag at randomized moments."""
        await self._application_effect(
            ctx,
            "lag",
            media,
            attachment,
            amount=amount,
            method=method,
            multi=multi,
        )

    @effect_2.command(name="shuffle")
    async def effect_2_shuffle(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Shuffle the frame order of a User/Emoji/Media URL."""
        await self._application_effect(ctx, "shuffle", media, attachment)

    @effect_2.command(name="tint")
    async def effect_2_tint(
        self,
        ctx: Context,
        color: str = "#5865f2",
        amount: float = 0.35,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Tint a User/Emoji/Media URL with a CSS color or hex value."""
        await self._application_effect(
            ctx,
            "tint",
            media,
            attachment,
            color=color,
            amount=amount,
        )

    @effect_2.command(name="implode")
    async def effect_2_implode(
        self,
        ctx: Context,
        strength: float = 0.5,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Pull a User/Emoji/Media URL inward toward its center."""
        await self._application_effect(
            ctx, "implode", media, attachment, strength=strength
        )

    @effect_2.command(name="explode")
    async def effect_2_explode(
        self,
        ctx: Context,
        strength: float = 0.5,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Push a User/Emoji/Media URL outward from its center."""
        await self._application_effect(
            ctx, "explode", media, attachment, strength=strength
        )

    @effect_2.command(name="sharpen")
    async def effect_2_sharpen(
        self,
        ctx: Context,
        amount: float = 2.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Sharpen a User/Emoji/Media URL."""
        await self._application_effect(ctx, "sharpen", media, attachment, amount=amount)

    @effect_2.command(name="legoify")
    async def effect_2_legoify(
        self,
        ctx: Context,
        size: int = 12,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Turn a User/Emoji/Media URL into Lego-like blocks."""
        await self._application_effect(ctx, "legoify", media, attachment, size=size)

    @effect_2.command(name="bounce")
    async def effect_2_bounce(
        self,
        ctx: Context,
        amount: float = 20.0,
        speed: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Bounce a User/Emoji/Media URL in an animated loop."""
        await self._application_effect(
            ctx,
            "bounce",
            media,
            attachment,
            amount=amount,
            speed=speed,
        )

    @effect_2.command(name="fisheye")
    async def effect_2_fisheye(
        self,
        ctx: Context,
        strength: float = 0.65,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Apply a fisheye distortion to a User/Emoji/Media URL."""
        await self._application_effect(
            ctx, "fisheye", media, attachment, strength=strength
        )

    @effect_2.command(name="sepia")
    async def effect_2_sepia(
        self,
        ctx: Context,
        amount: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Apply a sepia effect to a User/Emoji/Media URL."""
        await self._application_effect(ctx, "sepia", media, attachment, amount=amount)

    @effect_2.command(name="pixelate")
    async def effect_2_pixelate(
        self,
        ctx: Context,
        size: int = 12,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Pixelate a User/Emoji/Media URL."""
        await self._application_effect(ctx, "pixelate", media, attachment, size=size)

    @effect_2.command(name="slide-in")
    async def effect_2_slide_in(
        self,
        ctx: Context,
        direction: Literal["left", "right", "up", "down"] = "left",
        duration: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Slide a User/Emoji/Media URL into frame."""
        await self._application_effect(
            ctx,
            "slidein",
            media,
            attachment,
            direction=direction,
            duration=duration,
        )

    @effect_2.command(name="slide-out")
    async def effect_2_slide_out(
        self,
        ctx: Context,
        direction: Literal["left", "right", "up", "down"] = "left",
        duration: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Slide a User/Emoji/Media URL out of frame."""
        await self._application_effect(
            ctx,
            "slideout",
            media,
            attachment,
            direction=direction,
            duration=duration,
        )

    @effect_2.command(name="vignette")
    async def effect_2_vignette(
        self,
        ctx: Context,
        amount: float = 0.65,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Darken the edges of a User/Emoji/Media URL."""
        await self._application_effect(
            ctx, "vignette", media, attachment, amount=amount
        )

    @effect_2.command(name="resize")
    async def effect_2_resize(
        self,
        ctx: Context,
        scale: float = 1.0,
        ratio: str = "",
        size: str = "",
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Resize a User/Emoji/Media URL."""
        await self._application_effect(
            ctx,
            "resize",
            media,
            attachment,
            scale=scale,
            ratio=ratio,
            size=size,
        )

    @effect_2.command(name="distort")
    async def effect_2_distort(
        self,
        ctx: Context,
        amount: float = 0.25,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Shear and distort a User/Emoji/Media URL."""
        await self._application_effect(ctx, "distort", media, attachment, amount=amount)

    @effect_2.command(name="grain")
    async def effect_2_grain(
        self,
        ctx: Context,
        amount: float = 20.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Add monochrome grain to a User/Emoji/Media URL."""
        await self._application_effect(ctx, "grain", media, attachment, amount=amount)

    @effect_2.command(name="rotate")
    async def effect_2_rotate(
        self,
        ctx: Context,
        degrees: float = 90.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Rotate a User/Emoji/Media URL by a number of degrees."""
        await self._application_effect(
            ctx, "rotate", media, attachment, degrees=degrees
        )

    @effect_2.command(name="noise")
    async def effect_2_noise(
        self,
        ctx: Context,
        amount: float = 20.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Add colored noise to a User/Emoji/Media URL."""
        await self._application_effect(ctx, "noise", media, attachment, amount=amount)

    @effect_2.command(name="brightness")
    async def effect_2_brightness(
        self,
        ctx: Context,
        amount: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Adjust the brightness of a User/Emoji/Media URL."""
        await self._application_effect(
            ctx, "brightness", media, attachment, amount=amount
        )

    @effect_2.command(name="contrast")
    async def effect_2_contrast(
        self,
        ctx: Context,
        amount: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Adjust the contrast of a User/Emoji/Media URL."""
        await self._application_effect(
            ctx, "contrast", media, attachment, amount=amount
        )

    @effect_2.command(name="saturation")
    async def effect_2_saturation(
        self,
        ctx: Context,
        amount: float = 1.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Adjust the saturation of a User/Emoji/Media URL."""
        await self._application_effect(
            ctx, "saturation", media, attachment, amount=amount
        )

    @effect_2.command(name="exposure")
    async def effect_2_exposure(
        self,
        ctx: Context,
        stops: float = 0.0,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Adjust the exposure of a User/Emoji/Media URL."""
        await self._application_effect(ctx, "exposure", media, attachment, stops=stops)

    @cast(Any, commands.hybrid_group)(
        name="effect-3",
        aliases=("effects-3", "effect3", "effects3"),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def effect_3(self, ctx: Context) -> None:
        """Apply more effects to a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @effect_3.command(name="text")
    @app_commands.autocomplete(font=_font_autocomplete)
    async def effect_3_text(
        self,
        ctx: Context,
        text: str,
        font: str = "Roboto",
        size: app_commands.Range[int, 8, 512] = 48,
        position: str = "center",
        color: str = "#ffffff",
        style: str = "outline",
        bold: bool = False,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Add styled Unicode and emoji text to a User/Emoji/Media URL."""
        await self._apply_text_effect(
            ctx,
            source=media or "",
            attachment=attachment,
            text=text,
            font=font,
            size=size,
            position=position,
            color=color,
            style=style,
            bold=bold,
        )

    @effect_3.command(name="combine")
    async def effect_3_combine(
        self,
        ctx: Context,
        position: Literal["top", "bottom", "left", "right"] = "right",
        mode: Literal["resize", "stretch", "original"] = "resize",
        audio: Literal["mix", "first", "second", "none"] = "mix",
        media: str | None = None,
        second_media: str | None = None,
        attachment: discord.Attachment | None = None,
        second_attachment: discord.Attachment | None = None,
    ) -> None:
        """Combine two User/Emoji/Media URLs on one extended canvas."""
        await self._apply_combine_effect(
            ctx,
            source=media or "",
            second_source=second_media or "",
            attachment=attachment,
            second_attachment=second_attachment,
            position=position,
            mode=mode,
            audio=audio,
        )

    @effect_3.command(name="hallway")
    async def effect_3_hallway(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Animate a User/Emoji/Media URL into a hallway effect."""
        await self._application_effect(ctx, "hallway", media, attachment)

    @effect_3.command(name="parallax")
    async def effect_3_parallax(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
    ) -> None:
        """Add a moving parallax effect to a User/Emoji/Media URL."""
        await self._application_effect(
            ctx,
            "parallax",
            media,
            attachment,
            speed=speed,
        )

    @effect_3.command(name="huerotate")
    async def effect_3_huerotate(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        degrees: float = 180.0,
    ) -> None:
        """Rotate the colors of a User/Emoji/Media URL."""
        await self._application_effect(
            ctx,
            "huerotate",
            media,
            attachment,
            degrees=degrees,
        )

    @effect_3.command(name="zoom")
    async def effect_3_zoom(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 2.0,
        forever: bool = False,
    ) -> None:
        """Zoom a User/Emoji/Media URL, optionally looping forever."""
        await self._application_effect(
            ctx,
            "zoom",
            media,
            attachment,
            amount=amount,
            forever=forever,
        )

    @effect_3.command(name="squishy")
    async def effect_3_squishy(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 0.18,
    ) -> None:
        """Make a User/Emoji/Media URL pulse and squash."""
        await self._application_effect(
            ctx,
            "squishy",
            media,
            attachment,
            amount=amount,
        )

    @effect_3.command(name="glitch")
    async def effect_3_glitch(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 12.0,
    ) -> None:
        """Add a glitch effect to a User/Emoji/Media URL."""
        await self._application_effect(
            ctx,
            "glitch",
            media,
            attachment,
            amount=amount,
        )

    @effect_3.command(name="tremble")
    async def effect_3_tremble(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 8.0,
    ) -> None:
        """Make a User/Emoji/Media URL tremble."""
        await self._application_effect(
            ctx,
            "tremble",
            media,
            attachment,
            amount=amount,
        )

    @effect_3.command(name="quilt")
    async def effect_3_quilt(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        tiles: int = 3,
    ) -> None:
        """Turn a User/Emoji/Media URL into a tiled quilt."""
        await self._application_effect(
            ctx,
            "quilt",
            media,
            attachment,
            tiles=tiles,
        )

    @effect_3.group(name="remove", invoke_without_command=True)
    async def effect_3_remove(self, ctx: Context) -> None:
        """Remove bars or captions from a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @effect_3_remove.command(name="bars")
    async def effect_3_remove_bars(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Remove black bars from a User/Emoji/Media URL."""
        await self._application_effect(ctx, "removebars", media, attachment)

    @effect_3_remove.command(name="caption")
    async def effect_3_remove_caption(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Remove caption bars from a User/Emoji/Media URL."""
        await self._application_effect(ctx, "removecaption", media, attachment)

    @effect_3_remove.command(name="outro-tiktok")
    async def effect_3_remove_outro_tiktok(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Detect and remove a TikTok outro from a video."""
        await self._apply_video_effect(
            ctx,
            "removeoutrotiktok",
            source=media or "",
            attachment=attachment,
        )

    @effect_3_remove.command(name="outro-reels")
    async def effect_3_remove_outro_reels(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Detect and remove an Instagram Reels outro from a video."""
        await self._apply_video_effect(
            ctx,
            "removeoutroreels",
            source=media or "",
            attachment=attachment,
        )

    @effect_3.command(name="enlarge")
    async def effect_3_enlarge(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 2.0,
    ) -> None:
        """Enlarge a User/Emoji/Media URL with a preset scale."""
        await self._application_effect(
            ctx,
            "enlarge",
            media,
            attachment,
            amount=amount,
        )

    @effect_3.command(name="falsecolor")
    async def effect_3_falsecolor(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Apply a false-color palette to a User/Emoji/Media URL."""
        await self._application_effect(ctx, "falsecolor", media, attachment)

    @effect_3.command(name="watercolor")
    async def effect_3_watercolor(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Give a User/Emoji/Media URL a watercolor look."""
        await self._application_effect(ctx, "watercolor", media, attachment)

    @effect_3.command(name="oilpaint")
    async def effect_3_oilpaint(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Give a User/Emoji/Media URL an oil-paint look."""
        await self._application_effect(ctx, "oilpaint", media, attachment)

    @effect_3.command(name="random")
    async def effect_3_random(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Apply a random compatible effect to a User/Emoji/Media URL."""
        chosen = _random_effect_choices(1)[0]
        await self._apply_image_effect(
            ctx,
            chosen,
            source=media or "",
            attachment=attachment,
            note=f"Applied: random ({chosen})",
        )

    @effect_3.command(name="meme")
    async def effect_3_meme(
        self,
        ctx: Context,
        text: str,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Add impact-style text to a User/Emoji/Media URL."""
        await self._application_effect(
            ctx,
            "meme",
            media,
            attachment,
            text=text,
        )

    @cast(Any, commands.hybrid_group)(
        name="effect",
        aliases=(
            "effects",
            "image-effect",
            "imageeffect",
            "video-effect",
            "videoeffect",
        ),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def image_effect(self, ctx: Context) -> None:
        """Apply an effect to a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @image_effect.command(name="globe")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 3",
        clockwise="Rotate clockwise",
    )
    async def image_effect_globe(
        self,
        ctx: Context,
        media: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap a User/Emoji/Media URL around a rotating globe."""
        source = media or (attachment.url if attachment else "")
        argument = f"{source} -speed {speed:g}"
        if clockwise:
            argument += " -clockwise"
        await self._globe_effect(ctx, argument=argument.strip())

    @image_effect.command(name="spin3d")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image or GIF",
        tilt="Vertical tilt from -360 to 360",
        zoom="Zoom from -3 to 3",
        speed="Rotation speed from 0.25 to 3",
        clockwise="Rotate clockwise",
    )
    async def image_effect_spin3d(
        self,
        ctx: Context,
        media: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
        tilt: float = 15.0,
        zoom: float = 1.5,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Spin a User/Emoji/Media URL in 3D space."""
        source = media or (attachment.url if attachment else "")
        argument = f"{source} -tilt {tilt:g} -zoom {zoom:g} -speed {speed:g}"
        if clockwise:
            argument += " -clockwise"
        await self._spin3d_effect(ctx, argument=argument.strip())

    @image_effect.command(name="caption")
    @app_commands.describe(
        text="The caption text",
        media=MEDIA_INPUT_DESCRIPTION,
        user="Use this user's avatar",
        attachment="Attach an image or video",
    )
    async def image_effect_caption(
        self,
        ctx: Context,
        text: str = "",
        media: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add a caption to a User/Emoji/Media URL."""
        await self._caption_effect(
            ctx,
            text=text,
            media=media,
            user=user,
            attachment=attachment,
        )

    @image_effect.command(name="speed")
    @app_commands.describe(
        speed="Playback speed from -5 to -1 (slower) or 1 to 5 (faster)",
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, video, or audio",
        start="Time where the speed change begins",
        stop="Time where the speed change ends, or 0 for the end",
    )
    async def image_effect_speed(
        self,
        ctx: Context,
        speed: float = 2.0,
        media: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
        start: float = 0.0,
        stop: float = 0.0,
    ) -> None:
        """Change the playback speed of a User/Emoji/Media URL."""
        source = media or (attachment.url if attachment else "")
        input_value = (
            f"{source} -speed {speed:g} -start {start:g} -stop {stop:g}".strip()
        )
        await self._speed_effect(ctx, input=input_value)

    @image_effect.command(name="invert")
    @app_commands.rename(preserve_transparency="preserve-transparency")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        preserve_transparency="Keep transparent backgrounds transparent",
    )
    async def image_effect_invert(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        preserve_transparency: bool = False,
    ) -> None:
        """Invert the colors of a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "invert",
            source=media or "",
            attachment=attachment,
            preserve_transparency=preserve_transparency,
        )

    @image_effect.command(name="average-colors")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach a still image",
    )
    async def image_effect_average_colors(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Create a labeled palette from a still image."""
        await self._average_colors_effect(
            ctx, source=media or "", attachment=attachment
        )

    @image_effect.command(name="spin")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_spin(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Rotate a User/Emoji/Media URL in a flat animated loop."""
        await self._apply_image_effect(
            ctx,
            "spin",
            source=media or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="magik")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        strength="Distortion strength from 1 to 80",
    )
    async def image_effect_magik(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 20.0,
    ) -> None:
        """Distort a User/Emoji/Media URL with a liquid effect."""
        await self._apply_image_effect(
            ctx,
            "magik",
            source=media or "",
            attachment=attachment,
            strength=strength,
        )

    @image_effect.command(name="amagik")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        strength="Distortion strength from 1 to 80",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_amagik(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 20.0,
        speed: float = 1.0,
    ) -> None:
        """Animate a liquid distortion over a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "gifmagik",
            source=media or "",
            attachment=attachment,
            strength=strength,
            speed=speed,
        )

    @image_effect.command(name="flip")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        direction="The direction to flip",
    )
    async def image_effect_flip(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        direction: Literal["horizontal", "vertical"] = "horizontal",
    ) -> None:
        """Flip a User/Emoji/Media URL horizontally or vertically."""
        await self._apply_image_effect(
            ctx,
            "flip",
            source=media or "",
            attachment=attachment,
            direction=direction,
        )

    @image_effect.command(name="cube")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_cube(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap a User/Emoji/Media URL around a rotating cube."""
        await self._apply_image_effect(
            ctx,
            "cube",
            source=media or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="pyramid")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_pyramid(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap a User/Emoji/Media URL around a rotating pyramid."""
        await self._apply_image_effect(
            ctx,
            "pyramid",
            source=media or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="blur")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        radius="Blur radius from 0.1 to 50",
        blur_type="Choose gaussian, box, or motion blur",
    )
    async def image_effect_blur(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        radius: float = 5.0,
        blur_type: Literal["gaussian", "box", "motion"] = "gaussian",
    ) -> None:
        """Blur a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "blur",
            source=media or "",
            attachment=attachment,
            radius=radius,
            blur_type=blur_type,
        )

    @image_effect.command(name="deepfry")
    @app_commands.rename(preserve_transparency="preserve-transparency")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        intensity="Deepfry intensity from 0.25 to 3",
        preserve_transparency="Keep transparent backgrounds transparent",
    )
    async def image_effect_deepfry(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        intensity: float = 1.0,
        preserve_transparency: bool = False,
    ) -> None:
        """Deep-fry a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "deepfry",
            source=media or "",
            attachment=attachment,
            intensity=intensity,
            preserve_transparency=preserve_transparency,
        )

    @image_effect.command(name="grayscale")
    @app_commands.rename(preserve_transparency="preserve-transparency")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        preserve_transparency="Keep transparent backgrounds transparent",
    )
    async def image_effect_grayscale(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        preserve_transparency: bool = False,
    ) -> None:
        """Convert a User/Emoji/Media URL to grayscale."""
        await self._apply_image_effect(
            ctx,
            "grayscale",
            source=media or "",
            attachment=attachment,
            preserve_transparency=preserve_transparency,
        )

    @image_effect.command(name="jpeg")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        quality="JPEG quality from 1 to 50, lower is worse",
    )
    async def image_effect_jpeg(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        quality: int = 8,
    ) -> None:
        """Make a User/Emoji/Media URL look heavily JPEG-compressed."""
        await self._apply_image_effect(
            ctx,
            "jpeg",
            source=media or "",
            attachment=attachment,
            quality=quality,
        )

    @image_effect.command(name="swirl")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        strength="Swirl strength from -720 to 720 degrees",
    )
    async def image_effect_swirl(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 180.0,
    ) -> None:
        """Swirl a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "swirl",
            source=media or "",
            attachment=attachment,
            strength=strength,
        )

    @image_effect.command(name="aswirl")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        strength="Swirl strength from -720 to 720 degrees",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_aswirl(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 180.0,
        speed: float = 1.0,
    ) -> None:
        """Wind up a swirl across a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "gifswirl",
            source=media or "",
            attachment=attachment,
            strength=strength,
            speed=speed,
        )

    @image_effect.command(name="wiggle")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach an image, GIF, or video",
        amount="Wiggle amount from 1 to 30",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_wiggle(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 8.0,
        speed: float = 1.0,
    ) -> None:
        """Make a User/Emoji/Media URL wiggle."""
        await self._apply_image_effect(
            ctx,
            "wiggle",
            source=media or "",
            attachment=attachment,
            amount=amount,
            speed=speed,
        )

    @image_effect.group(name="crop", invoke_without_command=True)
    async def image_effect_crop(self, ctx: Context) -> None:
        """Crop a User/Emoji/Media URL into a shape."""
        await ctx.send_help(ctx.command)

    @image_effect_crop.command(name="circle")
    async def image_effect_crop_circle(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Crop a User/Emoji/Media URL into a circle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media or "",
            attachment=attachment,
            shape="circle",
        )

    @image_effect_crop.command(name="triangle")
    async def image_effect_crop_triangle(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Crop a User/Emoji/Media URL into a triangle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media or "",
            attachment=attachment,
            shape="triangle",
        )

    @effect_3.group(name="mirror", invoke_without_command=True)
    async def image_effect_mirror(self, ctx: Context) -> None:
        """Mirror one side of a User/Emoji/Media URL onto the other."""
        await ctx.send_help(ctx.command)

    @image_effect_mirror.command(name="bottom")
    async def image_effect_mirror_bottom(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the bottom half of a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media or "",
            attachment=attachment,
            direction="bottom",
        )

    @image_effect_mirror.command(name="right")
    async def image_effect_mirror_right(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the right half of a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media or "",
            attachment=attachment,
            direction="right",
        )

    @image_effect_mirror.command(name="left")
    async def image_effect_mirror_left(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the left half of a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media or "",
            attachment=attachment,
            direction="left",
        )

    @image_effect_mirror.command(name="top")
    async def image_effect_mirror_top(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the top half of a User/Emoji/Media URL."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media or "",
            attachment=attachment,
            direction="top",
        )

    @effect_3.command(name="overlay")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        overlay_media=(
            "User/Emoji/Media URL or [user|media|emoji|asset|flag] [name|random]"
            " to place on top"
        ),
        audio_media="Audio or video URL to mix into the main media",
        flag="Pride flag, pirate, country name, or two-letter country code",
        attachment="Attach the background image, GIF, or video",
        overlay_attachment="Attach the image, GIF, or video to place on top",
        audio_attachment="Attach audio or video to mix in",
        opacity="Overlay opacity from 0 to 100%",
        scale="Overlay size from 0.05 to 2 times the background",
        size="Overlay dimensions such as 100x100, from 1 to 4096 pixels per side",
        position="Where to place the overlay",
        x="Horizontal offset from -4096 to 4096 pixels",
        y="Vertical offset from -4096 to 4096 pixels",
        start="Time when the overlay starts",
        stop="Time when the overlay stops",
        stretch="Stretch the overlay to fill the background",
        extend="Extend the output when the overlay video is longer",
        overlay_audio="Mix audio from the overlay video",
        at="Main-media time where audio starts",
        source_start="Time to begin reading the audio file",
        source_stop="Time to stop reading the audio file, or 0 for its end",
        duration="Maximum audio length, or 0 for the remaining source",
        volume="Audio volume from 0 to 5",
        pitch="Audio pitch from -12 to 12 semitones",
        random_time="Place the audio at a random valid time",
    )
    async def effect_3_overlay(
        self,
        ctx: Context,
        media: str | None = None,
        overlay_media: str | None = None,
        audio_media: str | None = None,
        flag: str | None = None,
        attachment: discord.Attachment | None = None,
        overlay_attachment: discord.Attachment | None = None,
        audio_attachment: discord.Attachment | None = None,
        opacity: float = 70.0,
        scale: float = 1.0,
        size: str | None = None,
        position: str = "center",
        x: int = 0,
        y: int = 0,
        start: float = 0.0,
        stop: float = 0.0,
        stretch: bool = False,
        extend: bool = False,
        overlay_audio: bool = True,
        at: float = 0.0,
        source_start: float = 0.0,
        source_stop: float = 0.0,
        duration: float = 0.0,
        volume: float = 1.0,
        pitch: float = 0.0,
        random_time: bool = False,
    ) -> None:
        """Overlay visual media or mix audio into another media item."""
        if audio_media or audio_attachment is not None:
            if flag or overlay_media or overlay_attachment is not None:
                raise commands.BadArgument(
                    "Choose visual overlay options or audio media, not both."
                )
            await self._apply_video_effect(
                ctx,
                "audiooverlay",
                source=media or "",
                attachment=attachment,
                second_source=audio_media or "",
                second_attachment=audio_attachment,
                at=at,
                source_start=source_start,
                source_stop=source_stop,
                duration=duration,
                volume=volume,
                pitch=pitch,
                random_time=random_time,
            )
            return
        if flag and (overlay_media or overlay_attachment is not None):
            raise commands.BadArgument(
                "Choose a flag or an overlay media item, not both."
            )
        options: dict[str, Any] = {
            "opacity": opacity,
            "scale": scale,
            "position": position,
            "x": x,
            "y": y,
            "start": start,
            "stop": stop,
            "stretch": stretch,
            "extend": extend,
            "overlay_audio": overlay_audio,
        }
        if size:
            options["size"] = size
        if flag:
            options["overlay_data"] = await self._flag_data(ctx, flag)
            options["_flag_name"] = flag
            options["stretch"] = True
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media or "",
            attachment=attachment,
            second_source=overlay_media or "",
            second_attachment=overlay_attachment,
            **options,
        )

    @image_effect.command(name="reverse")
    async def video_effects_reverse(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Reverse a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "reverse",
            source=media or "",
            attachment=attachment,
        )

    @app_commands.command(name="convert")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        output_format="The format to convert every file into",
        media=MEDIA_INPUT_DESCRIPTION,
        attachment_1="First media file",
        attachment_2="Second media file",
        attachment_3="Third media file",
        attachment_4="Fourth media file",
        attachment_5="Fifth media file",
    )
    async def video_effects_convert(
        self,
        interaction: discord.Interaction["Fishie"],
        output_format: Literal["mp4", "webm", "gif", "mp3", "wav", "ogg"],
        media: str | None = None,
        attachment_1: discord.Attachment | None = None,
        attachment_2: discord.Attachment | None = None,
        attachment_3: discord.Attachment | None = None,
        attachment_4: discord.Attachment | None = None,
        attachment_5: discord.Attachment | None = None,
    ) -> None:
        """Convert up to five User/Emoji/Media URLs to another format."""
        ctx = cast("Context", await self.bot.get_context(interaction))
        attachments = [
            attachment
            for attachment in (
                attachment_1,
                attachment_2,
                attachment_3,
                attachment_4,
                attachment_5,
            )
            if attachment is not None
        ]
        await self._convert_effect(
            ctx,
            output_format,
            source=media or "",
            attachments=attachments,
        )

    @image_effect.command(name="volume")
    @app_commands.describe(
        volume="Volume multiplier from 0 to 10",
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach a video or audio file",
    )
    async def video_effects_volume(
        self,
        ctx: Context,
        volume: float,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Change the volume of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "volume",
            source=media or "",
            attachment=attachment,
            volume=volume,
            start=start,
            stop=stop,
            duration=duration,
        )

    @image_effect.group(name="bass", invoke_without_command=True)
    async def video_effects_bass(self, ctx: Context) -> None:
        """Change the bass of a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @video_effects_bass.command(name="boost")
    async def video_effects_bass_boost(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        gain: float = 12.0,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Boost the bass of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "bassboost",
            source=media or "",
            attachment=attachment,
            gain=gain,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_bass.command(name="lower")
    async def video_effects_bass_lower(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        gain: float = 12.0,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Lower the bass of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "basslower",
            source=media or "",
            attachment=attachment,
            gain=gain,
            start=start,
            stop=stop,
            duration=duration,
        )

    @image_effect.group(name="audio", invoke_without_command=True)
    async def video_effects_audio(self, ctx: Context) -> None:
        """Apply an audio effect to a User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @image_effect.command(name="adhd")
    async def image_effect_adhd(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Randomly speed up and slow down a video or audio file."""
        await self._apply_video_effect(
            ctx,
            "adhd",
            source=media or "",
            attachment=attachment,
        )

    @video_effects_audio.command(name="reverse")
    async def video_effects_audio_reverse(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Reverse the audio of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audioreverse",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="reverb")
    async def video_effects_audio_reverb(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        room: float = 0.5,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Add reverb to a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audioreverb",
            source=media or "",
            attachment=attachment,
            room=room,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="extract")
    async def video_effects_audio_extract(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Extract audio tracks from a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "extract",
            source=media or "",
            attachment=attachment,
        )

    @video_effects_audio.command(name="replace")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        audio_url="Replacement User/Emoji/Media URL",
        attachment="Attach the video",
        audio_attachment="Attach the replacement audio",
    )
    async def video_effects_audio_replace(
        self,
        ctx: Context,
        media: str | None = None,
        audio_url: str | None = None,
        attachment: discord.Attachment | None = None,
        audio_attachment: discord.Attachment | None = None,
    ) -> None:
        """Replace the audio of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audioreplace",
            source=media or "",
            attachment=attachment,
            second_source=audio_url or "",
            second_attachment=audio_attachment,
        )

    @video_effects_audio.command(name="overlay")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        audio_media="Audio or video URL to mix into the main media",
        attachment="Attach the main video or audio",
        audio_attachment="Attach audio or video to mix in",
        at="Main-media time where the overlay starts",
        source_start="Time to begin reading the overlay file",
        source_stop="Time to stop reading the overlay file, or 0 for its end",
        duration="Maximum overlay length, or 0 for the remaining source",
        volume="Overlay volume from 0 to 5",
        pitch="Overlay pitch from -12 to 12 semitones",
        random_time="Place the overlay at a random valid time",
    )
    async def video_effects_audio_overlay(
        self,
        ctx: Context,
        audio_media: str | None = None,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        audio_attachment: discord.Attachment | None = None,
        at: float = 0.0,
        source_start: float = 0.0,
        source_stop: float = 0.0,
        duration: float = 0.0,
        volume: float = 1.0,
        pitch: float = 0.0,
        random_time: bool = False,
    ) -> None:
        """Mix audio from another audio or video file into the main media."""
        await self._apply_video_effect(
            ctx,
            "audiooverlay",
            source=media or "",
            attachment=attachment,
            second_source=audio_media or "",
            second_attachment=audio_attachment,
            at=at,
            source_start=source_start,
            source_stop=source_stop,
            duration=duration,
            volume=volume,
            pitch=pitch,
            random_time=random_time,
        )

    @video_effects_audio.command(name="sound-effect")
    @app_commands.autocomplete(effect=_sound_effect_autocomplete)
    @app_commands.describe(
        effect="Bundled sound-effect ID, name, or random",
        media=MEDIA_INPUT_DESCRIPTION,
        attachment="Attach the main video or audio",
        at="Main-media time where the sound starts",
        source_start="Time to begin reading the sound effect",
        source_stop="Time to stop reading it, or 0 for its end",
        duration="Maximum sound length, or 0 for the remaining source",
        volume="Sound-effect volume from 0 to 5",
        pitch="Sound-effect pitch from -12 to 12 semitones",
        speed="Sound-effect playback speed from 0.25 to 4",
        random_time="Place the sound at a random valid time",
        loop="Repeat the sound until the media ends",
        fade_in="Fade the sound in for this many seconds",
        fade_out="Fade the sound out for this many seconds",
        full_random="Randomize the sound pitch and speed",
    )
    async def video_effects_audio_sound_effect(
        self,
        ctx: Context,
        effect: str = "random",
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        at: float = 0.0,
        source_start: float = 0.0,
        source_stop: float = 0.0,
        duration: float = 0.0,
        volume: float = 1.0,
        pitch: float = 0.0,
        speed: float = 1.0,
        random_time: bool = True,
        loop: bool = False,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        full_random: bool = False,
    ) -> None:
        """Mix a bundled sound effect into a User/Emoji/Media URL."""
        options = _prepare_sound_effect_options(
            {
                "random_time": random_time,
                "loop": loop,
                "fade_in": fade_in,
                "fade_out": fade_out,
                "pitch": pitch,
                "speed": speed,
                "fullrandom": full_random,
            }
        )
        try:
            selected = find_audio_effect(effect)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error
        sound_data = await asyncio.to_thread(selected.path.read_bytes)
        await self._apply_video_effect(
            ctx,
            "soundeffect",
            source=media or "",
            attachment=attachment,
            second_data=sound_data,
            note=f"Sound effect: {selected.label}",
            at=at,
            source_start=source_start,
            source_stop=source_stop,
            duration=duration,
            volume=volume,
            **options,
        )

    @video_effects_audio.command(name="destroy")
    async def video_effects_audio_destroy(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: int = 6,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Destroy the audio quality of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiodestroy",
            source=media or "",
            attachment=attachment,
            amount=amount,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="compress")
    async def video_effects_audio_compress(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        ratio: float = 4.0,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Compress the audio of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiocompress",
            source=media or "",
            attachment=attachment,
            ratio=ratio,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="channels-combine")
    async def video_effects_audio_channels_combine(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Combine the audio channels of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "channelscombine",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="pitch")
    async def video_effects_audio_pitch(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        semitones: float = 3.0,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Change the pitch of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiopitch",
            source=media or "",
            attachment=attachment,
            semitones=semitones,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="underwater")
    async def video_effects_audio_underwater(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Apply an underwater effect to a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiounderwater",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="nightcore")
    async def video_effects_audio_nightcore(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Apply a nightcore effect to a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audionightcore",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="deepvoice")
    async def video_effects_audio_deepvoice(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Lower the voice of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiodeepvoice",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="surround")
    async def video_effects_audio_surround(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Widen the stereo field of a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audiosurround",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

    @video_effects_audio.command(name="echo")
    async def video_effects_audio_echo(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        start: float = 0.0,
        stop: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Add an echo to a User/Emoji/Media URL."""
        await self._apply_video_effect(
            ctx,
            "audioecho",
            source=media or "",
            attachment=attachment,
            start=start,
            stop=stop,
            duration=duration,
        )

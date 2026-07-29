from __future__ import annotations

import asyncio
import json
import math
import random as random_module
import re
import shlex
import time
from functools import lru_cache, wraps
from io import BytesIO
from typing import TYPE_CHECKING, Any, Literal, Optional, cast
from urllib.parse import urlsplit

import discord
import numpy as np
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
from utils import MediaConverter, SimplePages, fetch_public_bytes, to_thread
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

from .audio_effects import (
    AudioEffect,
    audio_effect_catalog,
    audio_effect_choices,
    find_audio_effect,
)
from .processing import (
    PRIDE_FLAGS,
    EffectResult,
    convert_media,
    make_flag_asset,
    probe_media,
    render_image_effect,
    render_video_effect,
)

if TYPE_CHECKING:
    from core.bot import Fishie
    from extensions.context import Context

Image.MAX_IMAGE_PIXELS = 25_000_000

MEDIA_INPUT_DESCRIPTION = "User/Emoji/Media URL"
MEDIA_EFFECT_TIMEOUT = 30
RANDOM_EFFECTS = (
    "blur",
    "deepfry",
    "distort",
    "falsecolor",
    "fisheye",
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
    "sepia",
    "sharpen",
    "swirl",
    "vignette",
    "watercolor",
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
    "channelscombine",
    "soundeffect",
    "volume",
)
RANDOM_OVERLAY_EFFECTS = ("overlayflag",)
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


def media_effect_timeout(callback: Any) -> Any:
    """Limit a complete media-effect command, including downloads, to 30 seconds."""

    @wraps(callback)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            async with asyncio.timeout(MEDIA_EFFECT_TIMEOUT):
                return await callback(*args, **kwargs)
        except TimeoutError as error:
            raise commands.BadArgument(
                "That effect took longer than 30 seconds. Try a shorter or smaller file."
            ) from error

    return wrapped


def _random_effect_choices(count: int) -> list[str]:
    if count > len(RANDOM_EFFECTS):
        raise commands.BadArgument(
            "A pipeline can contain up to "
            f"{len(RANDOM_EFFECTS)} unique random effects."
        )
    return random_module.SystemRandom().sample(RANDOM_EFFECTS, count)


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
    start = rng.uniform(0, max(0.0, upper - minimum_window))
    stop_minimum = min(upper, start + minimum_window)
    stop = rng.uniform(stop_minimum, upper) if stop_minimum < upper else upper

    start = round(start, 3)
    stop = round(stop, 3)
    if stop <= start:
        start = 0.0
        stop = upper

    options["start"] = start
    options["stop"] = stop
    if "duration" in bounds:
        options["duration"] = 0.0


def _resolve_pipeline_random_effects(
    effects: list[tuple[str, dict[str, Any]]],
    *,
    allow_audio: bool = False,
    media_duration: float = 0.0,
) -> list[tuple[str, str, dict[str, Any]]]:
    rng = random_module.SystemRandom()
    used: set[str] = set()
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
                pool = RANDOM_OVERLAY_EFFECTS
            elif category == "visual":
                pool = RANDOM_EFFECTS
            else:
                pool = (
                    (*RANDOM_EFFECTS, *RANDOM_AUDIO_EFFECTS)
                    if allow_audio
                    else RANDOM_EFFECTS
                )
            available = [candidate for candidate in pool if candidate not in used]
            if not available:
                raise commands.BadArgument(
                    f"There are only {len(pool)} unique random {category} effects "
                    "available for this media."
                )
            chosen = rng.choice(available)
            used.add(chosen)
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
            if not no_random:
                _randomize_effect_timing(
                    chosen,
                    chosen_options,
                    media_duration,
                    rng,
                )
            if category == "overlay":
                chosen_options["flag"] = rng.choice(
                    EFFECT_CATEGORICAL_CHOICES["overlayflag"]["flag"]
                )
            label_category = "" if category == "all" else f" {category}"
            resolved.append(
                (chosen, f"random{label_category} ({chosen})", chosen_options)
            )
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
            dummy = Image.new("RGBA", (vw, 1), (0, 0, 0, 0))
            if animated_caption:
                frame_duration = 50
                overlay_frames = [
                    _caption_frame(
                        dummy,
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
                overlay = _caption_frame(
                    dummy,
                    caption_text,
                    inline_images or {},
                )
                overlay.save(overlay_path)
            command = ["ffmpeg", "-y", "-i", tmp_path]
            if animated_caption:
                command.extend(["-stream_loop", "-1"])
            command.extend(
                [
                    "-i",
                    overlay_path,
                    "-filter_complex",
                    "[0:v][1:v]overlay=0:0:eof_action=repeat",
                    "-t",
                    str(min(60, duration) if duration > 0 else 60),
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
    inline_images = inline_images or {}
    padding = max(10, int(img.width * 0.04))
    max_w = max(1, img.width - padding * 2)

    font_size = max(12, min(96, img.width // 10))
    font = text_font(caption_text, font_size)
    lines = wrap_inline_text(
        caption_text,
        font,
        font_size,
        inline_images,
        max_w,
    )
    while font_size > 12 and any(
        measure_inline_tokens(line, font, font_size, inline_images) > max_w
        for line in lines
    ):
        font_size -= 1
        font = text_font(caption_text, font_size)
        lines = wrap_inline_text(
            caption_text,
            font,
            font_size,
            inline_images,
            max_w,
        )

    font_box = font.getbbox("Ag")
    line_h = int(max(font_size, font_box[3] - font_box[1]))
    gap = max(2, line_h // 5)
    box_h = len(lines) * (line_h + gap) + padding * 2

    new_img = Image.new("RGBA", (img.width, img.height + box_h), (255, 255, 255, 255))
    new_img.paste(img, (0, box_h))

    y = padding
    for line in lines:
        width = measure_inline_tokens(line, font, font_size, inline_images)
        draw_inline_tokens(
            new_img,
            line,
            (round((new_img.width - width) / 2), y),
            font=font,
            image_size=font_size,
            assets=inline_images,
            timestamp_ms=timestamp_ms,
            fill="black",
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
            raise ValueError("Speed currently requires a video or GIF.")

        factor = _playback_factor(speed)
        try:
            total_duration = float((metadata.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            total_duration = 0.0
        if not 0 <= start <= 180 or not 0 <= stop <= 180:
            raise ValueError("Start and stop must be between 0 and 180 seconds.")
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
    values: dict[str, tuple[tuple[str, ...], type, Any]] | None = None,
    switches: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Parse lightweight text-command flags while leaving the media argument."""

    value_specs = dict(values or {})
    switch_specs = switches or {}
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
    aliases: dict[str, tuple[str, type]] = {}
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
    return " ".join(media), options


def _parse_bool(value: str) -> bool:
    normalized = value.casefold().strip()
    if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    raise ValueError("Expected true or false.")


PipelineFlagValues = dict[str, tuple[tuple[str, ...], type, Any]]
PipelineFlagSwitches = dict[str, tuple[str, ...]]
PipelineEffectSpec = tuple[str, PipelineFlagValues, PipelineFlagSwitches]

AUDIO_TIMING_VALUES: PipelineFlagValues = {
    "start": (("from",), float, 0.0),
    "stop": (("end",), float, 0.0),
    "duration": (("length", "d"), float, 0.0),
}


def _audio_values(**values: tuple[tuple[str, ...], type, Any]) -> PipelineFlagValues:
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
    "overlayimage": (
        "special",
        {
            "overlay": (("second", "o"), str, ""),
            "opacity": (("alpha",), float, 70.0),
            "scale": (("size",), float, 1.0),
            "position": (("pos", "p"), str, "center"),
            "x": (("left",), int, 0),
            "y": (("top",), int, 0),
        },
        {"stretch": ("fill",)},
    ),
    "overlayvideo": (
        "special",
        {
            "overlay": (("second", "o"), str, ""),
            "opacity": (("alpha",), float, 70.0),
            "scale": (("size",), float, 1.0),
            "position": (("pos", "p"), str, "center"),
            "x": (("left",), int, 0),
            "y": (("top",), int, 0),
        },
        {"stretch": ("fill",), "no_audio": ("no-audio", "mute-audio")},
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
    "overlay": {
        "opacity": (0, 100, False),
        "scale": (0.05, 2, False),
        "x": (-4096, 4096, True),
        "y": (-4096, 4096, True),
    },
    "overlayflag": {"opacity": (0, 100, False)},
    "overlayimage": {
        "opacity": (0, 100, False),
        "scale": (0.05, 2, False),
        "x": (-4096, 4096, True),
        "y": (-4096, 4096, True),
    },
    "overlayvideo": {
        "opacity": (0, 100, False),
        "scale": (0.05, 2, False),
        "x": (-4096, 4096, True),
        "y": (-4096, 4096, True),
    },
    "speed": {
        "speed": (-5, 5, False),
        "start": (0, 180, False),
        "stop": (0, 180, False),
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
        "at": (0, 180, False),
        "source_start": (0, 180, False),
        "source_stop": (0, 180, False),
        "duration": (0, 180, False),
        "volume": (0, 5, False),
        "pitch": (-12, 12, False),
    },
    "soundeffect": {
        "at": (0, 180, False),
        "source_start": (0, 180, False),
        "source_stop": (0, 180, False),
        "duration": (0, 180, False),
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
            "start": (0, 180, False),
            "stop": (0, 180, False),
            "duration": (0, 180, False),
        }
    )

VIDEO_TIMED_VISUAL_EFFECTS = {
    effect for effect, (engine, _, _) in PIPELINE_EFFECTS.items() if engine == "image"
}
for _timed_effect in VIDEO_TIMED_VISUAL_EFFECTS:
    EFFECT_NUMERIC_BOUNDS.setdefault(_timed_effect, {}).update(
        {
            "start": (0, 180, False),
            "stop": (0, 180, False),
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
        clamped = min(max(numeric, 0), 180)
        normalized[name] = clamped
        if numeric != clamped:
            adjustments.append(
                f"Rounded {effect} {name} from {_format_numeric(numeric)} to "
                f"{_format_numeric(clamped)} (allowed 0–180)"
            )
    return normalized, adjustments


def _renderer_effect_options(
    effect: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    rendered = options.copy()
    if effect in {"overlay", "overlayflag", "overlayimage", "overlayvideo"}:
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
    ("overlay", "image"): ("overlayimage", {}),
    ("overlay", "video"): ("overlayvideo", {}),
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
        if (
            current is not None
            and current[0] == "soundeffect"
            and token.casefold() in {"random", "rand"}
        ):
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

        effect = _pipeline_segment_name(token) if not token.startswith("-") else None
        if effect is not None:
            current = (effect, [], {})
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
    if len(segments) > MAX_PIPELINE_EFFECTS:
        raise commands.BadArgument(
            f"An effect pipeline can contain up to {MAX_PIPELINE_EFFECTS} steps."
        )

    parsed: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []
    for effect, effect_tokens, qualified_defaults in segments:
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
        elif effect in {"caption", "meme"} and remainder and not options["text"]:
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
        parsed.append((effect, options))
    return " ".join(source_tokens), parsed, skipped


COUNTRY_FLAG_ALIASES = {
    "usa": "us",
    "unitedstates": "us",
    "america": "us",
    "uk": "gb",
    "unitedkingdom": "gb",
    "england": "gb",
}


def _effect_name_from_qualified(qualified_name: str) -> str:
    qualified = qualified_name.casefold().split()
    leaf = qualified[-1]
    effect = PIPELINE_EFFECT_ALIASES.get(leaf, leaf)
    if "overlay" in qualified:
        effect = {
            "flag": "overlayflag",
            "image": "overlayimage",
            "video": "overlayvideo",
        }.get(leaf, "overlay")
    elif "audio" in qualified:
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
    elif "bass" in qualified:
        effect = {"boost": "bassboost", "lower": "basslower"}.get(leaf, effect)
    elif "fade" in qualified:
        effect = {"in": "fadein", "out": "fadeout"}.get(leaf, effect)
    return effect


def describe_media_parameters(command: Any) -> None:
    """Fill app-command parameter descriptions and include numeric ranges."""
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            describe_media_parameters(child)
        return
    if not isinstance(command, app_commands.Command):
        return

    effect = _effect_name_from_qualified(command.qualified_name)
    generic = {
        "effects": "Effects and flags in the order they should run",
        "media": MEDIA_INPUT_DESCRIPTION,
        "overlay_media": "User/Emoji/Media URL to place on top",
        "attachment": "Attach the media to process",
        "overlay_attachment": "Attach the media to place on top",
        "text": "Text used by the effect",
        "position": "Where to place the overlay",
        "direction": "Direction used by the effect",
        "clockwise": "Rotate clockwise",
        "stretch": "Stretch the overlay to fill the background",
        "overlay_audio": "Mix audio from the overlay",
        "audio_media": "Audio or video URL to use",
        "audio_attachment": "Attach audio or video to use",
        "effect": "Effect ID, name, or random",
        "start": "Time where the effect begins",
        "stop": "Time where the effect stops, or 0 for the end",
        "duration": "Effect duration, or 0 for the remaining media",
        "at": "Time where the overlay begins",
        "source_start": "Time to begin reading the overlay source",
        "source_stop": "Time to stop reading the overlay source",
        "random_time": "Choose a random valid start time",
        "preserve_transparency": "Keep transparent areas transparent",
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
    for name, description in descriptions.items():
        internal_parameter = command._params.get(name)
        if internal_parameter is not None:
            internal_parameter.description = app_commands.locale_str(description)


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

    candidate = tokens[-1]
    normalized = candidate.casefold().strip().replace(" ", "").replace("-", "")
    if make_flag_asset(candidate) is None and not re.fullmatch(r"[a-z]{2}", normalized):
        return media, None
    return " ".join(tokens[:-1]), candidate


class Images(Cog):
    """Image manipulation commands."""

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
        if len(result.data) > max_size:
            raise commands.BadArgument(
                "The result is too large for this server's upload limit."
            )

        info_lines = [
            f"-# Invoked by {ctx.author.mention}",
            f"-# Took {time.monotonic() - started:.1f}s",
        ]
        if note:
            info_lines.append(f"-# {note}")
        info_text = "\n".join(info_lines)
        filename = result.filename.replace("/", "_").replace("\\", "_")
        media_item: ui.Item[Any]
        if result.displayable:
            media_item = ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
        else:
            media_item = ui.File(f"attachment://{filename}")
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
        if any(len(result.data) > max_size for result in results):
            raise commands.BadArgument(
                "At least one converted file is too large for this server."
            )
        filenames = [
            result.filename.replace("/", "_").replace("\\", "_") for result in results
        ]
        items: list[ui.Item[Any]] = []
        for result, filename in zip(results, filenames):
            if result.displayable:
                items.append(
                    ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
                )
            else:
                items.append(ui.File(f"attachment://{filename}"))
        items.append(
            ui.TextDisplay(
                f"-# Invoked by {ctx.author.mention}\n"
                f"-# Took {time.monotonic() - started:.1f}s"
            )
        )
        container = ui.Container(*items, accent_color=self.bot.embedcolor)
        view_type = type("BulkMediaEffectView", (ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        await ctx.send(
            files=[
                discord.File(BytesIO(result.data), filename)
                for result, filename in zip(results, filenames)
            ],
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
        second_url = ""
        if second_source or second_attachment is not None:
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
            if second_url:
                options["overlay_data"] = await self._fetch_effect_media(
                    ctx, second_url
                )
            options, adjustments = _normalize_effect_options(effect, options)
            renderer_options = _renderer_effect_options(effect, options)
            try:
                result = await render_image_effect(
                    media_data,
                    effect,
                    **renderer_options,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            combined_note = " | ".join(
                part for part in (note, "; ".join(adjustments)) if part
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
        if second_source or second_attachment is not None:
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
                result = await render_video_effect(
                    media_data,
                    effect,
                    second_data=second_data,
                    **renderer_options,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            combined_note = " | ".join(
                part for part in (note, "; ".join(adjustments)) if part
            )
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note=combined_note,
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
        normalized = flag.casefold().strip().replace(" ", "").replace("-", "")
        country = COUNTRY_FLAG_ALIASES.get(normalized, normalized)
        if not re.fullmatch(r"[a-z]{2}", country):
            raise commands.BadArgument(
                "Unknown flag. Use a two-letter country code, pride flag, or pirate."
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

        max_size = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )
        async with self.bot.media_semaphore, ctx.typing():
            import time

            started = time.time()
            image_data = await self._fetch_effect_media(ctx, media_url)
            try:
                output = await make_globe(
                    image_data,
                    speed,
                    clockwise,
                    max_size,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            elapsed = time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"
            if adjustments:
                info_text += f"\n-# {'; '.join(adjustments)}"
            gallery = ui.MediaGallery(MediaGalleryItem("attachment://globe.gif"))
            container = ui.Container(
                gallery,
                ui.TextDisplay(info_text),
                accent_color=self.bot.embedcolor,
            )
            view_type = type("GlobeView", (ui.LayoutView,), {})
            view = view_type(timeout=None)
            view.add_item(container)
            await ctx.send(
                file=discord.File(output, "globe.gif"),
                view=view,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
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

        max_size = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )
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
                    max_size,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            elapsed = time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"
            if adjustments:
                info_text += f"\n-# {'; '.join(adjustments)}"
            gallery = ui.MediaGallery(MediaGalleryItem("attachment://spin3d.gif"))
            container = ui.Container(
                gallery,
                ui.TextDisplay(info_text),
                accent_color=self.bot.embedcolor,
            )
            view_type = type("Spin3DView", (ui.LayoutView,), {})
            view = view_type(timeout=None)
            view.add_item(container)
            await ctx.send(
                file=discord.File(output, "spin3d.gif"),
                view=view,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
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
            view_type = type("CaptionView", (ui.LayoutView,), {})
            view = view_type(timeout=None)
            view.add_item(container)
            await ctx.send(
                file=discord.File(buf, filename),
                view=view,
                reference=ctx.message.to_reference(fail_if_not_exists=False),
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
                raise commands.BadArgument("No image or video found.")

        if not image_str or not isinstance(image_str, str):
            raise commands.BadArgument("Could not resolve an image or video source.")
        async with self.bot.media_semaphore, ctx.typing():
            import time as _time

            started = _time.time()
            img_data = await self._fetch_effect_media(ctx, image_str)
            try:
                result = await _speed_video(
                    img_data,
                    speed_val,
                    start=start,
                    stop=stop,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error

            elapsed = _time.time() - started
            info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {elapsed:.1f}s"
            if adjustments:
                info_text += f"\n-# {'; '.join(adjustments)}"

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
        """Resize a User/Emoji/Media URL."""
        await self._parsed_effect(
            ctx,
            "resize",
            argument,
            values={
                "scale": (("s",), float, 1.0),
                "ratio": (("aspect", "r"), str, ""),
            },
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
        extras={"usage": "<media> [-format mp4]"},
    )
    async def convert_command(self, ctx: Context, *, argument: str = "") -> None:
        """Convert up to five User/Emoji/Media URLs to another format.

        -# -format    Choose the output format. Defaults to MP4.
        """
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

    @commands.group(name="overlay", invoke_without_command=True)
    async def overlay_group(self, ctx: Context) -> None:
        """Overlay a flag or second User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @overlay_group.command(
        name="flag",
        extras={"usage": "<media> [-flag pride -opacity 35]"},
    )
    async def overlay_group_flag(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a pride, country, or pirate flag on a User/Emoji/Media URL.

        -# -flag       Choose a pride flag, pirate flag, or country code.
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
        options["stretch"] = True
        await self._apply_image_effect(ctx, "overlay", source=media, **options)

    @overlay_group.command(
        name="image",
        extras={
            "usage": (
                "<media> -overlay <media> [-opacity 70 -scale 1 "
                "-position center -x 0 -y 0 -stretch]"
            )
        },
    )
    async def overlay_group_image(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a User/Emoji/Media URL on another.

        -# -overlay    The User/Emoji/Media URL placed over the main input.
        -# -opacity    Change the overlay opacity from 0 to 100%. Defaults to 70.
        -# -scale      Change the overlay size. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 70.0),
                "scale": (("size",), float, 1.0),
                "position": (("pos", "p"), str, "center"),
                "x": (("left",), int, 0),
                "y": (("top",), int, 0),
            },
            switches={"stretch": ("fill",)},
        )
        second_source = str(options.pop("overlay"))
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media,
            second_source=second_source,
            **options,
        )

    @overlay_group.command(
        name="video",
        extras={
            "usage": (
                "<media> -overlay <media> [-opacity 70 -scale 1 "
                "-position center -x 0 -y 0 -stretch -no-audio]"
            )
        },
    )
    async def overlay_group_video(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a User/Emoji/Media URL on another.

        -# -overlay    The User/Emoji/Media URL placed over the main input.
        -# -opacity    Change the overlay opacity from 0 to 100%. Defaults to 70.
        -# -scale      Change the overlay size. Defaults to 1.
        -# -no-audio   Do not mix audio from the overlay video.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 70.0),
                "scale": (("size",), float, 1.0),
                "position": (("pos", "p"), str, "center"),
                "x": (("left",), int, 0),
                "y": (("top",), int, 0),
            },
            switches={"stretch": ("fill",), "no_audio": ("no-audio", "mute-audio")},
        )
        second_source = str(options.pop("overlay"))
        options["overlay_audio"] = not bool(options.pop("no_audio", False))
        await self._apply_video_effect(
            ctx,
            "overlay",
            source=media,
            second_source=second_source,
            **options,
        )

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
            (
                f"`{effect.id}` • **{effect.display_name}** "
                f"• {effect.category.title()}"
            )
            for effect in audio_effect_catalog()
        ]
        pager = SimplePages(entries, ctx=ctx, per_page=15)
        pager.embed.title = "Sound effects"
        pager.embed.colour = ctx.color
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
        max_size = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )

        if effect == "globe":
            speed = float(options["speed"])
            if not 0.25 <= speed <= 3:
                raise ValueError("Speed must be between 0.25 and 3.")
            output = await make_globe(
                current_data,
                speed,
                bool(options["clockwise"]),
                max_size,
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
                max_size,
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
            return EffectResult(
                await _speed_video(
                    current_data,
                    speed,
                    start=float(options.get("start", 0)),
                    stop=float(options.get("stop", 0)),
                ),
                "speed.mp4",
            )

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
            return await render_image_effect(
                current_data,
                "overlay",
                overlay_data=overlay_data,
                opacity=float(renderer_options["opacity"]),
                stretch=True,
            )

        if effect in {"overlayimage", "overlayvideo"}:
            overlay_source = str(options.pop("overlay")).strip()
            if not overlay_source:
                raise ValueError(
                    "Overlay requires `-overlay` followed by a second "
                    "User/Emoji/Media URL."
                )
            overlay_url = await self._resolve_effect_media(
                ctx,
                overlay_source,
                scan_messages=False,
            )
            overlay_data = await self._fetch_effect_media(ctx, overlay_url)
            renderer_options = _renderer_effect_options(effect, options)
            if effect == "overlayvideo":
                renderer_options["overlay_audio"] = not bool(
                    renderer_options.pop("no_audio", False)
                )
                return await render_video_effect(
                    current_data,
                    "overlay",
                    second_data=overlay_data,
                    **renderer_options,
                )
            return await render_image_effect(
                current_data,
                "overlay",
                overlay_data=overlay_data,
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
            for effect, label, options in _resolve_pipeline_random_effects(
                effects,
                allow_audio=bool(media_probe and media_probe.has_audio),
                media_duration=float(getattr(media_probe, "duration", 0.0)),
            ):
                engine = PIPELINE_EFFECTS[effect][0]
                normalized_options, normalized_notes = _normalize_effect_options(
                    effect,
                    options,
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
                try:
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
                    skipped.append(f"{label} (took longer than 30 seconds)")
                    continue
                except ValueError as error:
                    skipped.append(f"{label} ({error})")
                    continue
                current_data = result.data
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
            await self._send_effect_result(
                ctx,
                result,
                started=started,
                note=" | ".join(note_parts),
            )

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
        attachment="Attach an image, GIF, or video",
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

    @effect_3.group(name="overlay", invoke_without_command=True)
    async def image_effect_overlay(self, ctx: Context) -> None:
        """Overlay a flag or second User/Emoji/Media URL."""
        await ctx.send_help(ctx.command)

    @image_effect_overlay.command(name="flag")
    @app_commands.describe(
        flag="Pride flag, pirate, or two-letter country code",
        opacity="Overlay opacity from 0 to 100%",
    )
    async def image_effect_overlay_flag(
        self,
        ctx: Context,
        flag: str,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        opacity: float = 35.0,
    ) -> None:
        """Overlay a pride, country, or pirate flag on a User/Emoji/Media URL."""
        flag_data = await self._flag_data(ctx, flag)
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media or "",
            attachment=attachment,
            overlay_data=flag_data,
            opacity=opacity,
            scale=1.0,
            stretch=True,
        )

    @image_effect_overlay.command(name="image")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        overlay_media="User/Emoji/Media URL to place on top",
        attachment="Attach the background image or GIF",
        overlay_attachment="Attach the image to place on top",
        opacity="Overlay opacity from 0 to 100%",
        scale="Overlay size from 0.05 to 2 times the background",
        position="Where to place the overlay",
        x="Horizontal offset from -4096 to 4096 pixels",
        y="Vertical offset from -4096 to 4096 pixels",
        stretch="Stretch instead of preserving aspect ratio",
    )
    async def image_effect_overlay_image(
        self,
        ctx: Context,
        media: str | None = None,
        overlay_media: str | None = None,
        attachment: discord.Attachment | None = None,
        overlay_attachment: discord.Attachment | None = None,
        opacity: float = 70.0,
        scale: float = 1.0,
        position: Literal[
            "center",
            "top-left",
            "top",
            "top-right",
            "left",
            "right",
            "bottom-left",
            "bottom",
            "bottom-right",
        ] = "center",
        x: int = 0,
        y: int = 0,
        stretch: bool = False,
    ) -> None:
        """Overlay a User/Emoji/Media URL on another."""
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media or "",
            attachment=attachment,
            second_source=overlay_media or "",
            second_attachment=overlay_attachment,
            opacity=opacity,
            scale=scale,
            position=position,
            x=x,
            y=y,
            stretch=stretch,
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

    @image_effect_overlay.command(name="video")
    @app_commands.describe(
        media=MEDIA_INPUT_DESCRIPTION,
        overlay_media="User/Emoji/Media URL to place on top",
        attachment="Attach the background video",
        overlay_attachment="Attach the video or image to place on top",
        opacity="Overlay opacity from 0 to 100%",
        scale="Overlay size from 0.05 to 2 times the background",
        position="Where to place the overlay",
        x="Horizontal offset from -4096 to 4096 pixels",
        y="Vertical offset from -4096 to 4096 pixels",
        stretch="Stretch instead of preserving aspect ratio",
        overlay_audio="Mix audio from the overlay video",
    )
    async def video_effects_overlay_video(
        self,
        ctx: Context,
        media: str | None = None,
        overlay_media: str | None = None,
        attachment: discord.Attachment | None = None,
        overlay_attachment: discord.Attachment | None = None,
        opacity: float = 70.0,
        scale: float = 1.0,
        position: Literal[
            "center",
            "top-left",
            "top",
            "top-right",
            "left",
            "right",
            "bottom-left",
            "bottom",
            "bottom-right",
        ] = "center",
        x: int = 0,
        y: int = 0,
        stretch: bool = False,
        overlay_audio: bool = True,
    ) -> None:
        """Overlay a User/Emoji/Media URL on another."""
        await self._apply_video_effect(
            ctx,
            "overlay",
            source=media or "",
            attachment=attachment,
            second_source=overlay_media or "",
            second_attachment=overlay_attachment,
            opacity=opacity,
            scale=scale,
            position=position,
            x=x,
            y=y,
            stretch=stretch,
            overlay_audio=overlay_audio,
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

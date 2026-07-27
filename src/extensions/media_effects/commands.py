from __future__ import annotations

import math
import re
import shlex
import time
from functools import lru_cache
from io import BytesIO
from typing import TYPE_CHECKING, Any, Literal, Optional, cast
from urllib.parse import urlsplit

import discord
import numpy as np
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

from core import Cog
from utils import MediaConverter, fetch_public_bytes, to_image, to_thread
from utils.rich_text import (
    draw_inline_tokens,
    inline_animation_duration,
    measure_inline_tokens,
    resolve_inline_images,
    text_font,
    wrap_inline_text,
)

from .processing import (
    EffectResult,
    convert_media,
    make_flag_asset,
    render_image_effect,
    render_video_effect,
)

if TYPE_CHECKING:
    from extensions.context import Context

Image.MAX_IMAGE_PIXELS = 25_000_000

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
CAPTION_GIF_FILTER = (
    "fps=10,scale=480:-1:flags=lanczos,"
    "split[s0][s1];"
    "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
    "[s1][p]paletteuse=dither=bayer:bayer_scale=5"
)


def _is_klipy_media_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return hostname in {"klipy.com", "www.klipy.com", "static.klipy.com"}


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

    if not -360 <= tilt <= 360:
        raise ValueError("Tilt must be between -360 and 360.")
    if not -3 <= zoom <= 3:
        raise ValueError("Zoom must be between -3 and 3.")
    if not 0.25 <= speed <= 3:
        raise ValueError("Speed must be between 0.25 and 3.")

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
    if not 0.25 <= speed <= 3:
        raise ValueError("Speed must be between 0.25 and 3.")

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
                timeout=60,
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
                    timeout=60,
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
                f"[0:v]setpts={1 / speed}*PTS[v];[0:a]{audio_filter}[a]",
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
            command += ["-filter:v", f"setpts={1 / speed}*PTS", "-an"]
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


def _parse_effect_flags(
    argument: str,
    *,
    values: dict[str, tuple[tuple[str, ...], type, Any]] | None = None,
    switches: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Parse lightweight text-command flags while leaving the media argument."""

    value_specs = values or {}
    switch_specs = switches or {}
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
    index = 0
    while index < len(tokens):
        token = tokens[index]
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
        index += 1

    return " ".join(media), options


COUNTRY_FLAG_ALIASES = {
    "usa": "us",
    "unitedstates": "us",
    "america": "us",
    "uk": "gb",
    "unitedkingdom": "gb",
    "england": "gb",
}


def _positional_flag(media: str) -> tuple[str, str | None]:
    """Extract a trailing flag name from the text-only overlay syntax."""
    try:
        tokens = shlex.split(media)
    except ValueError:
        return media, None
    if len(tokens) < 2:
        return media, None

    candidate = tokens[-1]
    normalized = candidate.casefold().strip().replace(" ", "").replace("-", "")
    if make_flag_asset(candidate) is None and not re.fullmatch(r"[a-z]{2}", normalized):
        return media, None
    return " ".join(tokens[:-1]), candidate


class Images(Cog):
    """Image manipulation commands."""

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
                return await MediaConverter().convert(
                    ctx,
                    source,
                    include_message_media=False,
                )
            except commands.BadArgument as error:
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
                return await MediaConverter().convert(ctx, "")
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "No media found. Attach a file, reply to media, or provide a URL."
                ) from error
        raise commands.BadArgument("A second media file or URL is required.")

    async def _fetch_effect_media(self, ctx: Context, url: str) -> bytes:
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

        info_text = f"-# Invoked by {ctx.author.mention}\n-# Took {time.monotonic() - started:.1f}s"
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

    async def _apply_image_effect(
        self,
        ctx: Context,
        effect: str,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
        second_source: str = "",
        second_attachment: discord.Attachment | None = None,
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
            try:
                result = await render_image_effect(media_data, effect, **options)
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            await self._send_effect_result(ctx, result, started=started)

    async def _apply_video_effect(
        self,
        ctx: Context,
        effect: str,
        *,
        source: str = "",
        attachment: discord.Attachment | None = None,
        second_source: str = "",
        second_attachment: discord.Attachment | None = None,
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
        elif effect in {"overlay", "audioreplace"} and len(ctx.message.attachments) > 1:
            second_url = await self._resolve_effect_media(
                ctx,
                attachment_index=1,
                scan_messages=False,
            )

        started = time.monotonic()
        async with self.bot.media_semaphore, ctx.typing():
            media_data = await self._fetch_effect_media(ctx, media_url)
            second_data = (
                await self._fetch_effect_media(ctx, second_url) if second_url else None
            )
            try:
                result = await render_video_effect(
                    media_data,
                    effect,
                    second_data=second_data,
                    **options,
                )
            except ValueError as error:
                raise commands.BadArgument(str(error)) from error
            await self._send_effect_result(ctx, result, started=started)

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

    async def _globe_effect(self, ctx: Context, *, argument: str = "") -> None:
        try:
            media_argument, speed, clockwise = _parse_globe_input(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error

        converter = MediaConverter()
        try:
            media_url = await converter.convert(ctx, media_argument)
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
            image_data = cast(
                bytes,
                await to_image(ctx.session, media_url, bytes=True),
            )
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

    async def _spin3d_effect(self, ctx: Context, *, argument: str = "") -> None:
        try:
            media_argument, tilt, zoom, speed, clockwise = _parse_spin3d_input(argument)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from error

        converter = MediaConverter()
        try:
            media_url = await converter.convert(ctx, media_argument)
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
            image_data = cast(
                bytes,
                await to_image(ctx.session, media_url, bytes=True),
            )
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

    async def _caption_effect(
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

    async def _speed_effect(self, ctx: Context, *, input: str) -> None:
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

    @commands.command(
        name="globe",
        aliases=("sphere3d", "imageglobe"),
        extras={"usage": "<input> [-speed 1 -clockwise]"},
    )
    async def globe(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap an image around a rotating globe.

        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        await self._globe_effect(ctx, argument=argument)

    @commands.command(
        name="spin3d",
        aliases=("spin3", "3dspin"),
        extras={"usage": "<input> [-tilt 15 -zoom 1.5 -speed 1 -clockwise]"},
    )
    async def spin3d(self, ctx: Context, *, argument: str = "") -> None:
        """Spin an image in 3D space.

        -# -tilt         Change the vertical tilt. Defaults to 15.
        -# -zoom         Change the image zoom. Defaults to 1.5.
        -# -speed        Change the rotation speed. Defaults to 1.
        -# -clockwise    Rotate clockwise instead of counterclockwise.
        """
        await self._spin3d_effect(ctx, argument=argument)

    @commands.command(
        name="caption",
        extras={"usage": "<text> [input]"},
    )
    async def caption(
        self,
        ctx: Context,
        *,
        text: str = "",
        media_url: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add a caption to an image."""
        await self._caption_effect(
            ctx,
            text=text,
            media_url=media_url,
            user=user,
            attachment=attachment,
        )

    @commands.command(
        name="speed",
        extras={"usage": "<input> <speed>"},
    )
    async def speed(self, ctx: Context, *, input: str) -> None:
        """Change playback speed of a video or GIF. 2.0 = 2x, 0.5 = half speed."""
        await self._speed_effect(ctx, input=input)

    @commands.command(name="invert", extras={"usage": "<input>"})
    async def invert(self, ctx: Context, *, media: str = "") -> None:
        """Invert the colors of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "invert", source=media)

    @commands.command(
        name="spin",
        extras={"usage": "<input> [-speed 1 -clockwise]"},
    )
    async def spin(self, ctx: Context, *, argument: str = "") -> None:
        """Rotate media in a flat animated loop.

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
        extras={"usage": "<input> [-strength 20]"},
    )
    async def magik(self, ctx: Context, *, argument: str = "") -> None:
        """Distort an image or GIF with a liquid effect.

        -# -strength    Change the distortion strength. Defaults to 20.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"strength": (("amount", "s"), float, 20.0)},
        )
        await self._apply_image_effect(ctx, "magik", source=media, **options)

    @commands.command(
        name="gifmagik",
        aliases=("gmagik",),
        extras={"usage": "<input> [-strength 20 -speed 1]"},
    )
    async def gifmagik(self, ctx: Context, *, argument: str = "") -> None:
        """Animate a liquid distortion over an image or GIF.

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
        extras={"usage": "<input> [-horizontal | -vertical]"},
    )
    async def flip(self, ctx: Context, *, argument: str = "") -> None:
        """Flip an image, GIF, or video horizontally or vertically.

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
        extras={"usage": "<input> [-speed 1 -clockwise]"},
    )
    async def cube(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap an image or GIF around a rotating cube.

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
        extras={"usage": "<input> [-speed 1 -clockwise]"},
    )
    async def pyramid(self, ctx: Context, *, argument: str = "") -> None:
        """Wrap an image or GIF around a rotating pyramid.

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
        extras={"usage": "<input> [-radius 5]"},
    )
    async def blur(self, ctx: Context, *, argument: str = "") -> None:
        """Blur an image, GIF, or video.

        -# -radius    Change the blur strength. Defaults to 5.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"radius": (("r", "strength"), float, 5.0)},
        )
        await self._apply_image_effect(ctx, "blur", source=media, **options)

    @commands.command(
        name="cropcircle",
        aliases=("circlecrop",),
        extras={"usage": "<input>"},
    )
    async def crop_circle(self, ctx: Context, *, media: str = "") -> None:
        """Crop an image or GIF into a circle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media,
            shape="circle",
        )

    @commands.command(
        name="croptriangle",
        aliases=("trianglecrop",),
        extras={"usage": "<input>"},
    )
    async def crop_triangle(self, ctx: Context, *, media: str = "") -> None:
        """Crop an image or GIF into a triangle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media,
            shape="triangle",
        )

    @commands.command(
        name="deepfry",
        extras={"usage": "<input> [-intensity 1]"},
    )
    async def deepfry(self, ctx: Context, *, argument: str = "") -> None:
        """Deep-fry an image, GIF, or video.

        -# -intensity    Change the effect intensity. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"intensity": (("amount", "i"), float, 1.0)},
        )
        await self._apply_image_effect(ctx, "deepfry", source=media, **options)

    @commands.command(
        name="grayscale",
        aliases=("greyscale",),
        extras={"usage": "<input>"},
    )
    async def grayscale(self, ctx: Context, *, media: str = "") -> None:
        """Convert an image, GIF, or video to grayscale."""
        await self._apply_image_effect(ctx, "grayscale", source=media)

    @commands.command(name="mirrorbottom", extras={"usage": "<input>"})
    async def mirror_bottom(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the bottom half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="bottom")

    @commands.command(name="mirrorright", extras={"usage": "<input>"})
    async def mirror_right(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the right half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="right")

    @commands.command(name="mirrorleft", extras={"usage": "<input>"})
    async def mirror_left(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the left half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="left")

    @commands.command(name="mirrortop", extras={"usage": "<input>"})
    async def mirror_top(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the top half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="top")

    @commands.command(
        name="jpeg",
        aliases=("needsmorejpeg",),
        extras={"usage": "<input> [-quality 8]"},
    )
    async def jpeg(self, ctx: Context, *, argument: str = "") -> None:
        """Make media look heavily JPEG-compressed.

        -# -quality    Change the JPEG quality from 1 to 50. Defaults to 8.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"quality": (("q",), int, 8)},
        )
        await self._apply_image_effect(ctx, "jpeg", source=media, **options)

    @commands.command(
        name="overlayflag",
        aliases=("flagoverlay",),
        extras={"usage": "<input> [-flag pride -opacity 0.35]"},
    )
    async def overlay_flag(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a pride, country, or pirate flag on media.

        -# -flag       Choose a pride flag, pirate flag, or country code.
        -# -opacity    Change the flag opacity. Defaults to 0.35.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "flag": (("f",), str, "pride"),
                "opacity": (("o",), float, 0.35),
            },
        )
        flag = str(options.pop("flag"))
        if flag == "pride" and not re.search(r"(?:^|\s)-{1,2}(?:flag|f)(?:[=\s]|$)", argument):
            media, positional = _positional_flag(media)
            if positional is not None:
                flag = positional
        options["overlay_data"] = await self._flag_data(ctx, flag)
        options["stretch"] = True
        await self._apply_image_effect(ctx, "overlay", source=media, **options)

    @commands.command(
        name="overlayimage",
        aliases=("imageoverlay",),
        extras={"usage": "<input> -overlay <input> [-opacity 0.7 -scale 1]"},
    )
    async def overlay_image(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a second image on top of the first.

        -# -overlay    The image placed over the main input.
        -# -opacity    Change the overlay opacity. Defaults to 0.7.
        -# -scale      Change the overlay size. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 0.7),
                "scale": (("size",), float, 1.0),
            },
        )
        second_source = str(options.pop("overlay"))
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media,
            second_source=second_source,
            **options,
        )

    @commands.command(
        name="swirl",
        extras={"usage": "<input> [-strength 180]"},
    )
    async def swirl(self, ctx: Context, *, argument: str = "") -> None:
        """Swirl an image or GIF.

        -# -strength    Change the swirl in degrees. Defaults to 180.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"strength": (("degrees", "s"), float, 180.0)},
        )
        await self._apply_image_effect(ctx, "swirl", source=media, **options)

    @commands.command(
        name="gifswirl",
        extras={"usage": "<input> [-strength 180 -speed 1]"},
    )
    async def gifswirl(self, ctx: Context, *, argument: str = "") -> None:
        """Animate a swirling distortion over an image or GIF.

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

    @commands.command(
        name="wiggle",
        extras={"usage": "<input> [-amount 8 -speed 1]"},
    )
    async def wiggle(self, ctx: Context, *, argument: str = "") -> None:
        """Make an image, GIF, or video wiggle.

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

    @commands.command(name="reverse", extras={"usage": "<input>"})
    async def reverse(self, ctx: Context, *, media: str = "") -> None:
        """Reverse a video, GIF, or audio file."""
        await self._apply_video_effect(ctx, "reverse", source=media)

    @commands.command(
        name="overlayvideo",
        aliases=("videooverlay",),
        extras={"usage": "<input> -overlay <input> [-opacity 0.7 -scale 0.5]"},
    )
    async def overlay_video(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a second video or image on top of a video.

        -# -overlay    The video or image placed over the main input.
        -# -opacity    Change the overlay opacity. Defaults to 0.7.
        -# -scale      Change the overlay size. Defaults to 0.5.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 0.7),
                "scale": (("size",), float, 0.5),
            },
        )
        second_source = str(options.pop("overlay"))
        await self._apply_video_effect(
            ctx,
            "overlay",
            source=media,
            second_source=second_source,
            **options,
        )

    @commands.command(
        name="bassboost",
        extras={"usage": "<input> [-gain 12]"},
    )
    async def bass_boost(self, ctx: Context, *, argument: str = "") -> None:
        """Boost the bass in a video or audio file.

        -# -gain    Change the bass gain in decibels. Defaults to 12.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"gain": (("amount", "g"), float, 12.0)},
        )
        await self._apply_video_effect(ctx, "bassboost", source=media, **options)

    @commands.command(
        name="basslower",
        aliases=("bassreduce",),
        extras={"usage": "<input> [-gain 12]"},
    )
    async def bass_lower(self, ctx: Context, *, argument: str = "") -> None:
        """Lower the bass in a video or audio file.

        -# -gain    Change the bass reduction in decibels. Defaults to 12.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"gain": (("amount", "g"), float, 12.0)},
        )
        await self._apply_video_effect(ctx, "basslower", source=media, **options)

    @commands.command(name="audioreverse", extras={"usage": "<input>"})
    async def audio_reverse(self, ctx: Context, *, media: str = "") -> None:
        """Reverse the audio while keeping video playback forward."""
        await self._apply_video_effect(ctx, "audioreverse", source=media)

    @commands.command(
        name="audioreverb",
        extras={"usage": "<input> [-room 0.5]"},
    )
    async def audio_reverb(self, ctx: Context, *, argument: str = "") -> None:
        """Add reverb to a video or audio file.

        -# -room    Change the reverb room size. Defaults to 0.5.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"room": (("amount", "r"), float, 0.5)},
        )
        await self._apply_video_effect(ctx, "audioreverb", source=media, **options)

    @commands.command(name="audioextract", extras={"usage": "<input>"})
    async def audio_extract(self, ctx: Context, *, media: str = "") -> None:
        """Extract up to five audio tracks from a video."""
        await self._apply_video_effect(ctx, "extract", source=media)

    @commands.command(
        name="audioreplace",
        extras={"usage": "<input> -audio <input>"},
    )
    async def audio_replace(self, ctx: Context, *, argument: str = "") -> None:
        """Replace a video's audio with another audio file.

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

    @commands.command(
        name="audiodestroy",
        extras={"usage": "<input> [-amount 6]"},
    )
    async def audio_destroy(self, ctx: Context, *, argument: str = "") -> None:
        """Deliberately destroy the quality of audio.

        -# -amount    Change how heavily the audio is destroyed. Defaults to 6.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"amount": (("strength", "a"), int, 6)},
        )
        await self._apply_video_effect(ctx, "audiodestroy", source=media, **options)

    @commands.command(
        name="audiocompress",
        extras={"usage": "<input> [-ratio 4]"},
    )
    async def audio_compress(self, ctx: Context, *, argument: str = "") -> None:
        """Apply dynamic-range compression to audio.

        -# -ratio    Change the compression ratio. Defaults to 4.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"ratio": (("r",), float, 4.0)},
        )
        await self._apply_video_effect(ctx, "audiocompress", source=media, **options)

    @commands.command(
        name="audiochannelscombine",
        aliases=("channelscombine", "audiomono"),
        extras={"usage": "<input>"},
    )
    async def audio_channels_combine(self, ctx: Context, *, media: str = "") -> None:
        """Combine every audio channel into one mono channel."""
        await self._apply_video_effect(ctx, "channelscombine", source=media)

    @commands.command(
        name="convert",
        aliases=("covert",),
        extras={"usage": "<input> [-format mp4]"},
    )
    async def convert_command(self, ctx: Context, *, argument: str = "") -> None:
        """Convert up to five uploaded media files to another format.

        -# -format    Choose the output format. Defaults to MP4.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"format": (("f",), str, "mp4")},
        )
        await self._convert_effect(ctx, str(options["format"]), source=media)

    @commands.command(
        name="volume",
        extras={"usage": "<input> [-volume 1]"},
    )
    async def volume(self, ctx: Context, *, argument: str = "") -> None:
        """Change the volume of a video or audio file.

        -# -volume    Change the volume multiplier. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"volume": (("amount", "v"), float, 1.0)},
        )
        await self._apply_video_effect(ctx, "volume", source=media, **options)

    @commands.group(name="crop", invoke_without_command=True)
    async def crop_group(self, ctx: Context) -> None:
        """Crop media into a shape."""
        await ctx.send_help(ctx.command)

    @crop_group.command(name="circle", extras={"usage": "<input>"})
    async def crop_group_circle(self, ctx: Context, *, media: str = "") -> None:
        """Crop an image or GIF into a circle."""
        await self._apply_image_effect(ctx, "crop", source=media, shape="circle")

    @crop_group.command(name="triangle", extras={"usage": "<input>"})
    async def crop_group_triangle(self, ctx: Context, *, media: str = "") -> None:
        """Crop an image or GIF into a triangle."""
        await self._apply_image_effect(ctx, "crop", source=media, shape="triangle")

    @commands.group(name="mirror", invoke_without_command=True)
    async def mirror_group(self, ctx: Context) -> None:
        """Mirror one side of media onto the other."""
        await ctx.send_help(ctx.command)

    @mirror_group.command(name="bottom", extras={"usage": "<input>"})
    async def mirror_group_bottom(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the bottom half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="bottom")

    @mirror_group.command(name="right", extras={"usage": "<input>"})
    async def mirror_group_right(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the right half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="right")

    @mirror_group.command(name="left", extras={"usage": "<input>"})
    async def mirror_group_left(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the left half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="left")

    @mirror_group.command(name="top", extras={"usage": "<input>"})
    async def mirror_group_top(self, ctx: Context, *, media: str = "") -> None:
        """Mirror the top half of an image, GIF, or video."""
        await self._apply_image_effect(ctx, "mirror", source=media, direction="top")

    @commands.group(name="overlay", invoke_without_command=True)
    async def overlay_group(self, ctx: Context) -> None:
        """Overlay a flag, image, or video on media."""
        await ctx.send_help(ctx.command)

    @overlay_group.command(
        name="flag",
        extras={"usage": "<input> [-flag pride -opacity 0.35]"},
    )
    async def overlay_group_flag(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a pride, country, or pirate flag on media.

        -# -flag       Choose a pride flag, pirate flag, or country code.
        -# -opacity    Change the flag opacity. Defaults to 0.35.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "flag": (("f",), str, "pride"),
                "opacity": (("o",), float, 0.35),
            },
        )
        flag = str(options.pop("flag"))
        if flag == "pride" and not re.search(r"(?:^|\s)-{1,2}(?:flag|f)(?:[=\s]|$)", argument):
            media, positional = _positional_flag(media)
            if positional is not None:
                flag = positional
        options["overlay_data"] = await self._flag_data(ctx, flag)
        options["stretch"] = True
        await self._apply_image_effect(ctx, "overlay", source=media, **options)

    @overlay_group.command(
        name="image",
        extras={"usage": "<input> -overlay <input> [-opacity 0.7 -scale 1]"},
    )
    async def overlay_group_image(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a second image on top of the first.

        -# -overlay    The image placed over the main input.
        -# -opacity    Change the overlay opacity. Defaults to 0.7.
        -# -scale      Change the overlay size. Defaults to 1.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 0.7),
                "scale": (("size",), float, 1.0),
            },
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
        extras={"usage": "<input> -overlay <input> [-opacity 0.7 -scale 0.5]"},
    )
    async def overlay_group_video(self, ctx: Context, *, argument: str = "") -> None:
        """Overlay a second video or image on top of a video.

        -# -overlay    The video or image placed over the main input.
        -# -opacity    Change the overlay opacity. Defaults to 0.7.
        -# -scale      Change the overlay size. Defaults to 0.5.
        """
        media, options = _parse_effect_flags(
            argument,
            values={
                "overlay": (("second", "o"), str, ""),
                "opacity": (("alpha",), float, 0.7),
                "scale": (("size",), float, 0.5),
            },
        )
        second_source = str(options.pop("overlay"))
        await self._apply_video_effect(
            ctx,
            "overlay",
            source=media,
            second_source=second_source,
            **options,
        )

    @commands.group(name="bass", invoke_without_command=True)
    async def bass_group(self, ctx: Context) -> None:
        """Change the bass in a video or audio file."""
        await ctx.send_help(ctx.command)

    @bass_group.command(
        name="boost",
        extras={"usage": "<input> [-gain 12]"},
    )
    async def bass_group_boost(self, ctx: Context, *, argument: str = "") -> None:
        """Boost the bass in a video or audio file.

        -# -gain    Change the bass gain in decibels. Defaults to 12.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"gain": (("amount", "g"), float, 12.0)},
        )
        await self._apply_video_effect(ctx, "bassboost", source=media, **options)

    @bass_group.command(
        name="lower",
        extras={"usage": "<input> [-gain 12]"},
    )
    async def bass_group_lower(self, ctx: Context, *, argument: str = "") -> None:
        """Lower the bass in a video or audio file.

        -# -gain    Change the bass reduction in decibels. Defaults to 12.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"gain": (("amount", "g"), float, 12.0)},
        )
        await self._apply_video_effect(ctx, "basslower", source=media, **options)

    @commands.group(name="audio", invoke_without_command=True)
    async def audio_group(self, ctx: Context) -> None:
        """Apply an effect to the audio in a media file."""
        await ctx.send_help(ctx.command)

    @audio_group.command(name="reverse", extras={"usage": "<input>"})
    async def audio_group_reverse(self, ctx: Context, *, media: str = "") -> None:
        """Reverse the audio while keeping video playback forward."""
        await self._apply_video_effect(ctx, "audioreverse", source=media)

    @audio_group.command(
        name="reverb",
        extras={"usage": "<input> [-room 0.5]"},
    )
    async def audio_group_reverb(self, ctx: Context, *, argument: str = "") -> None:
        """Add reverb to a video or audio file.

        -# -room    Change the reverb room size. Defaults to 0.5.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"room": (("amount", "r"), float, 0.5)},
        )
        await self._apply_video_effect(ctx, "audioreverb", source=media, **options)

    @audio_group.command(name="extract", extras={"usage": "<input>"})
    async def audio_group_extract(self, ctx: Context, *, media: str = "") -> None:
        """Extract up to five audio tracks from a video."""
        await self._apply_video_effect(ctx, "extract", source=media)

    @audio_group.command(
        name="replace",
        extras={"usage": "<input> -audio <input>"},
    )
    async def audio_group_replace(self, ctx: Context, *, argument: str = "") -> None:
        """Replace a video's audio with another audio file.

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
        name="destroy",
        extras={"usage": "<input> [-amount 6]"},
    )
    async def audio_group_destroy(self, ctx: Context, *, argument: str = "") -> None:
        """Deliberately destroy the quality of audio.

        -# -amount    Change how heavily the audio is destroyed. Defaults to 6.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"amount": (("strength", "a"), int, 6)},
        )
        await self._apply_video_effect(ctx, "audiodestroy", source=media, **options)

    @audio_group.command(
        name="compress",
        extras={"usage": "<input> [-ratio 4]"},
    )
    async def audio_group_compress(self, ctx: Context, *, argument: str = "") -> None:
        """Apply dynamic-range compression to audio.

        -# -ratio    Change the compression ratio. Defaults to 4.
        """
        media, options = _parse_effect_flags(
            argument,
            values={"ratio": (("r",), float, 4.0)},
        )
        await self._apply_video_effect(ctx, "audiocompress", source=media, **options)

    @audio_group.command(
        name="channels-combine",
        aliases=("mono",),
        extras={"usage": "<input>"},
    )
    async def audio_group_channels_combine(
        self, ctx: Context, *, media: str = ""
    ) -> None:
        """Combine every audio channel into one mono channel."""
        await self._apply_video_effect(ctx, "channelscombine", source=media)

    @cast(Any, commands.hybrid_group)(
        name="image-effect",
        aliases=("imageeffect",),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def image_effect(self, ctx: Context) -> None:
        """Apply an image, GIF, or video effect."""
        await ctx.send_help(ctx.command)

    @image_effect.command(name="globe")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 3",
        clockwise="Rotate clockwise",
    )
    async def image_effect_globe(
        self,
        ctx: Context,
        media_url: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap an image around a rotating globe."""
        source = media_url or (attachment.url if attachment else "")
        argument = f"{source} -speed {speed:g}"
        if clockwise:
            argument += " -clockwise"
        await self._globe_effect(ctx, argument=argument.strip())

    @image_effect.command(name="spin3d")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        tilt="Vertical tilt from -360 to 360",
        zoom="Zoom from -3 to 3",
        speed="Rotation speed from 0.25 to 3",
        clockwise="Rotate clockwise",
    )
    async def image_effect_spin3d(
        self,
        ctx: Context,
        media_url: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
        tilt: float = 15.0,
        zoom: float = 1.5,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Spin an image in 3D space."""
        source = media_url or (attachment.url if attachment else "")
        argument = f"{source} -tilt {tilt:g} -zoom {zoom:g} -speed {speed:g}"
        if clockwise:
            argument += " -clockwise"
        await self._spin3d_effect(ctx, argument=argument.strip())

    @image_effect.command(name="caption")
    @app_commands.describe(
        text="The caption text",
        media_url="An image/video URL or custom emoji",
        user="Use this user's avatar",
        attachment="Attach an image or video",
    )
    async def image_effect_caption(
        self,
        ctx: Context,
        text: str = "",
        media_url: Optional[str] = None,
        user: Optional[discord.User] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add a caption to an image."""
        await self._caption_effect(
            ctx,
            text=text,
            media_url=media_url,
            user=user,
            attachment=attachment,
        )

    @image_effect.command(name="speed")
    @app_commands.describe(
        speed="Playback speed from 0.1 to 10",
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
    )
    async def image_effect_speed(
        self,
        ctx: Context,
        speed: float = 2.0,
        media_url: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Change playback speed of a video or GIF."""
        source = media_url or (attachment.url if attachment else "")
        input_value = f"{source} {speed:g}".strip() if source else f"{speed:g}"
        await self._speed_effect(ctx, input=input_value)

    @image_effect.command(name="invert")
    async def image_effect_invert(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Invert the colors of an image, GIF, or video."""
        await self._apply_image_effect(
            ctx, "invert", source=media_url or "", attachment=attachment
        )

    @image_effect.command(name="spin")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_spin(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Rotate media in a flat animated loop."""
        await self._apply_image_effect(
            ctx,
            "spin",
            source=media_url or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="magik")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        strength="Distortion strength from 1 to 80",
    )
    async def image_effect_magik(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 20.0,
    ) -> None:
        """Distort an image or GIF with a liquid effect."""
        await self._apply_image_effect(
            ctx,
            "magik",
            source=media_url or "",
            attachment=attachment,
            strength=strength,
        )

    @image_effect.command(name="gifmagik")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        strength="Distortion strength from 1 to 80",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_gifmagik(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 20.0,
        speed: float = 1.0,
    ) -> None:
        """Animate a liquid distortion over an image or GIF."""
        await self._apply_image_effect(
            ctx,
            "gifmagik",
            source=media_url or "",
            attachment=attachment,
            strength=strength,
            speed=speed,
        )

    @image_effect.command(name="flip")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        direction="The direction to flip",
    )
    async def image_effect_flip(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        direction: Literal["horizontal", "vertical"] = "horizontal",
    ) -> None:
        """Flip an image, GIF, or video horizontally or vertically."""
        await self._apply_image_effect(
            ctx,
            "flip",
            source=media_url or "",
            attachment=attachment,
            direction=direction,
        )

    @image_effect.command(name="cube")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_cube(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap an image or GIF around a rotating cube."""
        await self._apply_image_effect(
            ctx,
            "cube",
            source=media_url or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="pyramid")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        speed="Rotation speed from 0.25 to 4",
        clockwise="Rotate clockwise",
    )
    async def image_effect_pyramid(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        speed: float = 1.0,
        clockwise: bool = False,
    ) -> None:
        """Wrap an image or GIF around a rotating pyramid."""
        await self._apply_image_effect(
            ctx,
            "pyramid",
            source=media_url or "",
            attachment=attachment,
            speed=speed,
            clockwise=clockwise,
        )

    @image_effect.command(name="blur")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        radius="Blur radius from 0.1 to 50",
    )
    async def image_effect_blur(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        radius: float = 5.0,
    ) -> None:
        """Blur an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "blur",
            source=media_url or "",
            attachment=attachment,
            radius=radius,
        )

    @image_effect.command(name="deepfry")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        intensity="Deepfry intensity from 0.25 to 3",
    )
    async def image_effect_deepfry(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        intensity: float = 1.0,
    ) -> None:
        """Deep-fry an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "deepfry",
            source=media_url or "",
            attachment=attachment,
            intensity=intensity,
        )

    @image_effect.command(name="grayscale")
    async def image_effect_grayscale(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Convert an image, GIF, or video to grayscale."""
        await self._apply_image_effect(
            ctx, "grayscale", source=media_url or "", attachment=attachment
        )

    @image_effect.command(name="jpeg")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        quality="JPEG quality from 1 to 50, lower is worse",
    )
    async def image_effect_jpeg(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        quality: int = 8,
    ) -> None:
        """Make media look heavily JPEG-compressed."""
        await self._apply_image_effect(
            ctx,
            "jpeg",
            source=media_url or "",
            attachment=attachment,
            quality=quality,
        )

    @image_effect.command(name="swirl")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        strength="Swirl strength from -720 to 720 degrees",
    )
    async def image_effect_swirl(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 180.0,
    ) -> None:
        """Swirl an image or GIF."""
        await self._apply_image_effect(
            ctx,
            "swirl",
            source=media_url or "",
            attachment=attachment,
            strength=strength,
        )

    @image_effect.command(name="gifswirl")
    @app_commands.describe(
        media_url="An image or GIF URL",
        attachment="Attach an image or GIF",
        strength="Swirl strength from -720 to 720 degrees",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_gifswirl(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        strength: float = 180.0,
        speed: float = 1.0,
    ) -> None:
        """Animate a swirling distortion over an image or GIF."""
        await self._apply_image_effect(
            ctx,
            "gifswirl",
            source=media_url or "",
            attachment=attachment,
            strength=strength,
            speed=speed,
        )

    @image_effect.command(name="wiggle")
    @app_commands.describe(
        media_url="An image, GIF, or video URL",
        attachment="Attach an image, GIF, or video",
        amount="Wiggle amount from 1 to 30",
        speed="Animation speed from 0.25 to 4",
    )
    async def image_effect_wiggle(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: float = 8.0,
        speed: float = 1.0,
    ) -> None:
        """Make an image, GIF, or video wiggle."""
        await self._apply_image_effect(
            ctx,
            "wiggle",
            source=media_url or "",
            attachment=attachment,
            amount=amount,
            speed=speed,
        )

    @image_effect.group(name="crop", invoke_without_command=True)
    async def image_effect_crop(self, ctx: Context) -> None:
        """Crop media into a shape."""
        await ctx.send_help(ctx.command)

    @image_effect_crop.command(name="circle")
    async def image_effect_crop_circle(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Crop an image or GIF into a circle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media_url or "",
            attachment=attachment,
            shape="circle",
        )

    @image_effect_crop.command(name="triangle")
    async def image_effect_crop_triangle(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Crop an image or GIF into a triangle."""
        await self._apply_image_effect(
            ctx,
            "crop",
            source=media_url or "",
            attachment=attachment,
            shape="triangle",
        )

    @image_effect.group(name="mirror", invoke_without_command=True)
    async def image_effect_mirror(self, ctx: Context) -> None:
        """Mirror one side of media onto the other."""
        await ctx.send_help(ctx.command)

    @image_effect_mirror.command(name="bottom")
    async def image_effect_mirror_bottom(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the bottom half of an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media_url or "",
            attachment=attachment,
            direction="bottom",
        )

    @image_effect_mirror.command(name="right")
    async def image_effect_mirror_right(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the right half of an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media_url or "",
            attachment=attachment,
            direction="right",
        )

    @image_effect_mirror.command(name="left")
    async def image_effect_mirror_left(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the left half of an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media_url or "",
            attachment=attachment,
            direction="left",
        )

    @image_effect_mirror.command(name="top")
    async def image_effect_mirror_top(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Mirror the top half of an image, GIF, or video."""
        await self._apply_image_effect(
            ctx,
            "mirror",
            source=media_url or "",
            attachment=attachment,
            direction="top",
        )

    @image_effect.group(name="overlay", invoke_without_command=True)
    async def image_effect_overlay(self, ctx: Context) -> None:
        """Overlay a flag or second image on media."""
        await ctx.send_help(ctx.command)

    @image_effect_overlay.command(name="flag")
    @app_commands.describe(
        flag="Pride flag, pirate, or two-letter country code",
        opacity="Overlay opacity from 0 to 1",
    )
    async def image_effect_overlay_flag(
        self,
        ctx: Context,
        flag: str,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        opacity: float = 0.35,
    ) -> None:
        """Overlay a pride, country, or pirate flag on media."""
        flag_data = await self._flag_data(ctx, flag)
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media_url or "",
            attachment=attachment,
            overlay_data=flag_data,
            opacity=opacity,
            scale=1.0,
            stretch=True,
        )

    @image_effect_overlay.command(name="image")
    @app_commands.describe(
        media_url="The background image or GIF URL",
        overlay_url="The image to place on top",
        attachment="Attach the background image or GIF",
        overlay_attachment="Attach the image to place on top",
        opacity="Overlay opacity from 0 to 1",
        scale="Overlay size relative to the background",
    )
    async def image_effect_overlay_image(
        self,
        ctx: Context,
        media_url: str | None = None,
        overlay_url: str | None = None,
        attachment: discord.Attachment | None = None,
        overlay_attachment: discord.Attachment | None = None,
        opacity: float = 0.7,
        scale: float = 1.0,
    ) -> None:
        """Overlay a second image on top of the first."""
        await self._apply_image_effect(
            ctx,
            "overlay",
            source=media_url or "",
            attachment=attachment,
            second_source=overlay_url or "",
            second_attachment=overlay_attachment,
            opacity=opacity,
            scale=scale,
        )

    @cast(Any, commands.hybrid_group)(
        name="video-effects",
        aliases=("videoeffects",),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_effects(self, ctx: Context) -> None:
        """Apply an effect to a video or audio file."""
        await ctx.send_help(ctx.command)

    @video_effects.command(name="reverse")
    async def video_effects_reverse(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Reverse a video, GIF, or audio file."""
        await self._apply_video_effect(
            ctx,
            "reverse",
            source=media_url or "",
            attachment=attachment,
        )

    @video_effects.command(name="convert")
    @app_commands.describe(
        output_format="The format to convert every file into",
        media_url="A media URL when converting one file",
        attachment_1="First media file",
        attachment_2="Second media file",
        attachment_3="Third media file",
        attachment_4="Fourth media file",
        attachment_5="Fifth media file",
    )
    async def video_effects_convert(
        self,
        ctx: Context,
        output_format: Literal["mp4", "webm", "gif", "mp3", "wav", "ogg"],
        media_url: str | None = None,
        attachment_1: discord.Attachment | None = None,
        attachment_2: discord.Attachment | None = None,
        attachment_3: discord.Attachment | None = None,
        attachment_4: discord.Attachment | None = None,
        attachment_5: discord.Attachment | None = None,
    ) -> None:
        """Convert up to five uploaded media files to another format."""
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
            source=media_url or "",
            attachments=attachments,
        )

    @video_effects.command(name="volume")
    @app_commands.describe(
        volume="Volume multiplier from 0 to 10",
        media_url="A video or audio URL",
        attachment="Attach a video or audio file",
    )
    async def video_effects_volume(
        self,
        ctx: Context,
        volume: float,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Change the volume of a video or audio file."""
        await self._apply_video_effect(
            ctx,
            "volume",
            source=media_url or "",
            attachment=attachment,
            volume=volume,
        )

    @video_effects.group(name="overlay", invoke_without_command=True)
    async def video_effects_overlay(self, ctx: Context) -> None:
        """Overlay media on a video."""
        await ctx.send_help(ctx.command)

    @video_effects_overlay.command(name="video")
    @app_commands.describe(
        media_url="The background video URL",
        overlay_url="The video or image to place on top",
        attachment="Attach the background video",
        overlay_attachment="Attach the video or image to place on top",
        opacity="Overlay opacity from 0 to 1",
        scale="Overlay size relative to the background",
    )
    async def video_effects_overlay_video(
        self,
        ctx: Context,
        media_url: str | None = None,
        overlay_url: str | None = None,
        attachment: discord.Attachment | None = None,
        overlay_attachment: discord.Attachment | None = None,
        opacity: float = 0.7,
        scale: float = 0.5,
    ) -> None:
        """Overlay a second video or image on top of a video."""
        await self._apply_video_effect(
            ctx,
            "overlay",
            source=media_url or "",
            attachment=attachment,
            second_source=overlay_url or "",
            second_attachment=overlay_attachment,
            opacity=opacity,
            scale=scale,
        )

    @video_effects.group(name="bass", invoke_without_command=True)
    async def video_effects_bass(self, ctx: Context) -> None:
        """Change the bass in a video or audio file."""
        await ctx.send_help(ctx.command)

    @video_effects_bass.command(name="boost")
    async def video_effects_bass_boost(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        gain: float = 12.0,
    ) -> None:
        """Boost the bass in a video or audio file."""
        await self._apply_video_effect(
            ctx,
            "bassboost",
            source=media_url or "",
            attachment=attachment,
            gain=gain,
        )

    @video_effects_bass.command(name="lower")
    async def video_effects_bass_lower(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        gain: float = 12.0,
    ) -> None:
        """Lower the bass in a video or audio file."""
        await self._apply_video_effect(
            ctx,
            "basslower",
            source=media_url or "",
            attachment=attachment,
            gain=gain,
        )

    @video_effects.group(name="audio", invoke_without_command=True)
    async def video_effects_audio(self, ctx: Context) -> None:
        """Apply an audio effect."""
        await ctx.send_help(ctx.command)

    @video_effects_audio.command(name="reverse")
    async def video_effects_audio_reverse(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Reverse the audio while keeping video playback forward."""
        await self._apply_video_effect(
            ctx,
            "audioreverse",
            source=media_url or "",
            attachment=attachment,
        )

    @video_effects_audio.command(name="reverb")
    async def video_effects_audio_reverb(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        room: float = 0.5,
    ) -> None:
        """Add reverb to a video or audio file."""
        await self._apply_video_effect(
            ctx,
            "audioreverb",
            source=media_url or "",
            attachment=attachment,
            room=room,
        )

    @video_effects_audio.command(name="extract")
    async def video_effects_audio_extract(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Extract up to five audio tracks from a video."""
        await self._apply_video_effect(
            ctx,
            "extract",
            source=media_url or "",
            attachment=attachment,
        )

    @video_effects_audio.command(name="replace")
    @app_commands.describe(
        media_url="The video URL",
        audio_url="The replacement audio URL",
        attachment="Attach the video",
        audio_attachment="Attach the replacement audio",
    )
    async def video_effects_audio_replace(
        self,
        ctx: Context,
        media_url: str | None = None,
        audio_url: str | None = None,
        attachment: discord.Attachment | None = None,
        audio_attachment: discord.Attachment | None = None,
    ) -> None:
        """Replace a video's audio with another audio file."""
        await self._apply_video_effect(
            ctx,
            "audioreplace",
            source=media_url or "",
            attachment=attachment,
            second_source=audio_url or "",
            second_attachment=audio_attachment,
        )

    @video_effects_audio.command(name="destroy")
    async def video_effects_audio_destroy(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        amount: int = 6,
    ) -> None:
        """Deliberately destroy the quality of audio."""
        await self._apply_video_effect(
            ctx,
            "audiodestroy",
            source=media_url or "",
            attachment=attachment,
            amount=amount,
        )

    @video_effects_audio.command(name="compress")
    async def video_effects_audio_compress(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        ratio: float = 4.0,
    ) -> None:
        """Apply dynamic-range compression to audio."""
        await self._apply_video_effect(
            ctx,
            "audiocompress",
            source=media_url or "",
            attachment=attachment,
            ratio=ratio,
        )

    @video_effects_audio.command(name="channels-combine")
    async def video_effects_audio_channels_combine(
        self,
        ctx: Context,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Combine every audio channel into one mono channel."""
        await self._apply_video_effect(
            ctx,
            "channelscombine",
            source=media_url or "",
            attachment=attachment,
        )

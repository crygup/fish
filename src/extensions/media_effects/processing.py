from __future__ import annotations

import io
import json
import math
import os
import random
import re
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal, Sequence, cast

import numpy as np
from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

from utils.rich_text import (
    draw_inline_tokens,
    impact_font_path,
    inline_animation_duration,
    measure_inline_tokens,
    meme_font,
    text_font,
    wrap_inline_text,
)

from .fonts import load_effect_font
from .runtime import cancellable_to_thread as to_thread
from .subprocesses import probe_media_json, run_media_command

MAX_FRAME_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 150_000_000
MAX_ANIMATION_FRAMES = 300
MAX_MEDIA_DURATION = 10 * 60.0
MAX_MEDIA_DURATION_MINUTES = int(MAX_MEDIA_DURATION // 60)
MAX_MEDIA_DIMENSION = 4096
FFMPEG_TIMEOUT = 60
INVALID_H264_COLOR_VALUES = frozenset({"reserved", "unknown", "unspecified"})
MAGIK_WORKING_SIZE = 320
MAGIK_GIF_WORKING_SIZE = 320
MAGIK_MAX_GIF_FRAMES = 40
SWIRL_WORKING_SIZE = 512
HEAVY_EFFECT_MAX_GIF_FRAMES = 40
VIDEO_DISTORTION_SIZE = 480
VIDEO_DISTORTION_FPS = 12
VIDEO_SWIRL_SIZE = 360
VIDEO_SWIRL_FPS = 10
REFERENCE_EFFECT_SIZE = 300
REFERENCE_EFFECT_FRAMES = 39
REFERENCE_EFFECT_DURATION = 50
TREMBLE_POSITIONS = (
    (10, 5),
    (6, 12),
    (7, 10),
    (9, 10),
    (10, 5),
    (8, 6),
    (9, 5),
    (10, 10),
    (6, 9),
    (9, 7),
    (5, 8),
    (6, 8),
    (12, 11),
    (8, 11),
    (11, 6),
    (8, 8),
    (7, 6),
    (8, 7),
    (8, 5),
    (6, 11),
    (6, 6),
    (5, 11),
    (11, 9),
    (12, 5),
    (6, 11),
    (10, 7),
    (11, 9),
    (10, 9),
    (10, 10),
    (6, 9),
    (7, 9),
    (9, 7),
    (9, 12),
    (5, 10),
    (11, 12),
    (10, 6),
    (9, 7),
    (7, 8),
    (10, 10),
)

Image.MAX_IMAGE_PIXELS = MAX_FRAME_PIXELS


@dataclass(slots=True)
class EffectResult:
    data: bytes
    filename: str
    displayable: bool = True


@dataclass(slots=True, frozen=True)
class AverageColor:
    rgb: tuple[int, int, int]
    percentage: float


class NotPillowMedia(ValueError):
    """The input is not a still image or Pillow-readable animation."""


@dataclass(slots=True)
class MediaProbe:
    duration: float
    width: int
    height: int
    video_streams: int
    audio_streams: int
    format_names: frozenset[str]
    video_codecs: tuple[str, ...] = ()
    video_pixel_formats: tuple[str, ...] = ()

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0

    @property
    def can_copy_video_to_discord_mp4(self) -> bool:
        """Whether the primary video can be remuxed without losing compatibility."""

        return bool(
            self.video_codecs
            and self.video_codecs[0] == "h264"
            and self.video_pixel_formats
            and self.video_pixel_formats[0] in {"yuv420p", "yuvj420p"}
        )


def _run(command: list[str], *, timeout: int = FFMPEG_TIMEOUT) -> None:
    run_media_command(
        command,
        timeout=timeout,
        timeout_message=(
            f"That effect took longer than {timeout} seconds. "
            "Try a shorter or smaller file."
        ),
        failure_prefix="Could not process that media",
    )


def _probe_path(path: str) -> MediaProbe:
    try:
        payload = probe_media_json(
            path,
            show_entries=(
                "stream=codec_type,codec_name,pix_fmt,width,height:"
                "format=duration,format_name"
            ),
        )
    except ValueError as error:
        raise ValueError("That file is not supported media.") from error

    streams = payload.get("streams") or []
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    width = max((int(item.get("width") or 0) for item in video_streams), default=0)
    height = max((int(item.get("height") or 0) for item in video_streams), default=0)
    format_info = payload.get("format") or {}
    try:
        duration = float(format_info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    format_names = frozenset(
        item.strip()
        for item in str(format_info.get("format_name") or "").split(",")
        if item.strip()
    )
    probe = MediaProbe(
        duration=duration,
        width=width,
        height=height,
        video_streams=len(video_streams),
        audio_streams=len(audio_streams),
        format_names=format_names,
        video_codecs=tuple(
            str(item.get("codec_name") or "").casefold() for item in video_streams
        ),
        video_pixel_formats=tuple(
            str(item.get("pix_fmt") or "").casefold() for item in video_streams
        ),
    )
    if not probe.has_video and not probe.has_audio:
        raise ValueError("That file does not contain video or audio.")
    if duration > MAX_MEDIA_DURATION:
        raise ValueError(
            f"Media effects are limited to files {MAX_MEDIA_DURATION_MINUTES} "
            "minutes or shorter."
        )
    if width > MAX_MEDIA_DIMENSION or height > MAX_MEDIA_DIMENSION:
        raise ValueError("Media effects are limited to 4096 pixels per side.")
    return probe


def _has_invalid_h264_color_metadata(path: str) -> bool:
    """Detect H.264 streams whose reserved color metadata breaks FFmpeg filters."""

    try:
        payload = probe_media_json(
            path,
            show_entries=(
                "stream=codec_name,color_space,color_transfer,color_primaries"
            ),
        )
    except ValueError:
        return False

    for stream in payload.get("streams") or ():
        if str(stream.get("codec_name") or "").casefold() != "h264":
            continue
        metadata = (
            stream.get("color_space"),
            stream.get("color_transfer"),
            stream.get("color_primaries"),
        )
        if any(
            str(value or "").casefold() in INVALID_H264_COLOR_VALUES
            for value in metadata
        ):
            return True
    return False


def _repair_h264_color_metadata(
    path: str,
    directory: str,
    *,
    name: str,
) -> str:
    """Patch reserved H.264 color metadata before compositing the stream.

    Some mobile/CDN encoders write ``reserved`` color values. FFmpeg then
    rejects the stream while initializing a filter graph, even though the
    video itself is otherwise valid. Rewriting the bitstream metadata keeps
    the original video frames and makes it usable by the overlay filter.
    """

    if not _has_invalid_h264_color_metadata(path):
        return path

    repaired_path = os.path.join(directory, f"{name}-color-fixed.mp4")
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            repaired_path,
        ],
    )
    return repaired_path


def probe_media_sync(data: bytes) -> MediaProbe:
    with tempfile.TemporaryDirectory(prefix="fishie-media-probe-") as directory:
        path = os.path.join(directory, "input.media")
        Path(path).write_bytes(data)
        return _probe_path(path)


probe_media = to_thread(probe_media_sync)


def _recover_edge_transparency(frame: Image.Image) -> Image.Image:
    """Recover a GIF background that was stored opaque despite transparency metadata."""
    if frame.getchannel("A").getextrema() != (255, 255):
        return frame

    recovered = frame.copy()
    corners = (
        (0, 0),
        (recovered.width - 1, 0),
        (0, recovered.height - 1),
        (recovered.width - 1, recovered.height - 1),
    )
    for corner in corners:
        pixel = cast(tuple[int, int, int, int], recovered.getpixel(corner))
        if pixel[3] == 0:
            continue
        ImageDraw.floodfill(
            recovered,
            corner,
            (0, 0, 0, 0),
            thresh=12,
        )
    return recovered


def _load_image_frames(
    data: bytes,
    *,
    preserve_transparency: bool = False,
) -> tuple[list[Image.Image], list[int], bool]:
    try:
        opened = Image.open(BytesIO(data))
    except UnidentifiedImageError as error:
        raise NotPillowMedia("That file is not an image or GIF.") from error

    if opened.width * opened.height > MAX_FRAME_PIXELS:
        raise ValueError("Images are limited to 25 million pixels per frame.")

    frame_count = int(getattr(opened, "n_frames", 1))
    if frame_count > MAX_ANIMATION_FRAMES:
        raise ValueError("Animated images are limited to 300 frames.")
    if opened.width * opened.height * frame_count > MAX_TOTAL_PIXELS:
        raise ValueError("That animated image has too many decoded pixels.")

    recover_declared_transparency = (
        preserve_transparency
        and opened.format == "GIF"
        and "transparency" in opened.info
    )
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(frame_count):
        opened.seek(index)
        frame = opened.convert("RGBA")
        if recover_declared_transparency:
            frame = _recover_edge_transparency(frame)
        frames.append(frame)
        durations.append(max(20, int(opened.info.get("duration") or 100)))
    return frames, durations, frame_count > 1


def _save_frames(
    frames: list[Image.Image],
    durations: list[int],
    *,
    filename: str,
    jpeg_quality: int | None = None,
    per_frame_palette: bool = False,
) -> EffectResult:
    if not frames:
        raise ValueError("The effect did not produce any frames.")
    output = BytesIO()
    if len(frames) > 1:
        # Pillow can produce malformed frame rectangles when a GIF ends on a
        # completely transparent frame and may merge intentional duplicate
        # frames. Let ffmpeg build one shared palette and encode full frames.
        # This keeps the requested timing and avoids stale palette artifacts.
        with tempfile.TemporaryDirectory(prefix="fishie-gif-") as temp:
            temp_path = Path(temp)
            manifest = ["ffconcat version 1.0"]
            for index, (frame, frame_duration) in enumerate(zip(frames, durations)):
                frame_name = f"{index:04d}.png"
                frame.convert("RGBA").save(temp_path / frame_name, "PNG")
                manifest.extend(
                    (
                        f"file '{frame_name}'",
                        "option framerate 100",
                        f"duration {frame_duration / 1_000:.6f}",
                    )
                )
            # The concat demuxer applies the final duration only when the last
            # file is repeated. Limit output to the real frame count so the
            # repeated frame is not encoded.
            manifest.extend(
                (
                    f"file '{len(frames) - 1:04d}.png'",
                    "option framerate 100",
                )
            )
            manifest_path = temp_path / "frames.ffconcat"
            manifest_path.write_text("\n".join(manifest) + "\n")
            gif_path = temp_path / "output.gif"
            palette_options = (
                "reserve_transparent=1:"
                "transparency_color=ffffff:"
                "stats_mode=single"
                if per_frame_palette
                else "reserve_transparent=1:transparency_color=ffffff"
            )
            paletteuse_options = (
                "alpha_threshold=128:dither=sierra2_4a:new=1"
                if per_frame_palette
                else "alpha_threshold=128:dither=none"
            )
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(manifest_path),
                    "-filter_complex",
                    (
                        "[0:v]split[palette_input][video_input];"
                        f"[palette_input]palettegen={palette_options}[palette];"
                        f"[video_input][palette]paletteuse={paletteuse_options}"
                    ),
                    "-frames:v",
                    str(len(frames)),
                    "-gifflags",
                    "-offsetting-transdiff",
                    "-fps_mode",
                    "vfr",
                    "-final_delay",
                    str(max(2, round(durations[-1] / 10))),
                    "-loop",
                    "0",
                    str(gif_path),
                ]
            )
            output.write(gif_path.read_bytes())
        return EffectResult(output.getvalue(), f"{filename}.gif")

    frame = frames[0]
    if jpeg_quality is not None:
        background = Image.new("RGB", frame.size, "white")
        if frame.mode == "RGBA":
            background.paste(frame, mask=frame.getchannel("A"))
        else:
            background.paste(frame.convert("RGB"))
        background.save(
            output,
            "JPEG",
            quality=max(1, min(95, jpeg_quality)),
            optimize=True,
        )
        return EffectResult(output.getvalue(), f"{filename}.jpg")

    frame.save(output, "PNG", optimize=True)
    return EffectResult(output.getvalue(), f"{filename}.png")


def _preserve_alpha(source: Image.Image, rgb: Image.Image) -> Image.Image:
    result = rgb.convert("RGBA")
    result.putalpha(source.getchannel("A"))
    return result


def _invert(frame: Image.Image, _: dict[str, Any]) -> Image.Image:
    return _preserve_alpha(frame, ImageOps.invert(frame.convert("RGB")))


def _flip(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    direction = options.get("direction", "horizontal")
    transpose = (
        Image.Transpose.FLIP_TOP_BOTTOM
        if direction == "vertical"
        else Image.Transpose.FLIP_LEFT_RIGHT
    )
    return frame.transpose(transpose)


def _blur(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    radius = float(options.get("radius", 5))
    if not 0.1 <= radius <= 50:
        raise ValueError("Blur radius must be between 0.1 and 50.")
    blur_type = str(options.get("blur_type", "gaussian")).casefold()
    if blur_type in {"gaussian", "gauss"}:
        blurred = frame.filter(ImageFilter.GaussianBlur(radius))
    elif blur_type in {"box", "square"}:
        blurred = frame.filter(ImageFilter.BoxBlur(radius))
    elif blur_type in {"motion", "horizontal"}:
        # Pillow kernels support 3x3 and 5x5 matrices. A 5-wide horizontal
        # average gives the expected directional blur while keeping all modes.
        kernel_size = 3 if radius < 1.5 else 5
        weights = [0.0] * (kernel_size * kernel_size)
        center = kernel_size // 2
        for x in range(kernel_size):
            weights[center * kernel_size + x] = 1 / kernel_size
        blurred = frame.filter(
            ImageFilter.Kernel(
                (kernel_size, kernel_size),
                weights,
                scale=1,
            )
        )
    else:
        raise ValueError("Blur type must be gaussian, box, or motion.")

    position = str(options.get("position", "")).strip()
    if not position:
        return blurred
    shape = str(options.get("shape", "square")).casefold().strip()
    if shape not in {"square", "rectangle", "circle", "triangle"}:
        raise ValueError("Blur shape must be square, circle, or triangle.")
    center_x, center_y = _blur_position(position, frame.width, frame.height)
    region_size = max(16, min(frame.width, frame.height) // 3)
    left = max(0, min(frame.width - region_size, center_x - region_size // 2))
    top = max(0, min(frame.height - region_size, center_y - region_size // 2))
    mask = Image.new("L", frame.size, 0)
    draw = ImageDraw.Draw(mask)
    box = (left, top, left + region_size - 1, top + region_size - 1)
    if shape in {"square", "rectangle"}:
        draw.rectangle(box, fill=255)
    elif shape == "circle":
        draw.ellipse(box, fill=255)
    else:
        draw.polygon(
            (
                (left + region_size // 2, top),
                (left + region_size - 1, top + region_size - 1),
                (left, top + region_size - 1),
            ),
            fill=255,
        )
    result = frame.copy()
    result.paste(blurred, (0, 0), mask)
    return result


def _blur_position(value: str, width: int, height: int) -> tuple[int, int]:
    normalized = value.casefold().replace(" ", "")
    named = {
        "center": (width // 2, height // 2),
        "top-left": (width // 6, height // 6),
        "top": (width // 2, height // 6),
        "top-right": (width * 5 // 6, height // 6),
        "left": (width // 6, height // 2),
        "right": (width * 5 // 6, height // 2),
        "bottom-left": (width // 6, height * 5 // 6),
        "bottom": (width // 2, height * 5 // 6),
        "bottom-right": (width * 5 // 6, height * 5 // 6),
    }
    if normalized in named:
        return named[normalized]
    parts = value.split(",", 1)
    if len(parts) != 2:
        raise ValueError("Blur position must be x,y coordinates or a named position.")
    try:
        x = float(parts[0].strip())
        y = float(parts[1].strip())
    except ValueError as error:
        raise ValueError("Blur position must be x,y coordinates.") from error
    if 0 <= x <= 1 and 0 <= y <= 1:
        x *= width
        y *= height
    return (
        max(0, min(width - 1, round(x))),
        max(0, min(height - 1, round(y))),
    )


def _crop_shape(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    shape = options["shape"]
    size = min(frame.size)
    left = (frame.width - size) // 2
    top = (frame.height - size) // 2
    cropped = frame.crop((left, top, left + size, top + size))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    if shape == "circle":
        draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    else:
        draw.polygon(
            ((size // 2, 0), (size - 1, size - 1), (0, size - 1)),
            fill=255,
        )
    cropped.putalpha(ImageChops_multiply(cropped.getchannel("A"), mask))
    return cropped


def ImageChops_multiply(first: Image.Image, second: Image.Image) -> Image.Image:
    # Local import keeps the main Pillow import list readable.
    from PIL import ImageChops

    return ImageChops.multiply(first, second)


def _deepfry(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    intensity = float(options.get("intensity", 1.0))
    if not 0.25 <= intensity <= 3:
        raise ValueError("Deepfry intensity must be between 0.25 and 3.")
    alpha = frame.getchannel("A")
    image = frame.convert("RGB")
    image = ImageEnhance.Color(image).enhance(1 + 2.2 * intensity)
    image = ImageEnhance.Contrast(image).enhance(1 + 1.1 * intensity)
    image = ImageEnhance.Sharpness(image).enhance(1 + 4 * intensity)
    array = np.asarray(image, dtype=np.int16)
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 5 * intensity, array.shape[:2])[:, :, None]
    array = np.clip(array + noise, 0, 255).astype(np.uint8)
    result = Image.fromarray(array, "RGB").convert("RGBA")
    result.putalpha(alpha)
    return result


def _grayscale(frame: Image.Image, _: dict[str, Any]) -> Image.Image:
    return _preserve_alpha(frame, ImageOps.grayscale(frame).convert("RGB"))


def _effect_color(value: object) -> tuple[int, int, int] | tuple[int, int, int, int]:
    try:
        return ImageColor.getrgb(str(value or "#5865f2"))
    except ValueError as error:
        raise ValueError("Color must be a CSS color name or hex value.") from error


def _tint(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 0.35))
    if not 0 <= amount <= 1:
        raise ValueError("Tint amount must be between 0 and 1.")
    overlay = Image.new("RGB", frame.size, _effect_color(options.get("color")))
    tinted = Image.blend(frame.convert("RGB"), overlay, amount)
    return _preserve_alpha(frame, tinted)


def _radial_warp(
    frame: Image.Image,
    strength: float,
    *,
    mode: Literal["implode", "explode", "fisheye"],
) -> Image.Image:
    if not 0 <= strength <= 1:
        raise ValueError("Strength must be between 0 and 1.")
    source = np.asarray(frame.convert("RGBA"))
    height, width = source.shape[:2]
    y, x = np.indices((height, width), dtype=np.float32)
    center_x = (width - 1) / 2
    center_y = (height - 1) / 2
    dx = (x - center_x) / max(1.0, width / 2)
    dy = (y - center_y) / max(1.0, height / 2)
    radius = np.sqrt(dx * dx + dy * dy)
    safe_radius = np.maximum(radius, 1e-6)
    if mode == "implode":
        source_radius = np.power(safe_radius, max(0.1, 1 + 2.5 * strength))
    elif mode == "explode":
        source_radius = np.power(safe_radius, 1 / max(0.1, 1 + 2.5 * strength))
    else:
        source_radius = safe_radius * (
            1 - strength * np.clip(1 - safe_radius * safe_radius, 0, 1)
        )
    scale = source_radius / safe_radius
    source_x = center_x + dx * scale * max(1.0, width / 2)
    source_y = center_y + dy * scale * max(1.0, height / 2)
    inside = (
        (source_x >= 0) & (source_x < width) & (source_y >= 0) & (source_y < height)
    )
    source_x = np.clip(np.rint(source_x), 0, width - 1).astype(np.int32)
    source_y = np.clip(np.rint(source_y), 0, height - 1).astype(np.int32)
    result = source[source_y, source_x].copy()
    result[~inside] = (0, 0, 0, 0)
    return Image.fromarray(result, "RGBA")


def _implode(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    return _radial_warp(
        frame,
        float(options.get("strength", 0.5)),
        mode="implode",
    )


def _explode(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    return _radial_warp(
        frame,
        float(options.get("strength", 0.5)),
        mode="explode",
    )


def _fisheye(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    return _radial_warp(
        frame,
        float(options.get("strength", 0.65)),
        mode="fisheye",
    )


def _sharpen(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 2))
    if not 0 <= amount <= 5:
        raise ValueError("Sharpen amount must be between 0 and 5.")
    sharpened = ImageEnhance.Sharpness(frame.convert("RGB")).enhance(1 + amount)
    return _preserve_alpha(frame, sharpened)


def _legoify(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    size = int(options.get("size", 12))
    if not 3 <= size <= 64:
        raise ValueError("Lego block size must be between 3 and 64.")
    width = max(1, math.ceil(frame.width / size))
    height = max(1, math.ceil(frame.height / size))
    blocks = frame.resize((width, height), Image.Resampling.BOX).convert("RGBA")
    result = blocks.resize(frame.size, Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(result, "RGBA")
    radius = max(1, size // 4)
    for y in range(size // 2, frame.height, size):
        for x in range(size // 2, frame.width, size):
            color = cast(
                tuple[int, int, int, int],
                result.getpixel((min(x, frame.width - 1), min(y, frame.height - 1))),
            )
            if color[3] == 0:
                continue
            highlight = tuple(min(255, channel + 35) for channel in color[:3]) + (
                color[3],
            )
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=highlight,
            )
    return result


def _sepia(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 1))
    if not 0 <= amount <= 1:
        raise ValueError("Sepia amount must be between 0 and 1.")
    rgb = np.asarray(frame.convert("RGB"), dtype=np.float32)
    matrix = np.asarray(
        ((0.393, 0.769, 0.189), (0.349, 0.686, 0.168), (0.272, 0.534, 0.131)),
        dtype=np.float32,
    )
    sepia = np.clip(rgb @ matrix.T, 0, 255)
    mixed = np.clip(rgb * (1 - amount) + sepia * amount, 0, 255).astype(np.uint8)
    return _preserve_alpha(frame, Image.fromarray(mixed, "RGB"))


def _pixelate(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    size = int(options.get("size", 12))
    if not 2 <= size <= 128:
        raise ValueError("Pixel size must be between 2 and 128.")
    width = max(1, math.ceil(frame.width / size))
    height = max(1, math.ceil(frame.height / size))
    return frame.resize((width, height), Image.Resampling.BOX).resize(
        frame.size,
        Image.Resampling.NEAREST,
    )


def _vignette(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 0.65))
    if not 0 <= amount <= 1:
        raise ValueError("Vignette amount must be between 0 and 1.")
    array = np.asarray(frame.convert("RGBA"), dtype=np.float32)
    height, width = array.shape[:2]
    y, x = np.indices((height, width), dtype=np.float32)
    distance = np.sqrt(
        ((x - (width - 1) / 2) / max(1, width / 2)) ** 2
        + ((y - (height - 1) / 2) / max(1, height / 2)) ** 2
    )
    factor = np.clip(1 - amount * np.clip(distance, 0, 1) ** 2, 0, 1)
    array[:, :, :3] *= factor[:, :, None]
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGBA")


def _resize(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    scale = float(options.get("scale", 1))
    if not 0.1 <= scale <= 4:
        raise ValueError("Resize scale must be between 0.1 and 4.")
    ratio = str(options.get("ratio", "")).strip()
    target = frame
    exact_size = _resize_dimensions(str(options.get("size", "")))
    if exact_size is not None:
        target = frame.resize(exact_size, Image.Resampling.LANCZOS)
    elif ratio:
        separator = ":" if ":" in ratio else "/"
        try:
            ratio_width, ratio_height = (
                float(part) for part in ratio.split(separator, 1)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Ratio must look like 16:9 or 1:1.") from error
        if ratio_width <= 0 or ratio_height <= 0:
            raise ValueError("Ratio values must be positive.")
        current_area = max(1, frame.width * frame.height)
        target_width = max(
            1, round(math.sqrt(current_area * ratio_width / ratio_height))
        )
        target_height = max(1, round(target_width * ratio_height / ratio_width))
        target = ImageOps.fit(
            frame,
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
    return target.resize(
        (
            max(1, round(target.width * scale)),
            max(1, round(target.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def _resize_dimensions(value: str) -> tuple[int, int] | None:
    value = value.strip().lower().replace("×", "x")
    if not value:
        return None
    match = re.fullmatch(r"(?P<width>\d{1,4})x(?P<height>\d{1,4})", value)
    if match is None:
        raise ValueError("Resize dimensions must look like 640x480.")
    width = int(match.group("width"))
    height = int(match.group("height"))
    if not 1 <= width <= MAX_MEDIA_DIMENSION or not 1 <= height <= MAX_MEDIA_DIMENSION:
        raise ValueError(
            f"Resize dimensions must be between 1 and {MAX_MEDIA_DIMENSION} pixels."
        )
    if width * height > MAX_FRAME_PIXELS:
        raise ValueError("The requested resize dimensions are too large.")
    return width, height


def _distort(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 0.25))
    if not -1 <= amount <= 1:
        raise ValueError("Distort amount must be between -1 and 1.")
    shift = frame.width * amount
    return frame.transform(
        frame.size,
        Image.Transform.AFFINE,
        (1, amount, -shift / 2, amount * -0.2, 1, shift * 0.1),
        Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def _noise(
    frame: Image.Image, options: dict[str, Any], *, monochrome: bool
) -> Image.Image:
    amount = float(options.get("amount", 20))
    if not 0 <= amount <= 100:
        raise ValueError("Noise amount must be between 0 and 100.")
    array = np.asarray(frame.convert("RGBA"), dtype=np.int16)
    rng = np.random.default_rng(0)
    shape = (*array.shape[:2], 1 if monochrome else 3)
    generated = rng.normal(0, amount, shape)
    array[:, :, :3] = np.clip(array[:, :, :3] + generated, 0, 255)
    return Image.fromarray(array.astype(np.uint8), "RGBA")


def _grain(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    return _noise(frame, options, monochrome=True)


def _color_noise(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    return _noise(frame, options, monochrome=False)


def _average_colors_data(
    frame: Image.Image,
) -> tuple[Image.Image, list[AverageColor]]:
    """Build a compact palette image and its dominant-color measurements."""
    source = frame.convert("RGBA")
    # Quantizing a small copy is substantially faster for large uploads and
    # keeps the palette representative without changing the source media.
    sample = ImageOps.contain(source, (256, 256), Image.Resampling.BILINEAR)
    background = Image.new("RGB", sample.size, "white")
    background.paste(sample.convert("RGB"), mask=sample.getchannel("A"))
    quantized = background.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    palette = [int(value) for value in (quantized.getpalette() or [])]
    counts = cast(
        list[tuple[int, int]],
        quantized.getcolors(maxcolors=256) or [],
    )
    total_pixels = max(1, sum(count for count, _ in counts))
    colors: list[AverageColor] = []
    for count, palette_index in sorted(counts, reverse=True):
        offset = int(palette_index) * 3
        if offset + 2 >= len(palette):
            continue
        color = (
            palette[offset],
            palette[offset + 1],
            palette[offset + 2],
        )
        if any(item.rgb == color for item in colors):
            continue
        colors.append(
            AverageColor(
                rgb=color,
                percentage=(count / total_pixels) * 100,
            )
        )
    if not colors:
        colors = [AverageColor((255, 255, 255), 100.0)]
    colors = colors[:6]

    width = max(320, min(960, source.width))
    swatch_height = max(64, min(120, width // 6))
    output = Image.new("RGBA", (width, swatch_height * len(colors)), "white")
    draw = ImageDraw.Draw(output)
    font_size = max(18, min(36, swatch_height // 3))
    for index, item in enumerate(colors):
        color = item.rgb
        top = index * swatch_height
        draw.rectangle((0, top, width, top + swatch_height), fill=(*color, 255))
        luminance = (color[0] * 299 + color[1] * 587 + color[2] * 114) / 1000
        fill = "black" if luminance > 150 else "white"
        label = "#%02X%02X%02X" % color
        font = text_font(label, font_size)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (
                (width - (box[2] - box[0])) // 2,
                top + (swatch_height - (box[3] - box[1])) // 2,
            ),
            label,
            font=font,
            fill=fill,
        )
    return output, colors


def _average_colors_image(frame: Image.Image) -> Image.Image:
    """Render a compact, labeled palette made from the image's dominant colors."""
    output, _ = _average_colors_data(frame)
    return output


def render_average_colors_sync(
    data: bytes,
) -> tuple[EffectResult, list[AverageColor]]:
    """Extract dominant colors from one still image and render its palette."""
    try:
        with Image.open(BytesIO(data)) as opened:
            if str(opened.format).upper() == "GIF":
                raise ValueError(
                    "Average colors only supports still images, not GIFs or videos."
                )
    except UnidentifiedImageError as error:
        raise NotPillowMedia(
            "Average colors only supports still images, not GIFs or videos."
        ) from error

    frames, _, animated = _load_image_frames(data)
    if animated:
        raise ValueError(
            "Average colors only supports still images, not GIFs or videos."
        )
    palette, colors = _average_colors_data(frames[0])
    output = _save_frames([palette], [1000], filename="averagecolors")
    return output, colors


render_average_colors = to_thread(render_average_colors_sync)


def _rotate(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    degrees = float(options.get("degrees", 90))
    if not -3600 <= degrees <= 3600:
        raise ValueError("Rotation must be between -3600 and 3600 degrees.")
    return frame.rotate(
        -degrees,
        Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(0, 0, 0, 0),
    )


def _brightness(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 1))
    if not 0 <= amount <= 4:
        raise ValueError("Brightness must be between 0 and 4.")
    return _preserve_alpha(
        frame,
        ImageEnhance.Brightness(frame.convert("RGB")).enhance(amount),
    )


def _contrast(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 1))
    if not 0 <= amount <= 4:
        raise ValueError("Contrast must be between 0 and 4.")
    return _preserve_alpha(
        frame,
        ImageEnhance.Contrast(frame.convert("RGB")).enhance(amount),
    )


def _saturation(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    amount = float(options.get("amount", 1))
    if not 0 <= amount <= 4:
        raise ValueError("Saturation must be between 0 and 4.")
    return _preserve_alpha(
        frame,
        ImageEnhance.Color(frame.convert("RGB")).enhance(amount),
    )


def _exposure(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    stops = float(options.get("stops", 0))
    if not -5 <= stops <= 5:
        raise ValueError("Exposure must be between -5 and 5 stops.")
    return _preserve_alpha(
        frame,
        ImageEnhance.Brightness(frame.convert("RGB")).enhance(2**stops),
    )


def _mirror(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    direction = options["direction"]
    width, height = frame.size
    if direction in {"left", "right"}:
        half = max(1, width // 2)
        if direction == "left":
            source = frame.crop((0, 0, half, height))
            other = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            result = Image.new("RGBA", (half * 2, height))
            result.paste(source, (0, 0))
            result.paste(other, (half, 0))
        else:
            source = frame.crop((width - half, 0, width, height))
            other = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            result = Image.new("RGBA", (half * 2, height))
            result.paste(other, (0, 0))
            result.paste(source, (half, 0))
        return result.resize((width, height), Image.Resampling.LANCZOS)

    half = max(1, height // 2)
    if direction == "top":
        source = frame.crop((0, 0, width, half))
        other = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        result = Image.new("RGBA", (width, half * 2))
        result.paste(source, (0, 0))
        result.paste(other, (0, half))
    else:
        source = frame.crop((0, height - half, width, height))
        other = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        result = Image.new("RGBA", (width, half * 2))
        result.paste(other, (0, 0))
        result.paste(source, (0, half))
    return result.resize((width, height), Image.Resampling.LANCZOS)


def _jpeg(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    quality = int(options.get("quality", 8))
    if not 1 <= quality <= 50:
        raise ValueError("JPEG quality must be between 1 and 50.")
    flattened = Image.new("RGB", frame.size, "white")
    flattened.paste(frame, mask=frame.getchannel("A"))
    for _ in range(2):
        buffer = BytesIO()
        flattened.save(buffer, "JPEG", quality=quality)
        buffer.seek(0)
        with Image.open(buffer) as recompressed:
            flattened = recompressed.convert("RGB")
    return flattened.convert("RGBA")


def _magik_ratio(strength: float) -> float:
    if not 1 <= strength <= 80:
        raise ValueError("Magik strength must be between 1 and 80.")
    return max(0.28, 0.72 - strength * 0.011)


def _seam_energy(array: np.ndarray) -> np.ndarray:
    rgb = array[:, :, :3].astype(np.int16)
    horizontal = np.abs(np.roll(rgb, -1, axis=1) - np.roll(rgb, 1, axis=1))
    vertical = np.abs(np.roll(rgb, -1, axis=0) - np.roll(rgb, 1, axis=0))
    energy = horizontal.sum(axis=2) + vertical.sum(axis=2)
    energy[:, (0, -1)] = 1_000_000
    energy[(0, -1), :] = 1_000_000
    return energy.astype(np.float64)


def _find_vertical_seam(array: np.ndarray) -> np.ndarray:
    height, width = array.shape[:2]
    if width <= 2:
        return np.zeros(height, dtype=np.int32)
    energy = _seam_energy(array)
    cost = energy.copy()
    directions = np.zeros((height, width), dtype=np.int8)
    infinity = np.array([np.inf])
    for row in range(1, height):
        previous = cost[row - 1]
        candidates = np.vstack(
            (
                np.concatenate((infinity, previous[:-1])),
                previous,
                np.concatenate((previous[1:], infinity)),
            )
        )
        choice = np.argmin(candidates, axis=0)
        directions[row] = choice.astype(np.int8) - 1
        cost[row] += candidates[choice, np.arange(width)]

    seam = np.empty(height, dtype=np.int32)
    seam[-1] = int(np.argmin(cost[-1]))
    for row in range(height - 1, 0, -1):
        seam[row - 1] = seam[row] + int(directions[row, seam[row]])
    return seam


def _remove_vertical_path(array: np.ndarray, seam: np.ndarray) -> np.ndarray:
    height, width = array.shape[:2]
    keep = np.ones((height, width), dtype=bool)
    keep[np.arange(height), seam] = False
    return array[keep].reshape((height, width - 1, *array.shape[2:]))


def _remove_vertical_seam(array: np.ndarray) -> np.ndarray:
    if array.shape[1] <= 2:
        return array
    return _remove_vertical_path(array, _find_vertical_seam(array))


def _carve_to(array: np.ndarray, width: int, height: int) -> np.ndarray:
    while array.shape[1] > width:
        array = _remove_vertical_seam(array)
    if array.shape[0] > height:
        array = np.transpose(array, (1, 0, 2))
        while array.shape[1] > height:
            array = _remove_vertical_seam(array)
        array = np.transpose(array, (1, 0, 2))
    return array


def _carve_coordinate_map(
    reference: np.ndarray,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Build one content-aware coordinate map that can be reused across GIF frames."""
    current = reference
    coordinates = cast(
        np.ndarray[Any, np.dtype[np.int32]],
        np.indices(reference.shape[:2], dtype=np.int32),
    )
    source_y = coordinates[0]
    source_x = coordinates[1]
    while current.shape[1] > width:
        seam = _find_vertical_seam(current)
        current = _remove_vertical_path(current, seam)
        source_x = _remove_vertical_path(source_x, seam)
        source_y = _remove_vertical_path(source_y, seam)
    if current.shape[0] > height:
        current = np.transpose(current, (1, 0, 2))
        source_x = source_x.T
        source_y = source_y.T
        while current.shape[1] > height:
            seam = _find_vertical_seam(current)
            current = _remove_vertical_path(current, seam)
            source_x = _remove_vertical_path(source_x, seam)
            source_y = _remove_vertical_path(source_y, seam)
        source_x = source_x.T
        source_y = source_y.T
    return source_x, source_y


def _magik_working_frame(
    frame: Image.Image,
    max_size: int = MAGIK_WORKING_SIZE,
) -> tuple[Image.Image, tuple[int, int]]:
    original_size = frame.size
    # Seam carving is much more expensive than ordinary resizing. Work on a
    # compact copy and never upscale small inputs only to carve them again.
    working = frame.copy()
    working.thumbnail(
        (max_size, max_size),
        Image.Resampling.LANCZOS,
    )
    return working.convert("RGBA"), original_size


def _liquid_rescale(
    frame: Image.Image,
    strength: float,
    *,
    working_size: int = MAGIK_WORKING_SIZE,
) -> Image.Image:
    ratio = _magik_ratio(strength)
    working, original_size = _magik_working_frame(frame, working_size)
    array = np.asarray(working).copy()
    target_width = max(2, round(array.shape[1] * ratio))
    target_height = max(2, round(array.shape[0] * ratio))
    carved = _carve_to(array, target_width, target_height)
    result = Image.fromarray(carved, "RGBA").resize(
        working.size,
        Image.Resampling.LANCZOS,
    )
    return result.resize(original_size, Image.Resampling.LANCZOS)


def _liquid_rescale_frames(
    frames: list[Image.Image],
    strength: float,
    *,
    working_size: int,
) -> list[Image.Image]:
    """Apply one stable seam map to every frame of an animated image."""
    if not frames:
        return []

    ratio = _magik_ratio(strength)
    prepared = [_magik_working_frame(frame, working_size) for frame in frames]
    reference = np.asarray(prepared[0][0]).copy()
    target_width = max(2, round(reference.shape[1] * ratio))
    target_height = max(2, round(reference.shape[0] * ratio))
    source_x, source_y = _carve_coordinate_map(
        reference,
        target_width,
        target_height,
    )

    output: list[Image.Image] = []
    for working, original_size in prepared:
        array = np.asarray(working)
        carved = array[source_y, source_x]
        rendered = Image.fromarray(carved, "RGBA").resize(
            working.size,
            Image.Resampling.LANCZOS,
        )
        output.append(rendered.resize(original_size, Image.Resampling.LANCZOS))
    return output


def _liquid_rescale_sequence(
    frame: Image.Image,
    strength: float,
    frame_count: int = 15,
) -> list[Image.Image]:
    target_ratio = _magik_ratio(strength)
    working, original_size = _magik_working_frame(frame)
    current = np.asarray(working).copy()
    output: list[Image.Image] = []
    for ratio in np.linspace(0.95, target_ratio, frame_count):
        target_width = max(2, round(working.width * float(ratio)))
        target_height = max(2, round(working.height * float(ratio)))
        current = _carve_to(current, target_width, target_height)
        rendered = Image.fromarray(current, "RGBA").resize(
            working.size,
            Image.Resampling.LANCZOS,
        )
        output.append(rendered.resize(original_size, Image.Resampling.LANCZOS))
    return output


def _swirl_frame(frame: Image.Image, strength: float) -> Image.Image:
    if not -720 <= strength <= 720:
        raise ValueError("Swirl strength must be between -720 and 720 degrees.")
    original_size = frame.size
    working = frame
    if max(frame.size) > SWIRL_WORKING_SIZE:
        working = frame.copy()
        working.thumbnail(
            (SWIRL_WORKING_SIZE, SWIRL_WORKING_SIZE),
            Image.Resampling.LANCZOS,
        )
    source = np.asarray(working)
    height, width = source.shape[:2]
    y, x = np.indices((height, width), dtype=np.float32)
    center_x = (width - 1) / 2
    center_y = (height - 1) / 2
    dx = x - center_x
    dy = y - center_y
    radius = np.sqrt(dx * dx + dy * dy)
    maximum = max(1.0, min(width, height) / 2)
    factor = np.clip(1 - radius / maximum, 0, 1)
    angle = np.arctan2(dy, dx) - np.deg2rad(strength) * factor * factor
    source_x = center_x + radius * np.cos(angle)
    source_y = center_y + radius * np.sin(angle)
    source_x = np.clip(np.rint(source_x), 0, width - 1).astype(np.int32)
    source_y = np.clip(np.rint(source_y), 0, height - 1).astype(np.int32)
    result = Image.fromarray(source[source_y, source_x], "RGBA")
    if result.size != original_size:
        result = result.resize(original_size, Image.Resampling.LANCZOS)
    return result


def _sample_heavy_animation(
    frames: list[Image.Image],
    durations: list[int],
    *,
    max_frames: int = HEAVY_EFFECT_MAX_GIF_FRAMES,
) -> tuple[list[Image.Image], list[int]]:
    """Bound expensive per-frame effects while preserving total GIF timing."""
    if len(frames) <= max_frames:
        return frames, durations

    edges = [round(index * len(frames) / max_frames) for index in range(max_frames + 1)]
    sampled_frames: list[Image.Image] = []
    sampled_durations: list[int] = []
    for start, end in zip(edges, edges[1:]):
        end = max(start + 1, end)
        sampled_frames.append(frames[start])
        sampled_durations.append(sum(durations[start:end]))
    return sampled_frames, sampled_durations


OVERLAY_SIZE_RE = re.compile(r"^(?P<width>\d{1,4})[xX×](?P<height>\d{1,4})$")


def _overlay_dimensions(
    options: dict[str, Any],
    *,
    base_width: int,
    base_height: int,
    overlay_width: int,
    overlay_height: int,
) -> tuple[int, int]:
    requested_size = str(options.get("size", "") or "").strip()
    if requested_size:
        match = OVERLAY_SIZE_RE.fullmatch(requested_size)
        if match is None:
            raise ValueError("Overlay size must look like 100x100.")
        width = int(match.group("width"))
        height = int(match.group("height"))
        if (
            not 1 <= width <= MAX_MEDIA_DIMENSION
            or not 1 <= height <= MAX_MEDIA_DIMENSION
        ):
            raise ValueError(
                f"Overlay size must be between 1 and {MAX_MEDIA_DIMENSION} pixels per side."
            )
        return width, height

    scale = float(options.get("scale", 1.0))
    if not 0.05 <= scale <= 2:
        raise ValueError("Overlay scale must be between 0.05 and 2.")
    if bool(options.get("stretch")):
        return max(1, base_width), max(1, base_height)

    target_width = max(1, round(base_width * scale))
    target_height = max(1, round(base_height * scale))
    if overlay_width <= 0 or overlay_height <= 0:
        return target_width, target_height
    ratio = min(target_width / overlay_width, target_height / overlay_height)
    return (
        max(1, round(overlay_width * ratio)),
        max(1, round(overlay_height * ratio)),
    )


def _overlay(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    overlay_data = options.get("overlay_data")
    if not isinstance(overlay_data, bytes):
        raise ValueError("A second image is required.")
    opacity = float(options.get("opacity", 0.5))
    if not 0 <= opacity <= 1:
        raise ValueError("Opacity must be between 0 and 1.")
    try:
        with Image.open(BytesIO(overlay_data)) as opened:
            second = opened.convert("RGBA")
    except UnidentifiedImageError as error:
        raise ValueError("The overlay must be an image.") from error

    target_size = _overlay_dimensions(
        options,
        base_width=frame.width,
        base_height=frame.height,
        overlay_width=second.width,
        overlay_height=second.height,
    )
    second = second.resize(target_size, Image.Resampling.LANCZOS)
    alpha = second.getchannel("A").point(
        [round(value * opacity) for value in range(256)]
    )
    second.putalpha(alpha)
    result = frame.copy()
    position = str(options.get("position", "center")).casefold().replace("_", "-")
    positions = {
        "center": (
            (frame.width - second.width) // 2,
            (frame.height - second.height) // 2,
        ),
        "top-left": (0, 0),
        "top": ((frame.width - second.width) // 2, 0),
        "top-right": (frame.width - second.width, 0),
        "left": (0, (frame.height - second.height) // 2),
        "right": (frame.width - second.width, (frame.height - second.height) // 2),
        "bottom-left": (0, frame.height - second.height),
        "bottom": ((frame.width - second.width) // 2, frame.height - second.height),
        "bottom-right": (
            frame.width - second.width,
            frame.height - second.height,
        ),
    }
    if position not in positions:
        raise ValueError(
            "Overlay position must be center, top, bottom, left, right, "
            "or a corner such as top-left."
        )
    base_x, base_y = positions[position]
    base_x += int(options.get("x", 0))
    base_y += int(options.get("y", 0))
    result.alpha_composite(
        second,
        (base_x, base_y),
    )
    return result


def _hue_rotate(frame: Image.Image, degrees: float) -> Image.Image:
    rgba = frame.convert("RGBA")
    hsv = np.asarray(rgba.convert("HSV"), dtype=np.uint16)
    hsv[:, :, 0] = (hsv[:, :, 0] + round(degrees * 255 / 360)) % 256
    rotated = Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGBA")
    rotated.putalpha(rgba.getchannel("A"))
    return rotated


def _offset_frame(frame: Image.Image, x: int, y: int) -> Image.Image:
    result = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    result.alpha_composite(frame, (x, y))
    return result


def _reference_frame(frame: Image.Image) -> Image.Image:
    return ImageOps.fit(
        frame.convert("RGBA"),
        (REFERENCE_EFFECT_SIZE, REFERENCE_EFFECT_SIZE),
        Image.Resampling.LANCZOS,
    )


def _wrap_frame(frame: Image.Image, x: int = 0, y: int = 0) -> Image.Image:
    return Image.fromarray(
        np.roll(
            np.roll(np.asarray(frame.convert("RGBA")), y, axis=0),
            x,
            axis=1,
        ),
        "RGBA",
    )


def _zoom_frame(frame: Image.Image, amount: float) -> Image.Image:
    if amount <= 0:
        raise ValueError("Zoom must be greater than 0.")
    width = max(1, round(frame.width * amount))
    height = max(1, round(frame.height * amount))
    scaled = frame.resize((width, height), Image.Resampling.LANCZOS)
    if amount >= 1:
        left = (width - frame.width) // 2
        top = (height - frame.height) // 2
        return scaled.crop((left, top, left + frame.width, top + frame.height))
    result = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    result.alpha_composite(
        scaled, ((frame.width - width) // 2, (frame.height - height) // 2)
    )
    return result


def _recursive_zoom_frame(
    frame: Image.Image,
    amount: float,
    *,
    scale: float = 0.1,
) -> Image.Image:
    """Render each recursive zoom level directly from the source.

    Scaling one already-composited frame repeatedly causes every nested copy
    to soften as the animation advances. Drawing each level from the original
    keeps the moving image as sharp as the source allows.
    """

    result = _zoom_frame(frame, amount)
    level_scale = amount * scale
    while level_scale * min(frame.size) >= 1:
        width = max(1, round(frame.width * level_scale))
        height = max(1, round(frame.height * level_scale))
        nested = frame.resize((width, height), Image.Resampling.LANCZOS)
        result.alpha_composite(
            nested,
            ((frame.width - width) // 2, (frame.height - height) // 2),
        )
        level_scale *= scale
    return result


def _warp_to_quad(
    texture: Image.Image,
    size: tuple[int, int],
    quad: Sequence[tuple[float, float]],
) -> Image.Image:
    width, height = texture.size
    source = (
        (0.0, 0.0),
        (width - 1.0, 0.0),
        (width - 1.0, height - 1.0),
        (0.0, height - 1.0),
    )
    coefficients = _perspective_coefficients(quad, source)
    warped = texture.transform(
        size,
        Image.Transform.PERSPECTIVE,
        coefficients,
        Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )
    polygon_mask = Image.new("L", size, 0)
    ImageDraw.Draw(polygon_mask).polygon(list(quad), fill=255)
    alpha = ImageChops.multiply(warped.getchannel("A"), polygon_mask)
    warped.putalpha(alpha)
    return warped


def _hallway_pattern(frame: Image.Image, progress: float = 0.0) -> Image.Image:
    size = frame.size
    center_x = frame.width / 2
    center_y = frame.height / 2
    ratio = 0.65
    endpoint_scale = 0.1
    phase_scale = ratio ** (-progress)
    scales = [phase_scale / ratio]
    while scales[-1] * ratio > endpoint_scale:
        scales.append(scales[-1] * ratio)
    scales.append(endpoint_scale)
    rectangles: list[tuple[float, float, float, float]] = []
    for scale in scales:
        half_width = frame.width * scale / 2
        half_height = frame.height * scale / 2
        rectangles.append(
            (
                center_x - half_width,
                center_y - half_height,
                center_x + half_width,
                center_y + half_height,
            )
        )

    # Begin with an opaque source layer so perspective rounding at the outer
    # edges cannot leave transparent wedges. Only the fixed endpoint below is
    # intentionally transparent.
    result = frame.copy()
    for outer, inner in zip(rectangles, rectangles[1:]):
        left, top, right, bottom = outer
        inner_left, inner_top, inner_right, inner_bottom = inner
        quads = (
            (
                (left, top),
                (right, top),
                (inner_right, inner_top),
                (inner_left, inner_top),
            ),
            (
                (inner_left, inner_bottom),
                (inner_right, inner_bottom),
                (right, bottom),
                (left, bottom),
            ),
            (
                (left, top),
                (inner_left, inner_top),
                (inner_left, inner_bottom),
                (left, bottom),
            ),
            (
                (inner_right, inner_top),
                (right, top),
                (right, bottom),
                (inner_right, inner_bottom),
            ),
        )
        textures = (
            frame,
            ImageOps.flip(frame),
            frame.rotate(90, expand=False),
            frame.rotate(-90, expand=False),
        )
        for texture, quad in zip(textures, quads):
            result.alpha_composite(_warp_to_quad(texture, size, quad))

    # The reference animation ends in a fixed transparent opening instead of
    # filling the vanishing point with another increasingly blurry copy.
    hole_width = max(1, round(frame.width * endpoint_scale))
    hole_height = max(1, round(frame.height * endpoint_scale))
    hole = Image.new("L", size, 255)
    ImageDraw.Draw(hole).rectangle(
        (
            (frame.width - hole_width) // 2,
            (frame.height - hole_height) // 2,
            (frame.width + hole_width) // 2 - 1,
            (frame.height + hole_height) // 2 - 1,
        ),
        fill=0,
    )
    result.putalpha(ImageChops.multiply(result.getchannel("A"), hole))
    return result


def _glitch_frame(frame: Image.Image, index: int, amount: int) -> Image.Image:
    if index % 5 in {0, 2, 4}:
        return frame.copy()
    rng = random.Random(0xF15E + index)
    source = np.asarray(frame.convert("RGBA"))
    result = source.copy()
    band_count = 3 + rng.randrange(5)
    for _ in range(band_count):
        top = rng.randrange(0, frame.height - 4)
        band_height = rng.randrange(4, max(5, min(55, frame.height - top)))
        bottom = min(frame.height, top + band_height)
        shift = rng.randint(-amount * 2, amount * 2)
        result[top:bottom] = np.roll(result[top:bottom], shift, axis=1)
        if rng.random() < 0.55:
            channel_shift = rng.randrange(3)
            result[top:bottom, :, channel_shift] = np.roll(
                result[top:bottom, :, channel_shift],
                rng.randint(-amount, amount),
                axis=1,
            )
        if rng.random() < 0.35:
            tint = np.asarray(
                rng.choice(((30, 255, 30), (160, 20, 220), (0, 180, 255))),
                dtype=np.uint16,
            )
            colors = result[top:bottom, :, :3].astype(np.uint16)
            result[top:bottom, :, :3] = ((colors + tint) // 2).astype(np.uint8)
    return Image.fromarray(result, "RGBA")


def _animated_visual_frames(
    source_frames: list[Image.Image],
    durations: list[int],
    effect: str,
    options: dict[str, Any],
) -> tuple[list[Image.Image], list[int]]:
    speed = float(options.get("speed", 1.0))
    if not 0.25 <= speed <= 4:
        raise ValueError("Animation speed must be between 0.25 and 4.")
    preserve_source_timing = effect == "huerotate" and len(source_frames) > 1
    count = (
        len(source_frames)
        if preserve_source_timing
        else max(12, min(60, int(options.get("frames", REFERENCE_EFFECT_FRAMES))))
    )
    prepared = [
        frame.convert("RGBA") if preserve_source_timing else _reference_frame(frame)
        for frame in source_frames
    ]
    output: list[Image.Image] = []
    for index in range(count):
        source_index = index % len(prepared)
        source = prepared[source_index]
        phase = 2 * math.pi * index / count
        if effect == "zoom":
            target = (
                10.0 if options.get("forever") else float(options.get("amount", 10.0))
            )
            if not 1 <= target <= 12:
                raise ValueError("Zoom must be between 1 and 12.")
            progress = ((index + round(count * 0.795)) % count) / count
            output.append(_recursive_zoom_frame(source, target**progress))
        elif effect == "hallway":
            progress = index / count
            output.append(_hallway_pattern(source, progress))
        elif effect == "parallax":
            offset = -round(2 * source.width * index / count)
            output.append(_wrap_frame(source, offset, 0))
        elif effect == "squishy":
            amount = float(options.get("amount", 75))
            if amount <= 1:
                amount = 75 * amount / 0.18
            width = max(1, round(200 + amount * math.sin(phase)))
            height = max(1, round(200 + amount * math.cos(phase)))
            squished = source.resize((width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", source.size, (0, 0, 0, 0))
            canvas.alpha_composite(
                squished,
                ((source.width - width) // 2, (source.height - height) // 2),
            )
            output.append(canvas)
        elif effect == "tremble":
            amount = max(1, min(30, round(float(options.get("amount", 8)))))
            size = source.width - amount * 2
            trembled = source.resize((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", source.size, (0, 0, 0, 0))
            if amount == 8 and count == REFERENCE_EFFECT_FRAMES:
                position = TREMBLE_POSITIONS[index]
            else:
                rng = random.Random(0x7EAD + index)
                position = (
                    amount + rng.randint(-amount // 2, amount // 2),
                    amount + rng.randint(-amount // 2, amount // 2),
                )
            canvas.alpha_composite(
                trembled,
                position,
            )
            output.append(canvas)
        elif effect == "glitch":
            amount = max(1, min(40, round(float(options.get("amount", 12)))))
            output.append(_glitch_frame(source, index, amount))
        elif effect == "quilt":
            tile_size = max(20, min(100, int(options.get("tile_size", 60))))
            tile = source.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            tiled = Image.new("RGBA", source.size)
            for y in range(0, source.height, tile_size):
                for x in range(0, source.width, tile_size):
                    tiled.alpha_composite(tile, (x, y))
            offset = round(tile_size * index / count)
            output.append(_wrap_frame(tiled, -offset, offset))
        elif effect == "huerotate":
            rgb = np.asarray(source.convert("RGB"), dtype=np.uint16)
            step = index + 1
            offsets = np.asarray(
                (
                    round(256 * step / count),
                    round(512 * step / count),
                    round(768 * step / count),
                ),
                dtype=np.uint16,
            )
            cycled = ((rgb + offsets) % 256).astype(np.uint8)
            rotated = Image.fromarray(cycled, "RGB").convert("RGBA")
            rotated.putalpha(source.getchannel("A"))
            output.append(rotated)
        else:
            output.append(source.copy())
    if preserve_source_timing:
        return output, [max(20, round(duration / speed)) for duration in durations]
    frame_duration = max(20, round(REFERENCE_EFFECT_DURATION / speed))
    return output, [frame_duration] * count


def _edge_crop_box(
    frame: Image.Image,
    *,
    caption: bool = False,
) -> tuple[int, int, int, int] | None:
    rgba = frame.convert("RGBA")
    array = np.asarray(rgba.convert("RGB"), dtype=np.float32)
    brightness = array.mean(axis=2)
    row_spread = array.std(axis=2).mean(axis=1)
    if caption:
        # Caption text can fill most of a row, but generated caption panels
        # keep their side margins white. Find the first sustained transition
        # where both the margins and the full row stop resembling the panel.
        near_white = np.all(array > 240, axis=2)
        margin = max(2, min(rgba.width // 8, 64))
        margin_white = np.concatenate(
            (near_white[:, :margin], near_white[:, -margin:]),
            axis=1,
        ).mean(axis=1)
        row_white = near_white.mean(axis=1)
        window = max(3, min(12, rgba.height // 60))
        top = 0
        if margin_white[0] >= 0.9:
            for candidate in range(1, len(margin_white) // 2):
                end = min(len(margin_white), candidate + window)
                if (
                    float(margin_white[candidate]) < 0.86
                    and float(row_white[candidate]) < 0.8
                    and float(margin_white[candidate:end].mean()) < 0.86
                    and float(row_white[candidate:end].mean()) < 0.8
                ):
                    top = candidate
                    break
        if top <= 0 or rgba.height - top < 2:
            return None
        return (0, top, rgba.width, rgba.height)
    else:
        mask = (brightness.mean(axis=1) < 12) & (row_spread < 18)
    top = 0
    while top < len(mask) // 2 and bool(mask[top]):
        top += 1
    bottom = len(mask)
    while bottom > len(mask) // 2 and bool(mask[bottom - 1]):
        bottom -= 1
    if bottom - top < 2 or (top == 0 and bottom == len(mask)):
        return None
    return (0, top, rgba.width, bottom)


def _remove_bars(frame: Image.Image, *, caption: bool = False) -> Image.Image:
    rgba = frame.convert("RGBA")
    crop_box = _edge_crop_box(rgba, caption=caption)
    if crop_box is None:
        return rgba
    return rgba.crop(crop_box)


def _stable_edge_crop_box(
    frames: Sequence[Image.Image],
    *,
    caption: bool = False,
) -> tuple[int, int, int, int] | None:
    """Choose one crop for an entire animation.

    Detecting bars independently can make adjacent frames differ by one pixel.
    GIF and video encoders require a stable canvas, and some encoders stop at
    the first size change. The median detected boundary tolerates those small
    per-frame compression differences while preserving every frame.
    """

    boxes = [
        box
        for frame in frames
        if (box := _edge_crop_box(frame, caption=caption)) is not None
    ]
    if not boxes:
        return None
    middle = len(boxes) // 2
    return tuple(
        sorted(box[coordinate] for box in boxes)[middle] for coordinate in range(4)
    )  # type: ignore[return-value]


def _falsecolor(frame: Image.Image) -> Image.Image:
    gray = np.asarray(ImageOps.grayscale(frame), dtype=np.float32) / 255
    stops = np.array(
        ((0, 0, 32), (0, 180, 255), (255, 255, 0), (255, 32, 0)), dtype=np.float32
    )
    positions = np.linspace(0, 1, len(stops))
    channels = [np.interp(gray, positions, stops[:, channel]) for channel in range(3)]
    result = Image.fromarray(
        np.stack(channels, axis=-1).astype(np.uint8), "RGB"
    ).convert("RGBA")
    result.putalpha(frame.getchannel("A"))
    return result


def _watercolor(frame: Image.Image) -> Image.Image:
    softened = frame.convert("RGB").filter(ImageFilter.GaussianBlur(1.4))
    softened = ImageEnhance.Color(softened).enhance(1.5)
    return _preserve_alpha(frame, softened)


def _oilpaint(frame: Image.Image) -> Image.Image:
    # A broad median pass flattens small color variations while keeping the
    # strong illustrated contours intact. The reference effect also lifts the
    # shadows and softens contrast to create its painted finish.
    painted = frame.convert("RGB").filter(ImageFilter.MedianFilter(9))
    array = np.asarray(painted, dtype=np.float32)
    array = np.clip(array * 0.8 + 58, 0, 255).astype(np.uint8)
    return _preserve_alpha(frame, Image.fromarray(array, "RGB"))


def _meme(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    text = str(options.get("text", "")).strip()
    if not text:
        raise ValueError("Meme requires text.")
    image = frame.convert("RGBA").copy()
    # Leave enough room for the outline while keeping the tall, condensed
    # proportions of the usual meme font at Discord's attachment sizes.
    size = max(18, min(96, round(image.width / 8 * 0.91)))
    middle_text = ""
    if "|" in text:
        top_text, bottom_text = (part.strip() for part in text.split("|", 1))
    else:
        words = text.split()
        if len(words) < 6:
            top_text = bottom_text = ""
            middle_text = text
        else:
            midpoint = max(1, len(words) // 2)
            top_text = " ".join(words[:midpoint])
            bottom_text = " ".join(words[midpoint:])

    def draw_centered(value: str, bottom: int | None = None) -> None:
        if not value:
            return
        value = value.upper()
        font = meme_font(value, size)
        stroke = max(3, round(size * 0.04))
        box = font.getbbox(value, stroke_width=stroke)
        padding = stroke + 2
        layer = Image.new(
            "RGBA",
            (
                round(box[2] - box[0] + padding * 2),
                round(box[3] - box[1] + padding * 2),
            ),
            (0, 0, 0, 0),
        )
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text(
            (padding - box[0], padding - box[1]),
            value,
            font=font,
            fill="white",
            stroke_width=stroke,
            stroke_fill="black",
        )
        # Impact-style meme fonts are much narrower than the general-purpose
        # multilingual fonts available in the container. Compress only the
        # horizontal axis so the familiar tall lettering is retained.
        target_width = max(1, round(layer.width * 0.61))
        if target_width > image.width - 12:
            target_width = image.width - 12
        layer = layer.resize((target_width, layer.height), Image.Resampling.LANCZOS)
        x = (image.width - layer.width) // 2
        if bottom is None:
            y = 8
        else:
            y = image.height - layer.height - bottom
        image.alpha_composite(layer, (x, y))

    draw_centered(top_text)
    if bottom_text:
        draw_centered(bottom_text, bottom=4)
    if middle_text:
        font = meme_font(middle_text, size)
        box = font.getbbox(middle_text, stroke_width=max(3, round(size * 0.04)))
        draw_centered(middle_text, bottom=round((image.height - (box[3] - box[1])) / 2))
    return image


STATIC_EFFECTS: dict[str, Callable[[Image.Image, dict[str, Any]], Image.Image]] = {
    "invert": _invert,
    "flip": _flip,
    "blur": _blur,
    "crop": _crop_shape,
    "deepfry": _deepfry,
    "grayscale": _grayscale,
    "mirror": _mirror,
    "jpeg": _jpeg,
    "overlay": _overlay,
    "tint": _tint,
    "implode": _implode,
    "explode": _explode,
    "sharpen": _sharpen,
    "legoify": _legoify,
    "fisheye": _fisheye,
    "sepia": _sepia,
    "pixelate": _pixelate,
    "vignette": _vignette,
    "resize": _resize,
    "distort": _distort,
    "grain": _grain,
    "rotate": _rotate,
    "noise": _color_noise,
    "brightness": _brightness,
    "contrast": _contrast,
    "saturation": _saturation,
    "exposure": _exposure,
    "removebars": lambda frame, _: _remove_bars(frame),
    "removecaption": lambda frame, _: _remove_bars(frame, caption=True),
    "enlarge": lambda frame, options: _resize(
        frame, {"scale": options.get("amount", 2), "ratio": ""}
    ),
    "falsecolor": lambda frame, _: _falsecolor(frame),
    "watercolor": lambda frame, _: _watercolor(frame),
    "oilpaint": lambda frame, _: _oilpaint(frame),
    "meme": _meme,
}


def _square_source(frame: Image.Image, size: int = 384) -> Image.Image:
    contained = ImageOps.contain(frame, (size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        contained,
        ((size - contained.width) // 2, (size - contained.height) // 2),
    )
    return canvas


def _spin_frames(
    source_frames: list[Image.Image],
    *,
    speed: float,
    clockwise: bool,
) -> tuple[list[Image.Image], list[int]]:
    if not 0.25 <= speed <= 4:
        raise ValueError("Spin speed must be between 0.25 and 4.")
    count = max(12, min(60, round(36 / speed)))
    duration = max(20, round(40 / speed))
    direction = -1 if clockwise else 1
    frames: list[Image.Image] = []
    for index in range(count):
        source = _square_source(source_frames[index % len(source_frames)])
        frames.append(
            source.rotate(
                direction * 360 * index / count,
                Image.Resampling.BICUBIC,
                expand=False,
            )
        )
    return frames, [duration] * count


def _wiggle_frames(
    source_frames: list[Image.Image],
    *,
    amount: float,
    speed: float,
) -> tuple[list[Image.Image], list[int]]:
    if not 1 <= amount <= 30:
        raise ValueError("Wiggle amount must be between 1 and 30.")
    if not 0.25 <= speed <= 4:
        raise ValueError("Wiggle speed must be between 0.25 and 4.")
    count = 15
    duration = max(20, round(20 / speed))
    frames: list[Image.Image] = []
    for index in range(count):
        phase = 2 * math.pi * index / count
        source = source_frames[index % len(source_frames)].convert("RGBA")
        array = np.asarray(source)
        height, width = array.shape[:2]
        y, x = np.indices((height, width), dtype=np.float32)
        scaled_amount = max(1.0, amount * max(width, height) / 512)
        wavelength = max(24.0, height / 3)
        source_x = x + np.sin(2 * math.pi * y / wavelength + phase) * scaled_amount
        source_y = y + np.sin(2 * math.pi * x / max(24.0, width / 2) + phase) * (
            scaled_amount * 0.18
        )
        source_x = np.clip(np.rint(source_x), 0, width - 1).astype(np.int32)
        source_y = np.clip(np.rint(source_y), 0, height - 1).astype(np.int32)
        frames.append(Image.fromarray(array[source_y, source_x], "RGBA"))
    return frames, [duration] * count


def _animated_distortion(
    source_frames: list[Image.Image],
    *,
    kind: Literal["magik", "swirl"],
    strength: float,
    speed: float,
) -> tuple[list[Image.Image], list[int]]:
    if not 0.25 <= speed <= 4:
        raise ValueError("Animation speed must be between 0.25 and 4.")
    count = max(10, min(40, round(24 / speed)))
    duration = max(20, round(50 / speed))
    frames: list[Image.Image] = []
    if kind == "magik":
        frames = _liquid_rescale_sequence(
            source_frames[0],
            strength,
            frame_count=15,
        )
        return frames, [max(20, round(80 / speed))] * len(frames)

    for index in range(count):
        source = source_frames[index % len(source_frames)]
        phase = 2 * math.pi * index / count
        frames.append(_swirl_frame(source, math.sin(phase) * strength))
    return frames, [duration] * count


def _fade_frames(
    source_frames: list[Image.Image],
    durations: list[int],
    *,
    fade_in: bool,
    duration: float,
) -> tuple[list[Image.Image], list[int]]:
    if not 0.1 <= duration <= 10:
        raise ValueError("Fade duration must be between 0.1 and 10 seconds.")
    duration_ms = round(duration * 1_000)
    generated_from_still = len(source_frames) == 1
    if generated_from_still:
        count = max(5, min(100, round(duration * 25)))
        source_frames = [source_frames[0].copy() for _ in range(count)]
        durations = [max(20, round(duration_ms / count))] * count
    total_ms = sum(durations)
    elapsed_ms = 0
    output: list[Image.Image] = []
    # GIF has one-bit transparency. Error-diffused alpha represents partial
    # opacity without darkening the image or producing a visible checkerboard.
    for index, (source, frame_duration) in enumerate(zip(source_frames, durations)):
        if generated_from_still:
            progress = index / max(1, len(source_frames) - 1)
            opacity = progress if fade_in else 1 - progress
        elif fade_in:
            opacity = min(1.0, elapsed_ms / max(1, duration_ms))
        else:
            fade_start = max(0, total_ms - duration_ms)
            opacity = min(
                1.0,
                max(
                    0.0,
                    (total_ms - elapsed_ms - frame_duration) / max(1, duration_ms),
                ),
            )
            if elapsed_ms < fade_start:
                opacity = 1.0

        rgba = source.convert("RGBA")
        if opacity <= 0:
            frame = Image.new("RGBA", source.size, (0, 0, 0, 0))
        elif opacity >= 1:
            frame = rgba
        else:
            source_alpha = np.asarray(rgba.getchannel("A"), dtype=np.float32)
            visible_alpha = np.rint(source_alpha * opacity).astype(np.uint8)
            binary_alpha = Image.fromarray(visible_alpha, "L").convert(
                "1",
                dither=Image.Dither.FLOYDSTEINBERG,
            )
            frame = rgba.copy()
            frame.putalpha(binary_alpha.convert("L"))
        output.append(frame)
        elapsed_ms += frame_duration
    return output, durations


def _lag_frames(
    source_frames: list[Image.Image],
    durations: list[int],
    *,
    amount: int,
    method: str = "random",
    multi: bool = False,
) -> tuple[list[Image.Image], list[int]]:
    if len(source_frames) <= 1:
        raise ValueError("Lag requires a GIF or video, not a still image.")
    if not 2 <= amount <= 12:
        raise ValueError("Lag amount must be between 2 and 12.")
    methods = ("freeze", "stutter", "drop", "jitter")
    normalized = method.casefold().replace("_", "-")
    if normalized not in {*methods, "random"}:
        raise ValueError("Lag method must be random, freeze, stutter, drop, or jitter.")
    rng = random.SystemRandom()
    selected: list[str] = (
        [normalized]
        if normalized != "random"
        else rng.sample(methods, rng.randint(2, 4) if multi else 1)
    )
    if multi and normalized != "random":
        extras = [candidate for candidate in methods if candidate != normalized]
        selected.extend(rng.sample(extras, rng.randint(1, min(2, len(extras)))))

    output = [frame.convert("RGBA").copy() for frame in source_frames]
    frame_count = len(output)
    for selected_method in selected:
        changed: list[Image.Image] = []
        burst_left = 0
        held_index = 0
        for index, frame in enumerate(output):
            if selected_method == "freeze":
                if burst_left <= 0 and rng.random() < min(0.35, amount / 30):
                    held_index = index
                    burst_left = rng.randint(2, amount)
                if burst_left > 0:
                    changed.append(output[held_index].copy())
                    burst_left -= 1
                else:
                    changed.append(frame.copy())
            elif selected_method == "stutter":
                distance = rng.randint(1, min(amount, index)) if index else 0
                use_old = index > 0 and rng.random() < min(0.5, amount / 18)
                changed.append(
                    output[index - distance].copy() if use_old else frame.copy()
                )
            elif selected_method == "drop":
                jump = rng.randint(1, amount) if rng.random() < amount / 24 else 0
                changed.append(output[min(frame_count - 1, index + jump)].copy())
            else:
                distance = max(1, amount // 2)
                x = rng.randint(-distance, distance)
                y = rng.randint(-distance, distance)
                canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                canvas.alpha_composite(frame, (x, y))
                changed.append(canvas)
        output = changed
    return output, durations


def _shuffle_frames(
    source_frames: list[Image.Image],
    durations: list[int],
) -> tuple[list[Image.Image], list[int]]:
    if len(source_frames) <= 1:
        raise ValueError("Shuffle requires a GIF or video, not a still image.")
    indexes = list(range(len(source_frames)))
    random.Random(0).shuffle(indexes)
    return [source_frames[index].copy() for index in indexes], [
        durations[index] for index in indexes
    ]


def _bounce_frames(
    source_frames: list[Image.Image],
    durations: list[int],
    *,
    amount: float,
    speed: float,
) -> tuple[list[Image.Image], list[int]]:
    if not 1 <= amount <= 100 or not 0.25 <= speed <= 4:
        raise ValueError("Bounce amount must be 1 to 100 and speed 0.25 to 4.")
    count = len(source_frames) if len(source_frames) > 1 else 20
    output: list[Image.Image] = []
    for index in range(count):
        source = source_frames[index % len(source_frames)]
        canvas = Image.new("RGBA", source.size, (0, 0, 0, 0))
        distance = round(
            math.sin(2 * math.pi * index / count) * min(amount, source.height / 3)
        )
        canvas.alpha_composite(source, (0, distance))
        output.append(canvas)
    generated_durations = (
        [max(20, round(frame_duration / speed)) for frame_duration in durations]
        if len(source_frames) > 1
        else [max(20, round(40 / speed))] * count
    )
    return output, generated_durations


def _slide_frames(
    source_frames: list[Image.Image],
    durations: list[int],
    *,
    slide_in: bool,
    direction: str,
    duration: float,
) -> tuple[list[Image.Image], list[int]]:
    if direction not in {"left", "right", "up", "down"}:
        raise ValueError("Slide direction must be left, right, up, or down.")
    if not 0.1 <= duration <= 10:
        raise ValueError("Slide duration must be between 0.1 and 10 seconds.")
    duration_ms = round(duration * 1_000)
    generated_from_still = len(source_frames) == 1
    if not generated_from_still:
        count = len(source_frames)
        generated_durations = durations
    else:
        count = max(5, min(100, round(duration * 25)))
        generated_durations = [max(20, round(duration_ms / count))] * count
    output: list[Image.Image] = []
    elapsed_ms = 0
    for index in range(count):
        source = source_frames[index % len(source_frames)]
        progress = (
            index / max(1, count - 1)
            if generated_from_still
            else min(1.0, elapsed_ms / max(1, duration_ms))
        )
        if not slide_in:
            progress = 1 - progress
        if direction == "left":
            position = (round((1 - progress) * source.width), 0)
        elif direction == "right":
            position = (round((progress - 1) * source.width), 0)
        elif direction == "up":
            position = (0, round((1 - progress) * source.height))
        else:
            position = (0, round((progress - 1) * source.height))
        canvas = Image.new("RGBA", source.size, (0, 0, 0, 0))
        canvas.alpha_composite(source, position)
        output.append(canvas)
        elapsed_ms += generated_durations[index]
    return output, generated_durations


def _perspective_coefficients(
    destination: Sequence[tuple[float, float]],
    source: Sequence[tuple[float, float]],
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
        raise ValueError("Could not project that texture onto the shape.") from error
    return tuple(float(value) for value in coefficients)


def _rotate_project_vertices(
    vertices: Sequence[tuple[float, float, float]],
    angle: float,
    *,
    size: int,
    axis: Literal["x", "y", "z"] = "y",
    pitch_degrees: float = -10,
    scale: float = 82,
) -> tuple[list[tuple[float, float]], list[tuple[float, float, float]]]:
    axis_value = axis.casefold()
    if axis_value not in {"x", "y", "z"}:
        raise ValueError("Rotation axis must be x, y, or z.")
    axis = cast(Literal["x", "y", "z"], axis_value)
    rotation_cos = math.cos(angle)
    rotation_sin = math.sin(angle)
    pitch = math.radians(pitch_degrees)
    pitch_cos = math.cos(pitch)
    pitch_sin = math.sin(pitch)
    center = size / 2
    camera_distance = 5.0
    rotated: list[tuple[float, float, float]] = []
    projected: list[tuple[float, float]] = []
    for x, y, z in vertices:
        if axis == "x":
            rotation_x = x
            rotation_y = y * rotation_cos - z * rotation_sin
            rotation_z = y * rotation_sin + z * rotation_cos
        elif axis == "z":
            rotation_x = x * rotation_cos - y * rotation_sin
            rotation_y = x * rotation_sin + y * rotation_cos
            rotation_z = z
        else:
            rotation_x = x * rotation_cos + z * rotation_sin
            rotation_y = y
            rotation_z = -x * rotation_sin + z * rotation_cos
        pitch_y = rotation_y * pitch_cos - rotation_z * pitch_sin
        pitch_z = rotation_y * pitch_sin + rotation_z * pitch_cos
        perspective = camera_distance / (camera_distance - pitch_z)
        rotated.append((rotation_x, pitch_y, pitch_z))
        projected.append(
            (
                center + rotation_x * scale * perspective,
                center - pitch_y * scale * perspective,
            )
        )
    return projected, rotated


def _face_brightness(points: list[tuple[float, float, float]]) -> float:
    first = np.asarray(points[0], dtype=float)
    second = np.asarray(points[1], dtype=float)
    third = np.asarray(points[2], dtype=float)
    normal = np.cross(second - first, third - first)
    length = float(np.linalg.norm(normal))
    if length <= 1e-6:
        return 0.65
    normal /= length
    light = np.asarray((-0.35, 0.55, 0.76), dtype=float)
    light /= np.linalg.norm(light)
    return 0.58 + 0.42 * abs(float(np.dot(normal, light)))


def _warp_texture_face(
    texture: Image.Image,
    destination: list[tuple[float, float]],
    *,
    size: int,
    brightness: float,
) -> Image.Image:
    source = [
        (0.0, 0.0),
        (float(texture.width), 0.0),
        (float(texture.width), float(texture.height)),
        (0.0, float(texture.height)),
    ]
    coefficients = _perspective_coefficients(destination, source)
    warped = texture.transform(
        (size, size),
        Image.Transform.PERSPECTIVE,
        coefficients,
        Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255, 0),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(destination, fill=255)
    warped.putalpha(ImageChops_multiply(warped.getchannel("A"), mask))
    if brightness < 0.995:
        rgb = ImageEnhance.Brightness(warped.convert("RGB")).enhance(brightness)
        rgb.putalpha(warped.getchannel("A"))
        warped = rgb
    return warped


def _warp_texture_triangle(
    texture: Image.Image,
    destination: list[tuple[float, float]],
    *,
    size: int,
    brightness: float,
) -> Image.Image:
    source = [
        (texture.width / 2, 0.0),
        (float(texture.width), float(texture.height)),
        (0.0, float(texture.height)),
    ]
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0])
        values.append(u)
        matrix.append([0, 0, 0, x, y, 1])
        values.append(v)
    try:
        coefficients = np.linalg.solve(
            np.asarray(matrix, dtype=float),
            np.asarray(values, dtype=float),
        )
    except np.linalg.LinAlgError as error:
        raise ValueError("Could not project that texture onto the pyramid.") from error
    warped = texture.transform(
        (size, size),
        Image.Transform.AFFINE,
        tuple(float(value) for value in coefficients),
        Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255, 0),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(destination, fill=255)
    warped.putalpha(ImageChops_multiply(warped.getchannel("A"), mask))
    if brightness < 0.995:
        rgb = ImageEnhance.Brightness(warped.convert("RGB")).enhance(brightness)
        rgb.putalpha(warped.getchannel("A"))
        warped = rgb
    return warped


def _render_textured_polyhedron(
    texture: Image.Image,
    angle: float,
    shape: Literal["cube", "pyramid"],
    *,
    axis: Literal["x", "y", "z"] = "y",
    size: int = 300,
) -> Image.Image:
    if shape == "cube":
        vertices = [
            (-1, 1, 1),
            (1, 1, 1),
            (1, -1, 1),
            (-1, -1, 1),
            (1, 1, -1),
            (-1, 1, -1),
            (1, -1, -1),
            (-1, -1, -1),
        ]
        faces = [
            (0, 1, 2, 3),
            (1, 4, 6, 2),
            (4, 5, 7, 6),
            (5, 0, 3, 7),
            (5, 4, 1, 0),
            (3, 2, 6, 7),
        ]
        projected, rotated = _rotate_project_vertices(
            vertices,
            angle,
            size=size,
            axis=axis,
            pitch_degrees=-10,
            scale=78,
        )
        face_data = [
            (
                sum(rotated[index][2] for index in face) / 4,
                [projected[index] for index in face],
                [rotated[index] for index in face],
            )
            for face in faces
        ]
    else:
        vertices = [
            (0, 1.3, 0),
            (-1, -1, 1),
            (1, -1, 1),
            (1, -1, -1),
            (-1, -1, -1),
        ]
        faces = ((0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1))
        projected, rotated = _rotate_project_vertices(
            vertices,
            angle,
            size=size,
            axis=axis,
            pitch_degrees=-7,
            scale=88,
        )
        face_data = []
        for face in faces:
            destination = [projected[index] for index in face]
            face_data.append(
                (
                    sum(rotated[index][2] for index in face) / 3,
                    destination,
                    [rotated[index] for index in face],
                )
            )

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for _, destination, points_3d in sorted(face_data, key=lambda item: item[0]):
        if len(destination) == 3:
            rendered_face = _warp_texture_triangle(
                texture,
                destination,
                size=size,
                brightness=_face_brightness(points_3d),
            )
        else:
            rendered_face = _warp_texture_face(
                texture,
                destination,
                size=size,
                brightness=_face_brightness(points_3d),
            )
        canvas.alpha_composite(rendered_face)
    return canvas


def _shape_frames(
    source_frames: list[Image.Image],
    *,
    shape: Literal["cube", "pyramid"],
    speed: float,
    clockwise: bool,
    axis: str = "y",
    rotation: float = 0.0,
) -> tuple[list[Image.Image], list[int]]:
    if not 0.25 <= speed <= 4:
        raise ValueError("Rotation speed must be between 0.25 and 4.")
    count = max(13, min(78, round(39 / speed)))
    duration = 50
    direction = -1 if clockwise else 1
    axis = axis.casefold()
    if axis not in {"x", "y", "z"}:
        raise ValueError("Rotation axis must be x, y, or z.")
    rotation_offset = math.radians(rotation)
    frames: list[Image.Image] = []
    for index in range(count):
        source = ImageOps.fit(
            source_frames[index % len(source_frames)],
            (256, 256),
            Image.Resampling.LANCZOS,
        )
        angle = direction * 2 * math.pi * index / count + rotation_offset
        frames.append(
            _render_textured_polyhedron(
                source,
                angle,
                shape,
                axis=cast(Literal["x", "y", "z"], axis),
            )
        )
    return frames, [duration] * count


def _video_swirl_filter(
    strength: float,
    *,
    progress: str = "1",
) -> str:
    radians = math.radians(strength)
    radius = "hypot(X-W/2,Y-H/2)"
    falloff = f"pow(max(0,1-{radius}/(min(W,H)/2)),2)"
    angle = f"atan2(Y-H/2,X-W/2)-({radians:g})*({progress})*({falloff})"
    source_x = f"clip(W/2+{radius}*cos({angle}),0,W-1)"
    source_y = f"clip(H/2+{radius}*sin({angle}),0,H-1)"
    return (
        _video_distortion_prefix(size=VIDEO_SWIRL_SIZE, fps=VIDEO_SWIRL_FPS)
        + "format=rgb24,"
        f"geq=r='r({source_x},{source_y})':"
        f"g='g({source_x},{source_y})':"
        f"b='b({source_x},{source_y})'"
    )


def _video_distortion_prefix(
    *,
    size: int = VIDEO_DISTORTION_SIZE,
    fps: int = VIDEO_DISTORTION_FPS,
) -> str:
    return (
        f"scale=w='min({size},iw)':"
        f"h='min({size},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"fps={fps},"
    )


def _video_magik_filter(strength: float, speed: float | None) -> str:
    """Create a liquid warp while keeping video output as video."""
    if not 1 <= strength <= 80:
        raise ValueError("Magik strength must be between 1 and 80.")
    if speed is not None and not 0.25 <= speed <= 4:
        raise ValueError("Animation speed must be between 0.25 and 4.")
    amount = f"min(W,H)*{strength / 900:g}"
    phase = "0" if speed is None else f"2*PI*T*{speed:g}"
    source_x = f"clip(X+sin(Y/max(24,H/5)+({phase}))*({amount}),0,W-1)"
    source_y = f"clip(Y+sin(X/max(24,W/4)+({phase})+PI/2)*({amount})*0.35,0,H-1)"
    return (
        _video_distortion_prefix() + "format=rgb24,"
        f"geq=r='r({source_x},{source_y})':"
        f"g='g({source_x},{source_y})':"
        f"b='b({source_x},{source_y})'"
    )


def _video_lag_filter(options: dict[str, Any]) -> str:
    amount = int(options.get("amount", 4))
    if not 2 <= amount <= 12:
        raise ValueError("Lag amount must be between 2 and 12.")
    methods = ("freeze", "stutter", "drop", "jitter")
    method = str(options.get("method", "random")).casefold().replace("_", "-")
    if method not in {*methods, "random"}:
        raise ValueError("Lag method must be random, freeze, stutter, drop, or jitter.")
    multi = bool(options.get("multi"))
    rng = random.SystemRandom()
    selected: list[str] = (
        [method]
        if method != "random"
        else rng.sample(methods, rng.randint(2, 4) if multi else 1)
    )
    if multi and method != "random":
        extras = [candidate for candidate in methods if candidate != method]
        selected.extend(rng.sample(extras, rng.randint(1, min(2, len(extras)))))

    filters: list[str] = []
    for selected_method in selected:
        if selected_method == "freeze":
            filters.append(f"fps={max(2, round(30 / amount))}")
        elif selected_method == "stutter":
            frames = max(2, min(8, amount))
            weights = " ".join(["1"] + ["0"] * (frames - 1))
            filters.append(f"tmix=frames={frames}:weights='{weights}'")
        elif selected_method == "drop":
            chance = min(0.45, amount / 30)
            filters.append(
                f"select='gte(random(0),{chance:g})',setpts=N/(FRAME_RATE*TB)"
            )
        else:
            distance = max(2, amount)
            filters.append(
                f"crop=iw-{distance * 2}:ih-{distance * 2}:"
                f"x='{distance}+{distance}*sin(n*1.7)':"
                f"y='{distance}+{distance}*cos(n*1.3)',"
                f"scale=iw+{distance * 2}:ih+{distance * 2}"
            )
    return ",".join(filters)


def _video_filter(
    effect: str,
    options: dict[str, Any],
    *,
    duration: float = 0,
) -> tuple[str, str]:
    if effect == "invert":
        return "negate", "effect.mp4"
    if effect == "flip":
        return (
            "vflip" if options.get("direction") == "vertical" else "hflip",
            "effect.mp4",
        )
    if effect == "blur":
        radius = float(options.get("radius", 5))
        if not 0.1 <= radius <= 50:
            raise ValueError("Blur radius must be between 0.1 and 50.")
        blur_type = str(options.get("blur_type", "gaussian")).casefold()
        position = str(options.get("position", "")).strip()
        if position:
            return (
                _video_positioned_blur_filter(
                    radius,
                    blur_type=blur_type,
                    position=position,
                    shape=str(options.get("shape", "square")),
                ),
                "blur.mp4",
            )
        if blur_type in {"gaussian", "gauss"}:
            return f"gblur=sigma={radius:g}", "blur.mp4"
        if blur_type in {"box", "square"}:
            return f"boxblur=luma_radius={radius:g}", "blur.mp4"
        if blur_type in {"motion", "horizontal"}:
            size = max(1, min(51, round(radius) * 2 + 1))
            return f"avgblur=sizeX={size}:sizeY=1", "blur.mp4"
        raise ValueError("Blur type must be gaussian, box, or motion.")
    if effect == "deepfry":
        intensity = float(options.get("intensity", 1.0))
        if not 0.25 <= intensity <= 3:
            raise ValueError("Deepfry intensity must be between 0.25 and 3.")
        return (
            f"eq=saturation={1 + 2 * intensity:g}:contrast={1 + intensity:g},"
            f"unsharp=5:5:{1.5 * intensity:g}:5:5:0",
            "deepfry.mp4",
        )
    if effect == "grayscale":
        return "hue=s=0", "grayscale.mp4"
    if effect == "magik":
        strength = float(options.get("strength", 20))
        return _video_magik_filter(strength, None), "magik.mp4"
    if effect == "gifmagik":
        strength = float(options.get("strength", 20))
        speed = float(options.get("speed", 1))
        return _video_magik_filter(strength, speed), "amagik.mp4"
    if effect == "swirl":
        strength = float(options.get("strength", 180))
        if not -720 <= strength <= 720:
            raise ValueError("Swirl strength must be between -720 and 720 degrees.")
        return _video_swirl_filter(strength), "swirl.mp4"
    if effect == "gifswirl":
        strength = float(options.get("strength", 180))
        speed = float(options.get("speed", 1))
        if not -720 <= strength <= 720 or not 0.25 <= speed <= 4:
            raise ValueError("Swirl strength must be -720 to 720 and speed 0.25 to 4.")
        frame_total = max(1.0, duration * 30)
        progress = f"min(1,N/{frame_total:g})*{speed:g}"
        return _video_swirl_filter(strength, progress=progress), "aswirl.mp4"
    if effect in {"fadein", "fadeout"}:
        fade_duration = float(options.get("duration", 1))
        if not 0.1 <= fade_duration <= 10:
            raise ValueError("Fade duration must be between 0.1 and 10 seconds.")
        start = 0 if effect == "fadein" else max(0, duration - fade_duration)
        direction = "in" if effect == "fadein" else "out"
        return (
            f"fade=t={direction}:st={start:g}:d={fade_duration:g}",
            f"{effect}.mp4",
        )
    if effect == "lag":
        return _video_lag_filter(options), "lag.mp4"
    if effect == "shuffle":
        return "shuffleframes=2 0 3 1", "shuffle.mp4"
    if effect == "tint":
        color = _effect_color(options.get("color"))
        amount = float(options.get("amount", 0.35))
        if not 0 <= amount <= 1:
            raise ValueError("Tint amount must be between 0 and 1.")
        keep = 1 - amount
        return (
            "lutrgb="
            f"r='val*{keep:g}+{color[0]}*{amount:g}':"
            f"g='val*{keep:g}+{color[1]}*{amount:g}':"
            f"b='val*{keep:g}+{color[2]}*{amount:g}'",
            "tint.mp4",
        )
    if effect in {"implode", "explode", "fisheye"}:
        strength = float(options.get("strength", 0.5))
        if not 0 <= strength <= 1:
            raise ValueError("Strength must be between 0 and 1.")
        sign = -1 if effect == "implode" else 1
        if effect == "fisheye":
            sign = 1
            strength = max(strength, 0.1) * 0.75
        return (
            f"lenscorrection=k1={sign * strength:g}:k2={sign * strength * 0.25:g}",
            f"{effect}.mp4",
        )
    if effect == "sharpen":
        amount = float(options.get("amount", 2))
        if not 0 <= amount <= 5:
            raise ValueError("Sharpen amount must be between 0 and 5.")
        return f"unsharp=5:5:{amount:g}:5:5:0", "sharpen.mp4"
    if effect in {"legoify", "pixelate"}:
        size = int(options.get("size", 12))
        minimum, maximum = (3, 64) if effect == "legoify" else (2, 128)
        if not minimum <= size <= maximum:
            raise ValueError(
                f"{effect.title()} size must be between {minimum} and {maximum}."
            )
        return (
            f"scale=iw/{size}:ih/{size}:flags=area,"
            f"scale=iw*{size}:ih*{size}:flags=neighbor",
            f"{effect}.mp4",
        )
    if effect == "bounce":
        amount = float(options.get("amount", 20))
        speed = float(options.get("speed", 1))
        if not 1 <= amount <= 100 or not 0.25 <= speed <= 4:
            raise ValueError("Bounce amount must be 1 to 100 and speed 0.25 to 4.")
        return (
            "format=rgba,split[fg][bg];"
            "[bg]colorchannelmixer=aa=0[clear];"
            f"[clear][fg]overlay=x=0:y='{amount:g}*sin(2*PI*t*{speed:g})'",
            "bounce.mp4",
        )
    if effect == "sepia":
        amount = float(options.get("amount", 1))
        if not 0 <= amount <= 1:
            raise ValueError("Sepia amount must be between 0 and 1.")
        return (
            "colorchannelmixer=" ".393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
            "sepia.mp4",
        )
    if effect in {"slidein", "slideout"}:
        direction = str(options.get("direction", "left")).casefold()
        if direction not in {"left", "right", "up", "down"}:
            raise ValueError("Slide direction must be left, right, up, or down.")
        progress = (
            f"min(1,t/{max(0.1, float(options.get('duration', 1))):g})"
            if effect == "slidein"
            else f"max(0,1-t/{max(0.1, float(options.get('duration', 1))):g})"
        )
        x = "0"
        y = "0"
        if direction == "left":
            x = f"W*(1-({progress}))"
        elif direction == "right":
            x = f"-W*(1-({progress}))"
        elif direction == "up":
            y = f"H*(1-({progress}))"
        else:
            y = f"-H*(1-({progress}))"
        return (
            "format=rgba,split[fg][bg];"
            "[bg]colorchannelmixer=aa=0[clear];"
            f"[clear][fg]overlay=x='{x}':y='{y}'",
            f"{effect}.mp4",
        )
    if effect == "vignette":
        amount = float(options.get("amount", 0.65))
        if not 0 <= amount <= 1:
            raise ValueError("Vignette amount must be between 0 and 1.")
        return f"vignette=angle={math.pi / 2 * amount:g}", "vignette.mp4"
    if effect == "resize":
        scale = float(options.get("scale", 1))
        if not 0.1 <= scale <= 4:
            raise ValueError("Resize scale must be between 0.1 and 4.")
        filters: list[str] = []
        ratio = str(options.get("ratio", "")).strip()
        exact_size = _resize_dimensions(str(options.get("size", "")))
        if exact_size is not None:
            width, height = exact_size
            # H.264/yuv420p requires even dimensions. Only the video renderer
            # needs this one-pixel normalization; still images keep exact size.
            width = max(2, width - width % 2)
            height = max(2, height - height % 2)
            filters.append(f"scale={width}:{height}")
        elif ratio:
            separator = ":" if ":" in ratio else "/"
            try:
                ratio_width, ratio_height = (
                    float(part) for part in ratio.split(separator, 1)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("Ratio must look like 16:9 or 1:1.") from error
            if ratio_width <= 0 or ratio_height <= 0:
                raise ValueError("Ratio values must be positive.")
            ratio_value = ratio_width / ratio_height
            filters.append(
                "crop="
                f"'if(gt(a,{ratio_value:g}),ih*{ratio_value:g},iw)':"
                f"'if(gt(a,{ratio_value:g}),ih,iw/{ratio_value:g})'"
            )
        if scale != 1 or exact_size is None:
            filters.append(f"scale=trunc(iw*{scale:g}/2)*2:trunc(ih*{scale:g}/2)*2")
        return ",".join(filters), "resize.mp4"
    if effect == "distort":
        amount = float(options.get("amount", 0.25))
        if not -1 <= amount <= 1:
            raise ValueError("Distort amount must be between -1 and 1.")
        shift = abs(amount) * 0.45
        if amount >= 0:
            coordinates = (
                f"x0=0:y0=0:x1=W:y1=H*{shift:g}:" f"x2=0:y2=H:x3=W:y3=H*(1-{shift:g})"
            )
        else:
            coordinates = (
                f"x0=0:y0=H*{shift:g}:x1=W:y1=0:" f"x2=0:y2=H*(1-{shift:g}):x3=W:y3=H"
            )
        return f"perspective={coordinates}:sense=destination", "distort.mp4"
    if effect in {"grain", "noise"}:
        amount = float(options.get("amount", 20))
        if not 0 <= amount <= 100:
            raise ValueError("Noise amount must be between 0 and 100.")
        flags = "t" if effect == "grain" else "t+u"
        return f"noise=alls={amount:g}:allf={flags}", f"{effect}.mp4"
    if effect == "rotate":
        degrees = float(options.get("degrees", 90))
        if not -3600 <= degrees <= 3600:
            raise ValueError("Rotation must be between -3600 and 3600 degrees.")
        return (
            f"rotate={math.radians(-degrees):g}:"
            "ow='ceil(hypot(iw,ih)/2)*2':oh='ceil(hypot(iw,ih)/2)*2':c=black",
            "rotate.mp4",
        )
    if effect in {"brightness", "contrast", "saturation"}:
        amount = float(options.get("amount", 1))
        if not 0 <= amount <= 4:
            raise ValueError(f"{effect.title()} must be between 0 and 4.")
        option = {
            "brightness": "brightness",
            "contrast": "contrast",
            "saturation": "saturation",
        }[effect]
        value = (
            (amount - 1 if amount <= 1 else (amount - 1) / 3)
            if effect == "brightness"
            else amount
        )
        return f"eq={option}={value:g}", f"{effect}.mp4"
    if effect == "exposure":
        stops = float(options.get("stops", 0))
        if not -5 <= stops <= 5:
            raise ValueError("Exposure must be between -5 and 5 stops.")
        return f"eq=brightness={max(-1, min(1, 2**stops - 1)):g}", "exposure.mp4"
    if effect == "huerotate":
        degrees = float(options.get("degrees", 180))
        if not -3600 <= degrees <= 3600:
            raise ValueError("Hue rotation must be between -3600 and 3600 degrees.")
        return f"hue=h={degrees:g}", "huerotate.mp4"
    if effect == "zoom":
        amount = float(options.get("amount", 2))
        if not 1 <= amount <= 4:
            raise ValueError("Zoom must be between 1 and 4.")
        return (
            f"scale=iw*{amount:g}:ih*{amount:g},crop=iw/{amount:g}:ih/{amount:g}:"
            f"(iw-ow)/2+(iw-ow)*0.1*sin(2*PI*t):"
            f"(ih-oh)/2+(ih-oh)*0.1*cos(2*PI*t)",
            "zoom.mp4",
        )
    if effect in {"hallway", "parallax", "tremble", "glitch", "squishy", "quilt"}:
        if effect == "quilt":
            return "split=2[a][b];[a]hflip[af];[b][af]hstack", "quilt.mp4"
        if effect == "hallway":
            return (
                "scale=iw*1.25:ih*1.25,crop=iw/1.25:ih/1.25:(iw-ow)/2:(ih-oh)/2+30*sin(2*PI*t)",
                "hallway.mp4",
            )
        if effect == "parallax":
            return (
                "scale=iw*1.15:ih*1.15,crop=iw/1.15:ih/1.15:(iw-ow)/2+20*sin(2*PI*t):(ih-oh)/2+8*cos(2*PI*t)",
                "parallax.mp4",
            )
        if effect == "tremble":
            amount = max(1, min(30, round(float(options.get("amount", 8)))))
            return (
                f"crop=iw-2*{amount}:ih-2*{amount}:{amount}+{amount}*sin(40*PI*t):{amount}+{amount}*cos(37*PI*t),scale=iw:ih",
                "tremble.mp4",
            )
        if effect == "glitch":
            return "rgbashift=rh=3:rv=0:bh=-3:bv=0,noise=alls=8:allf=t+u", "glitch.mp4"
        return (
            "scale=iw:ih,geq=r='r(X+8*sin(2*PI*T),Y)':g='g(X,Y)':b='b(X-8*sin(2*PI*T),Y)'",
            "squishy.mp4",
        )
    if effect == "removebars":
        return "crop=iw:ih*0.8:0:ih*0.1", "remove-bars.mp4"
    if effect == "removecaption":
        return "crop=iw:ih*0.8:0:ih*0.1", "remove-caption.mp4"
    if effect == "enlarge":
        amount = float(options.get("amount", 2))
        if not 1 <= amount <= 4:
            raise ValueError("Enlarge must be between 1 and 4.")
        return (
            f"scale=trunc(iw*{amount:g}/2)*2:trunc(ih*{amount:g}/2)*2:flags=lanczos",
            "enlarge.mp4",
        )
    if effect == "falsecolor":
        return "hue=s=0,lutrgb=r='val*2':g='val*0.6':b='255-val'", "falsecolor.mp4"
    if effect == "watercolor":
        return "gblur=sigma=1.4,eq=saturation=1.5", "watercolor.mp4"
    if effect == "oilpaint":
        return (
            "convolution='0 0 0 0 1 0 0 0 0',eq=contrast=1.2:saturation=1.3",
            "oilpaint.mp4",
        )
    if effect == "meme":
        text = str(options.get("text", "")).strip().upper()
        if not text:
            raise ValueError("Meme requires text.")
        middle_text = ""
        if "|" in text:
            top_text, bottom_text = (part.strip() for part in text.split("|", 1))
        else:
            words = text.split()
            if len(words) < 6:
                top_text = bottom_text = ""
                middle_text = text
            else:
                midpoint = max(1, len(words) // 2)
                top_text = " ".join(words[:midpoint])
                bottom_text = " ".join(words[midpoint:])

        def quote_drawtext(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

        font_path = impact_font_path() or Path(
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
        )
        fontfile = (
            str(font_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        )
        filters: list[str] = []
        if top_text:
            filters.append(
                f"drawtext=fontfile={fontfile}:"
                f"text='{quote_drawtext(top_text)}':x=(w-text_w)/2:y=8:fontsize=min(w/8\\,96):"
                "fontcolor=white:borderw=3:bordercolor=black"
            )
        if bottom_text:
            filters.append(
                f"drawtext=fontfile={fontfile}:"
                f"text='{quote_drawtext(bottom_text)}':x=(w-text_w)/2:y=h-text_h-12:fontsize=min(w/8\\,96):"
                "fontcolor=white:borderw=3:bordercolor=black"
            )
        if middle_text:
            filters.append(
                f"drawtext=fontfile={fontfile}:"
                f"text='{quote_drawtext(middle_text)}':x=(w-text_w)/2:y=(h-text_h)/2:fontsize=min(w/8\\,96):"
                "fontcolor=white:borderw=3:bordercolor=black"
            )
        return ",".join(filters), "meme.mp4"
    if effect == "random":
        return _video_filter(
            random.choice(("glitch", "parallax", "huerotate")),
            options,
            duration=duration,
        )
    if effect == "spin":
        speed = float(options.get("speed", 1))
        direction = -1 if options.get("clockwise") else 1
        if not 0.25 <= speed <= 4:
            raise ValueError("Spin speed must be between 0.25 and 4.")
        return (
            f"rotate={direction}*2*PI*t*{speed:g}:"
            "ow='ceil(hypot(iw,ih)/2)*2':oh='ceil(hypot(iw,ih)/2)*2':c=none",
            "spin.mp4",
        )
    if effect == "wiggle":
        amount = float(options.get("amount", 8))
        speed = float(options.get("speed", 1))
        if not 1 <= amount <= 30 or not 0.25 <= speed <= 4:
            raise ValueError("Wiggle amount must be 1 to 30 and speed 0.25 to 4.")
        radians = amount * math.pi / 180
        return (
            f"rotate={radians:g}*sin(2*PI*t*{speed:g}):"
            "ow='ceil(hypot(iw,ih)/2)*2':oh='ceil(hypot(iw,ih)/2)*2':c=none",
            "wiggle.mp4",
        )
    if effect == "jpeg":
        quality = int(options.get("quality", 8))
        if not 1 <= quality <= 50:
            raise ValueError("JPEG quality must be between 1 and 50.")
        noise = max(4, 55 - quality)
        return (
            f"scale=iw/2:ih/2:flags=neighbor,scale=iw*2:ih*2:flags=neighbor,"
            f"noise=alls={noise}:allf=t",
            "needsmorejpeg.mp4",
        )
    if effect == "mirror":
        direction = str(options["direction"])
        if direction == "left":
            return (
                "crop=iw/2:ih:0:0,split[l][r0];[r0]hflip[r];[l][r]hstack",
                "mirror-left.mp4",
            )
        if direction == "right":
            return (
                "crop=iw/2:ih:iw/2:0,split[r][l0];[l0]hflip[l];[l][r]hstack",
                "mirror-right.mp4",
            )
        if direction == "top":
            return (
                "crop=iw:ih/2:0:0,split[t][b0];[b0]vflip[b];[t][b]vstack",
                "mirror-top.mp4",
            )
        return (
            "crop=iw:ih/2:0:ih/2,split[b][t0];[t0]vflip[t];[t][b]vstack",
            "mirror-bottom.mp4",
        )
    raise ValueError("That effect currently supports images and GIFs, not video.")


def _video_positioned_blur_filter(
    radius: float,
    *,
    blur_type: str,
    position: str,
    shape: str,
) -> str:
    """Blur a bounded shape at a requested video position."""
    shape = shape.casefold().strip()
    if shape not in {"square", "rectangle", "circle", "triangle"}:
        raise ValueError("Blur shape must be square, circle, or triangle.")
    if blur_type in {"gaussian", "gauss"}:
        blur = f"gblur=sigma={radius:g}"
    elif blur_type in {"box", "square"}:
        blur = f"boxblur=luma_radius={radius:g}"
    elif blur_type in {"motion", "horizontal"}:
        blur = f"avgblur=sizeX={max(1, min(51, round(radius) * 2 + 1))}:sizeY=1"
    else:
        raise ValueError("Blur type must be gaussian, box, or motion.")

    normalized = position.casefold().replace(" ", "")
    region_width = "iw/3"
    region_height = "ih/3"
    named = {
        "center": ("(iw-iw/3)/2", "(ih-ih/3)/2"),
        "top-left": ("0", "0"),
        "top": ("(iw-iw/3)/2", "0"),
        "top-right": ("iw-iw/3", "0"),
        "left": ("0", "(ih-ih/3)/2"),
        "right": ("iw-iw/3", "(ih-ih/3)/2"),
        "bottom-left": ("0", "ih-ih/3"),
        "bottom": ("(iw-iw/3)/2", "ih-ih/3"),
        "bottom-right": ("iw-iw/3", "ih-ih/3"),
    }
    if normalized in named:
        x, y = named[normalized]
    else:
        parts = position.split(",", 1)
        if len(parts) != 2:
            raise ValueError(
                "Blur position must be x,y coordinates or a named position."
            )
        try:
            raw_x = float(parts[0].strip())
            raw_y = float(parts[1].strip())
        except ValueError as error:
            raise ValueError("Blur position must be x,y coordinates.") from error
        x = f"iw*{raw_x:g}-iw/6" if 0 <= raw_x <= 1 else f"{raw_x:g}-iw/6"
        y = f"ih*{raw_y:g}-ih/6" if 0 <= raw_y <= 1 else f"{raw_y:g}-ih/6"

    mask = ""
    if shape == "circle":
        expression = "lte(pow((X-W/2)/(W/2),2)+pow((Y-H/2)/(H/2),2),1)"
        mask = f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if({expression},255,0)'"
    elif shape == "triangle":
        expression = "gte(Y,abs(2*X-W)*H/W)"
        mask = f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if({expression},255,0)'"
    overlay_x = x.replace("iw", "main_w").replace("ih", "main_h")
    overlay_y = y.replace("iw", "main_w").replace("ih", "main_h")
    return (
        "split[blur_base][blur_source];"
        f"[blur_source]{blur},crop={region_width}:{region_height}:{x}:{y},"
        f"format=rgba{mask}[blur_region];"
        f"[blur_base][blur_region]overlay=x={overlay_x}:y={overlay_y}:shortest=1"
    )


def _visual_effect_window(
    options: dict[str, Any],
    total_duration: float,
) -> tuple[float, float | None]:
    start = float(options.get("start", 0) or 0)
    stop = float(options.get("stop", 0) or 0)
    media_limit = (
        min(MAX_MEDIA_DURATION, total_duration)
        if total_duration > 0
        else MAX_MEDIA_DURATION
    )
    if not 0 <= start <= media_limit:
        raise ValueError(f"Effect start must be between 0 and {media_limit:g} seconds.")
    if not 0 <= stop <= media_limit:
        raise ValueError(f"Effect stop must be between 0 and {media_limit:g} seconds.")
    end: float | None = stop or None
    if end is not None and end <= start:
        raise ValueError("Effect stop must be after its start time.")
    if total_duration > 0:
        if start >= total_duration:
            raise ValueError("Effect start must be before the end of the media.")
        if end is not None:
            end = min(end, total_duration)
    return start, end


def _timed_visual_filter(
    filter_value: str,
    *,
    start: float,
    end: float | None,
    duration: float,
    width: int,
    height: int,
) -> str:
    width = max(2, width - width % 2)
    height = max(2, height - height % 2)
    segments: list[tuple[float, float | None, bool]] = []
    if start > 0:
        segments.append((0.0, start, False))
    segments.append((start, end, True))
    if end is not None and (duration <= 0 or end < duration - 0.001):
        segments.append((end, None, False))

    inputs = "".join(f"[timed{index}]" for index in range(len(segments)))
    filters = [f"[0:v]split={len(segments)}{inputs}"]
    outputs: list[str] = []
    for index, (segment_start, segment_end, changed) in enumerate(segments):
        trim = f"trim=start={segment_start:g}"
        if segment_end is not None:
            trim += f":end={segment_end:g}"
        chain = f"[timed{index}]{trim},setpts=PTS-STARTPTS"
        if changed:
            chain += f",{filter_value}"
        # Geometry-changing effects still need to concatenate with the
        # untouched sections. Normalize every segment back to the source size.
        chain += (
            f",scale={width}:{height}:" "force_original_aspect_ratio=disable,setsar=1"
        )
        filters.append(f"{chain}[segment{index}]")
        outputs.append(f"[segment{index}]")
    filters.append(f"{''.join(outputs)}concat=n={len(outputs)}:v=1:a=0[v]")
    return ";".join(filters)


def _render_video_overlay_sync(
    input_data: bytes,
    second_data: bytes,
    *,
    allow_image_base: bool,
    **options: Any,
) -> EffectResult:
    with tempfile.TemporaryDirectory(prefix="fishie-overlay-") as directory:
        input_path = os.path.join(directory, "input.media")
        second_path = os.path.join(directory, "overlay.media")
        Path(input_path).write_bytes(input_data)
        Path(second_path).write_bytes(second_data)

        try:
            _, _, base_animated = _load_image_frames(input_data)
        except NotPillowMedia:
            base_is_image = False
            base_animated = False
        else:
            base_is_image = True
        try:
            _load_image_frames(second_data)
        except NotPillowMedia:
            second_is_image = False
        else:
            second_is_image = True

        base_probe = _probe_path(input_path)
        second_probe = _probe_path(second_path)
        if not base_is_image:
            input_path = _repair_h264_color_metadata(
                input_path,
                directory,
                name="input",
            )
        if not second_is_image:
            second_path = _repair_h264_color_metadata(
                second_path,
                directory,
                name="overlay",
            )
        if not second_probe.has_video:
            raise ValueError("The overlay must contain an image, GIF, or video.")
        if base_is_image and not allow_image_base:
            raise ValueError("The first input has to be a video.")

        if base_is_image:
            target_duration = second_probe.duration
            if target_duration <= 0:
                raise ValueError("A video or animated overlay is required.")
        else:
            if not base_probe.has_video:
                raise ValueError("The first input has to contain video.")
            target_duration = base_probe.duration
            if target_duration <= 0:
                raise ValueError("The first video has no usable duration.")
            if bool(options.get("extend")) and second_probe.duration > target_duration:
                target_duration = second_probe.duration

        start, end = _visual_effect_window(options, target_duration)
        opacity = float(options.get("opacity", 0.7))
        if not 0 <= opacity <= 1:
            raise ValueError("Opacity must be between 0 and 1.")
        overlay_width, overlay_height = _overlay_dimensions(
            options,
            base_width=base_probe.width,
            base_height=base_probe.height,
            overlay_width=second_probe.width,
            overlay_height=second_probe.height,
        )
        position = str(options.get("position", "center")).casefold().replace("_", "-")
        position_map = {
            "center": ("(W-w)/2", "(H-h)/2"),
            "top-left": ("0", "0"),
            "top": ("(W-w)/2", "0"),
            "top-right": ("W-w", "0"),
            "left": ("0", "(H-h)/2"),
            "right": ("W-w", "(H-h)/2"),
            "bottom-left": ("0", "H-h"),
            "bottom": ("(W-w)/2", "H-h"),
            "bottom-right": ("W-w", "H-h"),
        }
        if position not in position_map:
            raise ValueError(
                "Overlay position must be center, top, bottom, left, right, "
                "or a corner such as top-left."
            )
        x_expression, y_expression = position_map[position]
        x_expression = f"({x_expression})+{int(options.get('x', 0))}"
        y_expression = f"({y_expression})+{int(options.get('y', 0))}"

        command = ["ffmpeg", "-y"]
        if base_is_image:
            command.extend(["-stream_loop", "-1"] if base_animated else ["-loop", "1"])
        elif (
            bool(options.get("extend")) and second_probe.duration > base_probe.duration
        ):
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", input_path])

        second_needs_loop = second_is_image or second_probe.duration < target_duration
        if second_needs_loop:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", second_path])

        filter_complex = (
            f"[0:v]setpts=PTS-STARTPTS[base];"
            f"[1:v]setpts=PTS-STARTPTS,scale={overlay_width}:{overlay_height}:"
            "flags=lanczos,format=rgba,colorchannelmixer="
            f"aa={opacity:g}[over];"
            f"[base][over]overlay=x='{x_expression}':y='{y_expression}':"
            "eof_action=repeat:shortest=0"
        )
        if start > 0 or end is not None:
            enable = (
                f"between(t,{start:g},{end:g})"
                if end is not None
                else f"gte(t,{start:g})"
            )
            filter_complex += f":enable='{enable}'"
        # libx264 with yuv420p requires even output dimensions. Avatar images
        # often arrive at an odd size (for example 435x435), so trim at most
        # one pixel after the timed overlay filter is finalized. Keeping this
        # as a separate filter also ensures a timed overlay's ``enable`` is
        # applied to the overlay filter, not to ``scale``.
        filter_complex += ",scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos[v]"

        command.extend(["-filter_complex", filter_complex, "-map", "[v]"])
        overlay_audio = bool(options.get("overlay_audio", True))
        audio_mapped = False
        if overlay_audio and base_probe.has_audio and second_probe.has_audio:
            filter_complex += (
                ";[0:a:0][1:a:0]amix=inputs=2:duration=longest:"
                "dropout_transition=2[a]"
            )
            command[command.index("-filter_complex") + 1] = filter_complex
            command.extend(["-map", "[a]"])
            audio_mapped = True
        elif overlay_audio and second_probe.has_audio and not base_probe.has_audio:
            command.extend(["-map", "1:a:0"])
            audio_mapped = True
        elif base_probe.has_audio:
            command.extend(["-map", "0:a:0"])
            audio_mapped = True

        output_path = os.path.join(directory, "overlay-video.mp4")
        command.extend(
            [
                "-t",
                f"{target_duration:g}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "24",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if audio_mapped:
            command.extend(["-c:a", "aac", "-b:a", "96k"])
        else:
            command.append("-an")
        command.extend(["-movflags", "+faststart", output_path])
        _run(command)
        return EffectResult(Path(output_path).read_bytes(), "overlay-video.mp4")


def _render_video_visual(
    data: bytes, effect: str, options: dict[str, Any]
) -> EffectResult:
    if effect in {"magik", "swirl", "gifmagik", "gifswirl"}:
        raise ValueError(
            "Magik and swirl only support still images, not videos or animated media."
        )
    if effect == "overlay":
        overlay_data = options.get("overlay_data")
        if not isinstance(overlay_data, bytes):
            raise ValueError("A second image is required.")
        return render_video_effect_sync(
            data,
            "overlay",
            second_data=overlay_data,
            opacity=float(options.get("opacity", 0.5)),
            scale=float(options.get("scale", 1.0)),
            stretch=bool(options.get("stretch")),
            position=str(options.get("position", "center")),
            x=int(options.get("x", 0)),
            y=int(options.get("y", 0)),
        )

    with tempfile.TemporaryDirectory(prefix="fishie-image-effect-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, "output.mp4")
        Path(input_path).write_bytes(data)
        probe = _probe_path(input_path)
        if not probe.has_video:
            raise ValueError("That effect requires an image, GIF, or video.")
        if effect in {"removebars", "removecaption"}:
            preview_path = os.path.join(directory, "crop-preview.png")
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    input_path,
                    "-frames:v",
                    "1",
                    preview_path,
                ]
            )
            with Image.open(preview_path) as preview:
                crop_box = _edge_crop_box(
                    preview,
                    caption=effect == "removecaption",
                )
                preview_height = preview.height
            if crop_box is None:
                filter_value = "null"
            else:
                left, top, right, bottom = crop_box
                if effect == "removebars":
                    # Keep a small safety edge around encoded video content.
                    # Letterbox boundaries often contain a couple of blended
                    # rows and subtitles can extend slightly into the lower
                    # bar. This matches the visible crop instead of cutting
                    # those pixels away.
                    content_height = bottom - top
                    top = min(bottom - 2, top + (2 if top else 0))
                    bottom = min(
                        preview_height,
                        bottom + round(content_height * 0.04),
                    )
                width = right - left
                height = bottom - top
                # H.264 requires even crop geometry.
                left -= left % 2
                top -= top % 2
                width -= width % 2
                height -= height % 2
                filter_value = f"crop={width}:{height}:{left}:{top}"
            filename = (
                "remove-caption.mp4" if effect == "removecaption" else "remove-bars.mp4"
            )
        elif effect == "crop":
            shape = options.get("shape")
            if shape == "circle":
                inside = "lte(pow((X-W/2)/(W/2),2)+pow((Y-H/2)/(H/2),2),1)"
                filename = "crop-circle.mp4"
            elif shape == "triangle":
                inside = "gte(Y,abs(2*X-W)*H/W)"
                filename = "crop-triangle.mp4"
            else:
                raise ValueError("Unknown crop shape.")

            def channel(plane: str) -> str:
                return f"if({inside},{plane}(X,Y),0)"

            filter_value = (
                f"geq=r='{channel('r')}':g='{channel('g')}':b='{channel('b')}'"
            )
        else:
            filter_value, filename = _video_filter(
                effect,
                options,
                duration=probe.duration,
            )

        start, end = _visual_effect_window(options, probe.duration)
        reaches_end = end is None or (
            probe.duration > 0 and end >= probe.duration - 0.001
        )
        if start <= 0 and reaches_end:
            filter_complex = f"[0:v]{filter_value}[v]"
        else:
            filter_complex = _timed_visual_filter(
                filter_value,
                start=start,
                end=end,
                duration=probe.duration,
                width=probe.width,
                height=probe.height,
            )

        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
        ]
        if probe.has_audio and effect == "lag":
            amount = int(options.get("amount", 4))
            method = str(options.get("method", "random")).casefold()
            if method == "random":
                method = random.SystemRandom().choice(
                    ("freeze", "stutter", "drop", "jitter")
                )
            period = max(0.08, amount * 0.04)
            audio_filters = {
                "freeze": (
                    "volume="
                    f"'if(lt(mod(t,{period * 3:g}),{period:g}),0.15,1)'"
                    ":eval=frame"
                ),
                "stutter": (
                    f"aecho=0.8:0.6:{max(20, amount * 12)}:"
                    f"{min(0.75, amount / 16):g}"
                ),
                "drop": (
                    "volume="
                    f"'if(lt(mod(t,{period:g}),{period / 3:g}),0,1)'"
                    ":eval=frame"
                ),
                "jitter": f"tremolo=f={max(2, amount / 2):g}:d=0.7",
            }
            command.extend(["-af", audio_filters[method]])
        elif probe.has_audio and effect in {"fadein", "fadeout"}:
            fade_duration = float(options.get("duration", 1))
            start = 0 if effect == "fadein" else max(0, probe.duration - fade_duration)
            command.extend(
                [
                    "-af",
                    f"afade=t={'in' if effect == 'fadein' else 'out'}:"
                    f"st={start:g}:d={fade_duration:g}",
                ]
            )
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )
        _run(command)
        return EffectResult(Path(output_path).read_bytes(), filename)


def render_image_effect_sync(
    data: bytes,
    effect: str,
    **options: Any,
) -> EffectResult:
    try:
        frames, durations, animated = _load_image_frames(
            data,
            preserve_transparency=bool(options.get("preserve_transparency")),
        )
    except NotPillowMedia:
        if effect in {"magik", "swirl", "gifmagik", "gifswirl"}:
            raise ValueError(
                "Magik and swirl only support still images, not videos or animated media."
            )
        return _render_video_visual(data, effect, options)

    if effect in {"magik", "swirl", "gifmagik", "gifswirl"} and animated:
        raise ValueError(
            "Magik and swirl only support still images, not videos or animated media."
        )

    if effect in {"removebars", "removecaption"}:
        crop_box = _stable_edge_crop_box(
            frames,
            caption=effect == "removecaption",
        )
        transformed = [
            (
                frame.convert("RGBA").crop(crop_box)
                if crop_box is not None
                else frame.convert("RGBA")
            )
            for frame in frames
        ]
        return _save_frames(transformed, durations, filename=effect)

    if effect == "averagecolors":
        # A palette describes the supplied image as a whole. For an animated
        # input use its first frame so the command remains a quick PNG result.
        return _save_frames([_average_colors_image(frames[0])], [1000], filename=effect)

    if effect in STATIC_EFFECTS:
        transformed = [STATIC_EFFECTS[effect](frame, options) for frame in frames]
        jpeg_quality = int(options.get("quality", 8)) if effect == "jpeg" else None
        return _save_frames(
            transformed,
            durations,
            filename=effect,
            jpeg_quality=jpeg_quality if not animated else None,
        )
    if effect == "random":
        candidates = (
            "huerotate",
            "falsecolor",
            "watercolor",
            "oilpaint",
            "glitch",
            "parallax",
        )
        return render_image_effect_sync(data, random.choice(candidates), **options)
    if effect in {
        "hallway",
        "parallax",
        "zoom",
        "squishy",
        "glitch",
        "tremble",
        "quilt",
        "huerotate",
    }:
        generated, generated_durations = _animated_visual_frames(
            frames,
            durations,
            effect,
            options,
        )
        return _save_frames(
            generated,
            generated_durations,
            filename=effect,
            per_frame_palette=effect in {"hallway", "zoom"},
        )
    if effect == "magik":
        strength = float(options.get("strength", 20))
        effect_frames, effect_durations = _sample_heavy_animation(
            frames,
            durations,
            max_frames=MAGIK_MAX_GIF_FRAMES,
        )
        working_size = MAGIK_GIF_WORKING_SIZE if animated else MAGIK_WORKING_SIZE
        if animated:
            transformed = _liquid_rescale_frames(
                effect_frames,
                strength,
                working_size=working_size,
            )
        else:
            transformed = [
                _liquid_rescale(
                    frame,
                    strength,
                    working_size=working_size,
                )
                for frame in effect_frames
            ]
        return _save_frames(transformed, effect_durations, filename="magik")
    if effect == "swirl":
        strength = float(options.get("strength", 180))
        effect_frames, effect_durations = _sample_heavy_animation(frames, durations)
        transformed = [_swirl_frame(frame, strength) for frame in effect_frames]
        return _save_frames(transformed, effect_durations, filename="swirl")
    if effect in {"fadein", "fadeout"}:
        generated, generated_durations = _fade_frames(
            frames,
            durations,
            fade_in=effect == "fadein",
            duration=float(options.get("duration", 2)),
        )
        return _save_frames(generated, generated_durations, filename=effect)
    if effect == "lag":
        generated, generated_durations = _lag_frames(
            frames,
            durations,
            amount=int(options.get("amount", 4)),
            method=str(options.get("method", "random")),
            multi=bool(options.get("multi")),
        )
        return _save_frames(generated, generated_durations, filename="lag")
    if effect == "shuffle":
        generated, generated_durations = _shuffle_frames(frames, durations)
        return _save_frames(generated, generated_durations, filename="shuffle")
    if effect == "bounce":
        generated, generated_durations = _bounce_frames(
            frames,
            durations,
            amount=float(options.get("amount", 20)),
            speed=float(options.get("speed", 1)),
        )
        return _save_frames(generated, generated_durations, filename="bounce")
    if effect in {"slidein", "slideout"}:
        generated, generated_durations = _slide_frames(
            frames,
            durations,
            slide_in=effect == "slidein",
            direction=str(options.get("direction", "left")).casefold(),
            duration=float(options.get("duration", 1)),
        )
        return _save_frames(generated, generated_durations, filename=effect)
    if effect == "spin":
        generated, generated_durations = _spin_frames(
            frames,
            speed=float(options.get("speed", 1)),
            clockwise=bool(options.get("clockwise")),
        )
        return _save_frames(generated, generated_durations, filename="spin")
    if effect == "wiggle":
        generated, generated_durations = _wiggle_frames(
            frames,
            amount=float(options.get("amount", 8)),
            speed=float(options.get("speed", 1)),
        )
        return _save_frames(generated, generated_durations, filename="wiggle")
    if effect in {"gifmagik", "gifswirl"}:
        generated, generated_durations = _animated_distortion(
            frames,
            kind="magik" if effect == "gifmagik" else "swirl",
            strength=float(
                options.get("strength", 20 if effect == "gifmagik" else 180)
            ),
            speed=float(options.get("speed", 1)),
        )
        filename = "amagik" if effect == "gifmagik" else "aswirl"
        return _save_frames(generated, generated_durations, filename=filename)
    if effect in {"cube", "pyramid"}:
        generated, generated_durations = _shape_frames(
            frames,
            shape=cast(Literal["cube", "pyramid"], effect),
            speed=float(options.get("speed", 1)),
            clockwise=bool(options.get("clockwise")),
            axis=str(options.get("axis", "y")),
            rotation=float(options.get("rotation", 0)),
        )
        return _save_frames(generated, generated_durations, filename=effect)
    raise ValueError("Unknown image effect.")


def render_overlay_effect_sync(
    input_data: bytes,
    second_data: bytes,
    **options: Any,
) -> EffectResult:
    try:
        _load_image_frames(input_data)
    except NotPillowMedia:
        base_is_image = False
    else:
        base_is_image = True
    try:
        _, _, second_animated = _load_image_frames(second_data)
    except NotPillowMedia:
        second_is_image = False
        second_animated = False
    else:
        second_is_image = True

    if base_is_image and second_is_image and not second_animated:
        return render_image_effect_sync(
            input_data,
            "overlay",
            overlay_data=second_data,
            **options,
        )
    return _render_video_overlay_sync(
        input_data,
        second_data,
        allow_image_base=True,
        **options,
    )


render_image_effect = to_thread(render_image_effect_sync)
render_overlay_effect = to_thread(render_overlay_effect_sync)


PRIDE_FLAGS: dict[str, tuple[str, ...]] = {
    "pride": ("#e40303", "#ff8c00", "#ffed00", "#008026", "#24408e", "#732982"),
    "rainbow": ("#e40303", "#ff8c00", "#ffed00", "#008026", "#24408e", "#732982"),
    "gay": ("#e40303", "#ff8c00", "#ffed00", "#008026", "#24408e", "#732982"),
    "trans": ("#5bcffa", "#f5abb9", "#ffffff", "#f5abb9", "#5bcffa"),
    "bisexual": ("#d60270", "#d60270", "#9b4f96", "#0038a8", "#0038a8"),
    "bi": ("#d60270", "#d60270", "#9b4f96", "#0038a8", "#0038a8"),
    "pan": ("#ff218c", "#ffd800", "#21b1ff"),
    "pansexual": ("#ff218c", "#ffd800", "#21b1ff"),
    "lesbian": (
        "#d52d00",
        "#ef7627",
        "#ff9a56",
        "#ffffff",
        "#d162a4",
        "#b55690",
        "#a30262",
    ),
    "nonbinary": ("#fff430", "#ffffff", "#9c59d1", "#000000"),
    "nb": ("#fff430", "#ffffff", "#9c59d1", "#000000"),
    "asexual": ("#000000", "#a3a3a3", "#ffffff", "#800080"),
    "ace": ("#000000", "#a3a3a3", "#ffffff", "#800080"),
    "aromantic": ("#3da542", "#a7d379", "#ffffff", "#a9a9a9", "#000000"),
    "aro": ("#3da542", "#a7d379", "#ffffff", "#a9a9a9", "#000000"),
    "genderfluid": ("#ff76a4", "#ffffff", "#c011d7", "#000000", "#2f3cbe"),
    "genderqueer": ("#b57edc", "#ffffff", "#4a8123"),
}


def make_flag_asset(name: str, width: int = 640, height: int = 384) -> bytes | None:
    normalized = name.casefold().strip().replace(" ", "").replace("-", "")
    colors = PRIDE_FLAGS.get(normalized)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if colors:
        stripe = height / len(colors)
        for index, color in enumerate(colors):
            draw.rectangle(
                (0, round(index * stripe), width, round((index + 1) * stripe)),
                fill=color,
            )
    elif normalized in {"pirate", "jollyroger", "skull"}:
        draw.rectangle((0, 0, width, height), fill="#050505")
        center_x, center_y = width // 2, height // 2 - 20
        draw.ellipse(
            (center_x - 80, center_y - 80, center_x + 80, center_y + 75),
            fill="white",
        )
        draw.rectangle(
            (center_x - 55, center_y + 35, center_x + 55, center_y + 105),
            fill="white",
        )
        draw.ellipse(
            (center_x - 48, center_y - 25, center_x - 10, center_y + 15),
            fill="black",
        )
        draw.ellipse(
            (center_x + 10, center_y - 25, center_x + 48, center_y + 15),
            fill="black",
        )
        draw.line((170, 310, 470, 90), fill="white", width=28)
        draw.line((170, 90, 470, 310), fill="white", width=28)
    else:
        return None

    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _video_command_output(
    input_data: bytes,
    *,
    filename: str,
    filter_complex: str | None = None,
    video_filter: str | None = None,
    audio_filter: str | None = None,
    second_data: bytes | None = None,
    second_loop: bool = False,
    additional_inputs: Sequence[tuple[bytes, bool]] | None = None,
    output_extension: str = "mp4",
    map_arguments: list[str] | None = None,
    shortest: bool = False,
    output_duration: float | None = None,
    video_crf: int = 22,
    video_bitrate: int | None = None,
    video_preset: str = "fast",
    audio_bitrate: str = "128k",
    copy_video: bool = False,
    media_probe: MediaProbe | None = None,
) -> EffectResult:
    with tempfile.TemporaryDirectory(prefix="fishie-video-effect-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, f"output.{output_extension}")
        Path(input_path).write_bytes(input_data)
        probe = media_probe or _probe_path(input_path)
        command = ["ffmpeg", "-y", "-i", input_path]
        extra_inputs = list(additional_inputs or ())
        if second_data is not None:
            extra_inputs.insert(0, (second_data, second_loop))
        for index, (extra_data, should_loop) in enumerate(extra_inputs, start=1):
            extra_path = os.path.join(directory, f"input-{index}.media")
            Path(extra_path).write_bytes(extra_data)
            if should_loop:
                command.extend(["-stream_loop", "-1"])
            command.extend(["-i", extra_path])
        if filter_complex:
            command.extend(["-filter_complex", filter_complex])
        if video_filter:
            command.extend(["-vf", video_filter])
        if audio_filter:
            command.extend(["-af", audio_filter])
        if map_arguments:
            command.extend(map_arguments)
        if shortest:
            command.append("-shortest")
        if output_duration is not None and output_duration > 0:
            command.extend(["-t", f"{output_duration:g}"])

        if output_extension == "mp4":
            if probe.has_video:
                if copy_video:
                    if video_filter:
                        raise ValueError(
                            "Video stream copying cannot be used with a video filter."
                        )
                    command.extend(["-c:v", "copy"])
                else:
                    command.extend(["-c:v", "libx264", "-preset", video_preset])
                    if video_bitrate is not None:
                        command.extend(
                            [
                                "-b:v",
                                str(video_bitrate),
                                "-maxrate",
                                str(round(video_bitrate * 1.15)),
                                "-bufsize",
                                str(video_bitrate * 2),
                            ]
                        )
                    else:
                        command.extend(["-crf", str(video_crf)])
                    command.extend(["-pix_fmt", "yuv420p"])
            if probe.has_audio or extra_inputs:
                command.extend(["-c:a", "aac", "-b:a", audio_bitrate])
            command.extend(["-movflags", "+faststart"])
        elif output_extension == "webm":
            if probe.has_video:
                if video_bitrate is not None:
                    command.extend(["-c:v", "libvpx-vp9", "-b:v", str(video_bitrate)])
                else:
                    command.extend(["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"])
            if probe.has_audio or extra_inputs:
                command.extend(["-c:a", "libopus", "-b:a", "96k"])
        elif output_extension == "mp3":
            command.extend(["-vn", "-c:a", "libmp3lame", "-q:a", "2"])
        elif output_extension == "wav":
            command.extend(["-vn", "-c:a", "pcm_s16le"])
        elif output_extension == "ogg":
            command.extend(["-vn", "-c:a", "libvorbis", "-q:a", "5"])
        elif output_extension == "gif":
            command.extend(["-an"])

        command.append(output_path)
        _run(command)
        return EffectResult(
            Path(output_path).read_bytes(),
            f"{filename}.{output_extension}",
            displayable=output_extension in {"mp4", "webm", "gif"},
        )


def _fit_video_dimensions(
    probe: MediaProbe,
    max_dimension: int | None,
) -> tuple[int, int] | None:
    if max_dimension is None or not probe.has_video:
        return None
    largest = max(probe.width, probe.height)
    if largest <= max_dimension:
        return None
    scale = max_dimension / largest
    width = max(2, int(probe.width * scale) // 2 * 2)
    height = max(2, int(probe.height * scale) // 2 * 2)
    return width, height


def compress_media_to_size_sync(
    data: bytes,
    filename: str,
    max_bytes: int,
) -> EffectResult:
    """Fit an oversized video with a duration-based bitrate in at most two passes."""
    extension = Path(filename).suffix.casefold().lstrip(".")
    if max_bytes <= 0 or extension not in {"mp4", "webm"}:
        return EffectResult(data, filename, displayable=True)

    probe = probe_media_sync(data)
    if not probe.has_video:
        return EffectResult(data, filename, displayable=True)

    if probe.duration <= 0:
        return EffectResult(data, filename, displayable=True)

    # Reserve space for AAC/Opus, muxing overhead, and Discord's multipart
    # boundary. Starting below the mathematical maximum makes one pass enough
    # for most inputs rather than trying a sequence of full CRF encodes.
    target_bytes = max(1, round(max_bytes * 0.96))
    total_bitrate = target_bytes * 8 / probe.duration
    audio_bitrate = (
        min(96_000, max(24_000, round(total_bitrate * 0.15))) if probe.has_audio else 0
    )
    video_bitrate = max(24_000, round((total_bitrate - audio_bitrate) * 0.9))

    def dimension_for_bitrate(bitrate: int) -> int | None:
        if bitrate >= 2_500_000:
            return None
        if bitrate >= 1_200_000:
            return 1080
        if bitrate >= 700_000:
            return 720
        if bitrate >= 350_000:
            return 480
        return 360

    original = EffectResult(data, filename, displayable=True)
    current = original
    smallest = original
    for attempt in range(2):
        max_dimension = dimension_for_bitrate(video_bitrate)
        if attempt:
            max_dimension = min(max_dimension or 1440, 720)
        dimensions = _fit_video_dimensions(probe, max_dimension)
        video_filter = (
            f"scale={dimensions[0]}:{dimensions[1]}:flags=lanczos"
            if dimensions is not None
            else None
        )
        output_extension = extension if attempt == 0 else "mp4"
        try:
            current = _video_command_output(
                data,
                filename=Path(filename).stem,
                video_filter=video_filter,
                output_extension=output_extension,
                video_bitrate=video_bitrate,
                video_preset="fast",
                audio_bitrate=f"{max(24, audio_bitrate // 1_000)}k",
                media_probe=probe,
            )
        except ValueError:
            continue
        if len(current.data) < len(smallest.data):
            smallest = current
        if len(current.data) <= target_bytes:
            return current
        # Correct the second target using the actual first-pass result while
        # leaving another safety margin for muxing variance.
        video_bitrate = max(
            64_000,
            round(video_bitrate * target_bytes / len(current.data) * 0.9),
        )
    return smallest


compress_media_to_size = to_thread(compress_media_to_size_sync)


def _text_effect_color(
    value: str,
    *,
    default: str,
    opacity: int = 255,
) -> tuple[int, int, int, int]:
    try:
        parsed = ImageColor.getrgb(value or default)
    except ValueError as error:
        raise ValueError(f"`{value}` is not a valid color.") from error
    red, green, blue = parsed[:3]
    return red, green, blue, max(0, min(255, opacity))


def _text_overlay(
    frame: Image.Image,
    options: dict[str, Any],
    *,
    timestamp_ms: int = 0,
) -> Image.Image:
    text = str(options.get("text", "")).replace("\\n", "\n").strip()
    if not text:
        raise ValueError("Text cannot be empty.")
    if len(text) > 1000:
        raise ValueError("Text cannot exceed 1,000 characters.")
    size = int(options.get("size", max(16, round(min(frame.size) * 0.1))) or 0)
    if not 8 <= size <= 512:
        raise ValueError("Text size must be between 8 and 512.")
    font = load_effect_font(
        str(options.get("font", "Roboto")),
        size,
        bold=bool(options.get("bold")),
    )
    assets = cast(dict[str, bytes], options.get("inline_images") or {})
    padding = max(0, min(256, int(options.get("padding", max(4, size // 5)))))
    max_width = max(1, frame.width - padding * 2)
    lines = []
    for paragraph in text.splitlines() or [""]:
        lines.extend(wrap_inline_text(paragraph or " ", font, size, assets, max_width))
    line_box = font.getbbox("Ag")
    line_height = max(size, round(line_box[3] - line_box[1]))
    gap = max(1, round(size * 0.18))
    widths = [round(measure_inline_tokens(line, font, size, assets)) for line in lines]
    content_width = min(max_width, max(widths, default=1))
    content_height = len(lines) * line_height + max(0, len(lines) - 1) * gap
    box_width = min(frame.width, content_width + padding * 2)
    box_height = min(frame.height, content_height + padding * 2)

    position = str(options.get("position", "center")).casefold().replace("_", "-")
    positions: dict[str, tuple[int, int]] = {
        "top-left": (0, 0),
        "top": ((frame.width - box_width) // 2, 0),
        "top-right": (frame.width - box_width, 0),
        "left": (0, (frame.height - box_height) // 2),
        "center": (
            (frame.width - box_width) // 2,
            (frame.height - box_height) // 2,
        ),
        "right": (frame.width - box_width, (frame.height - box_height) // 2),
        "bottom-left": (0, frame.height - box_height),
        "bottom": ((frame.width - box_width) // 2, frame.height - box_height),
        "bottom-right": (frame.width - box_width, frame.height - box_height),
    }
    if position not in positions:
        raise ValueError(
            "Text position must be top-left, top, top-right, left, center, "
            "right, bottom-left, bottom, or bottom-right."
        )
    box_x, box_y = positions[position]
    custom_x = int(options.get("x", -1))
    custom_y = int(options.get("y", -1))
    if custom_x >= 0:
        box_x = min(frame.width - box_width, custom_x)
    if custom_y >= 0:
        box_y = min(frame.height - box_height, custom_y)

    style = str(options.get("style", "outline")).casefold()
    if style not in {"normal", "outline", "shadow", "box"}:
        raise ValueError("Text style must be normal, outline, shadow, or box.")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    background = str(options.get("background", "")).strip()
    if style == "box" and not background:
        background = "#000000"
    if background:
        background_opacity = int(options.get("background_opacity", 160))
        draw.rounded_rectangle(
            (box_x, box_y, box_x + box_width, box_y + box_height),
            radius=max(0, round(size * 0.15)),
            fill=_text_effect_color(
                background,
                default="#000000",
                opacity=background_opacity,
            ),
        )
    color = _text_effect_color(str(options.get("color", "#ffffff")), default="#ffffff")
    stroke_width = int(options.get("stroke_width", 0) or 0)
    if style == "outline" and stroke_width == 0:
        stroke_width = max(1, round(size * 0.06))
    if bool(options.get("bold")) and stroke_width == 0:
        stroke_width = 1
    if not 0 <= stroke_width <= 32:
        raise ValueError("Text stroke width must be between 0 and 32.")
    stroke_color = _text_effect_color(
        str(options.get("stroke_color", "#000000")), default="#000000"
    )
    align = str(options.get("align", "center")).casefold()
    if align not in {"left", "center", "right"}:
        raise ValueError("Text alignment must be left, center, or right.")
    y = box_y + padding
    for line, width in zip(lines, widths):
        if align == "left":
            x = box_x + padding
        elif align == "right":
            x = box_x + box_width - padding - width
        else:
            x = box_x + (box_width - width) // 2
        if style == "shadow":
            shadow_offset = max(1, round(size * 0.08))
            draw_inline_tokens(
                overlay,
                line,
                (x + shadow_offset, y + shadow_offset),
                font=font,
                image_size=size,
                assets=assets,
                timestamp_ms=timestamp_ms,
                fill=_text_effect_color(
                    str(options.get("shadow_color", "#000000")),
                    default="#000000",
                    opacity=190,
                ),
            )
        draw_inline_tokens(
            overlay,
            line,
            (x, y),
            font=font,
            image_size=size,
            assets=assets,
            timestamp_ms=timestamp_ms,
            fill=color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        y += line_height + gap
    opacity = float(options.get("opacity", 1))
    if not 0 <= opacity <= 1:
        raise ValueError("Text opacity must be between 0 and 1.")
    if opacity < 1:
        alpha = overlay.getchannel("A").point(
            [round(value * opacity) for value in range(256)]
        )
        overlay.putalpha(alpha)
    result = frame.convert("RGBA").copy()
    result.alpha_composite(overlay)
    return result


def render_text_effect_sync(data: bytes, **options: Any) -> EffectResult:
    assets = cast(dict[str, bytes], options.get("inline_images") or {})
    try:
        frames, durations, animated = _load_image_frames(
            data, preserve_transparency=True
        )
    except NotPillowMedia:
        with tempfile.TemporaryDirectory(prefix="fishie-text-video-") as directory:
            input_path = Path(directory) / "input.media"
            input_path.write_bytes(data)
            probe = _probe_path(str(input_path))
            if not probe.has_video:
                raise ValueError("Text requires an image, GIF, or video.")
            animation_duration = inline_animation_duration(assets)
            overlay_data: bytes
            animated_overlay = animation_duration > 0
            if animated_overlay:
                frame_duration = 50
                overlays = [
                    _text_overlay(
                        Image.new("RGBA", (probe.width, probe.height), (0, 0, 0, 0)),
                        options,
                        timestamp_ms=timestamp,
                    )
                    for timestamp in range(0, animation_duration, frame_duration)
                ]
                buffer = BytesIO()
                overlays[0].save(
                    buffer,
                    format="GIF",
                    save_all=True,
                    append_images=overlays[1:],
                    duration=frame_duration,
                    loop=0,
                    disposal=2,
                )
                overlay_data = buffer.getvalue()
            else:
                overlay = _text_overlay(
                    Image.new("RGBA", (probe.width, probe.height), (0, 0, 0, 0)),
                    options,
                )
                buffer = BytesIO()
                overlay.save(buffer, "PNG")
                overlay_data = buffer.getvalue()
            return _video_command_output(
                data,
                filename="text",
                second_data=overlay_data,
                second_loop=animated_overlay,
                filter_complex="[0:v][1:v]overlay=0:0:eof_action=repeat[v]",
                map_arguments=["-map", "[v]", "-map", "0:a?"],
                output_duration=probe.duration or MAX_MEDIA_DURATION,
            )

    animation_duration = inline_animation_duration(assets)
    if not animated and animation_duration:
        frame_duration = 50
        source = frames[0]
        durations = [frame_duration] * max(1, animation_duration // frame_duration)
        frames = [source] * len(durations)
        animated = True
    elapsed = 0
    transformed: list[Image.Image] = []
    for frame, duration in zip(frames, durations):
        transformed.append(_text_overlay(frame, options, timestamp_ms=elapsed))
        elapsed += duration
    return _save_frames(transformed, durations, filename="text")


render_text_effect = to_thread(render_text_effect_sync)


def _combine_mode(value: object) -> str:
    mode = str(value or "resize").casefold().strip().replace("_", "-")
    aliases = {
        "fit": "resize",
        "scale": "resize",
        "original-size": "original",
        "originalsize": "original",
        "none": "original",
        "fill": "stretch",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"resize", "stretch", "original"}:
        raise ValueError("Combine mode must be resize, stretch, or original.")
    return mode


def _combine_position(value: object) -> str:
    position = str(value or "right").casefold().strip()
    if position not in {"top", "bottom", "left", "right"}:
        raise ValueError("Combine position must be top, bottom, left, or right.")
    return position


def _combine_audio(value: object) -> str:
    audio = str(value or "mix").casefold().strip()
    aliases = {"both": "mix", "mute": "none", "silent": "none"}
    audio = aliases.get(audio, audio)
    if audio not in {"mix", "first", "second", "none"}:
        raise ValueError("Combine audio must be mix, first, second, or none.")
    return audio


def _combine_resize_second(
    first: Image.Image,
    second: Image.Image,
    *,
    position: str,
    mode: str,
) -> Image.Image:
    if mode == "original":
        return second
    if mode == "stretch":
        target = first.size
    elif position in {"left", "right"}:
        target = (
            max(1, round(second.width * first.height / second.height)),
            first.height,
        )
    else:
        target = (
            first.width,
            max(1, round(second.height * first.width / second.width)),
        )
    if target == second.size:
        return second
    return second.resize(target, Image.Resampling.LANCZOS)


def _combine_pillow_frames(
    first: Image.Image,
    second: Image.Image,
    *,
    position: str,
    mode: str,
) -> Image.Image:
    first = first.convert("RGBA")
    second = _combine_resize_second(
        first,
        second.convert("RGBA"),
        position=position,
        mode=mode,
    )
    if position in {"left", "right"}:
        size = (first.width + second.width, max(first.height, second.height))
    else:
        size = (max(first.width, second.width), first.height + second.height)
    if size[0] > MAX_MEDIA_DIMENSION or size[1] > MAX_MEDIA_DIMENSION:
        raise ValueError("The combined canvas cannot exceed 4096 pixels per side.")
    if size[0] * size[1] > MAX_FRAME_PIXELS:
        raise ValueError("The combined canvas is too large.")
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    first_xy = (0, 0)
    second_xy = (0, 0)
    if position == "right":
        second_xy = (first.width, 0)
    elif position == "left":
        first_xy = (second.width, 0)
    elif position == "bottom":
        second_xy = (0, first.height)
    else:
        first_xy = (0, second.height)
    canvas.alpha_composite(first, first_xy)
    canvas.alpha_composite(second, second_xy)
    return canvas


def _animation_frame_at(
    frames: Sequence[Image.Image],
    durations: Sequence[int],
    timestamp: int,
) -> Image.Image:
    if len(frames) == 1:
        return frames[0]
    total = sum(durations)
    if total <= 0:
        return frames[0]
    position = timestamp % total
    elapsed = 0
    for frame, duration in zip(frames, durations):
        elapsed += duration
        if position < elapsed:
            return frame
    return frames[-1]


def _combined_timeline(
    first_durations: Sequence[int],
    second_durations: Sequence[int],
    *,
    first_animated: bool,
    second_animated: bool,
) -> tuple[list[int], list[int]]:
    first_total = sum(first_durations) if first_animated else 0
    second_total = sum(second_durations) if second_animated else 0
    total = max(first_total, second_total)
    if total <= 0:
        return [0], [100]
    if total > round(MAX_MEDIA_DURATION * 1_000):
        raise ValueError(
            f"Combined animations are limited to {MAX_MEDIA_DURATION_MINUTES} minutes."
        )
    source_durations = [
        duration
        for duration in (list(first_durations) if first_animated else [])
        + (list(second_durations) if second_animated else [])
        if duration > 0
    ]
    interval = math.gcd(*source_durations) if source_durations else 50
    interval = max(20, interval)
    if math.ceil(total / interval) > MAX_ANIMATION_FRAMES:
        interval = max(20, math.ceil(total / MAX_ANIMATION_FRAMES / 10) * 10)
    timestamps = list(range(0, total, interval))
    durations = [max(20, min(interval, total - timestamp)) for timestamp in timestamps]
    return timestamps, durations


def _pillow_media_info(data: bytes) -> tuple[bool, int] | None:
    try:
        _, durations, animated = _load_image_frames(data, preserve_transparency=True)
    except NotPillowMedia:
        return None
    return animated, sum(durations) if animated else 0


def _video_combine_dimensions(
    first: MediaProbe,
    second: MediaProbe,
    *,
    position: str,
    mode: str,
) -> tuple[int, int, int, int]:
    first_width = max(1, first.width)
    first_height = max(1, first.height)
    second_width = max(1, second.width)
    second_height = max(1, second.height)
    if mode == "stretch":
        second_width, second_height = first_width, first_height
    elif mode == "resize":
        if position in {"left", "right"}:
            second_width = max(1, round(second_width * first_height / second_height))
            second_height = first_height
        else:
            second_height = max(1, round(second_height * first_width / second_width))
            second_width = first_width
    canvas_width = (
        first_width + second_width
        if position in {"left", "right"}
        else max(first_width, second_width)
    )
    canvas_height = (
        max(first_height, second_height)
        if position in {"left", "right"}
        else first_height + second_height
    )
    if (
        canvas_width > MAX_MEDIA_DIMENSION
        or canvas_height > MAX_MEDIA_DIMENSION
        or canvas_width * canvas_height > MAX_FRAME_PIXELS
    ):
        raise ValueError("The combined canvas is too large.")
    return second_width, second_height, canvas_width, canvas_height


def _render_video_combine(
    first_data: bytes,
    second_data: bytes,
    *,
    position: str,
    mode: str,
    audio: str,
    first_pillow: tuple[bool, int] | None,
    second_pillow: tuple[bool, int] | None,
) -> EffectResult:
    with tempfile.TemporaryDirectory(prefix="fishie-combine-") as directory:
        first_path = Path(directory) / "first.media"
        second_path = Path(directory) / "second.media"
        output_path = Path(directory) / "combine.mp4"
        first_path.write_bytes(first_data)
        second_path.write_bytes(second_data)
        first_probe = _probe_path(str(first_path))
        second_probe = _probe_path(str(second_path))
        second_width, second_height, _, _ = _video_combine_dimensions(
            first_probe,
            second_probe,
            position=position,
            mode=mode,
        )
        first_duration = (
            first_pillow[1] / 1_000
            if first_pillow is not None and first_pillow[0]
            else first_probe.duration
        )
        second_duration = (
            second_pillow[1] / 1_000
            if second_pillow is not None and second_pillow[0]
            else second_probe.duration
        )
        duration = max(first_duration, second_duration)
        if duration <= 0:
            raise ValueError("Could not determine the combined media duration.")
        if duration > MAX_MEDIA_DURATION:
            raise ValueError(
                f"Combined media is limited to {MAX_MEDIA_DURATION_MINUTES} minutes."
            )

        command = ["ffmpeg", "-v", "error", "-y"]
        for path, source_duration, pillow_info in (
            (first_path, first_duration, first_pillow),
            (second_path, second_duration, second_pillow),
        ):
            if (
                pillow_info is not None
                and not pillow_info[0]
                or source_duration + 0.01 < duration
            ):
                command.extend(["-stream_loop", "-1"])
            command.extend(["-i", str(path)])

        filters = [
            "[0:v]setpts=PTS-STARTPTS,format=rgba[v0]",
            "[1:v]setpts=PTS-STARTPTS,format=rgba"
            + (
                f",scale={second_width}:{second_height}:flags=lanczos"
                if mode != "original"
                else ""
            )
            + "[v1]",
        ]
        if position == "left":
            layout = "0_0|w0_0"
            inputs = "[v1][v0]"
        elif position == "right":
            layout = "0_0|w0_0"
            inputs = "[v0][v1]"
        elif position == "top":
            layout = "0_0|0_h0"
            inputs = "[v1][v0]"
        else:
            layout = "0_0|0_h0"
            inputs = "[v0][v1]"
        filters.append(
            f"{inputs}xstack=inputs=2:layout={layout}:fill=black,"
            "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[v]"
        )

        map_arguments = ["-map", "[v]"]
        if audio == "mix" and first_probe.has_audio and second_probe.has_audio:
            filters.append(
                "[0:a][1:a]amix=inputs=2:duration=longest:"
                "dropout_transition=0:normalize=1[a]"
            )
            map_arguments.extend(["-map", "[a]"])
        elif audio in {"mix", "first"} and first_probe.has_audio:
            map_arguments.extend(["-map", "0:a:0?"])
        elif audio in {"mix", "second"} and second_probe.has_audio:
            map_arguments.extend(["-map", "1:a:0?"])

        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                *map_arguments,
                "-t",
                f"{duration:g}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        _run(command, timeout=60)
        return EffectResult(output_path.read_bytes(), "combine.mp4")


def render_combine_effect_sync(
    first_data: bytes,
    second_data: bytes,
    **options: Any,
) -> EffectResult:
    position = _combine_position(options.get("position"))
    mode = _combine_mode(options.get("mode"))
    audio = _combine_audio(options.get("audio"))
    first_pillow = _pillow_media_info(first_data)
    second_pillow = _pillow_media_info(second_data)
    if first_pillow is not None and second_pillow is not None:
        first_frames, first_durations, first_animated = _load_image_frames(
            first_data,
            preserve_transparency=True,
        )
        second_frames, second_durations, second_animated = _load_image_frames(
            second_data,
            preserve_transparency=True,
        )
        timestamps, durations = _combined_timeline(
            first_durations,
            second_durations,
            first_animated=first_animated,
            second_animated=second_animated,
        )
        frames = [
            _combine_pillow_frames(
                _animation_frame_at(first_frames, first_durations, timestamp),
                _animation_frame_at(second_frames, second_durations, timestamp),
                position=position,
                mode=mode,
            )
            for timestamp in timestamps
        ]
        return _save_frames(frames, durations, filename="combine")
    return _render_video_combine(
        first_data,
        second_data,
        position=position,
        mode=mode,
        audio=audio,
        first_pillow=first_pillow,
        second_pillow=second_pillow,
    )


render_combine_effect = to_thread(render_combine_effect_sync)


def _audio_effect_window(
    options: dict[str, Any],
    total_duration: float,
) -> tuple[float, float | None]:
    start = float(options.get("start", 0) or 0)
    stop = float(options.get("stop", 0) or 0)
    duration = float(options.get("duration", 0) or 0)
    media_limit = (
        min(MAX_MEDIA_DURATION, total_duration)
        if total_duration > 0
        else MAX_MEDIA_DURATION
    )
    if not 0 <= start <= media_limit:
        raise ValueError(
            f"Audio effect start must be between 0 and {media_limit:g} seconds."
        )
    if not 0 <= stop <= media_limit:
        raise ValueError(
            f"Audio effect stop must be between 0 and {media_limit:g} seconds."
        )
    if not 0 <= duration <= media_limit:
        raise ValueError(
            f"Audio effect duration must be between 0 and {media_limit:g} seconds."
        )
    end: float | None = start + duration if duration > 0 else stop or None
    if end is not None and end <= start:
        raise ValueError("Audio effect stop must be after its start time.")
    if total_duration > 0:
        start = min(start, total_duration)
        if end is not None:
            end = min(end, total_duration)
    return start, end


def _timed_audio_output(
    input_data: bytes,
    probe: MediaProbe,
    *,
    filename: str,
    audio_filter: str,
    options: dict[str, Any],
) -> EffectResult:
    start, end = _audio_effect_window(options, probe.duration)
    output_extension = "mp4" if probe.has_video else "mp3"
    reaches_end = end is None or (probe.duration > 0 and end >= probe.duration - 0.001)
    if start <= 0 and reaches_end:
        return _video_command_output(
            input_data,
            filename=filename,
            audio_filter=audio_filter,
            output_extension=output_extension,
        )

    segments: list[tuple[float, float | None, str | None]] = []
    if start > 0:
        segments.append((0, start, None))
    segments.append((start, end, audio_filter))
    if end is not None and (probe.duration <= 0 or end < probe.duration - 0.001):
        segments.append((end, None, None))

    split_labels = "".join(f"[source{index}]" for index in range(len(segments)))
    filters = [f"[0:a]asplit={len(segments)}{split_labels}"]
    output_labels: list[str] = []
    for index, (segment_start, segment_end, effect_filter) in enumerate(segments):
        trim = f"atrim=start={segment_start:g}"
        if segment_end is not None:
            trim += f":end={segment_end:g}"
        chain = f"[source{index}]{trim},asetpts=PTS-STARTPTS"
        if effect_filter:
            chain += f",{effect_filter}"
        chain += ",aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
        label = f"segment{index}"
        filters.append(f"{chain}[{label}]")
        output_labels.append(f"[{label}]")
    filters.append(f"{''.join(output_labels)}concat=n={len(output_labels)}:v=0:a=1[a]")
    return _video_command_output(
        input_data,
        filename=filename,
        filter_complex=";".join(filters),
        map_arguments=["-map", "0:v?", "-map", "[a]"],
        output_extension=output_extension,
    )


def _atempo_chain(speed: float) -> str:
    remaining = speed
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2:
        factors.append(2.0)
        remaining /= 2
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


def _audio_overlay_output(
    input_data: bytes,
    second_data: bytes,
    probe: MediaProbe,
    options: dict[str, Any],
) -> EffectResult:
    with tempfile.TemporaryDirectory(prefix="fishie-audio-overlay-probe-") as directory:
        second_path = os.path.join(directory, "second.media")
        Path(second_path).write_bytes(second_data)
        second_probe = _probe_path(second_path)
    if not second_probe.has_audio:
        raise ValueError("The overlay file does not contain audio.")

    at = float(options.get("at", 0) or 0)
    source_start = float(options.get("source_start", 0) or 0)
    source_stop = float(options.get("source_stop", 0) or 0)
    duration = float(options.get("duration", 0) or 0)
    volume = float(options.get("volume", 1) or 0)
    pitch = float(options.get("pitch", 0) or 0)
    speed = float(options.get("speed", 1) or 0)
    loop = bool(options.get("loop"))
    fade_in = float(options.get("fade_in", 0) or 0)
    fade_out = float(options.get("fade_out", 0) or 0)
    preserve_base_duration = bool(options.get("_preserve_base_duration"))
    if bool(options.get("random_time")):
        available = max(0.0, probe.duration - min(second_probe.duration, 10))
        at = random.SystemRandom().uniform(0, available) if available else 0
    if not 0 <= at <= MAX_MEDIA_DURATION:
        raise ValueError(
            f"Overlay time must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
        )
    if not 0 <= source_start <= MAX_MEDIA_DURATION:
        raise ValueError(
            f"Audio source start must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
        )
    if not 0 <= source_stop <= MAX_MEDIA_DURATION:
        raise ValueError(
            f"Audio source stop must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
        )
    if not 0 <= duration <= MAX_MEDIA_DURATION:
        raise ValueError(
            f"Audio duration must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
        )
    if not 0 <= volume <= 5:
        raise ValueError("Audio overlay volume must be between 0 and 5.")
    if not -12 <= pitch <= 12:
        raise ValueError("Audio overlay pitch must be between -12 and 12.")
    if not 0.25 <= speed <= 4:
        raise ValueError("Audio overlay speed must be between 0.25 and 4.")
    if not 0 <= fade_in <= 30 or not 0 <= fade_out <= 30:
        raise ValueError("Sound-effect fades must be between 0 and 30 seconds.")
    source_end = source_start + duration if duration > 0 else source_stop or None
    if source_end is not None and source_end <= source_start:
        raise ValueError("Audio source stop must be after its start time.")

    trim = f"atrim=start={source_start:g}"
    if source_end is not None:
        trim += f":end={source_end:g}"
    overlay_chain = f"[1:a]{trim},asetpts=PTS-STARTPTS"
    if pitch:
        ratio = 2 ** (pitch / 12)
        overlay_chain += (
            f",asetrate=44100*{ratio:g},aresample=44100,atempo={1 / ratio:g}"
        )
    if speed != 1:
        overlay_chain += f",{_atempo_chain(speed)}"
    source_duration = max(0.0, second_probe.duration - source_start)
    if source_end is not None:
        source_duration = min(source_duration, source_end - source_start)
    remaining_duration = max(0.0, probe.duration - at)
    overlay_duration = remaining_duration if loop else source_duration / speed
    target_duration = (
        probe.duration
        if preserve_base_duration and probe.duration > 0
        else max(probe.duration, at + overlay_duration)
    )
    target_duration = min(MAX_MEDIA_DURATION, target_duration)
    effective_duration = max(0.0, min(overlay_duration, target_duration - at))
    if loop and effective_duration:
        overlay_chain += f",atrim=duration={effective_duration:g}"
    elif effective_duration and effective_duration < overlay_duration:
        overlay_chain += f",atrim=duration={effective_duration:g}"
    if fade_in and effective_duration:
        fade_duration = min(fade_in, effective_duration)
        overlay_chain += f",afade=t=in:st=0:d={fade_duration:g}"
    if fade_out and effective_duration:
        fade_duration = min(fade_out, effective_duration)
        fade_start = max(0.0, effective_duration - fade_duration)
        overlay_chain += f",afade=t=out:st={fade_start:g}:d={fade_duration:g}"
    overlay_chain += f",volume={volume:g},adelay={round(at * 1000)}:all=1[overlay]"

    if probe.has_audio:
        if target_duration > 0:
            base_chain = (
                f"[0:a]apad=whole_dur={target_duration:g},"
                f"atrim=duration={target_duration:g}[base]"
            )
        else:
            base_chain = "[0:a]anull[base]"
        audio_graph = (
            f"{overlay_chain};{base_chain};[base][overlay]"
            "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95:level=0:latency=1,"
            f"apad=whole_dur={target_duration:g},"
            f"atrim=duration={target_duration:g}[a]"
        )
    else:
        duration_chain = (
            f"apad=whole_dur={target_duration:g}," f"atrim=duration={target_duration:g}"
            if target_duration > 0
            else "anull"
        )
        audio_graph = f"{overlay_chain};[overlay]{duration_chain}[a]"
    map_arguments: list[str] = []
    extension = 0.0
    if probe.has_video:
        extension = max(0.0, target_duration - probe.duration)
        if extension > 0.001:
            audio_graph += (
                f";[0:v]tpad=stop_mode=clone:stop_duration={extension:g}[video]"
            )
            map_arguments.extend(["-map", "[video]"])
        else:
            map_arguments.extend(["-map", "0:v:0"])
    map_arguments.extend(["-map", "[a]"])
    return _video_command_output(
        input_data,
        filename="audio-overlay",
        second_data=second_data,
        second_loop=loop,
        filter_complex=audio_graph,
        map_arguments=map_arguments,
        output_extension="mp4" if probe.has_video else "mp3",
        copy_video=(
            probe.has_video
            and extension <= 0.001
            and probe.can_copy_video_to_discord_mp4
        ),
        media_probe=probe,
    )


SoundEffectBatchItem = tuple[bytes, float, dict[str, Any]]


def render_sound_effect_batch_sync(
    input_data: bytes,
    sound_effects: Sequence[SoundEffectBatchItem],
    *,
    media_probe: MediaProbe | None = None,
) -> EffectResult:
    """Mix bundled sound effects in one FFmpeg pass.

    The video stream is copied when it is already Discord-compatible H.264.
    Only audio is decoded and encoded in that common case.
    """

    if not sound_effects:
        raise ValueError("At least one sound effect is required.")
    probe = media_probe or probe_media_sync(input_data)
    prepared: list[tuple[bytes, bool, str]] = []
    target_duration = probe.duration
    generator = random.SystemRandom()

    for index, (effect_data, catalog_duration, raw_options) in enumerate(
        sound_effects,
        start=1,
    ):
        options = raw_options.copy()
        at = float(options.get("at", 0) or 0)
        source_start = float(options.get("source_start", 0) or 0)
        source_stop = float(options.get("source_stop", 0) or 0)
        duration = float(options.get("duration", 0) or 0)
        volume = float(options.get("volume", 1) or 0)
        pitch = float(options.get("pitch", 0) or 0)
        speed = float(options.get("speed", 1) or 0)
        loop = bool(options.get("loop"))
        fade_in = float(options.get("fade_in", 0) or 0)
        fade_out = float(options.get("fade_out", 0) or 0)
        if bool(options.get("random_time")):
            available = max(0.0, probe.duration - min(catalog_duration, 10))
            at = generator.uniform(0, available) if available else 0
        if not 0 <= at <= MAX_MEDIA_DURATION:
            raise ValueError(
                f"Overlay time must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
            )
        if not 0 <= source_start <= MAX_MEDIA_DURATION:
            raise ValueError(
                "Audio source start must be between 0 and "
                f"{MAX_MEDIA_DURATION:g} seconds."
            )
        if not 0 <= source_stop <= MAX_MEDIA_DURATION:
            raise ValueError(
                "Audio source stop must be between 0 and "
                f"{MAX_MEDIA_DURATION:g} seconds."
            )
        if not 0 <= duration <= MAX_MEDIA_DURATION:
            raise ValueError(
                f"Audio duration must be between 0 and {MAX_MEDIA_DURATION:g} seconds."
            )
        if not 0 <= volume <= 5:
            raise ValueError("Audio overlay volume must be between 0 and 5.")
        if not -12 <= pitch <= 12:
            raise ValueError("Audio overlay pitch must be between -12 and 12.")
        if not 0.25 <= speed <= 4:
            raise ValueError("Audio overlay speed must be between 0.25 and 4.")
        if not 0 <= fade_in <= 30 or not 0 <= fade_out <= 30:
            raise ValueError("Sound-effect fades must be between 0 and 30 seconds.")

        source_end = source_start + duration if duration > 0 else source_stop or None
        if source_end is not None and source_end <= source_start:
            raise ValueError("Audio source stop must be after its start time.")
        source_duration = max(0.0, catalog_duration - source_start)
        if source_end is not None:
            source_duration = min(source_duration, source_end - source_start)
        remaining_duration = max(0.0, probe.duration - at)
        overlay_duration = remaining_duration if loop else source_duration / speed
        item_target = (
            probe.duration
            if probe.duration > 0
            else min(MAX_MEDIA_DURATION, at + overlay_duration)
        )
        target_duration = max(target_duration, item_target)
        effective_duration = max(0.0, min(overlay_duration, max(0.0, item_target - at)))

        trim = f"atrim=start={source_start:g}"
        if source_end is not None:
            trim += f":end={source_end:g}"
        chain = f"[{index}:a]{trim},asetpts=PTS-STARTPTS"
        if pitch:
            ratio = 2 ** (pitch / 12)
            chain += (
                f",asetrate=44100*{ratio:g},aresample=44100," f"atempo={1 / ratio:g}"
            )
        if speed != 1:
            chain += f",{_atempo_chain(speed)}"
        if effective_duration and (loop or effective_duration < overlay_duration):
            chain += f",atrim=duration={effective_duration:g}"
        if fade_in and effective_duration:
            chain += f",afade=t=in:st=0:d={min(fade_in, effective_duration):g}"
        if fade_out and effective_duration:
            fade_duration = min(fade_out, effective_duration)
            chain += (
                f",afade=t=out:st={max(0.0, effective_duration - fade_duration):g}:"
                f"d={fade_duration:g}"
            )
        chain += f",volume={volume:g},adelay={round(at * 1000)}:all=1[overlay{index}]"
        prepared.append((effect_data, loop, chain))

    target_duration = min(MAX_MEDIA_DURATION, target_duration)
    graph = [item[2] for item in prepared]
    mix_inputs: list[str] = []
    if probe.has_audio:
        graph.append(
            f"[0:a]apad=whole_dur={target_duration:g},"
            f"atrim=duration={target_duration:g}[base]"
        )
        mix_inputs.append("[base]")
    mix_inputs.extend(f"[overlay{index}]" for index in range(1, len(prepared) + 1))
    if len(mix_inputs) == 1:
        graph.append(
            f"{mix_inputs[0]}apad=whole_dur={target_duration:g},"
            f"atrim=duration={target_duration:g},"
            "alimiter=limit=0.95:level=0:latency=1[a]"
        )
    else:
        graph.append(
            f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:"
            "duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95:level=0:latency=1,"
            f"apad=whole_dur={target_duration:g},"
            f"atrim=duration={target_duration:g}[a]"
        )

    map_arguments: list[str] = []
    extension = 0.0
    if probe.has_video:
        extension = max(0.0, target_duration - probe.duration)
        if extension > 0.001:
            graph.append(
                f"[0:v]tpad=stop_mode=clone:stop_duration={extension:g}[video]"
            )
            map_arguments.extend(["-map", "[video]"])
        else:
            map_arguments.extend(["-map", "0:v:0"])
    map_arguments.extend(["-map", "[a]"])
    return _video_command_output(
        input_data,
        filename="audio-overlay",
        additional_inputs=[(data, loop) for data, loop, _ in prepared],
        filter_complex=";".join(graph),
        map_arguments=map_arguments,
        output_extension="mp4" if probe.has_video else "mp3",
        copy_video=(
            probe.has_video
            and extension <= 0.001
            and probe.can_copy_video_to_discord_mp4
        ),
        media_probe=probe,
    )


render_sound_effect_batch = to_thread(render_sound_effect_batch_sync)


AdhdMode = Literal["fast", "slow", "normal", "lowered", "nightcore"]


def _adhd_segments(
    duration: float,
    rng: random.Random | random.SystemRandom,
) -> list[tuple[float, float, float, AdhdMode]]:
    """Build varied ADHD sections while scaling short clips below two seconds."""
    if duration <= 0:
        return []
    if duration < 8:
        minimum = max(0.25, duration * 0.12)
        maximum = max(minimum, duration * 0.35)
    else:
        minimum = 2.0
        maximum = min(7.0, max(minimum, duration * 0.25))

    modes: list[AdhdMode] = ["fast", "slow", "normal", "lowered", "nightcore"]
    available = modes.copy()
    segments: list[tuple[float, float, float, AdhdMode]] = []
    position = 0.0
    previous: AdhdMode | None = None
    while position < duration - 0.001:
        if not available:
            available = modes.copy()
        candidates = [mode for mode in available if mode != previous] or available
        mode = cast(AdhdMode, rng.choice(candidates))
        available.remove(mode)
        segment_length = rng.uniform(minimum, maximum)
        end = min(duration, position + segment_length)
        factor = (
            rng.uniform(1.15, 3.0)
            if mode == "fast"
            else rng.uniform(0.5, 0.9) if mode == "slow" else 1.0
        )
        segments.append((position, end, factor, mode))
        position = end
        previous = mode
    return segments


def _adhd_output(input_data: bytes, probe: MediaProbe) -> EffectResult:
    if not probe.has_video and not probe.has_audio:
        raise ValueError("ADHD requires a video or audio file.")
    if probe.duration <= 0:
        raise ValueError("Could not determine the media duration.")

    segments = _adhd_segments(probe.duration, random.SystemRandom())

    filters: list[str] = []
    map_arguments: list[str] = []
    if probe.has_video:
        split = "".join(f"[vsrc{index}]" for index in range(len(segments)))
        filters.append(f"[0:v]split={len(segments)}{split}")
        video_outputs: list[str] = []
        for index, (start, end, factor, _) in enumerate(segments):
            label = f"v{index}"
            filters.append(
                f"[vsrc{index}]trim=start={start:g}:end={end:g},"
                f"setpts=(PTS-STARTPTS)/{factor:g}[{label}]"
            )
            video_outputs.append(f"[{label}]")
        filters.append(
            f"{''.join(video_outputs)}concat=n={len(video_outputs)}:v=1:a=0[v]"
        )
        map_arguments.extend(["-map", "[v]"])

    if probe.has_audio:
        split = "".join(f"[asrc{index}]" for index in range(len(segments)))
        filters.append(f"[0:a]asplit={len(segments)}{split}")
        audio_outputs: list[str] = []
        for index, (start, end, factor, mode) in enumerate(segments):
            label = f"a{index}"
            voice = {
                "fast": "asetrate=44100*1.25,aresample=44100,atempo=0.8",
                "slow": "asetrate=44100*0.8,aresample=44100,atempo=1.25",
                "normal": "anull",
                "lowered": "asetrate=44100*0.72,aresample=44100,atempo=1.388889",
                "nightcore": "asetrate=44100*1.35,aresample=44100,atempo=0.740741",
            }[mode]
            filters.append(
                f"[asrc{index}]atrim=start={start:g}:end={end:g},"
                f"asetpts=PTS-STARTPTS,{voice},{_atempo_chain(factor)},"
                "aresample=44100,aformat=sample_fmts=fltp:"
                f"channel_layouts=stereo[{label}]"
            )
            audio_outputs.append(f"[{label}]")
        filters.append(
            f"{''.join(audio_outputs)}concat=n={len(audio_outputs)}:v=0:a=1[a]"
        )
        map_arguments.extend(["-map", "[a]"])

    return _video_command_output(
        input_data,
        filename="adhd",
        filter_complex=";".join(filters),
        map_arguments=map_arguments,
        output_extension="mp4" if probe.has_video else "mp3",
    )


def _detect_platform_outro(path: str, duration: float, platform: str) -> float:
    if duration < 3:
        raise ValueError("That video is too short to contain the selected outro.")

    sample_start = max(0.0, duration - 6.0)
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{sample_start:g}",
        "-i",
        path,
        "-vf",
        (
            "fps=10,scale=96:96:force_original_aspect_ratio=decrease,"
            "pad=96:96:(ow-iw)/2:(oh-ih)/2:black"
        ),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        result = run_media_command(
            command,
            timeout=15,
            timeout_message="Could not inspect that video for an outro.",
            failure_prefix="Could not inspect that video for an outro",
        )
    except ValueError as error:
        raise ValueError("Could not inspect that video for an outro.") from error

    frame_size = 96 * 96 * 3
    frame_count = len(result.stdout) // frame_size
    if frame_count < 8:
        raise ValueError("Could not read enough video frames to find an outro.")
    frames = np.frombuffer(
        result.stdout[: frame_count * frame_size],
        dtype=np.uint8,
    ).reshape(frame_count, 96, 96, 3)
    frame_values = frames.astype(np.float32)
    differences = np.abs(np.diff(frame_values, axis=0)).mean(axis=(1, 2, 3))

    candidates = [
        index
        for index in range(1, frame_count)
        if 2.5 <= duration - (sample_start + index / 10) <= 5.5
    ]
    if not candidates:
        raise ValueError("Could not find the selected outro.")
    transition = max(candidates, key=lambda index: float(differences[index - 1]))
    transition_strength = float(differences[transition - 1])
    before = frame_values[max(0, transition - 5) : transition]
    after = frame_values[transition : min(frame_count, transition + 15)]
    before_brightness = float(before.mean())
    after_brightness = float(after.mean())
    dark_fraction = float((after.mean(axis=3) < 35).mean())

    minimum_change = 25.0 if platform == "tiktok" else 10.0
    darkened = after_brightness <= max(12.0, before_brightness * 0.78)
    if transition_strength < minimum_change or dark_fraction < 0.85 or not darkened:
        raise ValueError(
            f"Could not confidently find a {platform.title()} outro in that video."
        )
    return sample_start + transition / 10


def _remove_platform_outro(
    input_data: bytes,
    probe: MediaProbe,
    platform: str,
) -> EffectResult:
    if not probe.has_video:
        raise ValueError("Outro removal requires a video.")
    with tempfile.TemporaryDirectory(prefix="fishie-remove-outro-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, "output.mp4")
        Path(input_path).write_bytes(input_data)
        trim_at = _detect_platform_outro(input_path, probe.duration, platform)
        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-t",
            f"{trim_at:g}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            output_path,
        ]
        _run(command)
        return EffectResult(
            Path(output_path).read_bytes(),
            f"remove-outro-{platform}.mp4",
        )


def _reverse_video_chunked(input_data: bytes, probe: MediaProbe) -> EffectResult:
    """Reverse video in bounded chunks so ffmpeg never buffers the full clip."""

    if probe.duration <= 0:
        raise ValueError("Could not determine the video duration for reversing.")
    chunk_duration = 8.0
    with tempfile.TemporaryDirectory(prefix="fishie-video-reverse-") as directory:
        input_path = os.path.join(directory, "input.media")
        Path(input_path).write_bytes(input_data)
        chunks: list[str] = []
        end = probe.duration
        index = 0
        while end > 0.001:
            start = max(0.0, end - chunk_duration)
            length = end - start
            segment_path = os.path.join(directory, f"segment-{index:03d}.mkv")
            command = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-ss",
                f"{start:.6f}",
                "-t",
                f"{length:.6f}",
            ]
            if probe.has_audio:
                command.extend(
                    [
                        "-filter_complex",
                        "[0:v]reverse,setpts=PTS-STARTPTS[v];"
                        "[0:a]areverse,asetpts=PTS-STARTPTS[a]",
                        "-map",
                        "[v]",
                        "-map",
                        "[a]",
                    ]
                )
            else:
                command.extend(
                    [
                        "-vf",
                        "reverse,setpts=PTS-STARTPTS",
                        "-an",
                    ]
                )
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
            if probe.has_audio:
                command.extend(["-c:a", "aac", "-b:a", "128k"])
            command.append(segment_path)
            _run(command)
            chunks.append(segment_path)
            index += 1
            end = start

        concat_path = os.path.join(directory, "segments.txt")
        Path(concat_path).write_text(
            "".join(f"file '{path}'\n" for path in chunks),
            encoding="utf-8",
        )
        output_path = os.path.join(directory, "reverse.mp4")
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )
        return EffectResult(Path(output_path).read_bytes(), "reverse.mp4")


def _reverse_video_window(
    input_data: bytes,
    probe: MediaProbe,
    *,
    start: float,
    end: float | None,
) -> EffectResult:
    """Reverse only a selected video window while preserving the rest."""
    if start <= 0 and (end is None or end >= probe.duration - 0.001):
        return _reverse_video_chunked(input_data, probe)
    if probe.duration <= 0:
        raise ValueError("Could not determine the video duration for reversing.")

    effective_end = min(probe.duration, end if end is not None else probe.duration)
    segments: list[tuple[float, float, bool]] = []
    if start > 0:
        segments.append((0.0, start, False))
    segments.append((start, effective_end, True))
    if effective_end < probe.duration - 0.001:
        segments.append((effective_end, probe.duration, False))

    with tempfile.TemporaryDirectory(
        prefix="fishie-video-reverse-window-"
    ) as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, "reverse.mp4")
        Path(input_path).write_bytes(input_data)
        filters: list[str] = []
        video_labels: list[str] = []
        audio_labels: list[str] = []
        for index, (segment_start, segment_end, reverse) in enumerate(segments):
            video_chain = (
                f"[0:v]trim=start={segment_start:g}:end={segment_end:g},"
                "setpts=PTS-STARTPTS"
            )
            if reverse:
                video_chain += ",reverse"
            video_label = f"[v{index}]"
            filters.append(f"{video_chain}{video_label}")
            video_labels.append(video_label)
            if probe.has_audio:
                audio_chain = (
                    f"[0:a]atrim=start={segment_start:g}:end={segment_end:g},"
                    "asetpts=PTS-STARTPTS"
                )
                if reverse:
                    audio_chain += ",areverse"
                audio_label = f"[a{index}]"
                filters.append(f"{audio_chain}{audio_label}")
                audio_labels.append(audio_label)
        filters.append(
            f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vout]"
        )
        if probe.has_audio:
            filters.append(
                f"{''.join(audio_labels)}concat=n={len(audio_labels)}:v=0:a=1[aout]"
            )
        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
        ]
        if probe.has_audio:
            command.extend(["-map", "[aout]"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if probe.has_audio:
            command.extend(["-c:a", "aac", "-b:a", "128k"])
        else:
            command.append("-an")
        command.extend(["-movflags", "+faststart", output_path])
        _run(command)
        return EffectResult(Path(output_path).read_bytes(), "reverse.mp4")


def _reverse_image_window(
    frames: list[Image.Image],
    durations: list[int],
    *,
    start: float,
    end: float | None,
) -> tuple[list[Image.Image], list[int]]:
    """Reverse the frames overlapping a selected time window."""
    total_duration = sum(durations) / 1_000
    effective_end = end if end is not None else total_duration
    offsets: list[tuple[float, float]] = []
    elapsed = 0.0
    for duration in durations:
        next_elapsed = elapsed + duration / 1_000
        offsets.append((elapsed, next_elapsed))
        elapsed = next_elapsed
    selected = [
        index
        for index, (frame_start, frame_end) in enumerate(offsets)
        if frame_end > start and frame_start < effective_end
    ]
    if not selected:
        raise ValueError("The reverse window does not contain any frames.")
    selected_set = set(selected)
    reversed_indexes = iter(reversed(selected))
    output_frames: list[Image.Image] = []
    for index, frame in enumerate(frames):
        output_frames.append(
            frames[next(reversed_indexes)] if index in selected_set else frame
        )
    output_durations = [durations[index] for index in range(len(frames))]
    return output_frames, output_durations


def render_video_effect_sync(
    input_data: bytes,
    effect: str,
    *,
    second_data: bytes | None = None,
    media_probe: MediaProbe | None = None,
    **options: Any,
) -> EffectResult:
    if effect == "reverse":
        try:
            image_frames, image_durations, animated = _load_image_frames(input_data)
        except NotPillowMedia:
            pass
        else:
            if animated:
                start, end = _audio_effect_window(
                    options,
                    sum(image_durations) / 1_000,
                )
                reversed_frames, reversed_durations = _reverse_image_window(
                    image_frames,
                    image_durations,
                    start=start,
                    end=end,
                )
                return _save_frames(
                    reversed_frames,
                    reversed_durations,
                    filename="reverse",
                )
            raise ValueError(
                "Reverse requires a GIF, video, or audio file. "
                "A still image has no frames to reverse."
            )

    if media_probe is None:
        with tempfile.TemporaryDirectory(prefix="fishie-video-probe-") as directory:
            path = os.path.join(directory, "input.media")
            Path(path).write_bytes(input_data)
            probe = _probe_path(path)
    else:
        probe = media_probe

    if effect in {"magik", "swirl", "gifmagik", "gifswirl"}:
        raise ValueError(
            "Magik and swirl only support still images, not videos or animated media."
        )

    if effect == "adhd":
        return _adhd_output(input_data, probe)

    if effect in {"removeoutrotiktok", "removeoutroreels"}:
        platform = "tiktok" if effect == "removeoutrotiktok" else "reels"
        return _remove_platform_outro(input_data, probe, platform)

    if effect == "reverse":
        if probe.has_video:
            start, end = _audio_effect_window(options, probe.duration)
            return _reverse_video_window(
                input_data,
                probe,
                start=start,
                end=end,
            )
        return _timed_audio_output(
            input_data,
            probe,
            filename="reverse",
            audio_filter="areverse",
            options=options,
        )

    if effect == "overlay":
        if second_data is None:
            raise ValueError("A second video or image is required.")
        return _render_video_overlay_sync(
            input_data,
            second_data,
            allow_image_base=False,
            **options,
        )

    if effect in {"bassboost", "basslower"}:
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        gain = float(options.get("gain", 12))
        if not 1 <= gain <= 30:
            raise ValueError("Bass gain must be between 1 and 30 dB.")
        if effect == "basslower":
            gain = -gain
        return _timed_audio_output(
            input_data,
            probe,
            filename="bass-boost" if gain > 0 else "bass-lower",
            audio_filter=f"bass=g={gain:g}:f=110:w=0.6",
            options=options,
        )

    if effect == "audioreverse":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        return _timed_audio_output(
            input_data,
            probe,
            filename="audio-reverse",
            audio_filter="areverse",
            options=options,
        )

    if effect == "audioreverb":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        room = float(options.get("room", 0.5))
        if not 0.1 <= room <= 1:
            raise ValueError("Reverb room size must be between 0.1 and 1.")
        decay_one = min(0.9, room * 0.7)
        decay_two = min(0.8, room * 0.45)
        return _timed_audio_output(
            input_data,
            probe,
            filename="audio-reverb",
            audio_filter=f"aecho=0.8:0.8:60|120:{decay_one:g}|{decay_two:g}",
            options=options,
        )

    if effect in {
        "audiopitch",
        "audiounderwater",
        "audionightcore",
        "audiodeepvoice",
        "audiosurround",
        "audioecho",
    }:
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        if effect == "audiopitch":
            semitones = float(options.get("semitones", 3))
            if not -12 <= semitones <= 12:
                raise ValueError("Audio pitch must be between -12 and 12 semitones.")
            ratio = 2 ** (semitones / 12)
            audio_filter = (
                f"asetrate=44100*{ratio:g},aresample=44100,atempo={1 / ratio:g}"
            )
            filename = "audio-pitch"
        elif effect == "audiounderwater":
            audio_filter = "lowpass=f=900,highpass=f=80,aecho=0.8:0.8:120|240:0.35|0.2"
            filename = "audio-underwater"
        elif effect == "audionightcore":
            audio_filter = "asetrate=44100*1.25,aresample=44100,atempo=0.8"
            filename = "audio-nightcore"
        elif effect == "audiodeepvoice":
            audio_filter = "asetrate=44100*0.8,aresample=44100,atempo=1.25"
            filename = "audio-deepvoice"
        elif effect == "audiosurround":
            audio_filter = "extrastereo=m=2.5"
            filename = "audio-surround"
        else:
            audio_filter = "aecho=0.8:0.88:60|120:0.4|0.25"
            filename = "audio-echo"
        return _timed_audio_output(
            input_data,
            probe,
            filename=filename,
            audio_filter=audio_filter,
            options=options,
        )

    if effect == "audioreplace":
        if not probe.has_video:
            raise ValueError("The first file must contain video.")
        if second_data is None:
            raise ValueError("A replacement audio file is required.")
        return _video_command_output(
            input_data,
            filename="audio-replaced",
            second_data=second_data,
            map_arguments=["-map", "0:v:0", "-map", "1:a:0"],
            shortest=True,
        )

    if effect in {"audiooverlay", "soundeffect"}:
        if second_data is None:
            raise ValueError("An audio or video overlay file is required.")
        if effect == "soundeffect":
            options.setdefault("random_time", True)
            options["_preserve_base_duration"] = True
        return _audio_overlay_output(input_data, second_data, probe, options)

    if effect == "audiodestroy":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        amount = int(options.get("amount", 6))
        if not 2 <= amount <= 12:
            raise ValueError("Destroy amount must be between 2 and 12.")
        return _timed_audio_output(
            input_data,
            probe,
            filename="audio-destroyed",
            audio_filter=(
                # Keep a stable sample rate throughout the chain. Very low
                # intermediate rates made some WebM/AAC inputs fail during
                # conversion even though the same filter worked for WAV.
                f"aresample=44100,acrusher=bits={max(2, 13 - amount)}:"
                "mix=1:mode=lin,"
                f"acompressor=threshold=-{min(40, 16 + amount * 2)}dB:"
                f"ratio={min(20, 4 + amount)}:attack=2:release=60:makeup=6,"
                f"aecho=0.72:0.82:{max(35, amount * 18)}|"
                f"{max(70, amount * 37)}:"
                f"{min(0.8, 0.22 + amount * 0.04):g}|"
                f"{min(0.65, 0.12 + amount * 0.03):g},"
                "aresample=44100"
            ),
            options=options,
        )

    if effect == "audiocompress":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        ratio = float(options.get("ratio", 4))
        if not 1 <= ratio <= 20:
            raise ValueError("Compression ratio must be between 1 and 20.")
        return _timed_audio_output(
            input_data,
            probe,
            filename="audio-compressed",
            audio_filter=(
                f"acompressor=threshold=-18dB:ratio={ratio:g}:"
                "attack=20:release=250:makeup=4"
            ),
            options=options,
        )

    if effect == "channelscombine":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        return _timed_audio_output(
            input_data,
            probe,
            filename="mono",
            audio_filter="aformat=channel_layouts=mono",
            options=options,
        )

    if effect == "volume":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        volume = float(options.get("volume", 1))
        if not 0 <= volume <= 10:
            raise ValueError("Volume must be between 0 and 10.")
        return _timed_audio_output(
            input_data,
            probe,
            filename="volume",
            audio_filter=f"volume={volume:g}",
            options=options,
        )

    if effect == "extract":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        with tempfile.TemporaryDirectory(prefix="fishie-audio-extract-") as directory:
            input_path = os.path.join(directory, "input.media")
            Path(input_path).write_bytes(input_data)
            tracks: list[tuple[str, bytes]] = []
            for index in range(min(5, probe.audio_streams)):
                output_path = os.path.join(directory, f"track-{index + 1}.mp3")
                _run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        input_path,
                        "-map",
                        f"0:a:{index}",
                        "-vn",
                        "-c:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        output_path,
                    ]
                )
                tracks.append(
                    (f"track-{index + 1}.mp3", Path(output_path).read_bytes())
                )
            if len(tracks) == 1:
                return EffectResult(tracks[0][1], tracks[0][0], displayable=False)
            archive = BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                for name, track in tracks:
                    zipped.writestr(name, track)
            return EffectResult(
                archive.getvalue(),
                "audio-tracks.zip",
                displayable=False,
            )

    raise ValueError("Unknown video effect.")


render_video_effect = to_thread(render_video_effect_sync)


def convert_media_sync(data: bytes, output_format: str, index: int = 1) -> EffectResult:
    output_format = output_format.casefold()
    if output_format not in {
        "mp4",
        "mov",
        "webm",
        "gif",
        "mp3",
        "wav",
        "ogg",
        "opus",
    }:
        raise ValueError("Format must be mp4, mov, webm, gif, mp3, wav, ogg, or opus.")
    with tempfile.TemporaryDirectory(prefix="fishie-convert-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, f"converted.{output_format}")
        Path(input_path).write_bytes(data)
        probe = _probe_path(input_path)
        command = ["ffmpeg", "-y", "-i", input_path]
        if output_format in {"mp4", "mov"}:
            if not probe.has_video:
                raise ValueError(f"{output_format.upper()} conversion requires video.")
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                ]
            )
        elif output_format == "webm":
            if not probe.has_video:
                raise ValueError("WebM conversion requires video.")
            command.extend(
                [
                    "-c:v",
                    "libvpx-vp9",
                    "-crf",
                    "30",
                    "-b:v",
                    "0",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "96k",
                ]
            )
        elif output_format == "gif":
            if not probe.has_video:
                raise ValueError("GIF conversion requires video.")
            command.extend(
                [
                    "-filter_complex",
                    "fps=12,scale='min(640,iw)':-2:flags=lanczos,"
                    "split[s0][s1];[s0]palettegen=max_colors=192[p];"
                    "[s1][p]paletteuse=dither=bayer:bayer_scale=4",
                    "-an",
                ]
            )
        elif output_format == "mp3":
            if not probe.has_audio:
                raise ValueError("MP3 conversion requires audio.")
            command.extend(["-vn", "-c:a", "libmp3lame", "-q:a", "2"])
        elif output_format == "wav":
            if not probe.has_audio:
                raise ValueError("WAV conversion requires audio.")
            command.extend(["-vn", "-c:a", "pcm_s16le"])
        elif output_format == "ogg":
            if not probe.has_audio:
                raise ValueError("OGG conversion requires audio.")
            command.extend(["-vn", "-c:a", "libvorbis", "-q:a", "5"])
        else:
            if not probe.has_audio:
                raise ValueError("Opus conversion requires audio.")
            command.extend(["-vn", "-c:a", "libopus", "-b:a", "128k"])
        command.append(output_path)
        _run(command)
        return EffectResult(
            Path(output_path).read_bytes(),
            f"converted-{index}.{output_format}",
            displayable=output_format in {"mp4", "webm", "gif"},
        )


convert_media = to_thread(convert_media_sync)

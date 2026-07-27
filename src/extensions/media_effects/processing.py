from __future__ import annotations

import io
import json
import math
import os
import random
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal, Sequence, cast

import numpy as np
from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageOps,
    UnidentifiedImageError,
)

from utils import to_thread

MAX_FRAME_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 150_000_000
MAX_ANIMATION_FRAMES = 300
MAX_MEDIA_DURATION = 180.0
MAX_MEDIA_DIMENSION = 4096
FFMPEG_TIMEOUT = 180

Image.MAX_IMAGE_PIXELS = MAX_FRAME_PIXELS


@dataclass(slots=True)
class EffectResult:
    data: bytes
    filename: str
    displayable: bool = True


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

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0


def _run(command: list[str], *, timeout: int = FFMPEG_TIMEOUT) -> None:
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            "That effect took longer than 3 minutes. Try a shorter or smaller file."
        ) from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip().splitlines()
        message = detail[-1] if detail else "ffmpeg could not process that file."
        raise ValueError(f"Could not process that media: {message[:300]}") from error


def _probe_path(path: str) -> MediaProbe:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height:format=duration,format_name",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            timeout=15,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("That file is not supported media.") from error

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Could not inspect that media file.") from error

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
    )
    if not probe.has_video and not probe.has_audio:
        raise ValueError("That file does not contain video or audio.")
    if duration > MAX_MEDIA_DURATION:
        raise ValueError("Media effects are limited to files 3 minutes or shorter.")
    if width > MAX_MEDIA_DIMENSION or height > MAX_MEDIA_DIMENSION:
        raise ValueError("Media effects are limited to 4096 pixels per side.")
    return probe


def _load_image_frames(data: bytes) -> tuple[list[Image.Image], list[int], bool]:
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

    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(frame_count):
        opened.seek(index)
        frames.append(opened.convert("RGBA"))
        durations.append(max(20, int(opened.info.get("duration") or 100)))
    return frames, durations, frame_count > 1


def _save_frames(
    frames: list[Image.Image],
    durations: list[int],
    *,
    filename: str,
    jpeg_quality: int | None = None,
) -> EffectResult:
    if not frames:
        raise ValueError("The effect did not produce any frames.")
    output = BytesIO()
    if len(frames) > 1:
        frames[0].save(
            output,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
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
    return frame.filter(ImageFilter.GaussianBlur(radius))


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


def _remove_vertical_seam(array: np.ndarray) -> np.ndarray:
    height, width = array.shape[:2]
    if width <= 2:
        return array
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

    keep = np.ones((height, width), dtype=bool)
    keep[np.arange(height), seam] = False
    return array[keep].reshape(height, width - 1, array.shape[2])


def _carve_to(array: np.ndarray, width: int, height: int) -> np.ndarray:
    while array.shape[1] > width:
        array = _remove_vertical_seam(array)
    if array.shape[0] > height:
        array = np.transpose(array, (1, 0, 2))
        while array.shape[1] > height:
            array = _remove_vertical_seam(array)
        array = np.transpose(array, (1, 0, 2))
    return array


def _magik_working_frame(frame: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    original_size = frame.size
    working = ImageOps.contain(frame, (384, 384), Image.Resampling.LANCZOS)
    return working.convert("RGBA"), original_size


def _liquid_rescale(frame: Image.Image, strength: float) -> Image.Image:
    ratio = _magik_ratio(strength)
    working, original_size = _magik_working_frame(frame)
    array = np.asarray(working).copy()
    target_width = max(2, round(array.shape[1] * ratio))
    target_height = max(2, round(array.shape[0] * ratio))
    carved = _carve_to(array, target_width, target_height)
    result = Image.fromarray(carved, "RGBA").resize(
        working.size,
        Image.Resampling.LANCZOS,
    )
    return result.resize(original_size, Image.Resampling.LANCZOS)


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
    source = np.asarray(frame)
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
    return Image.fromarray(source[source_y, source_x], "RGBA")


def _overlay(frame: Image.Image, options: dict[str, Any]) -> Image.Image:
    overlay_data = options.get("overlay_data")
    if not isinstance(overlay_data, bytes):
        raise ValueError("A second image is required.")
    opacity = float(options.get("opacity", 0.5))
    scale = float(options.get("scale", 1.0))
    stretch = bool(options.get("stretch"))
    if not 0 <= opacity <= 1:
        raise ValueError("Opacity must be between 0 and 1.")
    if not 0.05 <= scale <= 2:
        raise ValueError("Overlay scale must be between 0.05 and 2.")
    try:
        with Image.open(BytesIO(overlay_data)) as opened:
            second = opened.convert("RGBA")
    except UnidentifiedImageError as error:
        raise ValueError("The overlay must be an image.") from error

    target_width = max(1, round(frame.width * scale))
    target_height = max(1, round(frame.height * scale))
    if stretch:
        second = second.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
    else:
        second.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
    alpha = second.getchannel("A").point(
        [round(value * opacity) for value in range(256)]
    )
    second.putalpha(alpha)
    result = frame.copy()
    result.alpha_composite(
        second,
        ((frame.width - second.width) // 2, (frame.height - second.height) // 2),
    )
    return result


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
        raise ValueError("Could not project that texture onto the shape.") from error
    return tuple(float(value) for value in coefficients)


def _rotate_project_vertices(
    vertices: Sequence[tuple[float, float, float]],
    angle: float,
    *,
    size: int,
    pitch_degrees: float = -10,
    scale: float = 82,
) -> tuple[list[tuple[float, float]], list[tuple[float, float, float]]]:
    yaw_cos = math.cos(angle)
    yaw_sin = math.sin(angle)
    pitch = math.radians(pitch_degrees)
    pitch_cos = math.cos(pitch)
    pitch_sin = math.sin(pitch)
    center = size / 2
    camera_distance = 5.0
    rotated: list[tuple[float, float, float]] = []
    projected: list[tuple[float, float]] = []
    for x, y, z in vertices:
        yaw_x = x * yaw_cos + z * yaw_sin
        yaw_z = -x * yaw_sin + z * yaw_cos
        pitch_y = y * pitch_cos - yaw_z * pitch_sin
        pitch_z = y * pitch_sin + yaw_z * pitch_cos
        perspective = camera_distance / (camera_distance - pitch_z)
        rotated.append((yaw_x, pitch_y, pitch_z))
        projected.append(
            (
                center + yaw_x * scale * perspective,
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

    canvas = Image.new("RGBA", (size, size), "white")
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
) -> tuple[list[Image.Image], list[int]]:
    if not 0.25 <= speed <= 4:
        raise ValueError("Rotation speed must be between 0.25 and 4.")
    count = max(13, min(78, round(39 / speed)))
    duration = 50
    direction = -1 if clockwise else 1
    frames: list[Image.Image] = []
    for index in range(count):
        source = ImageOps.fit(
            source_frames[index % len(source_frames)],
            (256, 256),
            Image.Resampling.LANCZOS,
        )
        angle = direction * 2 * math.pi * index / count
        frames.append(_render_textured_polyhedron(source, angle, shape))
    return frames, [duration] * count


def _video_filter(effect: str, options: dict[str, Any]) -> tuple[str, str]:
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
        return f"gblur=sigma={radius:g}", "blur.mp4"
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


def _render_video_visual(
    data: bytes, effect: str, options: dict[str, Any]
) -> EffectResult:
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
        )

    if effect == "crop":
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

        filter_value = f"geq=r='{channel('r')}':g='{channel('g')}':b='{channel('b')}'"
    else:
        filter_value, filename = _video_filter(effect, options)
    with tempfile.TemporaryDirectory(prefix="fishie-image-effect-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, "output.mp4")
        Path(input_path).write_bytes(data)
        probe = _probe_path(input_path)
        if not probe.has_video:
            raise ValueError("That effect requires an image, GIF, or video.")
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-filter_complex",
                f"[0:v]{filter_value}[v]",
                "-map",
                "[v]",
                "-map",
                "0:a?",
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
                output_path,
            ]
        )
        return EffectResult(Path(output_path).read_bytes(), filename)


def render_image_effect_sync(
    data: bytes,
    effect: str,
    **options: Any,
) -> EffectResult:
    try:
        frames, durations, animated = _load_image_frames(data)
    except NotPillowMedia:
        return _render_video_visual(data, effect, options)

    if effect in STATIC_EFFECTS:
        transformed = [STATIC_EFFECTS[effect](frame, options) for frame in frames]
        jpeg_quality = int(options.get("quality", 8)) if effect == "jpeg" else None
        return _save_frames(
            transformed,
            durations,
            filename=effect,
            jpeg_quality=jpeg_quality if not animated else None,
        )
    if effect == "magik":
        strength = float(options.get("strength", 20))
        transformed = [_liquid_rescale(frame, strength) for frame in frames]
        return _save_frames(transformed, durations, filename="magik")
    if effect == "swirl":
        strength = float(options.get("strength", 180))
        transformed = [_swirl_frame(frame, strength) for frame in frames]
        return _save_frames(transformed, durations, filename="swirl")
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
        return _save_frames(generated, generated_durations, filename=effect)
    if effect in {"cube", "pyramid"}:
        generated, generated_durations = _shape_frames(
            frames,
            shape=cast(Literal["cube", "pyramid"], effect),
            speed=float(options.get("speed", 1)),
            clockwise=bool(options.get("clockwise")),
        )
        return _save_frames(generated, generated_durations, filename=effect)
    raise ValueError("Unknown image effect.")


render_image_effect = to_thread(render_image_effect_sync)


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
    output_extension: str = "mp4",
    map_arguments: list[str] | None = None,
    shortest: bool = False,
) -> EffectResult:
    with tempfile.TemporaryDirectory(prefix="fishie-video-effect-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, f"output.{output_extension}")
        Path(input_path).write_bytes(input_data)
        probe = _probe_path(input_path)
        second_path = os.path.join(directory, "second.media")
        command = ["ffmpeg", "-y", "-i", input_path]
        if second_data is not None:
            Path(second_path).write_bytes(second_data)
            if second_loop:
                command.extend(["-stream_loop", "-1"])
            command.extend(["-i", second_path])
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

        if output_extension == "mp4":
            if probe.has_video:
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
                    ]
                )
            if probe.has_audio or second_data is not None:
                command.extend(["-c:a", "aac", "-b:a", "128k"])
            command.extend(["-movflags", "+faststart"])
        elif output_extension == "webm":
            if probe.has_video:
                command.extend(["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"])
            if probe.has_audio or second_data is not None:
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


def render_video_effect_sync(
    input_data: bytes,
    effect: str,
    *,
    second_data: bytes | None = None,
    **options: Any,
) -> EffectResult:
    if effect == "reverse":
        try:
            image_frames, image_durations, animated = _load_image_frames(input_data)
        except NotPillowMedia:
            pass
        else:
            if animated:
                return _save_frames(
                    list(reversed(image_frames)),
                    list(reversed(image_durations)),
                    filename="reverse",
                )

    with tempfile.TemporaryDirectory(prefix="fishie-video-probe-") as directory:
        path = os.path.join(directory, "input.media")
        Path(path).write_bytes(input_data)
        probe = _probe_path(path)

    if effect == "reverse":
        if probe.has_video:
            return _reverse_video_chunked(input_data, probe)
        return _video_command_output(
            input_data,
            filename="reverse",
            audio_filter="areverse",
            output_extension="mp3",
        )

    if effect == "overlay":
        if second_data is None:
            raise ValueError("A second video or image is required.")
        opacity = float(options.get("opacity", 0.7))
        scale = float(options.get("scale", 0.5))
        stretch = bool(options.get("stretch"))
        if not 0 <= opacity <= 1 or not 0.05 <= scale <= 2:
            raise ValueError("Opacity must be 0 to 1 and scale must be 0.05 to 2.")
        scale_filter = "w=main_w:h=main_h" if stretch else f"w=main_w*{scale:g}:h=-1"
        return _video_command_output(
            input_data,
            filename="overlay-video",
            second_data=second_data,
            second_loop=True,
            filter_complex=(
                f"[1:v][0:v]scale2ref={scale_filter}[over][base];"
                f"[over]format=rgba,colorchannelmixer=aa={opacity:g}[overa];"
                "[base][overa]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
            ),
            map_arguments=["-map", "[v]", "-map", "0:a?"],
            shortest=True,
        )

    if effect in {"bassboost", "basslower"}:
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        gain = float(options.get("gain", 12))
        if not 1 <= gain <= 30:
            raise ValueError("Bass gain must be between 1 and 30 dB.")
        if effect == "basslower":
            gain = -gain
        return _video_command_output(
            input_data,
            filename="bass-boost" if gain > 0 else "bass-lower",
            audio_filter=f"bass=g={gain:g}:f=110:w=0.6",
            output_extension="mp4" if probe.has_video else "mp3",
        )

    if effect == "audioreverse":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        return _video_command_output(
            input_data,
            filename="audio-reverse",
            audio_filter="areverse",
            output_extension="mp4" if probe.has_video else "mp3",
        )

    if effect == "audioreverb":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        room = float(options.get("room", 0.5))
        if not 0.1 <= room <= 1:
            raise ValueError("Reverb room size must be between 0.1 and 1.")
        decay_one = min(0.9, room * 0.7)
        decay_two = min(0.8, room * 0.45)
        return _video_command_output(
            input_data,
            filename="audio-reverb",
            audio_filter=f"aecho=0.8:0.8:60|120:{decay_one:g}|{decay_two:g}",
            output_extension="mp4" if probe.has_video else "mp3",
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

    if effect == "audiodestroy":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        amount = int(options.get("amount", 6))
        if not 2 <= amount <= 12:
            raise ValueError("Destroy amount must be between 2 and 12.")
        return _video_command_output(
            input_data,
            filename="audio-destroyed",
            audio_filter=(
                f"acrusher=bits={max(2, 14 - amount)}:mix=1:mode=lin,"
                f"aresample={max(4000, 22000 - amount * 1500)},aresample=44100"
            ),
            output_extension="mp4" if probe.has_video else "mp3",
        )

    if effect == "audiocompress":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        ratio = float(options.get("ratio", 4))
        if not 1 <= ratio <= 20:
            raise ValueError("Compression ratio must be between 1 and 20.")
        return _video_command_output(
            input_data,
            filename="audio-compressed",
            audio_filter=(
                f"acompressor=threshold=-18dB:ratio={ratio:g}:"
                "attack=20:release=250:makeup=4"
            ),
            output_extension="mp4" if probe.has_video else "mp3",
        )

    if effect == "channelscombine":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        return _video_command_output(
            input_data,
            filename="mono",
            audio_filter="aformat=channel_layouts=mono",
            output_extension="mp4" if probe.has_video else "mp3",
        )

    if effect == "volume":
        if not probe.has_audio:
            raise ValueError("That file does not contain audio.")
        volume = float(options.get("volume", 1))
        if not 0 <= volume <= 10:
            raise ValueError("Volume must be between 0 and 10.")
        return _video_command_output(
            input_data,
            filename="volume",
            audio_filter=f"volume={volume:g}",
            output_extension="mp4" if probe.has_video else "mp3",
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
    if output_format not in {"mp4", "webm", "gif", "mp3", "wav", "ogg"}:
        raise ValueError("Format must be mp4, webm, gif, mp3, wav, or ogg.")
    with tempfile.TemporaryDirectory(prefix="fishie-convert-") as directory:
        input_path = os.path.join(directory, "input.media")
        output_path = os.path.join(directory, f"converted.{output_format}")
        Path(input_path).write_bytes(data)
        probe = _probe_path(input_path)
        command = ["ffmpeg", "-y", "-i", input_path]
        if output_format == "mp4":
            if not probe.has_video:
                raise ValueError("MP4 conversion requires video.")
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
        else:
            if not probe.has_audio:
                raise ValueError("OGG conversion requires audio.")
            command.extend(["-vn", "-c:a", "libvorbis", "-q:a", "5"])
        command.append(output_path)
        _run(command)
        return EffectResult(
            Path(output_path).read_bytes(),
            f"converted-{index}.{output_format}",
            displayable=output_format in {"mp4", "webm", "gif"},
        )


convert_media = to_thread(convert_media_sync)

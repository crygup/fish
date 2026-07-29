from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import emoji
from cachetools import TTLCache
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .network import fetch_public_bytes

CUSTOM_EMOJI_RE = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>[0-9]{17,20})>"
)
TWEMOJI_URL = (
    "https://raw.githubusercontent.com/jdecked/twemoji/main/"
    "assets/72x72/{codepoints}.png"
)
INLINE_IMAGE_CACHE: TTLCache[str, bytes] = TTLCache[str, bytes](
    maxsize=2048,
    ttl=24 * 60 * 60,
)
INLINE_IMAGE_HOSTS = {
    "cdn.discordapp.com",
    "cdn.discordapp.net",
    "raw.githubusercontent.com",
}
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024

_FONT_DIRECTORY = Path("/usr/share/fonts/truetype/noto")
_CJK_FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
_DEFAULT_FONT_PATHS = (
    _FONT_DIRECTORY / "NotoSans-Bold.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
_IMPACT_FONT_PATHS = tuple(
    path
    for path in (
        (
            Path(os.environ["FISH_IMPACT_FONT"])
            if os.environ.get("FISH_IMPACT_FONT")
            else None
        ),
        Path("/app/src/files/fonts/Impact.ttf"),
        Path("/app/src/files/fonts/impact.ttf"),
    )
    if path is not None
)
_MONO_FONT_PATHS = (
    _FONT_DIRECTORY / "NotoSansMono-Regular.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
)
_SCRIPT_FONTS: tuple[tuple[tuple[int, int], str], ...] = (
    ((0x0590, 0x05FF), "NotoSansHebrew-Bold.ttf"),
    ((0x0600, 0x08FF), "NotoSansArabic-Bold.ttf"),
    ((0x0900, 0x097F), "NotoSansDevanagari-Bold.ttf"),
    ((0x0980, 0x09FF), "NotoSansBengali-Bold.ttf"),
    ((0x0A00, 0x0A7F), "NotoSansGurmukhi-Bold.ttf"),
    ((0x0A80, 0x0AFF), "NotoSansGujarati-Bold.ttf"),
    ((0x0B00, 0x0B7F), "NotoSansOriya-Bold.ttf"),
    ((0x0B80, 0x0BFF), "NotoSansTamil-Bold.ttf"),
    ((0x0C00, 0x0C7F), "NotoSansTelugu-Bold.ttf"),
    ((0x0C80, 0x0CFF), "NotoSansKannada-Bold.ttf"),
    ((0x0D00, 0x0D7F), "NotoSansMalayalam-Bold.ttf"),
    ((0x0D80, 0x0DFF), "NotoSansSinhala-Bold.ttf"),
    ((0x0E00, 0x0E7F), "NotoSansThai-Bold.ttf"),
    ((0x0E80, 0x0EFF), "NotoSansLao-Bold.ttf"),
    ((0x1000, 0x109F), "NotoSansMyanmar-Bold.ttf"),
    ((0x10A0, 0x10FF), "NotoSansGeorgian-Bold.ttf"),
    ((0x1200, 0x137F), "NotoSansEthiopic-Bold.ttf"),
    ((0x1780, 0x17FF), "NotoSansKhmer-Bold.ttf"),
)
_CJK_RANGES = (
    (0x2E80, 0x2FFF),
    (0x3040, 0x30FF),
    (0x3100, 0x312F),
    (0x31A0, 0x31BF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)


@dataclass(frozen=True, slots=True)
class InlineToken:
    kind: Literal["text", "image"]
    value: str


def _codepoints(value: str) -> str:
    return "-".join(
        f"{ord(character):x}" for character in value if ord(character) != 0xFE0F
    )


def _image_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges = [
        (match.start(), match.end(), match.group(0))
        for match in CUSTOM_EMOJI_RE.finditer(text)
    ]
    custom_spans = [(start, end) for start, end, _ in ranges]
    for item in emoji.emoji_list(text):
        start = int(item["match_start"])
        end = int(item["match_end"])
        if any(
            start < custom_end and end > custom_start
            for custom_start, custom_end in custom_spans
        ):
            continue
        ranges.append((start, end, str(item["emoji"])))
    return sorted(ranges)


def inline_image_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(value for _, _, value in _image_ranges(text)))


def tokenize_inline_text(text: str) -> list[InlineToken]:
    result: list[InlineToken] = []
    position = 0
    for start, end, value in _image_ranges(text):
        if start > position:
            result.append(InlineToken("text", text[position:start]))
        result.append(InlineToken("image", value))
        position = end
    if position < len(text):
        result.append(InlineToken("text", text[position:]))
    return result or [InlineToken("text", "")]


def _inline_image_url(token: str) -> str:
    custom = CUSTOM_EMOJI_RE.fullmatch(token)
    if custom is not None:
        extension = "gif" if custom.group("animated") else "png"
        return (
            f"https://cdn.discordapp.com/emojis/{custom.group('id')}.{extension}"
            "?quality=lossless"
        )
    return TWEMOJI_URL.format(codepoints=_codepoints(token))


async def resolve_inline_images(session: Any, texts: list[str]) -> dict[str, bytes]:
    tokens = list(
        dict.fromkeys(token for text in texts for token in inline_image_tokens(text))
    )

    async def fetch(token: str) -> tuple[str, bytes] | None:
        try:
            return token, INLINE_IMAGE_CACHE[token]
        except KeyError:
            pass
        try:
            fetched = await fetch_public_bytes(
                session,
                _inline_image_url(token),
                max_bytes=MAX_INLINE_IMAGE_BYTES,
                allowed_content_prefixes=("image/",),
                allowed_hosts=INLINE_IMAGE_HOSTS,
            )
        except Exception:
            return None
        INLINE_IMAGE_CACHE[token] = fetched.data
        return token, fetched.data

    fetched = await asyncio.gather(*(fetch(token) for token in tokens))
    return dict(item for item in fetched if item is not None)


def _contains_range(text: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(
        start <= ord(character) <= end for character in text for start, end in ranges
    )


def text_font(
    text: str,
    size: int,
    *,
    mono: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths: list[Path] = []
    if _contains_range(text, _CJK_RANGES):
        paths.append(_CJK_FONT)
    else:
        for (start, end), filename in _SCRIPT_FONTS:
            if any(start <= ord(character) <= end for character in text):
                paths.append(_FONT_DIRECTORY / filename)
                break
    paths.extend(_MONO_FONT_PATHS if mono else _DEFAULT_FONT_PATHS)
    paths.extend(_DEFAULT_FONT_PATHS)
    for path in paths:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def impact_font_path() -> Path | None:
    """Return a supplied Impact font path, if one is available."""
    return next((path for path in _IMPACT_FONT_PATHS if path.is_file()), None)


def meme_font(text: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Use Impact for Latin meme text and the normal script fallback otherwise."""
    if not _contains_range(text, _CJK_RANGES) and impact_font_path() is not None:
        try:
            return ImageFont.truetype(str(impact_font_path()), size)
        except OSError:
            pass
    return text_font(text, size)


def _font_group(character: str) -> str:
    codepoint = ord(character)
    if unicodedata.combining(character):
        return "inherited"
    if any(start <= codepoint <= end for start, end in _CJK_RANGES):
        return "cjk"
    for (start, end), filename in _SCRIPT_FONTS:
        if start <= codepoint <= end:
            return filename
    return "default"


def _text_runs(text: str) -> list[str]:
    if not text:
        return [""]
    runs: list[str] = []
    current = ""
    current_group = "default"
    for character in text:
        group = _font_group(character)
        if (
            group == "inherited"
            or character.isspace()
            or unicodedata.category(character)[0] in {"P", "S", "N"}
        ):
            current += character
            continue
        if current and group != current_group:
            runs.append(current)
            current = character
        else:
            current += character
        current_group = group
    if current:
        runs.append(current)
    return runs


def _font_size(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    return max(1, int(getattr(font, "size", 12)))


def _font_is_mono(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> bool:
    return "mono" in str(getattr(font, "path", "")).lower()


def _text_width(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    size = _font_size(font)
    mono = _font_is_mono(font)
    return sum(
        text_font(run, size, mono=mono).getlength(run) for run in _text_runs(text)
    )


@lru_cache(maxsize=512)
def _asset_frame(data: bytes, timestamp_ms: int, size: int) -> Image.Image:
    try:
        with Image.open(BytesIO(data)) as opened:
            frames = max(1, int(getattr(opened, "n_frames", 1)))
            if frames > 1:
                durations: list[int] = []
                total = 0
                for index in range(frames):
                    opened.seek(index)
                    duration = max(20, int(opened.info.get("duration") or 100))
                    durations.append(duration)
                    total += duration
                offset = timestamp_ms % max(1, total)
                elapsed = 0
                frame_index = 0
                for index, duration in enumerate(durations):
                    if offset < elapsed + duration:
                        frame_index = index
                        break
                    elapsed += duration
                opened.seek(frame_index)
            frame = opened.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError):
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))
    frame = ImageOps.contain(frame, (size, size), Image.Resampling.LANCZOS)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.alpha_composite(
        frame, ((size - frame.width) // 2, (size - frame.height) // 2)
    )
    return result


def inline_animation_duration(assets: dict[str, bytes]) -> int:
    longest = 0
    for data in assets.values():
        try:
            with Image.open(BytesIO(data)) as opened:
                if int(getattr(opened, "n_frames", 1)) <= 1:
                    continue
                duration = 0
                frame_count = int(getattr(opened, "n_frames", 1))
                for index in range(frame_count):
                    opened.seek(index)
                    duration += max(20, int(opened.info.get("duration") or 100))
                longest = max(longest, duration)
        except (UnidentifiedImageError, OSError, ValueError):
            continue
    return min(longest, 10_000)


def _token_width(
    token: InlineToken,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: int,
    assets: dict[str, bytes],
) -> float:
    if token.kind == "image" and token.value in assets:
        return float(image_size)
    return _text_width(token.value, font)


def measure_inline_tokens(
    tokens: list[InlineToken],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: int,
    assets: dict[str, bytes],
) -> float:
    return sum(_token_width(token, font, image_size, assets) for token in tokens)


def draw_inline_tokens(
    image: Image.Image,
    tokens: list[InlineToken],
    position: tuple[int, int],
    *,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: int,
    assets: dict[str, bytes],
    timestamp_ms: int = 0,
    fill: str | tuple[int, ...] = "black",
) -> None:
    draw = ImageDraw.Draw(image)
    x, y = position
    line_box = font.getbbox("Ag")
    image_y = round(y + (line_box[1] + line_box[3] - image_size) / 2)
    for token in tokens:
        data = assets.get(token.value) if token.kind == "image" else None
        if data is not None:
            emoji_image = _asset_frame(data, timestamp_ms, image_size)
            image.alpha_composite(emoji_image, (round(x), image_y))
            x += image_size
            continue
        for run in _text_runs(token.value):
            run_font = text_font(
                run,
                _font_size(font),
                mono=_font_is_mono(font),
            )
            draw.text((round(x), y), run, font=run_font, fill=fill)
            x += run_font.getlength(run)


def wrap_inline_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: int,
    assets: dict[str, bytes],
    max_width: int,
) -> list[list[InlineToken]]:
    atoms: list[InlineToken] = []
    for token in tokenize_inline_text(text):
        if token.kind == "image":
            atoms.append(token)
            continue
        atoms.extend(
            InlineToken("text", part) for part in re.findall(r"\s+|[^\s]+", token.value)
        )
    if not atoms:
        return [[InlineToken("text", "")]]

    expanded: list[InlineToken] = []
    for atom in atoms:
        if (
            atom.kind == "text"
            and not atom.value.isspace()
            and _token_width(atom, font, image_size, assets) > max_width
        ):
            expanded.extend(InlineToken("text", character) for character in atom.value)
        else:
            expanded.append(atom)

    lines: list[list[InlineToken]] = [[]]
    for atom in expanded:
        if atom.kind == "text" and atom.value.isspace() and not lines[-1]:
            continue
        candidate = [*lines[-1], atom]
        if (
            lines[-1]
            and measure_inline_tokens(candidate, font, image_size, assets) > max_width
        ):
            while (
                lines[-1]
                and lines[-1][-1].kind == "text"
                and lines[-1][-1].value.isspace()
            ):
                lines[-1].pop()
            lines.append([] if atom.kind == "text" and atom.value.isspace() else [atom])
        else:
            lines[-1].append(atom)
    return [line for line in lines if line] or [[InlineToken("text", "")]]


def fit_inline_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: int,
    assets: dict[str, bytes],
    max_width: int,
) -> list[InlineToken]:
    if (
        measure_inline_tokens(tokenize_inline_text(text), font, image_size, assets)
        <= max_width
    ):
        return tokenize_inline_text(text)
    truncated = text
    suffix = "…"
    while truncated:
        candidate = tokenize_inline_text(f"{truncated.rstrip()}{suffix}")
        if measure_inline_tokens(candidate, font, image_size, assets) <= max_width:
            return candidate
        truncated = truncated[:-1]
    return [InlineToken("text", suffix)]

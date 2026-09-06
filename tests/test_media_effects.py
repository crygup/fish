from __future__ import annotations

import asyncio
import json
import math
import random
import shutil
import subprocess
import threading
import traceback
from array import array
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageChops
from test_support import require_type

import extensions.media_effects.audio_effects as audio_effects_module
import extensions.media_effects.commands as media_effect_commands
import extensions.media_effects.delivery as media_effect_delivery
import extensions.media_effects.processing as media_effect_processing
import extensions.media_effects.runtime as media_effect_runtime

# Test doubles supply only the Discord/service fields exercised by each test.
from core import Fishie
from extensions.context import Context
from extensions.fun import Fun
from extensions.media_effects import MediaEffects
from extensions.media_effects.audio_effects import (
    audio_effect_catalog,
    audio_effect_data,
    find_audio_effect,
)
from extensions.media_effects.commands import (
    PIPELINE_EFFECTS,
    RANDOM_EFFECTS,
    Images,
    _country_flag_code,
    _extract_sound_effect_selector,
    _finalize_pipeline_result,
    _normalize_effect_options,
    _parse_effect_flags,
    _parse_effect_pipeline,
    _parse_meme_argument,
    _parse_overlay_selector,
    _parse_overlay_text_argument,
    _playback_factor,
    _prepare_sound_effect_options,
    _random_effect_choices,
    _random_overlay_display,
    _random_overlay_kind,
    _random_overlay_label,
    _randomize_effect_timing,
    _randomize_overlay_options,
    _resolve_pipeline_random_effects,
    _restricted_flag_media,
    _select_pipeline_sound_effect,
    _sound_effect_autocomplete,
    _speed_audio_sync,
    _speed_video_sync,
    media_effect_timeout,
)
from extensions.media_effects.fonts import EFFECT_FONTS, find_effect_font, font_names
from extensions.media_effects.image_assets import image_asset_catalog
from extensions.media_effects.processing import (
    HEAVY_EFFECT_MAX_GIF_FRAMES,
    MAGIK_GIF_WORKING_SIZE,
    MAGIK_MAX_GIF_FRAMES,
    MAGIK_WORKING_SIZE,
    EffectResult,
    _adhd_segments,
    _magik_working_frame,
    _overlay,
    _recursive_zoom_frame,
    _resize,
    _sample_heavy_animation,
    _split_meme_text,
    compress_media_to_size_sync,
    convert_media_sync,
    make_flag_asset,
    probe_media_sync,
    render_average_colors_sync,
    render_combine_effect_sync,
    render_image_effect_sync,
    render_overlay_effect_sync,
    render_sound_effect_batch_sync,
    render_text_effect_sync,
    render_video_effect_sync,
    repair_gif_sync,
)
from extensions.media_effects.runtime import cancellable_to_thread
from extensions.media_effects.subprocesses import run_media_command
from extensions.media_effects.video_assets import video_asset_catalog
from utils.converters import (
    KlipyUrlConverter,
    MediaConverter,
    TenorUrlConverter,
    TwemojiConverter,
)


def _png_bytes() -> bytes:
    output = BytesIO()
    image = Image.new("RGBA", (32, 24), (240, 20, 10, 255))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), (x * 7, y * 10, (x + y) * 4, 255))
    image.save(output, "PNG")
    return output.getvalue()


def _odd_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (435, 435), (240, 20, 10, 255)).save(output, "PNG")
    return output.getvalue()


def _gif_bytes() -> bytes:
    output = BytesIO()
    first = Image.new("RGBA", (24, 24), (255, 0, 0, 255))
    second = Image.new("RGBA", (24, 24), (0, 0, 255, 255))
    first.save(
        output,
        "GIF",
        save_all=True,
        append_images=[second],
        duration=[80, 120],
        loop=0,
    )
    return output.getvalue()


def _gif_with_unused_transparency_index() -> bytes:
    image = Image.new("P", (20, 20), 0)
    palette = [255, 255, 255, 255, 0, 0, 0, 0, 0] + [0] * (256 * 3 - 9)
    image.putpalette(palette)
    for x in range(7, 13):
        for y in range(7, 13):
            image.putpixel((x, y), 1)
    output = BytesIO()
    image.save(output, "GIF", transparency=2, optimize=False)
    return output.getvalue()


def _sample_video() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=8:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _sample_video_for_duration(duration: float) -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=64x48:rate=8:duration={duration:g}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={duration:g}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _sample_video_with_short_audio() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=8:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _sample_audio(source: str) -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout


def _audio_rms(data: bytes, duration: float = 0.3) -> float:
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-t",
            f"{duration:g}",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "44100",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=True,
    ).stdout
    samples = array("h", decoded)
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _sample_outro_video() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x96:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x080812:size=96x96:rate=10:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


@lru_cache(maxsize=1)
def _sample_long_video() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=32x24:rate=4:duration=31",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=31",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _sample_letterboxed_video() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=8:duration=0.5",
            "-vf",
            "pad=64:96:0:24:black",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _first_video_frame(data: bytes) -> Image.Image:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=True,
    )
    with Image.open(BytesIO(result.stdout)) as frame:
        return frame.convert("RGB")


def test_effect_text_flags_keep_media_and_parse_options() -> None:
    media, options = _parse_effect_flags(
        "https://example.com/a.gif --speed=2 -clockwise",
        values={"speed": (("s",), float, 1.0)},
        switches={"clockwise": ("c",)},
    )
    assert media == "https://example.com/a.gif"
    assert options == {"speed": 2.0, "clockwise": True}


def test_effect_text_flags_accept_a_separated_dash_and_leave_no_fake_media() -> None:
    media, options = _parse_effect_flags(
        "-start 5 - stop 10",
        values=PIPELINE_EFFECTS["audionightcore"][1],
    )
    assert media == ""
    assert options == {"start": 5.0, "stop": 10.0, "duration": 0.0}


def test_effect_pipeline_keeps_order_and_effect_specific_flags() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.gif invert blur -radius 8 -type motion pixelate -size 10"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("invert", {"preserve_transparency": False}),
        (
            "blur",
            {
                "radius": 8.0,
                "blur_type": "motion",
                "position": "",
                "shape": "square",
            },
        ),
        ("pixelate", {"size": 10}),
    ]
    assert skipped == []


def test_effect_pipeline_accepts_caption_text_and_keeps_later_steps() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.gif caption text here invert"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("caption", {"text": "text here"}),
        ("invert", {"preserve_transparency": False}),
    ]
    assert skipped == []


def test_effect_pipeline_recognizes_qualified_image_commands() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.gif crop triangle mirror top fade out "
        "-duration 3 globe -speed 2 -clockwise"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("crop", {"shape": "triangle"}),
        ("mirror", {"direction": "top"}),
        ("fadeout", {"duration": 3.0}),
        ("globe", {"speed": 2.0, "axis": "y", "rotation": 0.0, "clockwise": True}),
    ]
    assert skipped == []


def test_effect_pipeline_accepts_caption_and_meme_text() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        'https://example.com/a.gif caption "top text" meme "bottom text"'
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("caption", {"text": "top text"}),
        ("meme", {"text": "bottom text", "meme_no_split": True}),
    ]
    assert skipped == []


def test_meme_accepts_positional_media_in_either_order_and_keeps_later_links() -> None:
    media, options = _parse_meme_argument(
        "hello world https://example.com/first.png https://example.com/second.png"
    )
    assert media == "https://example.com/first.png"
    assert options["text"] == "hello world https://example.com/second.png"

    media, options = _parse_meme_argument(
        "https://example.com/first.png hello world",
        attachment_available=False,
    )
    assert media == "https://example.com/first.png"
    assert options["text"] == "hello world"


def test_meme_scale_is_trailing_and_supports_top_and_bottom_values() -> None:
    media, options = _parse_meme_argument(
        "https://example.com/first.png hello world -scale 4 1"
    )
    assert media == "https://example.com/first.png"
    assert options["scale_top"] == 4.0
    assert options["scale_bottom"] == 1.0

    # A non-trailing scale token is caption text; hyphens anywhere in the
    # caption must not trigger a parser error.
    media, options = _parse_meme_argument(
        "-scale 4 https://example.com/first.png hello"
    )
    assert media == "https://example.com/first.png"
    assert options["text"] == "-scale 4 hello"

    media, options = _parse_meme_argument(
        "https://example.com/first.png test -scale test"
    )
    assert media == "https://example.com/first.png"
    assert options["text"] == "test -scale test"


def test_meme_attachment_text_is_positional_and_quoted_long_text_stays_on_top() -> None:
    _, attachment_options = _parse_meme_argument(
        "hello world",
        attachment_available=True,
    )
    assert attachment_options["text"] == "hello world"

    long_text = (
        '"hello world today i went to the store and bought some grapes and then '
        'ran into someone i had not seen in a long time."'
    )
    _, quoted_options = _parse_meme_argument(long_text, attachment_available=True)
    assert quoted_options["meme_no_split"] is True


def test_meme_accepts_mentions_and_emoji_as_positional_media() -> None:
    media, options = _parse_meme_argument(
        "<@891372917978452028> hello world",
    )
    assert media == "<@891372917978452028>"
    assert options["text"] == "hello world"

    media, options = _parse_meme_argument(
        "hello world <:custom:123456789012345678>",
    )
    assert media == "<:custom:123456789012345678>"
    assert options["text"] == "hello world"

    media, options = _parse_meme_argument("😂 hello world")
    assert media == "😂"
    assert options["text"] == "hello world"


def test_meme_splits_short_captions_evenly_and_honors_explicit_controls() -> None:
    assert _split_meme_text("hello world") == ("hello", "world")
    assert _split_meme_text("hello world", allow_auto_split=False) == (
        "hello world",
        "",
    )
    assert _split_meme_text("hello | world") == ("hello", "world")


@pytest.mark.asyncio
async def test_meme_caption_mentions_use_display_names() -> None:
    images = object.__new__(Images)
    images.bot = cast(
        "Fishie",
        SimpleNamespace(
            get_user=lambda _: SimpleNamespace(display_name="hatt"),
            fetch_user=None,
        ),
    )
    ctx = SimpleNamespace(
        message=SimpleNamespace(mentions=()),
        guild=None,
    )
    result = await images._resolve_meme_mentions(
        cast("Context", ctx),
        "**Paul** given to <@891372917978452028>",
    )
    assert result == "**Paul** given to @hatt"


def test_effect_pipeline_recognizes_overlay_group_commands() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.gif overlay flag lesbian "
        "overlay image -overlay https://example.com/logo.png -opacity 0.5"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("overlayflag", {"flag": "lesbian", "opacity": 35.0}),
        (
            "overlay",
            {
                "overlay": "https://example.com/logo.png",
                "opacity": 0.5,
                "scale": 1.0,
                "size": "",
                "position": "center",
                "x": 0,
                "y": 0,
                "start": 0.0,
                "stop": 0.0,
                "stretch": False,
                "extend": False,
                "no_audio": False,
            },
        ),
    ]
    assert skipped == []


def test_effect_pipeline_accepts_random_overlay_sources() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.png overlay image -overlay random emoji"
    )
    assert source == "https://example.com/a.png"
    assert effects[0][0] == "overlay"
    assert effects[0][1]["overlay"] == "random emoji"
    assert skipped == []


def test_overlay_text_parser_accepts_positional_and_flagged_media() -> None:
    source, overlay, options = _parse_overlay_text_argument("base.png overlay.mp4")
    assert (source, overlay) == ("base.png", "overlay.mp4")
    assert options["opacity"] == 70.0

    source, overlay, options = _parse_overlay_text_argument(
        "base.png -overlay overlay.mp4 -size 100x100 -extend"
    )
    assert (source, overlay) == ("base.png", "overlay.mp4")
    assert options["size"] == "100x100"
    assert options["extend"] is True

    source, overlay, _ = _parse_overlay_text_argument("base.png random emoji")
    assert (source, overlay) == ("base.png", "random emoji")

    source, overlay, options = _parse_overlay_text_argument("base.png random -fr")
    assert (source, overlay) == ("base.png", "random")
    assert options["fullrandom"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("user random", ("user", None)),
        ("user notyyton", ("user", "notyyton")),
        (
            "media https://example.com/overlay.mp4",
            ("media", "https://example.com/overlay.mp4"),
        ),
        ("asset image", ("image", None)),
        ("image random", ("image", None)),
        ("flag United States", ("flag", "United States")),
        ("random flag de", ("flag", "de")),
    ),
)
def test_overlay_selector_supports_named_sources_and_aliases(
    value: str,
    expected: tuple[str, str | None],
) -> None:
    assert _parse_overlay_selector(value) == expected


def test_overlay_text_parser_accepts_named_source_with_flags() -> None:
    source, overlay, options = _parse_overlay_text_argument(
        "base.png flag United States -opacity 35 -fr"
    )
    assert (source, overlay) == ("base.png", "flag United States")
    assert options["opacity"] == 35.0
    assert options["fullrandom"] is True


def test_overlay_text_parser_accepts_short_opacity_flag() -> None:
    source, overlay, options = _parse_overlay_text_argument(
        "base.png image logo.png -op 42"
    )

    assert (source, overlay) == ("base.png", "image logo.png")
    assert options["opacity"] == 42.0


@pytest.mark.parametrize(
    ("argument", "expected_overlay"),
    (
        ("base.png video", "video"),
        ("base.png video 67", "video 67"),
        (
            "base.png video https://example.com/overlay.mp4",
            "video https://example.com/overlay.mp4",
        ),
        ("base.png image", "image"),
        ("base.png image 67", "image 67"),
        ("base.png image Bad Apple", "image Bad Apple"),
        (
            "base.png image https://example.com/overlay.png",
            "image https://example.com/overlay.png",
        ),
    ),
)
def test_overlay_text_parser_accepts_video_and_image_selectors(
    argument: str,
    expected_overlay: str,
) -> None:
    source, overlay, options = _parse_overlay_text_argument(argument)

    assert source == "base.png"
    assert overlay == expected_overlay
    assert options["opacity"] == 70.0


@pytest.mark.parametrize("kind", ("video", "image"))
def test_overlay_text_parser_keeps_full_random_with_typed_sources(kind: str) -> None:
    source, overlay, options = _parse_overlay_text_argument(f"base.png {kind} 67 -fr")

    assert source == "base.png"
    assert overlay == f"{kind} 67"
    assert options["fullrandom"] is True


def test_asset_remains_an_alias_for_image_overlay_sources() -> None:
    assert _parse_overlay_selector("asset") == _parse_overlay_selector("image")
    assert _parse_overlay_selector("asset 67") == _parse_overlay_selector("image 67")
    assert _parse_overlay_selector("asset Bad Apple") == _parse_overlay_selector(
        "image Bad Apple"
    )


def test_overlay_text_parser_rejects_more_than_two_media_items() -> None:
    with pytest.raises(commands.BadArgument, match="background and one overlay"):
        _parse_overlay_text_argument("base.png overlay.png extra.png")


@pytest.mark.parametrize(
    ("pipeline", "expected_overlay", "full_random"),
    (
        ("base.mp4 overlay", "random", False),
        ("base.mp4 overlay video", "video", False),
        ("base.mp4 overlay video 67", "video 67", False),
        (
            "base.mp4 overlay video https://example.com/overlay.mp4",
            "video https://example.com/overlay.mp4",
            False,
        ),
        ("base.mp4 overlay image", "image", False),
        ("base.mp4 overlay image 67", "image 67", False),
        ("base.mp4 overlay image Bad Apple", "image Bad Apple", False),
        (
            "base.mp4 overlay image https://example.com/overlay.png",
            "image https://example.com/overlay.png",
            False,
        ),
        ("base.mp4 overlay video 67 -fr", "video 67", True),
        ("base.mp4 overlay image Bad Apple -fr", "image Bad Apple", True),
    ),
)
def test_effect_pipeline_accepts_typed_overlay_sources(
    pipeline: str,
    expected_overlay: str,
    full_random: bool,
) -> None:
    source, effects, skipped = _parse_effect_pipeline(pipeline)

    assert source == "base.mp4"
    assert len(effects) == 1
    assert effects[0][0] == "overlay"
    assert effects[0][1]["overlay"] == expected_overlay
    assert bool(effects[0][1].get("fullrandom")) is full_random
    assert skipped == []


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("random", "all"),
        ("random emoji", "emoji"),
        ("random user", "user"),
        ("random asset", "image"),
    ),
)
def test_random_overlay_kind(value: str, expected: str) -> None:
    assert _random_overlay_kind(value) == expected


def test_random_overlay_kind_rejects_unknown_sources() -> None:
    with pytest.raises(commands.BadArgument, match="must be emoji, user, image, video"):
        _random_overlay_kind("random sound")


def test_random_overlay_labels_are_readable() -> None:
    assert _random_overlay_label("😄") == "grinning face with smiling eyes"
    assert _random_overlay_label("<:party:123>") == "party"
    assert _random_overlay_display("user: notyyton") == "overlay user notyyton"
    assert _random_overlay_display("video: 67") == "overlay video 67"


def test_full_random_overlay_stays_inside_the_background() -> None:
    options: dict[str, Any] = {
        "size": "100x100",
        "scale": 2.0,
        "position": "center",
        "x": 200,
        "y": -200,
        "stretch": True,
    }
    _randomize_overlay_options(options, random.Random(0))

    assert 0.1 <= options["scale"] <= 1.0
    assert options["position"] in media_effect_commands.RANDOM_OVERLAY_POSITIONS
    assert options["x"] == options["y"] == 0
    assert options["stretch"] is False
    assert "size" not in options


def test_random_asset_overlay_returns_bundled_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threaded_reads: list[Any] = []

    async def fake_to_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        threaded_reads.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(media_effect_commands.asyncio, "to_thread", fake_to_thread)
    ctx = SimpleNamespace(
        guild=None,
        author=SimpleNamespace(display_avatar=None),
        bot=SimpleNamespace(custom_emojis={}, user=None),
        session=None,
    )
    images = Images.__new__(Images)
    data, label = asyncio.run(
        images._random_overlay_data(cast("Context", ctx), "random image")
    )

    assert threaded_reads
    assert label.startswith("image: ")
    if data.startswith((b"\x89PNG", b"GIF8", b"RIFF")):
        with Image.open(BytesIO(data)) as image:
            assert image.width > 0
            assert image.height > 0
    else:
        assert data[4:8] == b"ftyp"


@pytest.mark.parametrize("selector", ("1", "Hattori"))
def test_image_asset_overlay_accepts_id_or_name(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    async def fake_to_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(media_effect_commands.asyncio, "to_thread", fake_to_thread)
    ctx = SimpleNamespace(
        guild=None,
        author=SimpleNamespace(display_avatar=None),
        bot=SimpleNamespace(custom_emojis={}, user=None),
        session=None,
    )
    images = Images.__new__(Images)
    data, label = asyncio.run(
        images._random_overlay_data(cast("Context", ctx), f"image {selector}")
    )

    assert label == "image: Hattori"
    assert data.startswith(b"\x89PNG")


def test_uploaded_video_overlay_resolves_an_approved_library_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, tuple[Any, ...]]] = []

    class Pool:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            queries.append((query, args))
            return {
                "id": 67,
                "source_url": "https://cdn.discordapp.com/attachments/video.mp4",
                "review_message_id": 123,
            }

    async def fake_refresh(_bot: Any, url: str) -> str:
        return url

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=b"approved-video")

    monkeypatch.setattr(
        media_effect_commands,
        "refresh_discord_attachment_url",
        fake_refresh,
    )
    monkeypatch.setattr(media_effect_commands, "fetch_public_bytes", fake_fetch)
    images = Images.__new__(Images)
    images.bot = cast(
        Any,
        SimpleNamespace(
            pool=Pool(),
            session=object(),
            get_cog=lambda _name: None,
        ),
    )

    ctx = cast(Any, SimpleNamespace(session=object()))
    data, label = asyncio.run(images._approved_video_overlay(ctx, "67"))

    assert data == b"approved-video"
    assert label == "video: 67"
    assert queries and queries[0][1] == (67,)
    assert "status = 'approved'" in queries[0][0]


def test_typed_video_url_bypasses_the_uploaded_video_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[tuple[str, bool]] = []

    async def resolve(
        _ctx: Any,
        source: str,
        *,
        scan_messages: bool,
    ) -> str:
        resolved.append((source, scan_messages))
        return source

    async def fetch(_ctx: Any, source: str) -> bytes:
        assert source == "https://example.com/overlay.mp4"
        return b"url-video"

    async def unexpected_library(_selector: str | None) -> tuple[bytes, str]:
        raise AssertionError("A direct video URL must not query the video library")

    images = Images.__new__(Images)
    images._resolve_effect_media = cast(Any, resolve)
    images._fetch_effect_media = cast(Any, fetch)
    images._approved_video_overlay = cast(Any, unexpected_library)

    data, label = asyncio.run(
        images._random_overlay_data(
            cast(Any, SimpleNamespace()),
            "video https://example.com/overlay.mp4",
        )
    )

    assert data == b"url-video"
    assert label == "video: https://example.com/overlay.mp4"
    assert resolved == [("https://example.com/overlay.mp4", False)]


def test_typed_image_url_bypasses_the_bundled_image_catalog() -> None:
    resolved: list[tuple[str, bool]] = []

    async def resolve(
        _ctx: Any,
        source: str,
        *,
        scan_messages: bool,
    ) -> str:
        resolved.append((source, scan_messages))
        return source

    async def fetch(_ctx: Any, source: str) -> bytes:
        assert source == "https://example.com/overlay.png"
        return b"url-image"

    images = Images.__new__(Images)
    images._resolve_effect_media = cast(Any, resolve)
    images._fetch_effect_media = cast(Any, fetch)

    data, label = asyncio.run(
        images._random_overlay_data(
            cast(Any, SimpleNamespace()),
            "image https://example.com/overlay.png",
        )
    )

    assert data == b"url-image"
    assert label == "image: https://example.com/overlay.png"
    assert resolved == [("https://example.com/overlay.png", False)]


def test_untyped_random_overlay_can_choose_an_approved_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pool:
        async def fetchval(self, _query: str) -> int:
            return 1

    class PickVideo:
        def choice(self, values: Any) -> Any:
            return "video" if "video" in values else values[0]

    async def approved(_ctx: Any, selector: str | None) -> tuple[bytes, str]:
        assert selector is None
        return b"approved-video", "video: 67"

    monkeypatch.setattr(media_effect_commands.random_module, "SystemRandom", PickVideo)
    images = Images.__new__(Images)
    images.bot = cast(Any, SimpleNamespace(pool=Pool()))
    images._approved_video_overlay = cast(Any, approved)
    ctx = SimpleNamespace(
        guild=None,
        author=SimpleNamespace(display_avatar=None),
        bot=SimpleNamespace(custom_emojis={}, user=None),
        session=None,
    )

    data, label = asyncio.run(
        images._random_overlay_data(cast("Context", ctx), "random")
    )

    assert data == b"approved-video"
    assert label == "video: 67"


def test_effect_pipeline_recognizes_audio_group_commands() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.mp4 audio extract audio replace "
        "-audio https://example.com/replacement.mp3 audio channels-combine"
    )
    assert source == "https://example.com/a.mp4"
    assert effects == [
        ("extract", {}),
        (
            "audioreplace",
            {"audio": "https://example.com/replacement.mp3"},
        ),
        (
            "channelscombine",
            {"start": 0.0, "stop": 0.0, "duration": 0.0},
        ),
    ]
    assert skipped == []


def test_effect_pipeline_parses_random_categories_and_sound_timing() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.mp4 random audio -nr random overlay -fr "
        "audio sound-effect -effect six-seven -at 4 -start 1 -cutoff 2"
    )
    assert source == "https://example.com/a.mp4"
    assert effects == [
        (
            "random",
            {
                "norandom": True,
                "fullrandom": False,
                "category": "audio",
            },
        ),
        (
            "random",
            {
                "norandom": False,
                "fullrandom": True,
                "category": "overlay",
            },
        ),
        (
            "soundeffect",
            {
                "effect": "six-seven",
                "at": 4.0,
                "source_start": 1.0,
                "source_stop": 2.0,
                "duration": 0.0,
                "volume": 1.0,
                "pitch": 0.0,
                "speed": 1.0,
                "random_time": True,
                "fade_in": 0.0,
                "fade_out": 0.0,
                "loop": False,
            },
        ),
    ]
    assert skipped == []


def test_animated_effect_pipeline_names_keep_old_aliases() -> None:
    for name, expected in (
        ("amagik", "gifmagik"),
        ("animatedmagik", "gifmagik"),
        ("gmagik", "gifmagik"),
        ("gifmagik", "gifmagik"),
        ("aswirl", "gifswirl"),
        ("animatedswirl", "gifswirl"),
        ("gswirl", "gifswirl"),
        ("gifswirl", "gifswirl"),
    ):
        _, effects, skipped = _parse_effect_pipeline(
            f"https://example.com/a.gif {name}"
        )
        assert effects[0][0] == expected
        assert skipped == []


def test_animated_text_commands_keep_legacy_aliases() -> None:
    assert Images.amagik.name == "amagik"
    assert set(Images.amagik.aliases) == {
        "animatedmagik",
        "gmagik",
        "gifmagik",
    }
    assert Images.aswirl.name == "aswirl"
    assert set(Images.aswirl.aliases) == {
        "animatedswirl",
        "gifswirl",
        "gswirl",
    }


def test_media_effect_timeout_returns_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "extensions.media_effects.commands.MEDIA_EFFECT_TIMEOUT",
        0.001,
    )

    @media_effect_timeout
    async def slow_effect() -> None:
        await asyncio.sleep(0.05)

    with pytest.raises(commands.BadArgument, match="longer than 60 seconds"):
        asyncio.run(slow_effect())


def test_run_progress_cleanup_only_deletes_its_separate_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ProgressMessage:
        async def delete(self) -> None:
            events.append("delete-progress")

    async def scenario() -> None:
        progress = object.__new__(media_effect_commands._RunProgress)

        class FakeContext:
            async def send_new(self, _content: str) -> ProgressMessage:
                events.append("send-progress")
                progress.done.set()
                return ProgressMessage()

            async def send(self, _content: str) -> None:
                raise AssertionError("Progress must not use the cached response")

        progress.ctx = cast(Any, FakeContext())
        progress.total = 3
        progress.step = 1
        progress.label = "invert"
        progress.changed = asyncio.Event()
        progress.done = asyncio.Event()
        progress.owner = None

        async def expire_immediately(awaitable: Any, *, timeout: float) -> None:
            assert timeout == 60
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(
            media_effect_commands.asyncio,
            "wait_for",
            expire_immediately,
        )
        await progress._report()

    asyncio.run(scenario())
    assert events == ["send-progress", "delete-progress"]


def test_run_progress_edits_the_original_interaction_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    class InteractionResponse:
        def __init__(self) -> None:
            self.done = False

        def is_done(self) -> bool:
            return self.done

        async def defer(self) -> None:
            self.done = True
            events.append("defer")

    class Interaction:
        def __init__(self) -> None:
            self.response = InteractionResponse()

        async def edit_original_response(self, *, content: str) -> object:
            events.append(("edit", content))
            return object()

    async def scenario() -> None:
        progress = object.__new__(media_effect_commands._RunProgress)
        interaction = Interaction()

        class FakeContext:
            def __init__(self) -> None:
                self.interaction = interaction
                self._previous_message: object | None = None

            async def send_new(self, _content: str) -> None:
                raise AssertionError("Application commands must edit their response")

        progress.ctx = cast(Any, FakeContext())
        progress.total = 2
        progress.step = 1
        progress.label = "invert"
        progress.changed = asyncio.Event()
        progress.done = asyncio.Event()
        progress.owner = None

        async def expire_immediately(awaitable: Any, *, timeout: float) -> None:
            assert timeout == 60
            awaitable.close()
            progress.done.set()
            raise TimeoutError

        monkeypatch.setattr(
            media_effect_commands.asyncio,
            "wait_for",
            expire_immediately,
        )
        await progress._report()
        assert progress.ctx._previous_message is not None

    asyncio.run(scenario())
    assert events[0] == "defer"
    assert events[1][0] == "edit"


def test_effect_pipeline_has_a_bounded_step_count() -> None:
    source, effects, skipped = _parse_effect_pipeline(" ".join(["invert"] * 67))
    assert source == ""
    assert len(effects) == 67
    assert skipped == []
    with pytest.raises(commands.BadArgument, match="up to 67"):
        _parse_effect_pipeline(" ".join(["invert"] * 68))


def test_effect_pipeline_expands_repeat_syntax_before_enforcing_the_cap() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/media.gif invert x3 blurx2 -radius 4"
    )
    assert source == "https://example.com/media.gif"
    assert [effect for effect, _ in effects] == [
        "invert",
        "invert",
        "invert",
        "blur",
        "blur",
    ]
    assert effects[-1][1]["radius"] == 4
    assert skipped == []

    with pytest.raises(commands.BadArgument, match="expanded effect pipeline"):
        _parse_effect_pipeline("invert x67 blur")
    with pytest.raises(commands.BadArgument, match="repetition must be between"):
        _parse_effect_pipeline("invert x0")


def test_pipeline_random_effects_allow_duplicate_choices_and_report_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("invert",))
    resolved = _resolve_pipeline_random_effects(
        [
            ("random", {}),
            ("invert", {"preserve_transparency": False}),
            ("random", {}),
            ("random", {}),
        ]
    )
    random_steps = [step for step in resolved if step[1].startswith("random (")]
    chosen = [effect for effect, _, _ in random_steps]
    assert chosen == ["invert", "invert", "invert"]
    assert all(label == f"random ({effect})" for effect, label, _ in random_steps)
    assert resolved[1] == (
        "invert",
        "invert",
        {"preserve_transparency": False},
    )


def test_pipeline_random_randomizes_numeric_options_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("blur",))
    resolved = _resolve_pipeline_random_effects([("random", {})])
    assert 0.1 <= float(resolved[0][2]["radius"]) <= 50
    assert resolved[0][2]["blur_type"] == "gaussian"


def test_pipeline_random_pixelate_uses_a_readable_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("pixelate",))
    for _ in range(25):
        resolved = _resolve_pipeline_random_effects([("random", {})])
        assert 2 <= int(resolved[0][2]["size"]) <= 24
    explicit, _adjustments = _normalize_effect_options("pixelate", {"size": 128})
    assert explicit["size"] == 128


def test_pipeline_random_flags_control_option_randomization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("blur",))
    no_random = _resolve_pipeline_random_effects(
        [("random", {"norandom": True, "fullrandom": False})]
    )
    assert no_random[0][2] == {
        "radius": 5.0,
        "blur_type": "gaussian",
        "position": "",
        "shape": "square",
    }

    full_random = _resolve_pipeline_random_effects(
        [("random", {"norandom": False, "fullrandom": True})]
    )
    assert 0.1 <= float(full_random[0][2]["radius"]) <= 50
    assert full_random[0][2]["blur_type"] in {"gaussian", "box", "motion"}


@pytest.mark.parametrize("full_random", (False, True))
def test_pipeline_random_timing_is_ordered_and_within_the_media(
    monkeypatch: pytest.MonkeyPatch,
    full_random: bool,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("sharpen",))
    resolved = _resolve_pipeline_random_effects(
        [("random", {"fullrandom": full_random})],
        media_duration=4.0,
    )
    options = resolved[0][2]
    assert 0 <= float(options["start"]) <= 4.0
    if "stop" in options:
        assert float(options["start"]) < float(options["stop"]) <= 4.0
    assert "duration" not in options
    assert 0 <= float(options["amount"]) <= 5


def test_random_timing_has_optional_end_and_respects_explicit_values() -> None:
    class TimingRandom:
        def __init__(self, end_chance: float):
            self.end_chance = end_chance

        def uniform(self, minimum: float, maximum: float) -> float:
            return (minimum + maximum) / 2

        def random(self) -> float:
            return self.end_chance

    no_end = {"start": 0.0, "stop": 0.0, "duration": 0.0}
    _randomize_effect_timing("sharpen", no_end, 10.0, cast(Any, TimingRandom(0.25)))
    assert 0 < no_end["start"] < 10
    assert "stop" not in no_end
    assert "duration" not in no_end

    with_end = {"start": 0.0, "stop": 0.0}
    _randomize_effect_timing("sharpen", with_end, 10.0, cast(Any, TimingRandom(0.75)))
    assert with_end["start"] < with_end["stop"] <= 10

    resolved = _resolve_pipeline_random_effects(
        [("random", {"start": 1.25, "stop": 2.5})],
        media_duration=10,
    )
    assert resolved[0][2]["start"] == 1.25
    assert resolved[0][2]["stop"] == 2.5


def test_sharpen_is_clamped_to_ffmpegs_supported_range() -> None:
    options, adjustments = _normalize_effect_options("sharpen", {"amount": 10})
    assert options["amount"] == 5
    assert adjustments
    result = render_image_effect_sync(_sample_video(), "sharpen", **options)
    assert result.filename == "sharpen.mp4"
    assert result.data


def test_pipeline_random_audio_requires_audio_and_keeps_random_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_AUDIO_EFFECTS",
        ("audiopitch",),
    )
    resolved = _resolve_pipeline_random_effects(
        [("random", {"category": "audio", "norandom": True})],
        allow_audio=True,
    )
    assert resolved == [
        (
            "audiopitch",
            "random audio (audiopitch)",
            {
                "semitones": 3.0,
                "start": 0.0,
                "stop": 0.0,
                "duration": 0.0,
            },
        )
    ]
    with pytest.raises(commands.BadArgument, match="needs media with an audio"):
        _resolve_pipeline_random_effects(
            [("random", {"category": "audio"})],
            allow_audio=False,
        )


def test_pipeline_random_video_includes_visual_and_video_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_EFFECTS",
        ("blur", "huerotate"),
    )
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_VIDEO_EFFECTS",
        ("reverse", "speed"),
    )
    resolved = _resolve_pipeline_random_effects(
        [("random", {})],
        allow_visual=True,
        media_is_video=True,
    )
    assert resolved[0][0] in {"blur", "huerotate", "reverse", "speed"}


def test_media_timing_help_does_not_expose_the_safety_cap() -> None:
    for command in MediaEffects(cast(Any, SimpleNamespace())).walk_commands():
        help_text = command.help or ""
        assert "0 to 600" not in help_text


def test_timed_media_usage_documents_start_and_stop_flags() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    for name in (
        "invert",
        "magik",
        "hallway",
        "blur",
        "exposure",
        "overlay flag",
    ):
        command = next(
            command for command in cog.walk_commands() if command.qualified_name == name
        )
        usage = str(command.extras.get("usage", ""))
        assert "-start" in usage and "-stop" in usage


def test_overlay_help_uses_image_and_video_but_hides_asset_alias() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    command = next(
        command
        for command in cog.walk_commands()
        if command.qualified_name == "overlay"
    )
    visible_help = f"{command.help or ''}\n{command.extras.get('usage', '')}".casefold()

    assert "image" in visible_help
    assert "video" in visible_help
    assert "asset" not in visible_help


def test_pipeline_random_overlay_selects_a_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands, "RANDOM_OVERLAY_EFFECTS", ("overlayflag",)
    )
    resolved = _resolve_pipeline_random_effects(
        [("random", {"category": "overlay", "norandom": True})]
    )
    assert resolved[0][0] == "overlayflag"
    assert resolved[0][1].startswith("random overlay (")
    assert resolved[0][2]["flag"] in media_effect_commands.PRIDE_FLAGS


def test_pipeline_random_overlay_can_select_a_random_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_OVERLAY_EFFECTS", ("overlay",))
    resolved = _resolve_pipeline_random_effects(
        [("random", {"category": "overlay", "norandom": True})]
    )
    assert resolved[0][0] == "overlay"
    assert resolved[0][2]["overlay"] == "random"


def test_pipeline_random_overlay_randomizes_image_layout_and_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_OVERLAY_EFFECTS", ("overlay",))
    resolved = _resolve_pipeline_random_effects(
        [
            (
                "random",
                {"category": "overlay"},
            )
        ],
        media_duration=8.0,
    )
    options = resolved[0][2]
    assert options["overlay"] == "random"
    assert 0.1 <= float(options["scale"]) <= 1.0
    assert options["position"] in media_effect_commands.RANDOM_OVERLAY_POSITIONS
    assert options["stretch"] is False
    assert 0 <= float(options["start"]) <= 8.0
    if "stop" in options:
        assert float(options["start"]) < float(options["stop"]) <= 8.0


def test_pipeline_random_flag_uses_random_layout_opacity_and_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_OVERLAY_EFFECTS",
        ("overlayflag",),
    )
    resolved = _resolve_pipeline_random_effects(
        [("random", {"category": "overlay"})],
        media_duration=8.0,
    )
    options = resolved[0][2]
    assert options["flag"] in media_effect_commands.PRIDE_FLAGS
    assert 0 <= float(options["opacity"]) <= 100
    assert 0.1 <= float(options["scale"]) <= 1.0
    assert options["position"] in media_effect_commands.RANDOM_OVERLAY_POSITIONS
    assert options["stretch"] is False
    assert 0 <= float(options["start"]) <= 8.0
    if "stop" in options:
        assert float(options["start"]) < float(options["stop"]) <= 8.0


def test_pipeline_full_random_overlay_randomizes_bounded_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_OVERLAY_EFFECTS", ("overlay",))
    resolved = _resolve_pipeline_random_effects(
        [
            (
                "random",
                {"category": "overlay", "norandom": True, "fullrandom": True},
            )
        ]
    )
    options = resolved[0][2]
    assert 0.1 <= options["scale"] <= 1.0
    assert options["position"] in media_effect_commands.RANDOM_OVERLAY_POSITIONS
    assert options["x"] == options["y"] == 0
    assert options["stretch"] is False


def test_run_displays_random_overlay_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_notes: list[str] = []
    probed_inputs: list[bytes] = []

    async def resolve_media(*_args: Any, **_kwargs: Any) -> str:
        return "https://example.com/media.png"

    async def fetch_media(*_args: Any, **_kwargs: Any) -> bytes:
        return b"input"

    async def probe_media(data: bytes, **_kwargs: Any) -> Any:
        probed_inputs.append(data)
        return SimpleNamespace(has_audio=False, has_video=True, duration=1.0)

    async def random_overlay(*_args: Any, **_kwargs: Any) -> tuple[bytes, str]:
        return b"overlay", "user: notyyton"

    async def render_overlay(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=b"result", filename="overlay.png", displayable=True)

    async def render_image(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            data=b"image-result", filename="removebars.png", displayable=True
        )

    async def send_result(
        _ctx: Any,
        _result: Any,
        *,
        started: float,
        note: str = "",
    ) -> None:
        assert started > 0
        sent_notes.append(note)

    @asynccontextmanager
    async def typing() -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(
        media_effect_commands,
        "_parse_effect_pipeline",
        lambda _pipeline: (
            "",
            [("removebars", {}), ("overlay", {"overlay": "random"})],
            [],
        ),
    )
    monkeypatch.setattr(media_effect_commands, "probe_media", probe_media)
    monkeypatch.setattr(media_effect_commands, "render_image_effect", render_image)
    monkeypatch.setattr(media_effect_commands, "render_overlay_effect", render_overlay)

    instance = SimpleNamespace(
        bot=SimpleNamespace(media_semaphore=asyncio.Semaphore(1)),
        _resolve_effect_media=resolve_media,
        _fetch_effect_media=fetch_media,
        _random_overlay_data=random_overlay,
        _send_effect_result=send_result,
    )
    instance._run_pipeline_special_effect = Images._run_pipeline_special_effect.__get__(
        instance,
        Images,
    )
    ctx = SimpleNamespace(guild=None, typing=typing)

    asyncio.run(
        Images._run_effect_pipeline(
            cast(Any, instance),
            cast(Any, ctx),
            "removebars overlay image random",
        )
    )

    assert "Applied: removebars, overlay user notyyton" in sent_notes[0]
    assert probed_inputs[:3] == [b"input", b"image-result", b"result"]


def test_image_asset_catalog_contains_only_normalized_media() -> None:
    catalog = image_asset_catalog()
    assert catalog
    assert all(asset.path.is_file() for asset in catalog)
    assert all(
        asset.format == "PNG" or asset.animated or asset.path.name == "pfp.jpg"
        for asset in catalog
    )
    assert {asset.path.name for asset in catalog} >= {
        "hattori.png",
        "hattori2.png",
        "pfp.jpg",
    }


def test_video_asset_catalog_contains_only_bundled_videos() -> None:
    catalog = video_asset_catalog()
    assert catalog
    assert [asset.id for asset in catalog] == list(range(1, len(catalog) + 1))
    assert all(asset.path.is_file() for asset in catalog)
    assert all(
        asset.path.suffix.casefold() in {".mp4", ".m4v", ".mov", ".webm"}
        for asset in catalog
    )
    assert {asset.path.name for asset in catalog} >= {
        "bad apple.mp4",
        "crab_rock.mp4",
        "subwaysurfers.mp4",
    }


def test_pipeline_accepts_reversed_random_categories_and_positional_speed() -> None:
    _, effects, skipped = _parse_effect_pipeline(
        "audio random -nr overlay random -fr speed -3"
    )
    assert effects == [
        (
            "random",
            {"category": "audio", "norandom": True, "fullrandom": False},
        ),
        (
            "random",
            {"category": "overlay", "norandom": False, "fullrandom": True},
        ),
        ("speed", {"speed": -3.0, "start": 0.0, "stop": 0.0}),
    ]
    assert skipped == []


def test_random_sound_effect_uses_random_time_without_pitch_or_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_AUDIO_EFFECTS",
        ("soundeffect",),
    )
    base = _resolve_pipeline_random_effects(
        [("random", {"category": "audio"})],
        allow_audio=True,
    )[0][2]
    assert base["effect"] == "random"
    assert base["random_time"] is True
    assert base["at"] == 0
    assert base["pitch"] == 0
    assert base["speed"] == 1

    no_random = _resolve_pipeline_random_effects(
        [("random", {"category": "audio", "norandom": True})],
        allow_audio=True,
    )[0][2]
    assert no_random["random_time"] is False
    assert no_random["at"] == 0

    full_random = _resolve_pipeline_random_effects(
        [("random", {"category": "audio", "fullrandom": True})],
        allow_audio=True,
    )[0][2]
    assert full_random["random_time"] is True
    assert -12 <= float(full_random["pitch"]) <= 12
    assert 0.5 <= float(full_random["speed"]) <= 2


def test_audio_only_random_uses_only_audio_compatible_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_effect_commands,
        "RANDOM_AUDIO_EFFECTS",
        ("audioecho",),
    )
    resolved = _resolve_pipeline_random_effects(
        [("random", {})],
        allow_audio=True,
        allow_visual=False,
    )
    assert resolved[0][0] == "audioecho"

    with pytest.raises(commands.BadArgument, match="Random visual needs"):
        _resolve_pipeline_random_effects(
            [("random", {"category": "visual"})],
            allow_audio=True,
            allow_visual=False,
        )


def test_repeated_positional_random_sound_effects_stay_sound_effects() -> None:
    _, effects, skipped = _parse_effect_pipeline(
        "audio sound-effect random audio sound-effect random"
    )
    assert [effect for effect, _ in effects] == ["soundeffect", "soundeffect"]
    assert all(options["random_time"] is True for _, options in effects)
    assert all(options["effect"] == "random" for _, options in effects)
    assert skipped == []

    used: set[int] = set()
    selected = []
    for _, options in effects:
        sound = _select_pipeline_sound_effect(options["effect"], used)
        used.add(sound.id)
        selected.append(sound.id)
    assert len(set(selected)) == 2


@pytest.mark.parametrize(
    "pipeline",
    (
        "audio sfx audio sfx",
        "audio sfx random audio sfx random",
    ),
)
def test_repeated_audio_sfx_aliases_create_separate_steps(pipeline: str) -> None:
    source, effects, skipped = _parse_effect_pipeline(pipeline)
    assert source == ""
    assert [effect for effect, _ in effects] == ["soundeffect", "soundeffect"]
    assert all(options["effect"] == "random" for _, options in effects)
    assert skipped == []


def test_run_batches_consecutive_sound_effects_into_one_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []
    sent_notes: list[str] = []

    async def resolve_media(*_args: Any, **_kwargs: Any) -> str:
        return "https://example.com/video.mp4"

    async def fetch_media(*_args: Any, **_kwargs: Any) -> bytes:
        return b"input"

    async def probe_media(_data: bytes) -> Any:
        return SimpleNamespace(has_audio=True, has_video=True, duration=10.0)

    async def render_batch(
        _data: bytes,
        effects: list[tuple[bytes, float, dict[str, Any]]],
        **_kwargs: Any,
    ) -> Any:
        batch_sizes.append(len(effects))
        return SimpleNamespace(
            data=b"batched", filename="audio-overlay.mp4", displayable=True
        )

    async def send_result(
        _ctx: Any, _result: Any, *, started: float, note: str = ""
    ) -> None:
        assert started > 0
        sent_notes.append(note)

    @asynccontextmanager
    async def typing() -> AsyncIterator[None]:
        yield

    class Progress:
        def __init__(self, _ctx: Any, _total: int) -> None:
            pass

        def update(self, _step: int, _label: str) -> None:
            pass

        def close(self) -> None:
            pass

    first, second = audio_effect_catalog()[:2]
    monkeypatch.setattr(
        media_effect_commands,
        "_parse_effect_pipeline",
        lambda _pipeline: (
            "",
            [
                ("soundeffect", {"effect": str(first.id)}),
                ("soundeffect", {"effect": str(second.id)}),
            ],
            [],
        ),
    )
    monkeypatch.setattr(media_effect_commands, "probe_media", probe_media)
    monkeypatch.setattr(media_effect_commands, "_RunProgress", Progress)
    monkeypatch.setattr(
        media_effect_commands, "render_sound_effect_batch", render_batch
    )
    monkeypatch.setattr(
        media_effect_commands, "_image_animation_state", lambda _data: (False, False)
    )
    instance = SimpleNamespace(
        bot=SimpleNamespace(media_semaphore=asyncio.Semaphore(1)),
        _resolve_effect_media=resolve_media,
        _fetch_effect_media=fetch_media,
        _send_effect_result=send_result,
    )
    ctx = SimpleNamespace(guild=None, typing=typing)

    asyncio.run(
        Images._run_effect_pipeline(cast(Any, instance), cast(Any, ctx), "audio sfx x2")
    )

    assert batch_sizes == [2]
    assert first.display_name in sent_notes[0]
    assert second.display_name in sent_notes[0]


def test_sound_effect_random_time_and_full_random_flags() -> None:
    _, effects, skipped = _parse_effect_pipeline(
        "audio sound-effect random -fr audio sound-effect random -random_time false"
    )
    first = effects[0][1]
    second = effects[1][1]
    assert first["random_time"] is True
    assert -12 <= float(first["pitch"]) <= 12
    assert 0.5 <= float(first["speed"]) <= 2
    assert second["random_time"] is False
    assert second["pitch"] == 0
    assert second["speed"] == 1
    assert skipped == []


@pytest.mark.parametrize(
    ("argument", "expected_media"),
    (
        ("https://example.com/video.mp4 3", "https://example.com/video.mp4"),
        ("3 https://example.com/video.mp4", "https://example.com/video.mp4"),
    ),
)
def test_sound_effect_id_can_go_before_or_after_media(
    argument: str,
    expected_media: str,
) -> None:
    media, selector = _extract_sound_effect_selector(argument, "random")
    assert media == expected_media
    assert selector == "3"


def test_sound_effect_options_default_to_random_time() -> None:
    options = _prepare_sound_effect_options(
        {"random_time": True, "norandom": True, "fullrandom": False}
    )
    assert options["random_time"] is False
    assert "norandom" not in options
    assert "fullrandom" not in options


def test_sound_effect_picker_includes_random() -> None:
    choices = asyncio.run(_sound_effect_autocomplete(cast(Any, None), ""))
    assert choices[0].name == "Random"
    assert choices[0].value == "random"
    assert len(choices) <= 25


def test_signed_speed_semantics_and_timed_speed_processing() -> None:
    assert _playback_factor(5) == 5
    assert _playback_factor(3) == 3
    assert _playback_factor(-5) == pytest.approx(0.2)
    assert _playback_factor(-3) == pytest.approx(1 / 3)
    for invalid in (0, 0.5, -0.5, 6, -6):
        with pytest.raises(ValueError, match="Speed must be"):
            _playback_factor(invalid)

    result = _speed_video_sync(_sample_video(), -3, start=0.1, stop=0.35)
    assert result


def test_speed_supports_audio_only_media() -> None:
    result = _speed_audio_sync(
        _sample_audio("sine=frequency=440:duration=0.8"),
        2,
        start=0.1,
        stop=0.5,
    )
    assert result
    assert probe_media_sync(result).has_audio


def test_average_colors_renders_a_labeled_palette() -> None:
    result = render_image_effect_sync(_png_bytes(), "averagecolors")
    image = Image.open(BytesIO(result.data))
    assert result.filename == "averagecolors.png"
    assert image.width >= 320
    assert image.height >= 64


def test_average_colors_reports_percentages_and_rejects_gifs() -> None:
    result, colors = render_average_colors_sync(_png_bytes())
    assert result.filename == "averagecolors.png"
    assert colors
    assert all(len(color.rgb) == 3 for color in colors)
    assert sum(color.percentage for color in colors) == pytest.approx(100, abs=0.1)
    with pytest.raises(ValueError, match="still images"):
        render_average_colors_sync(_gif_bytes())


def test_embedfix_reencodes_opaque_gifs_without_transparency() -> None:
    result = repair_gif_sync(_gif_bytes())
    assert result.filename == "embedfix.gif"
    repaired = Image.open(BytesIO(result.data))
    assert getattr(repaired, "n_frames", 1) == 2
    assert "transparency" not in repaired.info
    assert probe_media_sync(result.data).duration > 0.1


def test_average_colors_is_not_a_pipeline_effect() -> None:
    with pytest.raises(commands.BadArgument, match="supported effect"):
        _parse_effect_pipeline("average-colors")


def test_bundled_audio_effect_catalog_has_stable_ids_and_special_names() -> None:
    catalog = audio_effect_catalog()
    assert [effect.id for effect in catalog] == list(range(1, len(catalog) + 1))
    assert len(catalog) == 138
    assert find_audio_effect("six-one").name == "six-one"
    assert find_audio_effect("six-seven").name == "six-seven"
    assert find_audio_effect("21").name == "rezero_death"
    assert find_audio_effect("22").name == "metal_gear_solid_alert"
    assert find_audio_effect("23").name == "joe_biden_soda"
    assert find_audio_effect(str(catalog[0].id)) == catalog[0]
    assert all(effect.path.is_file() for effect in catalog)


def test_audio_effect_catalog_skips_malformed_entries(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio_path = tmp_path / "valid.mp3"
    audio_path.write_bytes(b"ID3")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "effects": [
                    {
                        "id": "invalid",
                        "name": "broken",
                        "display_name": "Broken",
                        "category": "test",
                        "path": "valid.mp3",
                        "duration": 1.0,
                    },
                    {
                        "id": 1,
                        "name": "valid",
                        "display_name": "Valid",
                        "category": "test",
                        "path": "valid.mp3",
                        "duration": 1.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audio_effects_module, "AUDIO_EFFECTS_ROOT", tmp_path)
    monkeypatch.setattr(audio_effects_module, "AUDIO_EFFECTS_CATALOG", catalog_path)
    audio_effect_catalog.cache_clear()
    try:
        catalog = audio_effect_catalog()
    finally:
        audio_effect_catalog.cache_clear()

    assert [effect.name for effect in catalog] == ["valid"]


def test_numeric_effect_options_clamp_and_report_adjustment() -> None:
    options, adjustments = _normalize_effect_options(
        "overlay",
        {"opacity": 250.0, "scale": 0.01, "x": 10_000},
    )
    assert options == {"opacity": 100.0, "scale": 0.05, "x": 4096}
    assert len(adjustments) == 3
    assert all("Rounded overlay" in adjustment for adjustment in adjustments)


def test_overlay_opacity_only_changes_the_second_media() -> None:
    overlay_file = BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 255, 255)).save(overlay_file, "PNG")
    base = Image.new("RGBA", (2, 1), (255, 0, 0, 255))

    result = _overlay(
        base,
        {
            "overlay_data": overlay_file.getvalue(),
            "opacity": 0.5,
            "scale": 0.5,
            "position": "left",
        },
    )

    assert result.getpixel((1, 0)) == (255, 0, 0, 255)
    assert result.getpixel((0, 0)) == (127, 0, 128, 255)


def test_effect_fetch_refreshes_discord_attachment_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[Any, dict[str, Any]]] = []
    fetched: list[str] = []

    async def fake_request(route: Any, **kwargs: Any) -> dict[str, Any]:
        requested.append((route, kwargs))
        return {
            "refreshed_urls": [
                {
                    "refreshed": (
                        "https://cdn.discordapp.com/attachments/1/2/file.png?ex=fresh"
                    )
                }
            ]
        }

    async def fake_fetch(_session: Any, url: str, **_kwargs: Any) -> Any:
        fetched.append(url)
        return SimpleNamespace(data=b"image")

    monkeypatch.setattr(media_effect_commands, "fetch_public_bytes", fake_fetch)
    instance = SimpleNamespace(
        bot=SimpleNamespace(http=SimpleNamespace(request=fake_request))
    )
    ctx = SimpleNamespace(session=object())
    result = asyncio.run(
        Images._fetch_effect_media(
            cast(Any, instance),
            cast(Any, ctx),
            "https://cdn.discordapp.com/attachments/1/2/file.png?ex=old",
        )
    )

    assert result == b"image"
    assert requested[0][1]["json"] == {
        "attachment_urls": ["https://cdn.discordapp.com/attachments/1/2/file.png"]
    }
    assert fetched == ["https://cdn.discordapp.com/attachments/1/2/file.png?ex=fresh"]


def test_pipeline_timeout_skips_only_the_slow_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_notes: list[str] = []

    async def resolve_media(*_args: Any, **_kwargs: Any) -> str:
        return "https://example.com/media.png"

    async def fetch_media(*_args: Any, **_kwargs: Any) -> bytes:
        return b"input"

    async def probe_media(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(has_audio=False)

    async def render(data: bytes, effect: str, **_options: Any) -> Any:
        if effect == "invert":
            await asyncio.sleep(0.05)
        return SimpleNamespace(
            data=data + effect.encode(),
            filename=f"{effect}.png",
            displayable=True,
        )

    async def send_result(
        _ctx: Any,
        _result: Any,
        *,
        started: float,
        note: str = "",
    ) -> None:
        assert started > 0
        sent_notes.append(note)

    @asynccontextmanager
    async def typing() -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(media_effect_commands, "MEDIA_EFFECT_TIMEOUT", 0.001)
    monkeypatch.setattr(
        media_effect_commands,
        "_parse_effect_pipeline",
        lambda _pipeline: (
            "",
            [("invert", {}), ("grayscale", {})],
            [],
        ),
    )
    monkeypatch.setattr(media_effect_commands, "render_image_effect", render)
    monkeypatch.setattr(media_effect_commands, "probe_media", probe_media)

    instance = SimpleNamespace(
        bot=SimpleNamespace(media_semaphore=asyncio.Semaphore(1)),
        _resolve_effect_media=resolve_media,
        _fetch_effect_media=fetch_media,
        _send_effect_result=send_result,
    )
    ctx = SimpleNamespace(guild=None, typing=typing)

    asyncio.run(
        Images._run_effect_pipeline(
            cast(Any, instance),
            cast(Any, ctx),
            "invert grayscale",
        )
    )

    assert "Applied: grayscale" in sent_notes[0]
    assert "Skipped: invert (took longer than 60 seconds)" in sent_notes[0]


def test_pipeline_converts_final_klipy_video_result_to_gif(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, str, int]] = []

    async def fake_convert(data: bytes, output_format: str, index: int) -> Any:
        calls.append((data, output_format, index))
        return SimpleNamespace(data=b"compatible-gif", displayable=True)

    monkeypatch.setattr(media_effect_commands, "convert_media", fake_convert)
    result = asyncio.run(
        _finalize_pipeline_result(
            media_effect_commands.EffectResult(b"effect-video", "distort.mp4"),
            "https://static.klipy.com/example/source.mp4",
        )
    )

    assert result.data == b"compatible-gif"
    assert result.filename == "distort.gif"
    assert calls == [(b"effect-video", "gif", 1)]


def test_pipeline_keeps_existing_final_klipy_gif_without_reencoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_convert(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Existing GIF should not be converted again")

    monkeypatch.setattr(media_effect_commands, "convert_media", unexpected_convert)
    source = media_effect_commands.EffectResult(_gif_bytes(), "distort.gif")

    assert (
        asyncio.run(
            _finalize_pipeline_result(
                source,
                "https://static.klipy.com/example/source.gif",
            )
        )
        is source
    )


def test_random_pipeline_rejects_more_choices_than_can_be_unique() -> None:
    with pytest.raises(commands.BadArgument, match="unique random effects"):
        _random_effect_choices(len(RANDOM_EFFECTS) + 1)


def test_random_text_command_reports_the_selected_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, Any] = {}

    async def apply_effect(
        ctx: Any,
        effect: str,
        **options: Any,
    ) -> None:
        called.update(ctx=ctx, effect=effect, options=options)

    monkeypatch.setattr(
        media_effect_commands,
        "_random_effect_choices",
        lambda count: ["magik"],
    )
    fake_cog = SimpleNamespace(_apply_image_effect=apply_effect)
    fake_ctx = object()
    callback = cast(Any, Images.random_effect.callback)
    asyncio.run(
        callback(
            cast(Any, fake_cog),
            cast(Any, fake_ctx),
            argument="https://example.com/source.gif",
        )
    )

    assert called == {
        "ctx": fake_ctx,
        "effect": "magik",
        "options": {
            "source": "https://example.com/source.gif",
            "note": "Applied: random (magik)",
        },
    }


def test_overlay_attachment_only_invocation_uses_two_attachments() -> None:
    called: dict[str, Any] = {}
    attachments = [object(), object(), object()]

    async def apply_effect(
        ctx: Any,
        effect: str,
        **options: Any,
    ) -> None:
        called.update(ctx=ctx, effect=effect, options=options)

    fake_cog = SimpleNamespace(_apply_image_effect=apply_effect)
    fake_ctx = SimpleNamespace(message=SimpleNamespace(attachments=attachments))
    callback = cast(Any, Images.overlay_group.callback)

    asyncio.run(
        callback(
            cast(Any, fake_cog),
            cast(Any, fake_ctx),
            argument="",
        )
    )

    assert called["ctx"] is fake_ctx
    assert called["effect"] == "overlay"
    assert called["options"]["source"] == ""
    assert called["options"]["second_source"] == ""
    assert called["options"]["second_attachment"] is attachments[1]


def test_sideways_hallway_command_and_pipeline_effect_are_removed() -> None:
    assert isinstance(Images.hallway, commands.Command)
    assert not isinstance(Images.hallway, commands.Group)
    assert "hallwaysideways" not in PIPELINE_EFFECTS


def test_average_colors_text_aliases_are_available() -> None:
    assert set(Images.average_colors.aliases) >= {
        "average-colours",
        "avgcolours",
        "averagecolours",
    }


def test_image_and_video_application_groups_are_within_discord_limits() -> None:
    assert len(Images.image_effect.commands) == 24
    assert len(Images.effect_2.commands) == 25
    assert len(Images.effect_3.commands) == 19
    assert {command.name for command in Images.image_effect.commands} >= {
        "crop",
        "invert",
        "spin",
        "magik",
        "amagik",
        "flip",
        "cube",
        "pyramid",
        "blur",
        "deepfry",
        "grayscale",
        "jpeg",
        "swirl",
        "aswirl",
        "wiggle",
        "average-colors",
    }
    assert {command.name for command in Images.effect_2.commands} >= {
        "fade-in",
        "fade-out",
        "lag",
        "shuffle",
        "tint",
        "implode",
        "explode",
        "sharpen",
        "legoify",
        "bounce",
        "fisheye",
        "sepia",
        "pixelate",
        "slide-in",
        "slide-out",
        "vignette",
        "resize",
        "distort",
        "grain",
        "rotate",
        "noise",
        "brightness",
        "contrast",
        "saturation",
        "exposure",
    }
    assert {command.name for command in Images.effect_3.commands} == {
        "enlarge",
        "falsecolor",
        "glitch",
        "hallway",
        "huerotate",
        "meme",
        "oilpaint",
        "mirror",
        "overlay",
        "parallax",
        "quilt",
        "random",
        "remove",
        "squishy",
        "tremble",
        "watercolor",
        "zoom",
        "text",
        "combine",
    }
    assert isinstance(Images.reverse, commands.Command)
    assert not isinstance(Images.reverse, commands.HybridCommand)
    assert {command.name for command in Images.crop_group.commands} == {
        "circle",
        "triangle",
    }
    assert {command.name for command in Images.fade_group.commands} == {
        "in",
        "out",
    }
    assert {command.name for command in Images.mirror_group.commands} == {
        "bottom",
        "left",
        "right",
        "top",
    }
    assert {command.name for command in Images.overlay_group.commands} == {
        "flag",
    }
    assert {command.name for command in Images.audio_group.commands} == {
        "adhd",
        "channels-combine",
        "compress",
        "destroy",
        "extract",
        "deepvoice",
        "echo",
        "nightcore",
        "overlay",
        "pitch",
        "replace",
        "reverb",
        "reverse",
        "surround",
        "sound-effect",
        "underwater",
        "volume",
    }
    assert {command.name for command in Images.video_effects_audio.commands} == {
        command.name
        for command in Images.audio_group.commands
        if command.name != "adhd"
    }


def test_media_application_commands_fit_discords_size_limit() -> None:
    def command_size(value: object) -> int:
        if isinstance(value, dict):
            size = sum(
                len(str(item))
                for key, item in value.items()
                if key in {"name", "description", "value"}
                and isinstance(item, (str, int, float))
            )
            return size + sum(command_size(item) for item in value.values())
        if isinstance(value, list):
            return sum(command_size(item) for item in value)
        return 0

    cog = MediaEffects(cast(Any, SimpleNamespace()))
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    for group in (cog.image_effect, cog.effect_2, cog.effect_3):
        assert group.app_command is not None
        payload = group.app_command.to_dict(bot.tree)
        assert command_size(payload) <= 8_000, group.name


def test_rebalanced_media_application_groups_use_sequential_names() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    groups = [
        require_type(child, commands.HybridGroup).app_command
        for child in cog.__cog_commands__
        if child.parent is None
        and isinstance(getattr(child, "app_command", None), app_commands.Group)
    ] + [
        command
        for command in cog.__cog_app_commands__
        if isinstance(command, app_commands.Group)
    ]
    assert {group.name for group in groups} == {
        "effect",
        "effect-2",
        "effect-3",
        "effect-4",
        "effect-5",
        "effect-6",
    }


def test_rebalanced_media_groups_do_not_duplicate_leaf_names() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    groups = [
        require_type(child, commands.HybridGroup).app_command
        for child in cog.__cog_commands__
        if child.parent is None
        and isinstance(getattr(child, "app_command", None), app_commands.Group)
    ] + [
        command
        for command in cog.__cog_app_commands__
        if isinstance(command, app_commands.Group)
    ]
    names: dict[str, list[str]] = {}
    for group in groups:
        for child in group.commands:
            names.setdefault(child.name, []).append(group.name)

    assert all(len(group_names) == 1 for group_names in names.values())
    overlay_commands = [
        child for group in groups for child in group.commands if child.name == "overlay"
    ]
    assert len(overlay_commands) == 1
    assert isinstance(overlay_commands[0], app_commands.Command)
    assert "audio_media" in {
        parameter.name for parameter in overlay_commands[0].parameters
    }
    assert not any(
        child.name == "audio-overlay" for group in groups for child in group.commands
    )


def test_audio_app_commands_stay_under_the_effect_audio_namespace() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    root = cog.image_effect.app_command
    assert root is not None
    audio = next(child for child in root.commands if child.name == "audio")
    assert isinstance(audio, app_commands.Group)
    assert {child.name for child in root.commands} == {"audio"}
    assert cog._media_app_command_size(root) <= 7_600
    assert cog._media_app_command_size(audio) <= 7_600
    assert {child.name for child in audio.commands} >= {
        "reverse",
        "reverb",
        "pitch",
        "sound-effect",
    }
    assert not any(
        child.name == "audio-reverse"
        for command in cog.__cog_app_commands__
        if isinstance(command, app_commands.Group)
        for child in command.commands
    )


def test_every_app_media_option_uses_the_shared_name_and_description() -> None:
    cog = MediaEffects(cast(Any, SimpleNamespace()))
    found = 0

    def check(command: Any) -> None:
        nonlocal found
        if isinstance(command, app_commands.Group):
            for child in command.commands:
                check(child)
            return
        assert isinstance(command, app_commands.Command)
        for parameter in command.parameters:
            assert parameter.name != "media_url"
            assert parameter.name != "overlay_url"
            if parameter.name == "media":
                found += 1
                assert parameter.description == "User/Emoji/Media URL"
                assert "User/Emoji/Media URL" not in command.description
                assert "Numeric limits" not in command.description
                assert "\n" not in command.description
            if parameter.name in {
                "amount",
                "degrees",
                "duration",
                "gain",
                "opacity",
                "radius",
                "room",
                "scale",
                "semitones",
                "size",
                "speed",
                "stops",
                "strength",
                "tiles",
                "volume",
                "x",
                "y",
            }:
                assert " to " in parameter.description

    for app_command in cog.get_app_commands():
        check(app_command)
    assert found > 0


def test_every_requested_effect_is_registered_in_the_slash_groups() -> None:
    def qualified_children(group: commands.Group) -> set[str]:
        names: set[str] = set()
        for command in group.commands:
            if isinstance(command, commands.Group):
                names.update(
                    f"{command.name} {child.name}" for child in command.commands
                )
            else:
                names.add(command.name)
        return names

    assert qualified_children(Images.image_effect) == {
        "audio channels-combine",
        "audio compress",
        "audio deepvoice",
        "audio destroy",
        "audio echo",
        "audio extract",
        "audio nightcore",
        "audio overlay",
        "audio pitch",
        "audio replace",
        "audio reverb",
        "audio reverse",
        "audio surround",
        "audio sound-effect",
        "audio underwater",
        "audio volume",
        "bass boost",
        "bass lower",
        "adhd",
        "blur",
        "caption",
        "average-colors",
        "crop circle",
        "crop triangle",
        "cube",
        "deepfry",
        "flip",
        "amagik",
        "aswirl",
        "globe",
        "grayscale",
        "invert",
        "jpeg",
        "magik",
        "pyramid",
        "reverse",
        "speed",
        "spin",
        "spin3d",
        "swirl",
        "wiggle",
    }
    assert qualified_children(Images.effect_2) == {
        "bounce",
        "brightness",
        "contrast",
        "distort",
        "explode",
        "exposure",
        "fade-in",
        "fade-out",
        "fisheye",
        "grain",
        "implode",
        "lag",
        "legoify",
        "noise",
        "pixelate",
        "resize",
        "rotate",
        "saturation",
        "sepia",
        "sharpen",
        "shuffle",
        "slide-in",
        "slide-out",
        "tint",
        "vignette",
    }
    assert qualified_children(Images.effect_3) == {
        "combine",
        "enlarge",
        "falsecolor",
        "glitch",
        "hallway",
        "huerotate",
        "meme",
        "mirror bottom",
        "mirror left",
        "mirror right",
        "mirror top",
        "oilpaint",
        "overlay",
        "parallax",
        "quilt",
        "random",
        "remove bars",
        "remove caption",
        "remove outro-reels",
        "remove outro-tiktok",
        "squishy",
        "tremble",
        "text",
        "watercolor",
        "zoom",
    }


def test_media_commands_are_separated_from_fun_and_quote_stays_directly_available() -> (
    None
):
    fun_roots = {
        command.root_parent.name if command.root_parent else command.name
        for command in Fun.__cog_commands__
    }
    media_roots = {
        command.root_parent.name if command.root_parent else command.name
        for command in MediaEffects.__cog_commands__
    }
    assert MediaEffects.__cog_name__ == "Media Effects"
    assert "image-effect" not in fun_roots
    assert "video-effects" not in fun_roots
    assert {"effect", "effect-2", "effect-3", "convert", "run"} <= media_roots

    quote = next(
        command
        for command in Fun.__cog_commands__
        if command.name.startswith("quoteisifyouhaveaproblem")
    )
    assert quote.hidden is True
    assert quote.enabled is True
    assert "e" in Images.enlarge.aliases


def test_media_converter_prefers_a_guild_member_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_page(*args: object, **kwargs: object) -> str:
        raise commands.BadArgument()

    async def member_convert(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(display_avatar=SimpleNamespace(url="guild-avatar"))

    async def user_convert(*args: object, **kwargs: object) -> object:
        raise AssertionError("User fallback should not run when the member resolves")

    monkeypatch.setattr(TenorUrlConverter, "convert", reject_page)
    monkeypatch.setattr(KlipyUrlConverter, "convert", reject_page)
    monkeypatch.setattr(commands.MemberConverter, "convert", member_convert)
    monkeypatch.setattr(commands.UserConverter, "convert", user_convert)
    ctx = SimpleNamespace(guild=object())

    result = asyncio.run(
        MediaConverter().convert(
            cast(Any, ctx), "123456789", include_message_media=False
        )
    )
    assert result == "guild-avatar"


def test_media_converter_falls_back_to_a_user_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_page(*args: object, **kwargs: object) -> str:
        raise commands.BadArgument()

    async def reject_member(*args: object, **kwargs: object) -> object:
        raise commands.MemberNotFound("123456789")

    async def user_convert(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(display_avatar=SimpleNamespace(url="user-avatar"))

    monkeypatch.setattr(TenorUrlConverter, "convert", reject_page)
    monkeypatch.setattr(KlipyUrlConverter, "convert", reject_page)
    monkeypatch.setattr(commands.MemberConverter, "convert", reject_member)
    monkeypatch.setattr(commands.UserConverter, "convert", user_convert)
    ctx = SimpleNamespace(guild=object())

    result = asyncio.run(
        MediaConverter().convert(
            cast(Any, ctx), "123456789", include_message_media=False
        )
    )
    assert result == "user-avatar"


def test_media_converter_resolves_unicode_emoji_to_twemoji_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_page(*args: object, **kwargs: object) -> str:
        raise commands.BadArgument()

    async def reject_user(*args: object, **kwargs: object) -> object:
        raise commands.UserNotFound("😄")

    async def reject_custom_emoji(*args: object, **kwargs: object) -> object:
        raise commands.BadArgument()

    monkeypatch.setattr(TenorUrlConverter, "convert", reject_page)
    monkeypatch.setattr(KlipyUrlConverter, "convert", reject_page)
    monkeypatch.setattr(commands.UserConverter, "convert", reject_user)
    monkeypatch.setattr(commands.PartialEmojiConverter, "convert", reject_custom_emoji)
    ctx = SimpleNamespace(guild=None)

    result = asyncio.run(
        MediaConverter().convert(
            cast(Any, ctx),
            "😄",
            include_message_media=False,
        )
    )
    assert result == TwemojiConverter.png_url("😄")
    assert result.endswith("/1f604.png")


def test_media_converter_reads_supported_urls_from_a_replied_message() -> None:
    replied = SimpleNamespace(
        id=10,
        attachments=[],
        embeds=[],
        components=[],
        stickers=[],
        content="watch https://www.instagram.com/reel/example/",
    )

    async def fetch_message(_message_id: int) -> Any:
        return replied

    ctx = SimpleNamespace(
        guild=None,
        message=SimpleNamespace(
            id=20,
            attachments=[],
            reference=SimpleNamespace(message_id=10),
        ),
        fetch_message=fetch_message,
    )
    result = asyncio.run(MediaConverter().convert(cast(Any, ctx)))
    assert result == "https://www.instagram.com/reel/example/"


def test_media_converter_reads_components_v2_media_from_a_reply() -> None:
    replied = SimpleNamespace(
        id=10,
        attachments=[],
        embeds=[],
        stickers=[],
        content="",
        components=[
            {
                "type": 17,
                "components": [
                    {
                        "type": 12,
                        "items": [
                            {"media": {"url": "https://cdn.example.test/replied.png"}}
                        ],
                    }
                ],
            }
        ],
    )

    async def fetch_message(_message_id: int) -> Any:
        return replied

    ctx = SimpleNamespace(
        guild=None,
        message=SimpleNamespace(
            id=20,
            attachments=[],
            reference=SimpleNamespace(message_id=10),
        ),
        fetch_message=fetch_message,
    )
    result = asyncio.run(MediaConverter().convert(cast(Any, ctx)))
    assert result == "https://cdn.example.test/replied.png"


def test_media_converter_reply_without_asset_uses_display_avatar() -> None:
    replied = SimpleNamespace(
        id=10,
        attachments=[],
        embeds=[],
        components=[],
        stickers=[],
        content="",
        author=SimpleNamespace(
            display_avatar=SimpleNamespace(url="https://cdn.example.test/avatar.png")
        ),
    )

    async def fetch_message(_message_id: int) -> Any:
        return replied

    ctx = SimpleNamespace(
        guild=None,
        message=SimpleNamespace(
            id=20,
            attachments=[],
            reference=SimpleNamespace(message_id=10),
        ),
        fetch_message=fetch_message,
    )
    result = asyncio.run(MediaConverter().convert(cast(Any, ctx)))
    assert result == "https://cdn.example.test/avatar.png"


def test_media_converter_reads_discord_urls_from_recent_message_content() -> None:
    recent = SimpleNamespace(
        id=10,
        attachments=[],
        embeds=[],
        components=[],
        stickers=[],
        content=("https://cdn.discordapp.com/attachments/1/2/video.mp4?ex=abc&hm=def"),
    )

    async def history(*, limit: int) -> AsyncIterator[Any]:
        assert limit == 6
        yield recent

    ctx = SimpleNamespace(
        guild=None,
        message=SimpleNamespace(id=20, attachments=[], reference=None),
        history=history,
    )
    result = asyncio.run(MediaConverter().convert(cast(Any, ctx)))
    assert result == recent.content


@pytest.mark.parametrize(
    "effect,options",
    (
        ("invert", {}),
        ("flip", {"direction": "vertical"}),
        ("blur", {"radius": 2}),
        ("blur", {"radius": 2, "blur_type": "motion"}),
        ("crop", {"shape": "circle"}),
        ("crop", {"shape": "triangle"}),
        ("deepfry", {"intensity": 0.5}),
        ("grayscale", {}),
        ("mirror", {"direction": "bottom"}),
        ("mirror", {"direction": "left"}),
        ("jpeg", {"quality": 10}),
        ("magik", {"strength": 5}),
        ("swirl", {"strength": 45}),
        ("tint", {"color": "#ff00ff", "amount": 0.25}),
        ("implode", {"strength": 0.3}),
        ("explode", {"strength": 0.3}),
        ("sharpen", {"amount": 2}),
        ("legoify", {"size": 6}),
        ("fisheye", {"strength": 0.4}),
        ("sepia", {"amount": 0.8}),
        ("pixelate", {"size": 6}),
        ("vignette", {"amount": 0.5}),
        ("resize", {"scale": 0.75, "ratio": "1:1"}),
        ("distort", {"amount": 0.2}),
        ("grain", {"amount": 8}),
        ("rotate", {"degrees": 30}),
        ("noise", {"amount": 8}),
        ("brightness", {"amount": 1.2}),
        ("contrast", {"amount": 1.2}),
        ("saturation", {"amount": 1.2}),
        ("exposure", {"stops": 0.5}),
    ),
)
def test_static_image_effects_produce_images(
    effect: str, options: dict[str, object]
) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, **options)
    with Image.open(BytesIO(result.data)) as output:
        output.verify()


def test_resize_accepts_exact_width_by_height_for_images_and_pipelines() -> None:
    result = render_image_effect_sync(_png_bytes(), "resize", size="80x60")
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == (80, 60)

    video = render_image_effect_sync(_sample_video(), "resize", size="80x60")
    video_probe = probe_media_sync(video.data)
    assert (video_probe.width, video_probe.height) == (80, 60)

    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/media.png resize 80x60"
    )
    assert source == "https://example.com/media.png"
    assert effects == [
        (
            "resize",
            {"scale": 1.0, "ratio": "", "size": "80x60"},
        )
    ]
    assert skipped == []


def test_resize_ratio_crops_without_enlarging_extreme_aspect_ratios() -> None:
    result = render_image_effect_sync(_png_bytes(), "resize", ratio="1:4")
    with Image.open(BytesIO(result.data)) as output:
        # The 32x24 source is cropped to the largest 1:4 rectangle (6x24),
        # rather than expanded to a same-area 14x56 canvas.
        assert output.size == (6, 24)

    # Keep the helper's dimensions useful to callers that render individual
    # frames (and ensure ratio input does not mutate the source image).
    frame = Image.new("RGBA", (32, 24), (0, 0, 0, 255))
    resized = _resize(frame, {"ratio": "4:1", "scale": 1})
    assert resized.size == (32, 8)


def test_text_effect_font_catalog_has_fifteen_distinct_ofl_choices() -> None:
    assert len(EFFECT_FONTS) == 15
    assert len(set(font_names())) == 15
    assert find_effect_font("open-sans").name == "Open Sans"
    assert find_effect_font("bebas").name == "Bebas Neue"


def test_text_effect_renders_unicode_and_inline_emoji_on_an_image() -> None:
    result = render_text_effect_sync(
        _png_bytes(),
        text="Hello 世界 😄",
        font="Roboto",
        size=14,
        position="bottom",
        style="box",
        inline_images={"😄": _png_bytes()},
    )
    assert result.filename == "text.png"
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == (32, 24)
        assert output.convert("RGBA").getbbox() is not None


def test_text_effect_preserves_gif_animation_and_video_audio() -> None:
    gif_result = render_text_effect_sync(
        _gif_bytes(),
        text="GIF",
        font="Roboto",
        size=10,
        position="center",
    )
    assert gif_result.filename == "text.gif"
    with Image.open(BytesIO(gif_result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) > 1

    video_result = render_text_effect_sync(
        _sample_video(),
        text="Video",
        font="Roboto",
        size=12,
        position="top",
        style="shadow",
    )
    video_probe = probe_media_sync(video_result.data)
    assert video_result.filename == "text.mp4"
    assert video_probe.has_video is True
    assert video_probe.has_audio is True


def test_text_effect_is_available_in_run_with_positional_text() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/media.gif text hello"
    )
    assert source == "https://example.com/media.gif"
    assert effects[0][0] == "text"
    assert effects[0][1]["text"] == "hello"
    assert skipped == []


@pytest.mark.parametrize(
    ("position", "expected_size"),
    (
        ("left", (64, 24)),
        ("right", (64, 24)),
        ("top", (32, 48)),
        ("bottom", (32, 48)),
    ),
)
def test_combine_extends_static_canvas_in_each_direction(
    position: str,
    expected_size: tuple[int, int],
) -> None:
    result = render_combine_effect_sync(
        _png_bytes(),
        _png_bytes(),
        position=position,
        mode="resize",
    )
    assert result.filename == "combine.png"
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == expected_size


def test_combine_modes_keep_resize_or_stretch_the_second_item() -> None:
    second = BytesIO()
    Image.new("RGBA", (10, 20), (0, 255, 0, 180)).save(second, "PNG")
    expected = {
        "original": (42, 24),
        "resize": (44, 24),
        "stretch": (64, 24),
    }
    for mode, size in expected.items():
        result = render_combine_effect_sync(
            _png_bytes(),
            second.getvalue(),
            position="right",
            mode=mode,
        )
        with Image.open(BytesIO(result.data)) as output:
            assert output.size == size


def test_combine_synchronizes_gifs_and_preserves_video_audio() -> None:
    gif_result = render_combine_effect_sync(
        _gif_bytes(),
        _png_bytes(),
        position="right",
        mode="original",
    )
    assert gif_result.filename == "combine.gif"
    with Image.open(BytesIO(gif_result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) > 1

    video_result = render_combine_effect_sync(
        _sample_video(),
        _png_bytes(),
        position="bottom",
        mode="stretch",
        audio="mix",
    )
    probe = probe_media_sync(video_result.data)
    assert video_result.filename == "combine.mp4"
    assert probe.has_video is True
    assert probe.has_audio is True
    assert (probe.width, probe.height) == (64, 96)

    muted = render_combine_effect_sync(
        _sample_video(),
        _sample_video(),
        audio="none",
    )
    assert probe_media_sync(muted.data).has_audio is False


def test_combine_is_available_in_run_with_defined_options() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/one.gif combine "
        "-second https://example.com/two.gif "
        "-position left -mode original -audio none"
    )
    assert source == "https://example.com/one.gif"
    assert effects == [
        (
            "combine",
            {
                "second": "https://example.com/two.gif",
                "position": "left",
                "mode": "original",
                "audio": "none",
            },
        )
    ]
    assert skipped == []


@pytest.mark.parametrize(
    "effect",
    (
        "spin",
        "gifmagik",
        "gifswirl",
        "wiggle",
        "cube",
        "pyramid",
        "fadein",
        "fadeout",
        "bounce",
        "slidein",
        "slideout",
    ),
)
def test_animated_effects_produce_looping_gifs(effect: str) -> None:
    result = render_image_effect_sync(
        _png_bytes(),
        effect,
        speed=3,
        strength=180 if effect == "gifswirl" else 20,
        amount=4,
    )
    with Image.open(BytesIO(result.data)) as output:
        assert output.format == "GIF"
        assert int(getattr(output, "n_frames", 1)) > 1
        assert output.info.get("loop") == 0


@pytest.mark.parametrize("effect", ("magik", "swirl", "gifmagik", "gifswirl"))
def test_distortions_reject_animated_inputs(effect: str) -> None:
    output = BytesIO()
    first = Image.new("RGBA", (8, 8), "red")
    second = Image.new("RGBA", (8, 8), "blue")
    first.save(
        output,
        "GIF",
        save_all=True,
        append_images=[second],
        duration=[15_100, 15_100],
        loop=0,
    )
    with pytest.raises(ValueError, match="only support still images"):
        render_image_effect_sync(output.getvalue(), effect)

    with pytest.raises(ValueError, match="only support still images"):
        render_image_effect_sync(_sample_video(), effect)


def test_magik_working_copy_is_bounded_without_upscaling() -> None:
    small = Image.new("RGBA", (24, 16), "red")
    small_working, original_size = _magik_working_frame(small)
    assert small_working.size == small.size
    assert original_size == small.size

    large = Image.new("RGBA", (1024, 768), "red")
    large_working, original_size = _magik_working_frame(large)
    assert max(large_working.size) == MAGIK_WORKING_SIZE
    assert original_size == large.size


def test_heavy_gif_sampling_preserves_total_timing() -> None:
    frames = [Image.new("RGBA", (8, 8), (index, 0, 0, 255)) for index in range(100)]
    durations = [20 + index for index in range(100)]

    sampled_frames, sampled_durations = _sample_heavy_animation(frames, durations)

    assert len(sampled_frames) == HEAVY_EFFECT_MAX_GIF_FRAMES
    assert len(sampled_durations) == HEAVY_EFFECT_MAX_GIF_FRAMES
    assert sum(sampled_durations) == sum(durations)


def test_magik_gif_sampling_uses_its_smaller_fidelity_bound() -> None:
    frames = [Image.new("RGBA", (8, 8), "red") for _ in range(50)]
    durations = [40] * len(frames)

    sampled_frames, sampled_durations = _sample_heavy_animation(
        frames,
        durations,
        max_frames=MAGIK_MAX_GIF_FRAMES,
    )
    working, _ = _magik_working_frame(
        Image.new("RGBA", (1024, 768), "red"),
        MAGIK_GIF_WORKING_SIZE,
    )

    assert len(sampled_frames) == MAGIK_MAX_GIF_FRAMES
    assert sum(sampled_durations) == sum(durations)
    assert max(working.size) == MAGIK_GIF_WORKING_SIZE


@pytest.mark.parametrize(
    ("effect", "expected"),
    (
        ("fadein", "fadein.gif"),
        ("fadeout", "fadeout.gif"),
        ("gifmagik", "amagik.gif"),
        ("gifswirl", "aswirl.gif"),
    ),
)
def test_animated_effect_filenames_use_public_command_names(
    effect: str,
    expected: str,
) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, speed=4)
    assert result.filename == expected


def test_gif_effect_preserves_animation() -> None:
    result = render_image_effect_sync(_gif_bytes(), "invert")
    with Image.open(BytesIO(result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) == 2


def test_magik_and_swirl_keep_still_image_support() -> None:
    for effect in ("magik", "swirl"):
        result = render_image_effect_sync(_png_bytes(), effect, strength=20)
        assert result.data


@pytest.mark.parametrize("effect", ("fadein", "fadeout"))
def test_image_fades_change_opacity_gradually(effect: str) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, duration=2)
    with Image.open(BytesIO(result.data)) as output:
        frame_count = int(getattr(output, "n_frames", 1))
        values: list[int] = []
        for index in (0, frame_count // 2, frame_count - 1):
            output.seek(index)
            alpha = output.convert("RGBA").getchannel("A")
            values.append(sum(cast(Any, alpha.get_flattened_data())))

    if effect == "fadein":
        assert values[0] < values[1] < values[2]
    else:
        assert values[0] > values[1] > values[2]


@pytest.mark.parametrize("effect", ("fadein", "fadeout"))
def test_image_fades_start_or_end_transparent(effect: str) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, duration=2)
    with Image.open(BytesIO(result.data)) as output:
        first = output.convert("RGBA")
        output.seek(int(getattr(output, "n_frames", 1)) - 1)
        last = output.convert("RGBA")

    if effect == "fadein":
        assert cast(tuple[int, int, int, int], first.getpixel((10, 10)))[3] == 0
        assert cast(tuple[int, int, int, int], last.getpixel((10, 10)))[3] == 255
    else:
        assert cast(tuple[int, int, int, int], first.getpixel((10, 10)))[3] == 255
        assert cast(tuple[int, int, int, int], last.getpixel((10, 10)))[3] == 0


@pytest.mark.parametrize("effect", ("fadein", "fadeout"))
def test_image_fades_keep_visible_colors_and_decode_every_frame(effect: str) -> None:
    source = BytesIO()
    Image.new("RGBA", (32, 24), (240, 20, 10, 255)).save(source, "PNG")
    result = render_image_effect_sync(source.getvalue(), effect, duration=2)

    with Image.open(BytesIO(result.data)) as output:
        frame_count = int(getattr(output, "n_frames", 1))
        assert frame_count == 50
        for index in range(frame_count):
            output.seek(index)
            rgba = output.convert("RGBA")
            visible = [
                pixel
                for pixel in cast(Any, rgba.get_flattened_data())
                if isinstance(pixel, tuple) and pixel[3] == 255
            ]
            assert all(
                red > 200 and green < 80 and blue < 80
                for red, green, blue, _alpha in visible
            )


@pytest.mark.parametrize("effect", ("invert", "deepfry", "grayscale"))
def test_color_effects_can_recover_declared_gif_transparency(effect: str) -> None:
    source = _gif_with_unused_transparency_index()
    normal = render_image_effect_sync(source, effect)
    preserved = render_image_effect_sync(
        source,
        effect,
        preserve_transparency=True,
    )

    with Image.open(BytesIO(normal.data)) as output:
        px = output.convert("RGBA").getpixel((0, 0))
        assert isinstance(px, tuple) and px[3] == 255
    with Image.open(BytesIO(preserved.data)) as output:
        rgba = output.convert("RGBA")
        px1 = rgba.getpixel((0, 0))
        assert isinstance(px1, tuple) and px1[3] == 0
        px2 = rgba.getpixel((10, 10))
        assert isinstance(px2, tuple) and px2[3] == 255


def test_built_in_flags_are_valid_images() -> None:
    for name in ("pride", "trans", "bi", "pirate"):
        data = make_flag_asset(name)
        assert data is not None
        with Image.open(BytesIO(data)) as image:
            assert image.size == (640, 384)


def test_flag_overlay_stretches_to_every_edge() -> None:
    background = BytesIO()
    Image.new("RGBA", (80, 80), "red").save(background, "PNG")
    flag = BytesIO()
    Image.new("RGBA", (120, 30), "blue").save(flag, "PNG")
    result = render_image_effect_sync(
        background.getvalue(),
        "overlay",
        overlay_data=flag.getvalue(),
        opacity=1,
        scale=1,
        stretch=True,
    )
    with Image.open(BytesIO(result.data)) as output:
        rgba = output.convert("RGBA")
        assert rgba.size == (80, 80)
        assert rgba.getpixel((0, 0)) == (0, 0, 255, 255)
        assert rgba.getpixel((79, 79)) == (0, 0, 255, 255)


def test_reference_style_animation_shapes_and_timing() -> None:
    source = _png_bytes()
    gifmagik = render_image_effect_sync(source, "gifmagik")
    wiggle = render_image_effect_sync(source, "wiggle")
    cube = render_image_effect_sync(source, "cube")
    pyramid = render_image_effect_sync(source, "pyramid")

    with Image.open(BytesIO(gifmagik.data)) as output:
        assert output.size == (32, 24)
        assert int(getattr(output, "n_frames", 1)) == 15
        assert output.info["duration"] == 80
    with Image.open(BytesIO(wiggle.data)) as output:
        assert output.size == (32, 24)
        assert int(getattr(output, "n_frames", 1)) == 15
        assert output.info["duration"] == 20
        for index in range(int(getattr(output, "n_frames", 1))):
            output.seek(index)
            assert output.convert("RGBA").getchannel("A").getextrema() == (255, 255)
    for result in (cube, pyramid):
        with Image.open(BytesIO(result.data)) as output:
            assert output.size == (300, 300)
            assert int(getattr(output, "n_frames", 1)) == 39
            assert output.info["duration"] == 50
            output.seek(0)
            first = output.convert("RGBA")
            output.seek(10)
            later = output.convert("RGBA")
            assert first.tobytes() != later.tobytes()
            px_first = first.getpixel((0, 0))
            assert isinstance(px_first, tuple) and px_first[3] == 0
            px_later = later.getpixel((0, 0))
            assert isinstance(px_later, tuple) and px_later[3] == 0


@pytest.mark.parametrize(
    ("effect", "options"),
    (
        ("glitch", {}),
        ("huerotate", {}),
        ("parallax", {}),
        ("hallway", {}),
        ("quilt", {}),
        ("zoom", {"forever": True, "amount": 2}),
        ("squishy", {"amount": 0.18}),
        ("tremble", {}),
    ),
)
def test_reference_effects_use_matching_geometry_and_timing(
    effect: str,
    options: dict[str, Any],
) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, **options)
    with Image.open(BytesIO(result.data)) as output:
        frame_count = int(getattr(output, "n_frames", 1))
        assert output.size == (300, 300)
        assert frame_count == 39
        durations: list[int] = []
        frames: list[bytes] = []
        for index in range(frame_count):
            output.seek(index)
            durations.append(int(output.info["duration"]))
            if index in {0, 6, 12}:
                frames.append(output.convert("RGBA").tobytes())
        assert set(durations) == {50}
        assert len(set(frames)) > 1


def test_hallway_keeps_a_fixed_transparent_endpoint() -> None:
    result = render_image_effect_sync(_png_bytes(), "hallway")
    with Image.open(BytesIO(result.data)) as output:
        frame_count = int(getattr(output, "n_frames", 1))
        for index in (0, frame_count // 2, frame_count - 1):
            output.seek(index)
            alpha = output.convert("RGBA").getchannel("A")
            transparent = alpha.point(lambda value: 255 - value).getbbox()
            assert transparent == (135, 135, 165, 165)


def test_recursive_zoom_renders_nested_levels_from_the_source() -> None:
    source = Image.new("RGBA", (300, 300), "red")
    source.paste(Image.new("RGBA", (280, 280), "green"), (10, 10))
    frame = _recursive_zoom_frame(source, 5)
    # At 5x zoom, the first direct nested copy spans (75, 75) to (225, 225).
    # Its source border must remain visible over the zoomed background.
    border_pixel = cast(tuple[int, int, int, int], frame.getpixel((76, 76)))
    center_pixel = cast(tuple[int, int, int, int], frame.getpixel((90, 90)))
    assert border_pixel[0] > 200
    assert center_pixel[1] > 64


def test_remove_caption_crops_the_panel_without_resizing_the_image() -> None:
    body = Image.new("RGBA", (80, 60), (30, 80, 140, 255))
    captioned = Image.new("RGBA", (80, 80), "white")
    captioned.alpha_composite(body, (0, 20))
    captioned.paste(Image.new("RGBA", (10, 4), "black"), (35, 8))
    source = BytesIO()
    captioned.save(source, "PNG")

    result = render_image_effect_sync(source.getvalue(), "removecaption")
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == body.size
        assert output.convert("RGBA").tobytes() == body.tobytes()


def test_remove_caption_tolerates_multiple_rows_of_caption_text() -> None:
    body = Image.new("RGBA", (120, 70), (30, 80, 140, 255))
    captioned = Image.new("RGBA", (120, 130), "white")
    captioned.alpha_composite(body, (0, 60))
    captioned.paste(Image.new("RGBA", (80, 8), "black"), (20, 12))
    captioned.paste(Image.new("RGBA", (92, 8), "black"), (14, 38))
    source = BytesIO()
    captioned.save(source, "PNG")

    result = render_image_effect_sync(source.getvalue(), "removecaption")
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == body.size
        assert output.convert("RGBA").tobytes() == body.tobytes()


def test_huerotate_preserves_animated_source_geometry_and_timing() -> None:
    source = BytesIO()
    first = Image.new("RGBA", (24, 40), "red")
    second = Image.new("RGBA", (24, 40), "blue")
    first.save(
        source,
        "GIF",
        save_all=True,
        append_images=[second],
        duration=[80, 120],
        loop=0,
    )

    result = render_image_effect_sync(source.getvalue(), "huerotate")
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == (24, 40)
        assert int(getattr(output, "n_frames", 1)) == 2
        durations = []
        for index in range(2):
            output.seek(index)
            durations.append(int(output.info["duration"]))
        assert sum(durations) == 200


def test_remove_bars_detects_video_crop_instead_of_using_a_fixed_percentage() -> None:
    result = render_image_effect_sync(_sample_letterboxed_video(), "removebars")
    output = _first_video_frame(result.data)
    assert output.size == (64, 48)


def test_remove_bars_preserves_every_frame_and_the_full_gif_duration() -> None:
    frames: list[Image.Image] = []
    for index in range(43):
        frame = Image.new("RGBA", (32, 60), "black")
        bottom = 50 if index % 4 else 49
        frame.paste(
            Image.new("RGBA", (32, bottom - 10), (index * 5, 80, 160, 255)),
            (0, 10),
        )
        frames.append(frame)
    source = BytesIO()
    frames[0].save(
        source,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100] * len(frames),
        disposal=2,
        loop=0,
    )

    result = render_image_effect_sync(source.getvalue(), "removebars")
    with Image.open(BytesIO(result.data)) as output:
        frame_count = int(getattr(output, "n_frames", 1))
        durations: list[int] = []
        sizes: set[tuple[int, int]] = set()
        for index in range(frame_count):
            output.seek(index)
            durations.append(int(output.info["duration"]))
            sizes.add(output.size)
        assert frame_count == 43
        assert sum(durations) == 4_300
        assert len(sizes) == 1


def test_meme_uses_large_condensed_bottom_text_for_pipe_syntax() -> None:
    source = Image.new("RGBA", (773, 773), (120, 170, 120, 255))
    source_data = BytesIO()
    source.save(source_data, "PNG")
    result = render_image_effect_sync(source_data.getvalue(), "meme", text="| test")

    with Image.open(BytesIO(result.data)) as output:
        difference = ImageChops.difference(output.convert("RGB"), source.convert("RGB"))
        bbox = difference.getbbox()
        assert bbox is not None
        assert bbox[0] < 330 and bbox[2] > 440
        assert bbox[1] > 670
        assert bbox[3] >= 760


def test_video_visual_and_audio_effects() -> None:
    video = _sample_video()
    inverted = render_image_effect_sync(video, "invert")
    circle = render_image_effect_sync(video, "crop", shape="circle")
    overlaid = render_image_effect_sync(
        video,
        "overlay",
        overlay_data=_png_bytes(),
        opacity=0.5,
        scale=0.5,
    )
    flag = make_flag_asset("lesbian")
    assert flag is not None
    flag_overlaid = render_image_effect_sync(
        video,
        "overlay",
        overlay_data=flag,
        opacity=1,
        scale=1,
        stretch=True,
    )
    reversed_audio = render_video_effect_sync(video, "audioreverse")
    assert inverted.filename.endswith(".mp4")
    assert circle.filename == "crop-circle.mp4"
    assert overlaid.filename == "overlay-video.mp4"
    assert flag_overlaid.filename == "overlay-video.mp4"
    assert reversed_audio.filename.endswith(".mp4")
    assert inverted.data
    assert circle.data
    assert overlaid.data
    assert flag_overlaid.data
    assert reversed_audio.data
    flag_frame = _first_video_frame(flag_overlaid.data)
    assert flag_frame.size == (64, 48)
    assert flag_frame.getpixel((1, 1)) != flag_frame.getpixel((1, 46))


@pytest.mark.parametrize(
    "effect,options",
    (
        ("fadein", {"duration": 0.2}),
        ("fadeout", {"duration": 0.2}),
        ("lag", {"amount": 3}),
        ("shuffle", {}),
        ("tint", {"color": "purple", "amount": 0.3}),
        ("implode", {"strength": 0.3}),
        ("explode", {"strength": 0.3}),
        ("sharpen", {"amount": 2}),
        ("legoify", {"size": 6}),
        ("bounce", {"amount": 5, "speed": 1}),
        ("fisheye", {"strength": 0.4}),
        ("sepia", {"amount": 1}),
        ("pixelate", {"size": 6}),
        ("slidein", {"direction": "left", "duration": 0.2}),
        ("slideout", {"direction": "right", "duration": 0.2}),
        ("vignette", {"amount": 0.5}),
        ("resize", {"scale": 0.75, "ratio": "1:1"}),
        ("distort", {"amount": 0.2}),
        ("grain", {"amount": 8}),
        ("rotate", {"degrees": 30}),
        ("noise", {"amount": 8}),
        ("brightness", {"amount": 1.2}),
        ("contrast", {"amount": 1.2}),
        ("saturation", {"amount": 1.2}),
        ("exposure", {"stops": 0.5}),
        ("hallway", {}),
        ("parallax", {}),
        ("huerotate", {"degrees": 90}),
        ("zoom", {"amount": 1.5}),
        ("squishy", {}),
        ("glitch", {}),
        ("tremble", {}),
        ("removebars", {}),
        ("removecaption", {}),
        ("enlarge", {"amount": 1.2}),
        ("falsecolor", {}),
        ("watercolor", {}),
        ("oilpaint", {}),
    ),
)
def test_new_effects_process_video(
    effect: str,
    options: dict[str, Any],
) -> None:
    result = render_image_effect_sync(_sample_video(), effect, **options)
    assert result.filename.endswith(".mp4")
    assert result.data


@pytest.mark.parametrize(
    "effect,options",
    (
        ("reverse", {}),
        ("bassboost", {"gain": 8}),
        ("basslower", {"gain": 8}),
        ("audioreverse", {}),
        ("audioreverb", {"room": 0.4}),
        ("audiodestroy", {"amount": 5}),
        ("audiocompress", {"ratio": 3}),
        ("channelscombine", {}),
        ("volume", {"volume": 0.5}),
        ("audiopitch", {"semitones": 2}),
        ("audiounderwater", {}),
        ("audionightcore", {}),
        ("audiodeepvoice", {}),
        ("audiosurround", {}),
        ("audioecho", {}),
    ),
)
def test_all_single_input_video_effects(effect: str, options: dict[str, Any]) -> None:
    result = render_video_effect_sync(_sample_video(), effect, **options)
    assert result.data


def test_audiodestroy_handles_audio_only_input() -> None:
    result = render_video_effect_sync(
        _sample_audio("sine=frequency=330:duration=0.8"),
        "audiodestroy",
        amount=8,
        start=0.1,
        stop=0.6,
    )
    assert result.filename == "audio-destroyed.mp3"
    assert result.data


def test_two_input_video_effects() -> None:
    video = _sample_video()
    overlay = render_video_effect_sync(
        video,
        "overlay",
        second_data=_png_bytes(),
        opacity=0.5,
        scale=0.5,
    )
    replaced = render_video_effect_sync(
        video,
        "audioreplace",
        second_data=video,
    )
    overlaid_audio = render_video_effect_sync(
        video,
        "audiooverlay",
        second_data=video,
        at=0.1,
        source_start=0.1,
        duration=0.2,
        volume=0.5,
        pitch=1,
        speed=1.5,
    )
    assert overlay.data
    assert replaced.data
    assert overlaid_audio.data


def test_sound_effect_supports_loop_and_fades() -> None:
    result = render_video_effect_sync(
        _sample_video(),
        "soundeffect",
        second_data=audio_effect_catalog()[0].path.read_bytes(),
        random_time=False,
        loop=True,
        fade_in=0.1,
        fade_out=0.1,
    )
    assert result.filename == "audio-overlay.mp4"
    assert result.data


def test_sound_effect_copies_compatible_video_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def video_output(_data: bytes, **options: Any) -> Any:
        captured.update(options)
        return media_effect_processing.EffectResult(b"result", "audio-overlay.mp4")

    monkeypatch.setattr(media_effect_processing, "_video_command_output", video_output)
    base = _sample_video()
    probe = probe_media_sync(base)
    render_video_effect_sync(
        base,
        "soundeffect",
        second_data=_sample_audio("sine=frequency=440:duration=0.1"),
        media_probe=probe,
        random_time=False,
    )
    assert probe.can_copy_video_to_discord_mp4 is True
    assert captured["copy_video"] is True
    assert captured["media_probe"] is probe


def test_sound_effect_batch_mixes_multiple_effects_in_one_output() -> None:
    base = _sample_video_for_duration(1.5)
    probe = probe_media_sync(base)
    first = _sample_audio("sine=frequency=440:duration=0.2")
    second = _sample_audio("sine=frequency=880:duration=0.2")
    result = render_sound_effect_batch_sync(
        base,
        [
            (first, 0.2, {"at": 0.1, "random_time": False}),
            (second, 0.2, {"at": 0.8, "random_time": False}),
        ],
        media_probe=probe,
    )
    output_probe = probe_media_sync(result.data)
    assert result.filename == "audio-overlay.mp4"
    assert output_probe.has_video is True
    assert output_probe.has_audio is True
    assert output_probe.duration >= probe.duration - 0.05
    assert output_probe.duration <= probe.duration + 0.12


def test_audio_effect_bytes_use_a_bounded_cache() -> None:
    effect = audio_effect_catalog()[0]
    audio_effect_data.cache_clear()
    first = audio_effect_data(effect.id)
    second = audio_effect_data(effect.id)
    assert first is second
    assert audio_effect_data.cache_info().maxsize == 32


def test_video_size_fitting_uses_a_calculated_target_without_growing_input() -> None:
    base = _sample_video_for_duration(3)
    limit = round(len(base) * 0.8)
    result = compress_media_to_size_sync(base, "video.mp4", limit)
    assert len(result.data) <= limit
    assert len(result.data) <= len(base)
    assert probe_media_sync(result.data).has_video is True


def test_sound_effect_keeps_video_when_base_audio_ends_early() -> None:
    result = render_video_effect_sync(
        _sample_video_with_short_audio(),
        "soundeffect",
        second_data=audio_effect_catalog()[0].path.read_bytes(),
        random_time=False,
    )
    assert probe_media_sync(result.data).duration >= 1.8


def test_sound_effect_converts_a_gif_without_shortening_its_animation() -> None:
    result = render_video_effect_sync(
        _gif_bytes(),
        "soundeffect",
        second_data=_sample_audio("sine=frequency=440:duration=0.1"),
        random_time=False,
    )
    probe = probe_media_sync(result.data)
    assert result.filename == "audio-overlay.mp4"
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.duration >= 0.19


def test_sound_effect_does_not_extend_a_gif_with_a_frozen_last_frame() -> None:
    gif = _gif_bytes()
    original_duration = probe_media_sync(gif).duration
    result = render_video_effect_sync(
        gif,
        "soundeffect",
        second_data=_sample_audio("sine=frequency=440:duration=1"),
        random_time=False,
    )
    probe = probe_media_sync(result.data)
    assert probe.duration >= original_duration - 0.03
    assert probe.duration <= original_duration + 0.12


def test_overlapping_sound_effects_preserve_the_base_duration_and_audio() -> None:
    base = _sample_video()
    original_duration = probe_media_sync(base).duration
    first = render_video_effect_sync(
        base,
        "soundeffect",
        second_data=_sample_audio("sine=frequency=440:duration=1"),
        at=0.1,
        random_time=False,
    )
    second = render_video_effect_sync(
        first.data,
        "soundeffect",
        second_data=_sample_audio("sine=frequency=880:duration=1"),
        at=0.2,
        random_time=False,
    )
    probe = probe_media_sync(second.data)
    assert probe.has_audio is True
    assert probe.duration >= original_duration - 0.05
    assert probe.duration <= original_duration + 0.12


def test_sound_effect_does_not_normalize_the_base_audio_quietly() -> None:
    base = _sample_audio("sine=frequency=440:duration=1")
    silence = _sample_audio("anullsrc=r=44100:cl=stereo:d=0.25")
    result = render_video_effect_sync(
        base,
        "soundeffect",
        second_data=silence,
        random_time=False,
    )
    assert _audio_rms(result.data) >= _audio_rms(base) * 0.85


def test_adhd_processes_video_and_audio_together() -> None:
    result = render_video_effect_sync(_sample_video(), "adhd")
    probe = probe_media_sync(result.data)
    assert result.filename == "adhd.mp4"
    assert probe.has_video is True
    assert probe.has_audio is True


def test_adhd_segments_scale_short_media_and_include_varied_modes() -> None:
    segments = _adhd_segments(3.0, random.Random(42))
    assert segments[0][0] == 0
    assert segments[-1][1] == 3.0
    assert all(0 < end - start <= 1.05 for start, end, _, _ in segments)
    assert len({mode for _, _, _, mode in segments}) > 1

    long_segments = _adhd_segments(30.0, random.Random(42))
    assert all(2 <= end - start <= 7 for start, end, _, _ in long_segments[:-1])
    assert {"normal", "lowered", "nightcore"} & {
        mode for _, _, _, mode in long_segments
    }


def test_random_effect_pool_includes_size_but_not_enlarge() -> None:
    assert {"zoom", "resize"} <= set(RANDOM_EFFECTS)
    assert "enlarge" not in RANDOM_EFFECTS
    assert "volume" in media_effect_commands.RANDOM_AUDIO_EFFECTS
    assert "extract" not in media_effect_commands.RANDOM_AUDIO_EFFECTS
    assert "channelscombine" not in media_effect_commands.RANDOM_AUDIO_EFFECTS


def test_cancelled_media_worker_finishes_before_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()

    @cancellable_to_thread
    def blocking_work() -> str:
        started.set()
        return "finished"

    async def exercise() -> None:
        real_loop = asyncio.get_running_loop()
        worker: asyncio.Future[str] = real_loop.create_future()

        class FakeLoop:
            def run_in_executor(self, *_args: Any, **_kwargs: Any) -> Any:
                started.set()
                return worker

        monkeypatch.setattr(
            media_effect_runtime.asyncio,
            "get_running_loop",
            lambda: FakeLoop(),
        )
        task = asyncio.ensure_future(blocking_work())
        await asyncio.sleep(0)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        worker.set_result("finished")
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_effect_delivery_compresses_to_the_discord_limit_and_keeps_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compression_limits: list[int] = []
    sent: list[dict[str, Any]] = []

    async def compress(
        _data: bytes,
        filename: str,
        max_bytes: int,
    ) -> Any:
        compression_limits.append(max_bytes)
        return SimpleNamespace(data=b"small", filename=filename, displayable=True)

    async def send(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(media_effect_delivery, "compress_media_to_size", compress)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(filesize_limit=10),
        author=SimpleNamespace(mention="<@1>"),
        message=SimpleNamespace(to_reference=lambda **_kwargs: object()),
        send=send,
    )
    result = SimpleNamespace(
        data=b"too-large!!", filename="effect.png", displayable=True
    )

    asyncio.run(
        media_effect_delivery.send_effect_result(
            cast("Fishie", SimpleNamespace(embedcolor=discord.Colour.blurple())),
            cast("Context", ctx),
            cast("EffectResult", result),
            started=0.0,
        )
    )

    assert compression_limits == [10]
    assert sent
    view = sent[0]["view"]
    text = "\n".join(
        str(item.content)
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Invoked by <@1>" in text


def test_bulk_effect_delivery_keeps_invoker_without_hosted_files() -> None:
    sent: list[dict[str, Any]] = []

    async def send(**kwargs: Any) -> None:
        sent.append(kwargs)

    ctx = SimpleNamespace(
        guild=SimpleNamespace(filesize_limit=100),
        author=SimpleNamespace(mention="<@2>"),
        message=SimpleNamespace(to_reference=lambda **_kwargs: object()),
        send=send,
    )
    result = SimpleNamespace(data=b"small", filename="effect.png", displayable=True)

    asyncio.run(
        media_effect_delivery.send_effect_results(
            cast("Fishie", SimpleNamespace(embedcolor=discord.Colour.blurple())),
            cast("Context", ctx),
            [cast("EffectResult", result)],
            started=0.0,
        )
    )

    view = sent[0]["view"]
    text = "\n".join(
        str(item.content)
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Invoked by <@2>" in text


def test_enlarge_is_rejected_from_run_and_random(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(commands.BadArgument, match="cannot be used inside"):
        _parse_effect_pipeline("enlarge")

    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("enlarge",))
    with pytest.raises(commands.BadArgument, match="No random effects"):
        _resolve_pipeline_random_effects([("random", {})])


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("United States", "us"),
        ("South Korea", "kr"),
        ("Israel", "il"),
        ("Japan", "jp"),
    ),
)
def test_country_flag_names_resolve_to_codes(name: str, expected: str) -> None:
    assert _country_flag_code(name) == expected


def test_israel_flag_restriction_detects_user_and_avatar_urls() -> None:
    user_id = "766953372309127168"
    assert _restricted_flag_media(f"<@{user_id}>", "")
    assert _restricted_flag_media(
        "",
        f"https://cdn.discordapp.com/avatars/{user_id}/hash.png",
    )
    assert _restricted_flag_media(
        "",
        f"https://cdn.discordapp.com/guilds/1/users/{user_id}/avatars/hash.png",
    )
    assert not _restricted_flag_media(
        "<@123456789012345678>",
        "https://cdn.discordapp.com/avatars/123456789012345678/hash.png",
    )
    with pytest.raises(commands.BadArgument, match="^no$"):
        Images._validate_flag_media(
            "Israel",
            f"<@{user_id}>",
            f"https://cdn.discordapp.com/avatars/{user_id}/hash.png",
        )


@pytest.mark.parametrize(
    "effect",
    ("removeoutrotiktok", "removeoutroreels"),
)
def test_platform_outro_removal_detects_the_transition(effect: str) -> None:
    result = render_video_effect_sync(_sample_outro_video(), effect)
    probe = probe_media_sync(result.data)
    assert result.filename == f"remove-outro-{effect.removeprefix('removeoutro')}.mp4"
    assert probe.duration == pytest.approx(2.0, abs=0.3)


def test_platform_outro_removal_does_not_blindly_trim_video() -> None:
    with pytest.raises(ValueError, match="too short|confidently"):
        render_video_effect_sync(_sample_video(), "removeoutrotiktok")


def test_timed_audio_effect_preserves_the_complete_video() -> None:
    result = render_video_effect_sync(
        _sample_video(),
        "audiodeepvoice",
        start=0.1,
        duration=0.2,
    )
    assert result.filename.endswith(".mp4")
    assert result.data


def test_timed_visual_effect_preserves_the_complete_video() -> None:
    result = render_image_effect_sync(
        _sample_video(),
        "huerotate",
        degrees=45,
        start=0.1,
        stop=0.35,
    )
    assert result.filename == "huerotate.mp4"
    assert result.data


@pytest.mark.parametrize("method", ("random", "freeze", "stutter", "drop", "jitter"))
def test_lag_methods_process_video(method: str) -> None:
    result = render_image_effect_sync(
        _sample_video(),
        "lag",
        amount=3,
        method=method,
        multi=method == "random",
    )
    assert result.filename.endswith(".mp4")
    assert result.data


def test_overlay_video_requires_a_video_as_the_first_input() -> None:
    with pytest.raises(ValueError, match="first input has to be a video"):
        render_video_effect_sync(_png_bytes(), "overlay", second_data=_png_bytes())


def test_overlay_video_accepts_audio_toggle() -> None:
    video = _sample_video()
    with_audio = render_video_effect_sync(
        video,
        "overlay",
        second_data=video,
        overlay_audio=True,
    )
    without_audio = render_video_effect_sync(
        video,
        "overlay",
        second_data=video,
        overlay_audio=False,
    )
    assert with_audio.data and without_audio.data


def test_unified_overlay_keeps_static_images_on_the_base_canvas() -> None:
    result = render_overlay_effect_sync(
        _png_bytes(),
        _png_bytes(),
        size="10x8",
    )
    assert result.filename == "overlay.png"
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == (32, 24)


def test_unified_overlay_turns_an_image_into_a_video_without_resizing_the_base() -> (
    None
):
    result = render_overlay_effect_sync(
        _png_bytes(),
        _sample_video(),
        size="20x12",
    )
    probe = probe_media_sync(result.data)
    assert result.filename == "overlay-video.mp4"
    assert (probe.width, probe.height) == (32, 24)
    assert probe.has_audio is True
    assert probe.duration >= 0.4


def test_unified_overlay_normalizes_odd_image_dimensions_for_h264() -> None:
    result = render_overlay_effect_sync(_odd_png_bytes(), _sample_video())
    probe = probe_media_sync(result.data)

    assert probe.has_video is True
    assert probe.width % 2 == 0
    assert probe.height % 2 == 0


def test_unified_overlay_repairs_reserved_h264_color_metadata() -> None:
    video_path = Path("src/files/videos/QIIYHAPWMTMAIYDHMNYDKMWETHAPWM.mp4")
    if not video_path.is_file():
        pytest.skip("The bundled H.264 color-metadata regression asset is unavailable.")
    result = render_overlay_effect_sync(
        _png_bytes(),
        video_path.read_bytes(),
    )
    probe = probe_media_sync(result.data)
    assert result.filename == "overlay-video.mp4"
    assert probe.has_video is True


def test_media_subprocess_error_keeps_full_diagnostic_in_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = "Invalid color space\\nConversion failed!"

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(
            234,
            ["ffmpeg", "-i", "input.media"],
            stderr=diagnostic.encode(),
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="Conversion failed!") as raised:
        run_media_command(
            ["ffmpeg", "-i", "input.media"],
            timeout=30,
            timeout_message="timed out",
            failure_prefix="Could not process that media",
        )
    assert raised.value.__notes__ == ["Media subprocess diagnostics:\n" + diagnostic]
    assert diagnostic in "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )


def test_unified_overlay_extend_only_expands_for_a_longer_second_video() -> None:
    base = _sample_video_for_duration(0.5)
    longer_overlay = _sample_video_for_duration(1.25)
    base_duration = probe_media_sync(base).duration
    overlay_duration = probe_media_sync(longer_overlay).duration

    normal = probe_media_sync(
        render_overlay_effect_sync(base, longer_overlay).data
    ).duration
    extended = probe_media_sync(
        render_overlay_effect_sync(base, longer_overlay, extend=True).data
    ).duration

    assert normal <= base_duration + 0.1
    assert extended >= overlay_duration - 0.1


def test_unified_overlay_applies_a_timed_image_overlay_to_video() -> None:
    result = render_overlay_effect_sync(
        _sample_video(),
        _png_bytes(),
        start=0.1,
        stop=0.3,
    )
    probe = probe_media_sync(result.data)
    assert result.filename == "overlay-video.mp4"
    assert probe.has_video is True


def test_reverse_preserves_gif_output() -> None:
    result = render_video_effect_sync(_gif_bytes(), "reverse")
    assert result.filename == "reverse.gif"
    with Image.open(BytesIO(result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) == 2


@pytest.mark.parametrize("effect", ("extract", "audioreverse"))
def test_audio_effects_reject_still_images_clearly(effect: str) -> None:
    with pytest.raises(ValueError, match="does not contain audio"):
        render_video_effect_sync(_png_bytes(), effect)


def test_audio_extract_returns_file() -> None:
    result = render_video_effect_sync(_sample_video(), "extract")
    assert result.filename == "track-1.mp3"
    assert result.data


def test_media_conversion_supports_video_and_audio_outputs() -> None:
    video = _sample_video()
    mov = convert_media_sync(video, "mov")
    webm = convert_media_sync(video, "webm")
    mp3 = convert_media_sync(video, "mp3")
    opus = convert_media_sync(video, "opus")
    assert mov.filename == "converted-1.mov"
    assert webm.filename == "converted-1.webm"
    assert mp3.filename == "converted-1.mp3"
    assert opus.filename == "converted-1.opus"
    assert mov.data
    assert webm.data
    assert mp3.data
    assert opus.data

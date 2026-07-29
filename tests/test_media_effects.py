from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
from array import array
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageChops

import extensions.media_effects.commands as media_effect_commands
from extensions.fun import Fun
from extensions.media_effects import MediaEffects
from extensions.media_effects.audio_effects import (
    audio_effect_catalog,
    find_audio_effect,
)
from extensions.media_effects.commands import (
    PIPELINE_EFFECTS,
    RANDOM_EFFECTS,
    Images,
    _extract_sound_effect_selector,
    _finalize_pipeline_result,
    _normalize_effect_options,
    _parse_effect_flags,
    _parse_effect_pipeline,
    _playback_factor,
    _prepare_sound_effect_options,
    _random_effect_choices,
    _resolve_pipeline_random_effects,
    _select_pipeline_sound_effect,
    _sound_effect_autocomplete,
    _speed_video_sync,
    media_effect_timeout,
)
from extensions.media_effects.processing import (
    HEAVY_EFFECT_MAX_GIF_FRAMES,
    MAGIK_GIF_WORKING_SIZE,
    MAGIK_MAX_GIF_FRAMES,
    MAGIK_WORKING_SIZE,
    _magik_working_frame,
    _overlay,
    _recursive_zoom_frame,
    _sample_heavy_animation,
    convert_media_sync,
    make_flag_asset,
    probe_media_sync,
    render_image_effect_sync,
    render_video_effect_sync,
)
from utils.converters import (
    KlipyUrlConverter,
    MediaConverter,
    TenorUrlConverter,
    TwemojiConverter,
)
from utils.downloads import is_downloadable_media_page


def _png_bytes() -> bytes:
    output = BytesIO()
    image = Image.new("RGBA", (32, 24), (240, 20, 10, 255))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), (x * 7, y * 10, (x + y) * 4, 255))
    image.save(output, "PNG")
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
        "https://example.com/a.gif invert blur -radius 8 -type motion "
        "pixelate -size 10"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("invert", {"preserve_transparency": False}),
        ("blur", {"radius": 8.0, "blur_type": "motion"}),
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
        ("globe", {"speed": 2.0, "clockwise": True}),
    ]
    assert skipped == []


def test_effect_pipeline_accepts_caption_and_meme_text() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        'https://example.com/a.gif caption "top text" meme "bottom text"'
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("caption", {"text": "top text"}),
        ("meme", {"text": "bottom text"}),
    ]
    assert skipped == []


def test_effect_pipeline_recognizes_overlay_group_commands() -> None:
    source, effects, skipped = _parse_effect_pipeline(
        "https://example.com/a.gif overlay flag lesbian "
        "overlay image -overlay https://example.com/logo.png -opacity 0.5"
    )
    assert source == "https://example.com/a.gif"
    assert effects == [
        ("overlayflag", {"flag": "lesbian", "opacity": 35.0}),
        (
            "overlayimage",
            {
                "overlay": "https://example.com/logo.png",
                "opacity": 0.5,
                "scale": 1.0,
                "position": "center",
                "x": 0,
                "y": 0,
                "stretch": False,
            },
        ),
    ]
    assert skipped == []


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

    with pytest.raises(commands.BadArgument, match="longer than 30 seconds"):
        asyncio.run(slow_effect())


def test_effect_pipeline_has_a_bounded_step_count() -> None:
    source, effects, skipped = _parse_effect_pipeline(" ".join(["invert"] * 67))
    assert source == ""
    assert len(effects) == 67
    assert skipped == []
    with pytest.raises(commands.BadArgument, match="up to 67"):
        _parse_effect_pipeline(" ".join(["invert"] * 68))


def test_pipeline_random_effects_are_unique_and_report_their_choices() -> None:
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
    assert len(chosen) == 3
    assert len(set(chosen)) == 3
    assert set(chosen) <= set(RANDOM_EFFECTS)
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


def test_pipeline_random_flags_control_option_randomization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_effect_commands, "RANDOM_EFFECTS", ("blur",))
    no_random = _resolve_pipeline_random_effects(
        [("random", {"norandom": True, "fullrandom": False})]
    )
    assert no_random[0][2] == {"radius": 5.0, "blur_type": "gaussian"}

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
    assert 0 <= float(options["start"]) < float(options["stop"]) <= 4.0
    assert options.get("duration", 0.0) == 0.0
    assert 0 <= float(options["amount"]) <= 5


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


def test_pipeline_random_overlay_selects_a_flag() -> None:
    resolved = _resolve_pipeline_random_effects(
        [("random", {"category": "overlay", "norandom": True})]
    )
    assert resolved[0][0] == "overlayflag"
    assert resolved[0][1].startswith("random overlay (")
    assert resolved[0][2]["flag"] in media_effect_commands.PRIDE_FLAGS


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


def test_sound_effect_random_time_and_full_random_flags() -> None:
    _, effects, skipped = _parse_effect_pipeline(
        "audio sound-effect random -fr " "audio sound-effect random -random_time false"
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


def test_bundled_audio_effect_catalog_has_stable_ids_and_special_names() -> None:
    catalog = audio_effect_catalog()
    assert [effect.id for effect in catalog] == list(range(1, len(catalog) + 1))
    assert len(catalog) == 100
    assert find_audio_effect("six-one").name == "six-one"
    assert find_audio_effect("six-seven").name == "six-seven"
    assert find_audio_effect(str(catalog[0].id)) == catalog[0]
    assert all(effect.path.is_file() for effect in catalog)


def test_numeric_effect_options_clamp_and_report_adjustment() -> None:
    options, adjustments = _normalize_effect_options(
        "overlayimage",
        {"opacity": 250.0, "scale": 0.01, "x": 10_000},
    )
    assert options == {"opacity": 100.0, "scale": 0.05, "x": 4096}
    assert len(adjustments) == 3
    assert all("Rounded overlayimage" in adjustment for adjustment in adjustments)


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
                        "https://cdn.discordapp.com/attachments/1/2/file.png"
                        "?ex=fresh"
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
    assert "Skipped: invert (took longer than 30 seconds)" in sent_notes[0]


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
            media="https://example.com/source.gif",
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


def test_sideways_hallway_command_and_pipeline_effect_are_removed() -> None:
    assert isinstance(Images.hallway, commands.Command)
    assert not isinstance(Images.hallway, commands.Group)
    assert "hallwaysideways" not in PIPELINE_EFFECTS


def test_image_and_video_application_groups_are_within_discord_limits() -> None:
    assert len(Images.image_effect.commands) == 24
    assert len(Images.effect_2.commands) == 25
    assert len(Images.effect_3.commands) == 17
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
        "image",
        "video",
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

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    for group in (Images.image_effect, Images.effect_2, Images.effect_3):
        assert group.app_command is not None
        payload = group.app_command.to_dict(bot.tree)
        assert command_size(payload) <= 8_000, group.name


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
                assert "User/Emoji/Media URL" in command.description
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
        "bass boost",
        "bass lower",
        "adhd",
        "blur",
        "caption",
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
        "volume",
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
        "overlay flag",
        "overlay image",
        "overlay video",
        "parallax",
        "quilt",
        "random",
        "remove bars",
        "remove caption",
        "remove outro-reels",
        "remove outro-tiktok",
        "squishy",
        "tremble",
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


def test_media_converter_reads_discord_urls_from_recent_message_content() -> None:
    recent = SimpleNamespace(
        id=10,
        attachments=[],
        embeds=[],
        components=[],
        stickers=[],
        content=(
            "https://cdn.discordapp.com/attachments/1/2/video.mp4" "?ex=abc&hm=def"
        ),
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


@pytest.mark.parametrize("effect", ("gifmagik", "gifswirl"))
def test_animated_distortions_keep_gif_inputs_as_gifs(effect: str) -> None:
    result = render_image_effect_sync(
        _gif_bytes(),
        effect,
        strength=20 if effect == "gifmagik" else 180,
        speed=1,
    )
    with Image.open(BytesIO(result.data)) as output:
        assert output.format == "GIF"


@pytest.mark.parametrize("effect", ("gifmagik", "gifswirl"))
def test_animated_distortions_support_video_inputs(effect: str) -> None:
    result = render_image_effect_sync(_sample_video(), effect)
    assert result.filename.endswith(".mp4")
    assert result.data


@pytest.mark.parametrize("effect", ("gifmagik", "gifswirl"))
def test_animated_distortions_support_videos_over_thirty_seconds(effect: str) -> None:
    result = render_image_effect_sync(
        _sample_long_video(),
        effect,
        strength=20 if effect == "gifmagik" else 90,
        speed=1,
    )
    assert result.filename.endswith(".mp4")
    assert result.data


@pytest.mark.parametrize("effect", ("gifmagik", "gifswirl"))
def test_animated_distortions_reject_gifs_over_thirty_seconds(effect: str) -> None:
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
    with pytest.raises(ValueError, match="up to 30 seconds"):
        render_image_effect_sync(output.getvalue(), effect)


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


def test_magik_reuses_a_stable_map_without_losing_gif_timing() -> None:
    result = render_image_effect_sync(_gif_bytes(), "magik", strength=20)
    with Image.open(BytesIO(result.data)) as output:
        assert output.format == "GIF"
        assert int(getattr(output, "n_frames", 1)) == 2
        durations: list[int] = []
        for index in range(2):
            output.seek(index)
            durations.append(int(output.info["duration"]))
        assert durations == [80, 120]


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
        ("magik", {"strength": 20}),
        ("swirl", {"strength": 45}),
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


def test_sound_effect_keeps_video_when_base_audio_ends_early() -> None:
    result = render_video_effect_sync(
        _sample_video_with_short_audio(),
        "soundeffect",
        second_data=audio_effect_catalog()[0].path.read_bytes(),
        random_time=False,
    )
    assert probe_media_sync(result.data).duration >= 1.8


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
        "swirl",
        strength=45,
        start=0.1,
        stop=0.35,
    )
    assert result.filename == "swirl.mp4"
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
    webm = convert_media_sync(video, "webm")
    mp3 = convert_media_sync(video, "mp3")
    assert webm.filename == "converted-1.webm"
    assert mp3.filename == "converted-1.mp3"
    assert webm.data
    assert mp3.data

from io import BytesIO

import pytest
from discord.ext import commands
from PIL import Image

from extensions.media_effects.commands import (
    Images,
    _atempo_filter,
    _caption_frame,
    _encode_spin3d,
    _globe_frame,
    _is_klipy_media_url,
    _normalize_effect_options,
    _parse_globe_input,
    _parse_spin3d_input,
    _positional_flag,
    _prepare_globe_texture,
    _prepare_spin3d_source,
    _spin3d_animation_rotations,
    _spin3d_animation_timing,
    _spin3d_frame,
    _spin3d_source_timeline,
    _spin3d_timing,
)


def test_image_effects_use_one_grouped_application_command() -> None:
    assert isinstance(Images.globe, commands.Command)
    assert not isinstance(Images.globe, commands.HybridCommand)
    assert isinstance(Images.spin3d, commands.Command)
    assert not isinstance(Images.spin3d, commands.HybridCommand)
    assert isinstance(Images.caption, commands.Command)
    assert not isinstance(Images.caption, commands.HybridCommand)
    assert isinstance(Images.speed, commands.Command)
    assert not isinstance(Images.speed, commands.HybridCommand)

    group = Images.image_effect
    assert isinstance(group, commands.HybridGroup)
    assert {command.name for command in group.commands} >= {
        "caption",
        "globe",
        "speed",
        "spin3d",
    }


def test_audio_speed_filter_stays_inside_ffmpeg_limits() -> None:
    for speed in (0.1, 0.25, 0.5, 1.0, 2.0, 10.0):
        factors = [
            float(item.split("=", 1)[1]) for item in _atempo_filter(speed).split(",")
        ]
        assert all(0.5 <= factor <= 2.0 for factor in factors)
        product = 1.0
        for factor in factors:
            product *= factor
        assert abs(product - speed) < 1e-5


def test_caption_supports_multilingual_text_and_inline_emoji() -> None:
    emoji_data = BytesIO()
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(emoji_data, "PNG")
    source = Image.new("RGBA", (480, 240), (20, 20, 20, 255))

    result = _caption_frame(
        source,
        "日本語 العربية 😀",
        {"😀": emoji_data.getvalue()},
    )

    assert result.width == source.width
    assert result.height > source.height
    caption_height = result.height - source.height
    caption = result.crop((0, 0, result.width, caption_height))
    assert caption.getcolors(maxcolors=result.width * caption_height) != [
        (result.width * caption_height, (255, 255, 255, 255))
    ]


def test_caption_recognizes_direct_klipy_media() -> None:
    assert _is_klipy_media_url("https://static.klipy.com/example.mp4")
    assert not _is_klipy_media_url("https://example.com/video.mp4")


def test_overlay_flag_accepts_trailing_positional_flag() -> None:
    media, flag = _positional_flag("<@766953372309127168> pride")

    assert media == "<@766953372309127168>"
    assert flag == "pride"


def test_gay_is_a_pride_flag_alias() -> None:
    media, flag = _positional_flag("<@766953372309127168> gay")

    assert media == "<@766953372309127168>"
    assert flag == "gay"


def test_overlay_flag_accepts_a_flag_without_explicit_media() -> None:
    media, flag = _positional_flag("lesbian")

    assert media == ""
    assert flag == "lesbian"


def test_overlay_flag_keeps_a_lone_media_source() -> None:
    source = "https://example.com/image.png"
    media, flag = _positional_flag(source)

    assert media == source
    assert flag is None


def test_spin3d_flags_are_removed_from_media_argument() -> None:
    media, tilt, zoom, speed, clockwise = _parse_spin3d_input(
        "https://example.com/image.png -tilt -20 --zoom 2.25 -s 1.5 -c"
    )

    assert media == "https://example.com/image.png"
    assert tilt == -20
    assert zoom == 2.25
    assert speed == 1.5
    assert clockwise is True


def test_globe_flags_are_removed_from_media_argument() -> None:
    media, speed, clockwise = _parse_globe_input(
        "https://example.com/image.png -speed 2 -clockwise"
    )

    assert media == "https://example.com/image.png"
    assert speed == 2
    assert clockwise is True


def test_globe_projects_texture_onto_transparent_sphere() -> None:
    texture_source = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    for x in range(32, 64):
        for y in range(64):
            texture_source.putpixel((x, y), (0, 0, 255, 255))
    texture = _prepare_globe_texture(texture_source)
    first = _globe_frame(texture, 0, 64)
    opposite = _globe_frame(texture, 180, 64)

    assert first.getchannel("A").getpixel((0, 0)) == 0
    assert first.getchannel("A").getpixel((32, 32)) == 255
    assert first.getpixel((32, 32)) != opposite.getpixel((32, 32))


@pytest.mark.parametrize(
    ("argument", "field", "expected"),
    (
        ("-tilt 361", "tilt", 360.0),
        ("-zoom -3.1", "zoom", -3.0),
        ("-speed 3.1", "speed", 3.0),
    ),
)
def test_spin3d_clamps_out_of_range_flags(
    argument: str,
    field: str,
    expected: float,
) -> None:
    _, tilt, zoom, speed, _ = _parse_spin3d_input(argument)
    options, adjustments = _normalize_effect_options(
        "spin3d",
        {"tilt": tilt, "zoom": zoom, "speed": speed},
    )

    assert options[field] == expected
    assert any(f"spin3d {field}" in adjustment for adjustment in adjustments)


def test_spin3d_rejects_a_flag_without_a_number() -> None:
    with pytest.raises(ValueError, match="require a number"):
        _parse_spin3d_input("-tilt nope")


def test_spin3d_renderer_produces_transparent_animated_gif() -> None:
    source = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    frames = [
        _spin3d_frame(source, angle, tilt_degrees=15, zoom=1.5)
        for angle in (0, 90, 180)
    ]
    output = _encode_spin3d(frames)

    with Image.open(output) as result:
        assert result.format == "GIF"
        assert result.size == (64, 64)
        assert int(getattr(result, "n_frames", 1)) == 3
        assert result.info["loop"] == 0


def test_spin3d_scales_small_images_to_fill_the_working_canvas() -> None:
    source = _prepare_spin3d_source(Image.new("RGBA", (89, 82), (255, 0, 0, 255)))

    assert source.size == (512, 512)
    assert source.getchannel("A").getbbox() == (0, 20, 512, 492)


def test_spin3d_animated_timing_preserves_short_gifs() -> None:
    timestamps, durations = _spin3d_animation_timing(200)

    assert timestamps == list(range(0, 200, 20))
    assert sum(durations) == 200


def test_spin3d_animated_timing_caps_long_gifs() -> None:
    timestamps, durations = _spin3d_animation_timing(10_000)

    assert len(timestamps) <= 150
    assert sum(durations) == 10_000


@pytest.mark.parametrize(
    ("duration", "speed", "rotations"),
    (
        (900, 1.0, 1),
        (900, 2.0, 2),
        (2_000, 1.0, 3),
    ),
)
def test_spin3d_animated_loops_use_complete_rotations(
    duration: int,
    speed: float,
    rotations: int,
) -> None:
    assert _spin3d_animation_rotations(duration, speed) == rotations


def test_spin3d_reads_animated_gif_timing() -> None:
    first = Image.new("RGBA", (16, 16), (255, 0, 0, 255))
    second = Image.new("RGBA", (16, 16), (0, 0, 255, 255))
    gif = BytesIO()
    first.save(
        gif,
        "GIF",
        save_all=True,
        append_images=[second],
        duration=[80, 120],
        loop=0,
        disposal=2,
    )
    gif.seek(0)

    with Image.open(gif) as animation:
        frame_starts, total_duration = _spin3d_source_timeline(animation)

    assert frame_starts == [0, 80]
    assert total_duration == 200


@pytest.mark.parametrize(
    ("speed", "expected_frames", "expected_duration"),
    (
        (0.5, 74, 20),
        (1.0, 37, 20),
        (2.0, 18, 20),
        (3.0, 12, 20),
    ),
)
def test_spin3d_speed_changes_loop_timing(
    speed: float,
    expected_frames: int,
    expected_duration: int,
) -> None:
    assert _spin3d_timing(speed) == (expected_frames, expected_duration)

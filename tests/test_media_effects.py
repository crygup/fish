from __future__ import annotations

import asyncio
import shutil
import subprocess
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord.ext import commands
from PIL import Image

from extensions.fun import Fun
from extensions.media_effects import MediaEffects
from extensions.media_effects.commands import Images, _parse_effect_flags
from extensions.media_effects.processing import (
    convert_media_sync,
    make_flag_asset,
    render_image_effect_sync,
    render_video_effect_sync,
)
from utils.converters import KlipyUrlConverter, MediaConverter, TenorUrlConverter


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


def test_effect_text_flags_keep_media_and_parse_options() -> None:
    media, options = _parse_effect_flags(
        "https://example.com/a.gif --speed=2 -clockwise",
        values={"speed": (("s",), float, 1.0)},
        switches={"clockwise": ("c",)},
    )
    assert media == "https://example.com/a.gif"
    assert options == {"speed": 2.0, "clockwise": True}


def test_image_and_video_application_groups_are_within_discord_limits() -> None:
    assert len(Images.image_effect.commands) <= 25
    assert len(Images.video_effects.commands) <= 25
    assert {command.name for command in Images.image_effect.commands} >= {
        "crop",
        "mirror",
        "overlay",
        "invert",
        "spin",
        "magik",
        "gifmagik",
        "flip",
        "cube",
        "pyramid",
        "blur",
        "deepfry",
        "grayscale",
        "jpeg",
        "swirl",
        "gifswirl",
        "wiggle",
    }
    assert {command.name for command in Images.video_effects.commands} == {
        "audio",
        "bass",
        "convert",
        "overlay",
        "reverse",
        "volume",
    }
    assert isinstance(Images.reverse, commands.Command)
    assert not isinstance(Images.reverse, commands.HybridCommand)
    assert {command.name for command in Images.crop_group.commands} == {
        "circle",
        "triangle",
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
        "channels-combine",
        "compress",
        "destroy",
        "extract",
        "replace",
        "reverb",
        "reverse",
    }


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
        "blur",
        "caption",
        "crop circle",
        "crop triangle",
        "cube",
        "deepfry",
        "flip",
        "gifmagik",
        "gifswirl",
        "globe",
        "grayscale",
        "invert",
        "jpeg",
        "magik",
        "mirror bottom",
        "mirror left",
        "mirror right",
        "mirror top",
        "overlay flag",
        "overlay image",
        "pyramid",
        "speed",
        "spin",
        "spin3d",
        "swirl",
        "wiggle",
    }
    assert qualified_children(Images.video_effects) == {
        "audio channels-combine",
        "audio compress",
        "audio destroy",
        "audio extract",
        "audio replace",
        "audio reverb",
        "audio reverse",
        "bass boost",
        "bass lower",
        "convert",
        "overlay video",
        "reverse",
        "volume",
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
    assert {"image-effect", "video-effects"} <= media_roots

    quote = next(
        command
        for command in Fun.__cog_commands__
        if command.name.startswith("quoteisifyouhaveaproblem")
    )
    assert quote.hidden is True
    assert quote.enabled is True


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


@pytest.mark.parametrize(
    "effect,options",
    (
        ("invert", {}),
        ("flip", {"direction": "vertical"}),
        ("blur", {"radius": 2}),
        ("crop", {"shape": "circle"}),
        ("crop", {"shape": "triangle"}),
        ("deepfry", {"intensity": 0.5}),
        ("grayscale", {}),
        ("mirror", {"direction": "bottom"}),
        ("mirror", {"direction": "left"}),
        ("jpeg", {"quality": 10}),
        ("magik", {"strength": 5}),
        ("swirl", {"strength": 45}),
    ),
)
def test_static_image_effects_produce_images(
    effect: str, options: dict[str, object]
) -> None:
    result = render_image_effect_sync(_png_bytes(), effect, **options)
    with Image.open(BytesIO(result.data)) as output:
        output.verify()


@pytest.mark.parametrize(
    "effect", ("spin", "gifmagik", "gifswirl", "wiggle", "cube", "pyramid")
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


def test_gif_effect_preserves_animation() -> None:
    result = render_image_effect_sync(_gif_bytes(), "invert")
    with Image.open(BytesIO(result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) == 2


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
            first = output.convert("RGB")
            output.seek(10)
            later = output.convert("RGB")
            assert first.tobytes() != later.tobytes()
            assert first.getpixel((0, 0)) == (255, 255, 255)


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
    flag = make_flag_asset("pride")
    assert flag is not None
    flag_overlaid = render_image_effect_sync(
        video,
        "overlay",
        overlay_data=flag,
        opacity=0.5,
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
    assert overlay.data
    assert replaced.data


def test_reverse_preserves_gif_output() -> None:
    result = render_video_effect_sync(_gif_bytes(), "reverse")
    assert result.filename == "reverse.gif"
    with Image.open(BytesIO(result.data)) as output:
        assert int(getattr(output, "n_frames", 1)) == 2


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

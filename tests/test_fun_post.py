from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import extensions.fun.post as post_module
from extensions.fun.post import PostCommands
from utils.converters import TenorUrlConverter


def test_post_accepts_images_and_gifs_but_not_other_media() -> None:
    assert PostCommands._is_post_attachment(  # type: ignore[arg-type]
        SimpleNamespace(filename="photo.png", content_type="image/png")
    )
    assert PostCommands._is_post_attachment(  # type: ignore[arg-type]
        SimpleNamespace(filename="animation.gif", content_type="image/gif")
    )
    assert not PostCommands._is_post_attachment(  # type: ignore[arg-type]
        SimpleNamespace(filename="video.mp4", content_type="video/mp4")
    )
    assert not PostCommands._is_post_attachment(  # type: ignore[arg-type]
        SimpleNamespace(filename="vector.svg", content_type="image/svg+xml")
    )


def test_post_accepts_common_image_signatures_when_cdn_omits_mime() -> None:
    assert PostCommands._looks_like_post_data(b"GIF89a" + b"\x00" * 10, "x.gif")
    assert PostCommands._looks_like_post_data(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "x.png"
    )
    assert PostCommands._looks_like_post_data(b"RIFF0000WEBP", "x.webp")
    assert PostCommands._looks_like_post_data(
        b"\x00\x00\x00\x18ftypavif" + b"\x00" * 4, "x.png"
    )
    assert not PostCommands._looks_like_post_data(b"not an image", "x.png")


def test_tenor_media1_host_is_accepted_for_original_gifs() -> None:
    assert "media1.tenor.com" in TenorUrlConverter._MEDIA_HOSTS


def test_tenor_legacy_media_url_has_current_cdn_variants() -> None:
    url = (
        "https://media1.tenor.com/m/qiDqskwrsKgAAAAC/"
        "take-your-clothes-off-paulie.gif"
    )
    variants = TenorUrlConverter.media_url_variants(url)
    assert (
        "https://media.tenor.com/qiDqskwrsKgAAAAM/"
        "take-your-clothes-off-paulie.gif"
    ) in variants


@pytest.mark.asyncio
async def test_direct_tenor_media_is_fetched_as_a_post_source() -> None:
    cog = PostCommands()
    expected = (Path("/tmp/post.gif"), "post.gif", 123, "https://media1.tenor.com/source")
    cog._download_post_with_downloader = AsyncMock(  # type: ignore[method-assign]
        return_value=expected
    )

    result = await cog._download_post_source(
        SimpleNamespace(),
        "https://media1.tenor.com/m/qiDqskwrsKgAAAAC/take-your-clothes-off-paulie.gif",
    )

    assert result == expected
    cog._download_post_with_downloader.assert_awaited_once()


def test_gif_converter_url_uses_its_animated_source() -> None:
    converter_url = (
        "https://gifconvert.vxtwitter.com/convert.avif?"
        "url=https://video.twimg.com/tweet_video/HMkP2NAW8AAFlnB.mp4"
    )
    assert PostCommands._gif_converter_source(converter_url) == (
        "https://video.twimg.com/tweet_video/HMkP2NAW8AAFlnB.mp4"
    )


def test_post_has_the_same_library_subcommands_as_video() -> None:
    assert {command.name for command in PostCommands.post.all_commands.values()} == {
        "random",
        "uploads",
        "stats",
        "alias",
        "block",
        "unblock",
        "hide",
        "unhide",
        "upload",
        "delete",
        "repair",
    }


def _post_cog(pool: object) -> PostCommands:
    cog = PostCommands()
    cog.bot = cast(
        Any,
        SimpleNamespace(
            pool=pool,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        ),
    )
    return cog


@pytest.mark.asyncio
async def test_current_post_url_refreshes_the_review_attachment() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    cog = _post_cog(pool)
    message = SimpleNamespace(
        attachments=[SimpleNamespace(url="https://cdn.discordapp.com/fresh.gif")]
    )
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    cog._post_review_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    result = await cog._current_post_url(
        upload_id=390,
        source_url="https://media1.tenor.com/m/old.gif",
        review_message_id=1234,
    )

    assert result == "https://cdn.discordapp.com/fresh.gif"
    pool.execute.assert_awaited_once_with(
        "UPDATE post_uploads SET source_url = $2 WHERE id = $1",
        390,
        "https://cdn.discordapp.com/fresh.gif",
    )


def test_review_embed_url_is_used_when_discord_attachment_list_is_empty() -> None:
    embed = SimpleNamespace(
        image=SimpleNamespace(
            url="https://cdn.discordapp.com/attachments/1538743622260752414/1/file.png"
        )
    )
    message = SimpleNamespace(attachments=[], embeds=[embed])

    assert PostCommands._message_discord_media_url(message) == embed.image.url


@pytest.mark.asyncio
async def test_legacy_review_gif_is_repaired_before_embedding() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    cog = _post_cog(pool)
    legacy = SimpleNamespace(
        filename="old.gif", url="https://cdn.discordapp.com/old.gif"
    )
    repaired = SimpleNamespace(
        filename="old-discord.gif", url="https://cdn.discordapp.com/repaired.gif"
    )
    message = SimpleNamespace(attachments=[legacy])
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    cog._post_review_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]
    cog._repair_review_gif = AsyncMock(return_value=repaired)  # type: ignore[method-assign]

    result = await cog._review_attachment_url(390, 1234)

    assert result == repaired.url
    cog._repair_review_gif.assert_awaited_once_with(390, message, legacy)


@pytest.mark.asyncio
async def test_reply_post_sources_does_not_duplicate_an_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessage:
        pass

    monkeypatch.setattr(post_module.discord, "Message", FakeMessage)
    monkeypatch.setattr(
        post_module.MediaConverter,
        "_message_content_media_url",
        AsyncMock(side_effect=AssertionError("attachment URLs must not be re-read")),
    )
    monkeypatch.setattr(
        post_module.MediaConverter,
        "_message_media_url",
        staticmethod(lambda _message: (_ for _ in ()).throw(AssertionError())),
    )
    attachment = SimpleNamespace(
        filename="picmix.gif",
        content_type="image/gif",
        url="https://cdn.discordapp.com/attachments/1/picmix.gif",
    )
    replied = FakeMessage()
    replied.attachments = [attachment]
    replied.content = attachment.url
    replied.embeds = []
    ctx = SimpleNamespace(
        message=SimpleNamespace(
            reference=SimpleNamespace(message_id=1, resolved=replied)
        )
    )

    attachments, urls = await PostCommands()._reply_post_sources(ctx)  # type: ignore[arg-type]

    assert attachments == [attachment]
    assert urls == []


@pytest.mark.asyncio
async def test_reply_post_sources_prefers_message_url_over_embed_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessage:
        pass

    monkeypatch.setattr(post_module.discord, "Message", FakeMessage)
    original = "https://cdn.discordapp.com/attachments/1/picmix.gif"
    monkeypatch.setattr(
        post_module.MediaConverter,
        "_message_content_media_url",
        AsyncMock(return_value=original),
    )
    monkeypatch.setattr(
        post_module.MediaConverter,
        "_message_media_url",
        staticmethod(lambda _message: "https://images-ext.example/picmix.gif"),
    )
    replied = FakeMessage()
    replied.attachments = []
    replied.content = original
    replied.embeds = []
    ctx = SimpleNamespace(
        message=SimpleNamespace(
            reference=SimpleNamespace(message_id=1, resolved=replied)
        )
    )

    attachments, urls = await PostCommands()._reply_post_sources(ctx)  # type: ignore[arg-type]

    assert attachments == []
    assert urls == [original]


@pytest.mark.asyncio
async def test_klipy_static_mp4_is_converted_to_a_gif_for_post_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeDownloader:
        def __init__(self, ctx: object, url: str, **kwargs: object) -> None:
            calls.append((ctx, url, kwargs))

        async def download_for_processing(self, **_kwargs: object) -> tuple[bytes, str]:
            return b"gif bytes", "klipy.gif"

    monkeypatch.setattr(post_module, "Downloader", FakeDownloader)
    monkeypatch.setattr(
        PostCommands,
        "_normalize_gif_for_discord",
        AsyncMock(side_effect=lambda path: (path, path.stat().st_size)),
    )
    source_url = "https://static.klipy.com/gifs/bart-simpson-118.mp4"
    path, filename, size, returned_url = await PostCommands()._download_post_source(
        SimpleNamespace(), source_url  # type: ignore[arg-type]
    )
    try:
        assert path.read_bytes() == b"gif bytes"
        assert filename == "klipy.gif"
        assert size == len(b"gif bytes")
        assert returned_url == source_url
        assert calls and calls[0][1] == source_url
        assert calls[0][2]["format"] == "gif"
    finally:
        Path(path).unlink(missing_ok=True)

from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.search.google import GoogleImageLayoutSource, _image_source_link
from utils.vars import GoogleImageData


def _entry(source_url: str) -> GoogleImageData:
    return GoogleImageData(
        image_url="https://images.example/image.png",
        url=source_url,
        snippet="Test image",
        query="test",
        author=cast(Any, SimpleNamespace()),
    )


def test_image_source_link_uses_only_the_hostname_as_its_label() -> None:
    assert _image_source_link("https://www.domain.com/path/to/page?id=2") == (
        "[domain.com](https://www.domain.com/path/to/page?id=2)"
    )


def test_image_footer_links_to_the_result_source() -> None:
    source = GoogleImageLayoutSource(
        [_entry("https://subdomain.example.com/articles/image")]
    )

    items = source.format_page(0)
    footer = cast(discord.ui.TextDisplay, items[-1]).content

    assert footer == (
        "-# Page 1/1 · Google Image search: test · "
        "[subdomain.example.com](https://subdomain.example.com/articles/image)"
    )


def test_image_footer_omits_invalid_source_urls() -> None:
    source = GoogleImageLayoutSource([_entry("not a URL")])

    items = source.format_page(0)
    footer = cast(discord.ui.TextDisplay, items[-1]).content

    assert footer == "-# Page 1/1 · Google Image search: test"

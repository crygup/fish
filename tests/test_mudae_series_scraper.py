from __future__ import annotations

from types import SimpleNamespace

import discord

from extensions.mudae.series_scraper import (
    MUDAE_USER_ID,
    MudaeSeriesAutoScraper,
    find_mudae_bundle_for_context,
    is_mudae_message,
    merge_bundle_pages,
    normalize_series_name,
    parse_mudae_bundle_message,
    parse_series_bundle_embed,
    scrape_series_from_context,
)


def _bundle_embed(*, page: str = "Page 1 / 2", wrapped: bool = False) -> discord.Embed:
    wrapped_series = (
        "JoJo's Bizarre Adventure: Diamond Is\nUnbreakable"
        if wrapped
        else "JoJo's Bizarre Adventure: Diamond Is Unbreakable"
    )
    return discord.Embed.from_dict(
        {
            "author": {"name": "JoJo's Bizarre Adventure (Bundle)"},
            "description": (
                "**381** chars with a main series in this bundle.\n"
                "**382** total chars.\n\n"
                "· Crazy Diamond's Demonic Heartbreak (**9**)\n"
                f"· {wrapped_series} (**49**)"
            ),
            "footer": {"text": page},
        }
    )


def _message(embed: discord.Embed, *, message_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=MUDAE_USER_ID, bot=True),
        guild=SimpleNamespace(id=456),
        channel=SimpleNamespace(id=789),
        embeds=[embed],
    )


def test_bundle_parser_extracts_page_and_wrapped_series_names() -> None:
    parsed = parse_series_bundle_embed(_bundle_embed(wrapped=True))
    assert parsed is not None
    assert parsed.name == "JoJo's Bizarre Adventure"
    assert parsed.page == 1
    assert parsed.pages == 2
    assert [entry.name for entry in parsed.entries] == [
        "Crazy Diamond's Demonic Heartbreak",
        "JoJo's Bizarre Adventure: Diamond Is Unbreakable",
    ]
    assert parsed.entries[1].character_count == 49


def test_parser_rejects_non_bundle_and_non_mudae_messages() -> None:
    ordinary = discord.Embed.from_dict(
        {"author": {"name": "Character"}, "description": "Series\n**42** ka"}
    )
    assert parse_series_bundle_embed(ordinary) is None
    assert parse_mudae_bundle_message(_message(ordinary)) is None
    assert is_mudae_message(_message(_bundle_embed()))
    assert not is_mudae_message(
        SimpleNamespace(author=SimpleNamespace(id=987), embeds=[])
    )


def test_bundle_message_includes_source_ids_and_page_footer_variants() -> None:
    message = _message(_bundle_embed(page="2/2"), message_id=987)
    parsed = parse_mudae_bundle_message(message)
    assert parsed is not None
    assert parsed.message_id == 987
    assert parsed.guild_id == 456
    assert parsed.channel_id == 789
    assert parsed.page == 2
    assert parsed.pages == 2


def test_merge_bundle_pages_deduplicates_case_and_wrapping_variants() -> None:
    first = parse_series_bundle_embed(_bundle_embed())
    second = parse_series_bundle_embed(
        discord.Embed.from_dict(
            {
                "author": {"name": "JoJo's Bizarre Adventure (Bundle)"},
                "description": "· jojo's bizarre adventure: diamond is\n"
                "unbreakable (**49**)\n"
                "· New Series (**3**)",
                "footer": {"text": "Page 2 / 2"},
            }
        )
    )
    assert first is not None and second is not None
    merged = merge_bundle_pages((first, second))
    assert [entry.name for entry in merged] == [
        "Crazy Diamond's Demonic Heartbreak",
        "JoJo's Bizarre Adventure: Diamond Is Unbreakable",
        "New Series",
    ]
    assert normalize_series_name("Diamond Is\nUnbreakable") == "diamond is unbreakable"


async def test_find_bundle_prefers_reply_then_history() -> None:
    reply = _message(_bundle_embed(), message_id=1)
    history_match = _message(_bundle_embed(page="2 / 2"), message_id=2)

    class Channel:
        def __init__(self) -> None:
            self.fetched: list[int] = []

        async def fetch_message(self, message_id: int) -> SimpleNamespace:
            self.fetched.append(message_id)
            return reply

        def history(self, *, limit: int):
            assert limit == 10

            async def iterator():
                yield SimpleNamespace(
                    author=SimpleNamespace(id=999),
                    embeds=[],
                )
                yield history_match

            return iterator()

    channel = Channel()
    reference = SimpleNamespace(message_id=1, resolved=None)
    found = await find_mudae_bundle_for_context(
        SimpleNamespace(channel=channel, message=SimpleNamespace(reference=reference))
    )
    assert found is reply
    assert channel.fetched == [1]


async def test_scrape_series_from_context_returns_parsed_source() -> None:
    message = _message(_bundle_embed(), message_id=9)

    class Channel:
        def history(self, *, limit: int):
            async def iterator():
                yield message

            return iterator()

    parsed = await scrape_series_from_context(
        SimpleNamespace(
            channel=Channel(),
            message=SimpleNamespace(reference=None),
        )
    )
    assert parsed is not None
    assert parsed.message_id == 9
    assert parsed.bundle_key == "jojo's bizarre adventure"


def test_auto_scraper_only_ingests_enabled_guilds() -> None:
    scraper = MudaeSeriesAutoScraper()
    message = _message(_bundle_embed())
    assert scraper.handle_message(message) is None
    scraper.set_enabled(456)
    parsed = scraper.handle_message(message)
    assert parsed is not None
    assert scraper.bundles[(456, "jojo's bizarre adventure")].entries
    scraper.clear_guild(456)
    assert scraper.bundles == {}

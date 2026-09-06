from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import discord

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import Context
from extensions.search.letterboxd import (
    FilmsPageSource,
    Letterboxd,
    _DiaryFilm,
    _films_diary_suffix,
    _films_omdb_title,
    _films_page_for_index,
    _films_parse_diary_row,
    _films_url_is_rewatch,
    _FilmsCache,
)
from utils.paginator import LayoutPager


def _film(index: int) -> _DiaryFilm:
    return _DiaryFilm(
        title=f"Film {index}",
        film_url=f"https://letterboxd.com/film/film-{index}/",
        diary_url=f"https://letterboxd.com/crygup/films/diary/film-{index}/",
        watched_date=(dt.date(2026, 1, 1) + dt.timedelta(days=index)).isoformat(),
        rewatch=index % 2 == 0,
        review_url=(
            f"https://letterboxd.com/crygup/film/film-{index}/review/"
            if index == 0
            else None
        ),
        year=str(2020 + index),
    )


def _source(entries: list[_DiaryFilm], *, total: int | None = None) -> FilmsPageSource:
    cache = _FilmsCache(
        username="crygup",
        total=len(entries) if total is None else total,
        pages={1: entries[:50]},
    )
    return FilmsPageSource(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cache,
    )


def test_films_page_source_maps_each_film_to_one_page() -> None:
    source = _source([_film(index) for index in range(101)], total=101)

    assert source.cache.page_size == 50
    assert source.get_max_pages() == 101
    assert source.cache.diary_page_count == 3
    assert _films_page_for_index(0, source.cache.page_size) == 1
    assert _films_page_for_index(49, source.cache.page_size) == 1
    assert _films_page_for_index(50, source.cache.page_size) == 2
    assert _films_page_for_index(100, source.cache.page_size) == 3


def test_films_uses_letterboxd_diary_page_paths() -> None:
    assert _films_diary_suffix(1) == "/diary/"
    assert _films_diary_suffix(2) == "/diary/films/page/2/"
    assert _films_diary_suffix(7) == "/diary/films/page/7/"


def test_films_page_source_builds_navigation_for_multiple_films() -> None:
    cache = _FilmsCache(
        username="crygup",
        total=2,
        pages={1: [_film(0), _film(1)]},
    )
    ctx = SimpleNamespace(
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        author=SimpleNamespace(id=1),
    )
    view = LayoutPager(
        FilmsPageSource(cast(Any, SimpleNamespace()), cast(Any, ctx), cache),
        ctx=cast(Any, ctx),
    )

    assert view.page_count == 2
    assert len(view._navigation_buttons) == 5


def test_films_does_not_fetch_beyond_first_diary_page() -> None:
    requested_pages: list[int] = []

    class LazyLetterboxd:
        async def _films_prepare_page(
            self, ctx: Any, cache: _FilmsCache, page_number: int
        ) -> bool:
            return await Letterboxd._films_prepare_page(
                cast("Letterboxd", self), ctx, cache, page_number
            )

        async def _scrape_diary_page(
            self, ctx: Any, username: str, page_number: int
        ) -> tuple[int, list[_DiaryFilm]]:
            requested_pages.append(page_number)
            return 550, [_film(50)] if page_number == 2 else []

        async def _films_fetch_omdb(self, entry: _DiaryFilm) -> None:
            return None

        async def _films_fetch_review(self, review_url: str) -> None:
            return None

    async def scenario() -> None:
        cache = _FilmsCache(
            username="crygup",
            total=50,
            pages={1: [_film(index) for index in range(50)]},
        )
        source = FilmsPageSource(
            cast(Any, LazyLetterboxd()), cast(Any, SimpleNamespace()), cache
        )

        assert not await source.prepare_page(50)
        assert requested_pages == []

    asyncio.run(scenario())


def test_films_missing_page_is_not_prefetched_or_used_to_change_total() -> None:
    requested_pages: list[int] = []

    class LastPageLetterboxd:
        async def _films_prepare_page(
            self, ctx: Any, cache: _FilmsCache, page_number: int
        ) -> bool:
            return await Letterboxd._films_prepare_page(
                cast("Letterboxd", self), ctx, cache, page_number
            )

        async def _scrape_diary_page(
            self, ctx: Any, username: str, page_number: int
        ) -> tuple[int, list[_DiaryFilm]]:
            requested_pages.append(page_number)
            return 455, []

        async def _films_fetch_omdb(self, entry: _DiaryFilm) -> None:
            return None

        async def _films_fetch_review(self, review_url: str) -> None:
            return None

    async def scenario() -> None:
        cache = _FilmsCache(
            username="crygup",
            total=600,
            pages={1: [_film(index) for index in range(50)]},
        )
        source = FilmsPageSource(
            cast(Any, LastPageLetterboxd()), cast(Any, SimpleNamespace()), cache
        )

        assert not await source.prepare_page(599)
        assert requested_pages == []
        assert cache.total == 600
        assert 10 not in cache.pages

    asyncio.run(scenario())


def test_films_page_source_formats_rewatch_and_release_dates() -> None:
    entry = _film(0)
    entry.rating = "★★★★½"
    entry.omdb = {"Released": "01 Jan 2020"}
    source = _source([entry])

    items = source.format_page(0)
    text = "\n".join(
        getattr(item, "content", "") for item in items if hasattr(item, "content")
    )

    assert "Film 0" in text
    assert "Rewatch" in text
    assert "Rating" in text
    assert "**Watched:** <t:" in text
    assert "**Released:** <t:" in text
    assert "Letterboxd diary" in text
    assert "OMDb metadata" not in text
    assert text.index("**Rating:**") < text.index("**Watched:**")
    assert text.index("**Watched:**") < text.index("**Released:**")


def test_films_page_source_omits_rewatch_for_normal_entries() -> None:
    entry = _film(1)
    entry.rating = "★★★"
    entry.omdb = {"Released": "01 Jan 2020"}
    source = _source([entry])

    text = "\n".join(
        getattr(item, "content", "")
        for item in source.format_page(0)
        if hasattr(item, "content")
    )

    assert "**Rating:**" in text
    assert "**Watched:**" in text
    assert "**Released:**" in text
    assert "**Rewatch:**" not in text


def test_films_page_source_includes_review_only_after_it_is_loaded() -> None:
    entry = _film(0)
    source = _source([entry])
    assert "Review" not in "\n".join(
        getattr(item, "content", "")
        for item in source.format_page(0)
        if hasattr(item, "content")
    )

    entry.review_text = "A review fetched from the requested film page."
    text = "\n".join(
        getattr(item, "content", "")
        for item in source.format_page(0)
        if hasattr(item, "content")
    )
    assert "**Review**" in text
    assert entry.review_text in text


def test_diary_total_uses_first_page_count_and_falls_back_for_later_pages() -> None:
    assert Letterboxd._diary_total("Showing 1-50 of 123 films", 1, 50) == 123
    assert Letterboxd._diary_total("Page 1 of 3", 1, 50, 3) == 150
    assert Letterboxd._diary_total("Page 1 of 3", 1, 50) == 150
    assert Letterboxd._diary_total("No total here", 3, 7) == 107


def test_diary_total_prefers_diary_entries_over_profile_film_count() -> None:
    body = "Diary\n455\nFilms\n608\nShowing 1-50 of 12"

    assert Letterboxd._diary_total(body, 1, 50, 12) == 455
    assert Letterboxd._diary_total("Diary 455 Films 608", 1, 50) == 455


def test_diary_total_does_not_treat_film_count_as_diary_count() -> None:
    assert Letterboxd._extract_diary_total("608 Films\nDiary\n455") == 455
    assert (
        Letterboxd._extract_diary_total(
            '<a href="/fluttershy/diary/">Diary</a><span>455</span>'
        )
        == 455
    )
    assert Letterboxd._diary_total("Page 1 of 12\n608 Films", 1, 50, 12) == 600


def test_films_cache_is_limited_to_the_first_diary_page() -> None:
    class CacheLetterboxd:
        def __init__(self) -> None:
            self.caches: dict[str, _FilmsCache] = {}

        def _films_caches(self) -> dict[str, _FilmsCache]:
            return self.caches

        async def _scrape_diary_page(
            self, ctx: Any, username: str, page_number: int
        ) -> tuple[int, list[_DiaryFilm]]:
            return 600, [_film(index) for index in range(75)]

    async def scenario() -> None:
        cog = CacheLetterboxd()
        cache = await Letterboxd._films_cache_for(
            cast("Letterboxd", cog),
            cast("Context", SimpleNamespace(session=object())),
            "fluttershy",
        )
        assert cache.total == 50
        assert len(cache.pages[1]) == 50
        assert cache.pages[1][0].title == "Film 0"

    asyncio.run(scenario())


def test_films_omdb_title_removes_markdown_and_release_year() -> None:
    assert _films_omdb_title("The Avengers (2012)") == "The Avengers"
    assert (
        _films_omdb_title(
            "[**The Avengers (2012)**](https://letterboxd.com/film/the-avengers/)"
        )
        == "The Avengers"
    )


def test_diary_review_text_is_lazy_until_page_is_prepared() -> None:
    film = _film(0)
    assert film.review_url is not None
    assert film.review_text is None

    loaded = replace(film, review_text="A review fetched from the film page.")
    assert loaded.review_text == "A review fetched from the film page."


def test_diary_parser_normalizes_date_rewatch_and_review_fields() -> None:
    row = {
        "title": "Example Film",
        "film_url": "/film/example-film/",
        "diary_url": "/crygup/films/diary/example-film/",
        "watched_date": "2026-02-03",
        "rewatch": True,
        "review_url": "/crygup/film/example-film/review/",
        "year": "2024",
        "rating": "★★★½",
        "poster_url": "https://a.ltrbxd.com/poster.jpg",
    }

    film = _films_parse_diary_row(row)

    assert film.title == "Example Film"
    assert film.film_url == "/film/example-film/"
    assert film.diary_url == "/crygup/films/diary/example-film/"
    assert film.watched_date == "2026-02-03"
    assert film.rewatch is True
    assert film.review_url == "/crygup/film/example-film/review/"
    assert film.year == "2024"
    assert film.rating == "★★★½"
    assert film.poster_url == "https://a.ltrbxd.com/poster.jpg"
    assert film.review_text is None


def test_diary_parser_handles_missing_optional_values() -> None:
    film = _films_parse_diary_row({"film_url": "/film/missing-title/"})

    assert film.title == "Untitled film"
    assert film.film_url == "/film/missing-title/"
    assert film.watched_date is None
    assert film.rewatch is False
    assert film.review_url is None


def test_diary_parser_prefers_complete_date_link_and_parses_false_rewatch() -> None:
    film = _films_parse_diary_row(
        {
            "title": "Example Film",
            "film_url": "/film/example-film/",
            "watched_date": "Apr 2025",
            "date_href": "/fluttershy/films/diary/for/2025/04/13/",
            "rewatch": "false",
        }
    )

    assert film.watched_date == "2025-04-13"
    assert film.rewatch is False


def test_diary_url_number_marks_later_watch_as_rewatch() -> None:
    assert _films_url_is_rewatch(
        "https://letterboxd.com/fluttershy/film/the-avengers-2012/2/"
    )
    assert not _films_url_is_rewatch(
        "https://letterboxd.com/fluttershy/film/the-avengers-2012/"
    )
    assert _films_url_is_rewatch(
        "https://letterboxd.com/fluttershy/film/the-avengers-2012/1/"
    )

    film = _films_parse_diary_row(
        {
            "title": "The Avengers (2012)",
            "film_url": "/fluttershy/film/the-avengers-2012/2/",
            "rewatch": False,
        }
    )
    assert film.rewatch is True

    marked = _films_parse_diary_row(
        {
            "title": "Thor: Ragnarok (2017)",
            "film_url": "/fluttershy/film/thor-ragnarok/1/",
            "rewatch": "Rewatch: Yes",
        }
    )
    assert marked.rewatch is True

    ant_man = _films_parse_diary_row(
        {
            "title": "Ant-Man (2015)",
            "film_url": "https://letterboxd.com/fluttershy/film/ant-man/1/",
        }
    )
    assert ant_man.rewatch is True


def test_first_diary_url_does_not_mark_spider_man_as_rewatch() -> None:
    film = _films_parse_diary_row(
        {
            "title": "Spider-Man: Brand New Day (2026)",
            "film_url": (
                "https://letterboxd.com/fluttershy/film/spider-man-brand-new-day/"
            ),
        }
    )

    assert film.rewatch is False


def test_generic_rewatch_icon_marker_does_not_mark_entry_as_rewatch() -> None:
    film = _films_parse_diary_row(
        {
            "title": "Spider-Man: Brand New Day (2026)",
            "film_url": (
                "https://letterboxd.com/fluttershy/film/spider-man-brand-new-day/"
            ),
            "rewatch": "icon-rewatch",
        }
    )

    assert film.rewatch is False


def test_rewatch_control_labels_do_not_mark_entry_as_rewatch() -> None:
    for marker in ("Rewatch", "↻"):
        film = _films_parse_diary_row(
            {
                "title": "Spider-Man: Brand New Day (2026)",
                "film_url": (
                    "https://letterboxd.com/fluttershy/film/spider-man-brand-new-day/"
                ),
                "rewatch": marker,
            }
        )

        assert film.rewatch is False


def test_films_footer_does_not_claim_omdb_when_only_letterboxd_data_exists() -> None:
    entry = _film(0)
    entry.poster_url = "https://a.ltrbxd.com/poster.jpg"
    source = _source([entry])

    text = "\n".join(
        getattr(item, "content", "")
        for item in source.format_page(0)
        if hasattr(item, "content")
    )

    assert "Letterboxd diary" in text
    assert "OMDb metadata" not in text

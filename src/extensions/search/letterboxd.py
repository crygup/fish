from __future__ import annotations

import asyncio
import datetime
import html
import math
import os
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image
from playwright.async_api import async_playwright

from core import Cog
from utils import to_thread
from utils.converters import LetterboxdConverter
from utils.paginator import LayoutPager
from utils.paths import FILES_ROOT
from utils.regexes import LBD_URL_RE

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

POSTER_HEIGHT = 300
MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 8_192
LBD_LOGO = "https://a.ltrbxd.com/logos/letterboxd-decal-dots-pos-rgb-500px.png"
COOKIE_FILE = FILES_ROOT / "cookies" / "letterboxd-cookies.txt"
_POSTER_HOSTS = {"a.ltrbxd.com", "s.ltrbxd.com"}
_PROFILE_READER = "https://r.jina.ai/https://letterboxd.com/"
FILMS_PAGE_SIZE = 50
FILMS_CACHE_SECONDS = 24 * 60 * 60


def _films_diary_suffix(page_number: int) -> str:
    """Return Letterboxd's public diary path for a one-based page number."""
    return "/diary/" if page_number <= 1 else f"/diary/films/page/{page_number}/"


@dataclass(slots=True)
class _DiaryFilm:
    """The small amount of diary data needed to render one film page."""

    title: str
    film_url: str
    diary_url: str | None = None
    watched_date: str | None = None
    rewatch: bool = False
    review_url: str | None = None
    year: str | None = None
    rating: str | None = None
    poster_url: str | None = None
    omdb: dict[str, Any] | None = None
    review_text: str | None = None
    omdb_loaded: bool = False
    review_loaded: bool = False


@dataclass(slots=True)
class _FilmsCache:
    """Cached Letterboxd pages, with one Discord page per diary entry."""

    username: str
    total: int
    page_size: int = FILMS_PAGE_SIZE
    pages: dict[int, list[_DiaryFilm]] = field(default_factory=dict)
    expires_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    @property
    def page_count(self) -> int:
        """Number of one-film pages exposed by the Discord paginator."""
        return max(0, self.total)

    @property
    def diary_page_count(self) -> int:
        """Number of Letterboxd pages needed to hold the cached diary."""
        return max(1, math.ceil(self.total / self.page_size)) if self.total else 0


def _films_page_for_index(index: int, page_size: int = FILMS_PAGE_SIZE) -> int:
    """Return the one-based Letterboxd diary page containing a zero-based film."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    return max(0, index) // page_size + 1


def _films_url_is_rewatch(value: object) -> bool:
    """Recognize Letterboxd's numbered film links for rewatch diary entries.

    Some diary renderings do not expose the rewatch cell or its icon.  In
    those renderings Letterboxd appends a positive diary occurrence number to
    the film URL (for example ``/film/foo/1/``).  An unnumbered film URL is
    still the normal first-watch form.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    path = urlparse(value.strip()).path.rstrip("/")
    match = re.search(r"/film/[^/]+/(\d+)$", path, re.IGNORECASE)
    if match is None:
        return False
    try:
        return int(match.group(1)) >= 1
    except ValueError:
        return False


def _films_parse_diary_row(data: Mapping[str, object]) -> _DiaryFilm:
    """Build a diary entry from normalized values extracted by Playwright."""
    title = str(data.get("title") or "Untitled film").strip() or "Untitled film"
    film_url = str(data.get("film_url") or "").strip()
    watched_date = str(data.get("watched_date") or "").strip() or None
    # The diary table often renders only ``Apr 2025`` in the calendar cell.
    # Prefer its day-link (``/diary/for/YYYY/MM/DD/``) whenever the text is not
    # a complete date so we do not silently lose the watched date.
    if watched_date is not None and _films_date_timestamp(watched_date) is None:
        watched_date = None
    if watched_date is None:
        date_href = str(data.get("date_href") or "")
        date_match = re.search(
            r"/(?:for/)?(\d{4})/(\d{1,2})/(\d{1,2})/?(?:$|[?#])",
            date_href,
        )
        if date_match:
            watched_date = "-".join(date_match.groups())
    rewatch_value = data.get("rewatch")
    rewatch_text = str(rewatch_value or "").strip().casefold()
    rewatch = (
        rewatch_value
        if isinstance(rewatch_value, bool)
        else bool(
            rewatch_text in {"1", "true", "yes", "y", "rewatched"}
            or re.search(
                r"rewatch\s*[:\-]?\s*(?:1|true|yes)\b",
                rewatch_text,
            )
            or re.search(
                r"(?:seen|watched)\s+(?:this\s+)?(?:film|movie)\s+before",
                rewatch_text,
            )
        )
    ) or _films_url_is_rewatch(film_url)
    return _DiaryFilm(
        title=title,
        film_url=film_url,
        diary_url=str(data.get("diary_url") or "").strip() or None,
        watched_date=watched_date,
        rewatch=rewatch,
        review_url=str(data.get("review_url") or "").strip() or None,
        year=str(data.get("year") or "").strip() or None,
        rating=str(data.get("rating") or "").strip() or None,
        poster_url=str(data.get("poster_url") or "").strip() or None,
    )


def _films_omdb_title(title: str) -> str:
    """Normalize Letterboxd display text before sending it to OMDb."""
    value = html.unescape(str(title or "")).strip()
    # The text mirror can leave markdown formatting around a film link.  OMDb
    # should receive only the visible title, never the URL or emphasis marks.
    value = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", value)
    value = re.sub(r"[*_~`]", "", value).strip()
    # Letterboxd occasionally includes the release year in the display title,
    # while OMDb accepts that year separately through the ``y`` parameter.
    value = re.sub(r"\s*\((?:18|19|20)\d{2}\)\s*$", "", value).strip()
    return value or str(title or "Untitled film").strip()


def _movie_text(value: object, limit: int = 1_500) -> str:
    """Escape OMDb text before putting it into a Components V2 view."""
    text = html.unescape(str(value or "")).strip()
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _movie_safe_url(value: object) -> str | None:
    """Only use ordinary HTTP(S) URLs supplied by OMDb."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _movie_available(value: object) -> str | None:
    text = html.unescape(str(value or "")).strip()
    return text if text and text.casefold() != "n/a" else None


def _movie_runtime(value: object) -> str | None:
    """Add an hours/minutes representation to OMDb's minute runtime."""
    runtime = _movie_available(value)
    if runtime is None:
        return None
    match = re.fullmatch(r"(\d+)\s*(?:minutes?|mins?|m)", runtime, re.IGNORECASE)
    if match is None:
        return runtime
    total_minutes = int(match.group(1))
    hours, minutes = divmod(total_minutes, 60)
    return f"{total_minutes} min ({hours}h{minutes}m)"


def _movie_release_timestamp(value: object) -> int | None:
    """Convert OMDb's release date into a Discord timestamp."""
    release_date = _movie_available(value)
    if not release_date:
        return None
    for date_format in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            parsed = datetime.datetime.strptime(release_date, date_format)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=datetime.timezone.utc).timestamp())
    return None


def _films_date_timestamp(value: object) -> int | None:
    """Parse diary dates while accepting both ISO and Letterboxd text dates."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        for date_format in (
            "%Y-%m-%d",
            "%d %b %Y",
            "%d %B %Y",
            "%B %d, %Y",
            "%b %d, %Y",
        ):
            try:
                parsed = datetime.datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


async def _read_remote_image(response: aiohttp.ClientResponse) -> bytes | None:
    if response.content_length and response.content_length > MAX_REMOTE_IMAGE_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_REMOTE_IMAGE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class Letterboxd(Cog):
    emoji = discord.PartialEmoji(name="\U0001f3ac")
    BASE = "https://letterboxd.com"
    _letterboxd_films_cache: dict[str, _FilmsCache]

    def _films_caches(self) -> dict[str, _FilmsCache]:
        """Create the in-process diary cache lazily for the combined Search cog."""
        caches = getattr(self, "_letterboxd_films_cache", None)
        if caches is None:
            caches = {}
            self._letterboxd_films_cache = caches
        return caches

    @staticmethod
    async def _resolve_letterboxd_username(ctx: Context, value: object) -> str:
        if isinstance(value, str):
            value = value.strip()
            if match := LBD_URL_RE.fullmatch(value.rstrip("/")):
                value = match.group(1)
        return await LetterboxdConverter().convert(ctx, cast(Any, value))

    @commands.hybrid_group(
        name="films",
        aliases=("movies",),
        fallback="list",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        username="Letterboxd username, profile URL, or Discord user."
    )
    async def films(
        self,
        ctx: Context,
        username: str = commands.param(
            default=commands.Author,
            description="Letterboxd username, profile URL, or Discord user",
        ),
    ) -> None:
        """Show a user's diary one film at a time."""
        async with ctx.typing():
            resolved = await self._resolve_letterboxd_username(ctx, username)
            cache = await self._films_cache_for(ctx, resolved)
            source = FilmsPageSource(self, ctx, cache)
            if not await source.prepare_page(0):
                raise commands.BadArgument(
                    f"No diary entries were found for **{resolved}**."
                )
            view = LayoutPager(
                source,
                ctx=ctx,
                accent_color=self.bot.embedcolor,
                timeout=600,
            )
            await view.start()

    @films.command(name="update", aliases=("refresh",))
    @commands.cooldown(1, 30 * 60, commands.BucketType.user)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        username="Letterboxd username, profile URL, or Discord user to refresh."
    )
    async def films_update(
        self,
        ctx: Context,
        username: str = commands.param(
            default=commands.Author,
            description="Letterboxd username, profile URL, or Discord user",
        ),
    ) -> None:
        """Clear a user's cached diary. This is limited to once per 30 minutes."""
        resolved = await self._resolve_letterboxd_username(ctx, username)
        self._films_caches().pop(resolved.casefold(), None)
        await ctx.send(
            f"Cleared the cached Letterboxd diary for **{resolved}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _films_cache_for(self, ctx: Context, username: str) -> _FilmsCache:
        key = username.casefold()
        now = datetime.datetime.now(datetime.timezone.utc)
        cache = self._films_caches().get(key)
        if cache is not None and cache.expires_at > now:
            return cache

        _total, entries = await self._scrape_diary_page(ctx, username, 1)
        if not entries:
            raise commands.BadArgument(
                f"No diary entries were found for **{username}**."
            )
        # Keep this command focused on the latest page.  Letterboxd can expose
        # a large page count in its navigation, but those older pages are not
        # needed for the command and should not inflate the paginator total.
        entries = entries[:FILMS_PAGE_SIZE]
        cache = _FilmsCache(
            username=username,
            total=len(entries),
            pages={1: entries},
            expires_at=now + datetime.timedelta(seconds=FILMS_CACHE_SECONDS),
        )
        self._films_caches()[key] = cache
        return cache

    async def _films_prepare_page(
        self, ctx: Context, cache: _FilmsCache, page_number: int
    ) -> bool:
        """Load one diary page and the requested film's remote metadata."""
        diary_page = _films_page_for_index(page_number, cache.page_size)
        if diary_page != 1 or diary_page not in cache.pages:
            return False
        entries = cache.pages[diary_page]
        if not entries:
            return False

        offset = page_number % cache.page_size
        if offset >= len(entries):
            return False
        entry = entries[offset]
        if not entry.omdb_loaded:
            entry.omdb = await self._films_fetch_omdb(entry)
            entry.omdb_loaded = True
        if entry.review_url and not entry.review_loaded:
            entry.review_text = await self._films_fetch_review(entry.review_url)
            entry.review_loaded = True
        return True

    async def _scrape_diary_page(
        self,
        ctx: Context,
        username: str,
        page_number: int,
        *,
        _url: str | None = None,
        _allow_route_fallback: bool = True,
    ) -> tuple[int, list[_DiaryFilm]]:
        """Read one diary page, falling back to the text mirror on a challenge."""
        suffix = _films_diary_suffix(page_number)
        url = _url or f"{self.BASE}/{username}{suffix}"
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=("--disable-blink-features=AutomationControlled",),
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                if os.path.isfile(COOKIE_FILE):
                    await self._load_cookies(context, COOKIE_FILE)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_selector("body", timeout=15_000)
                body_text = await page.locator("body").inner_text()
                pagination_text: list[str] = []
                pagination_pages: list[int] = []
                for selector in (
                    ".pagination",
                    ".paginate-pages",
                    "nav.pagination",
                ):
                    pagination_text.extend(
                        await page.locator(selector).all_inner_texts()
                    )
                for href in await page.locator(
                    'a[href*="/diary/films/page/"], '
                    'a[href*="/diary/page/"], '
                    'a[href*="/films/diary/page/"]'
                ).evaluate_all(
                    "links => links.map(link => link.getAttribute('href') || '')"
                ):
                    match = re.search(
                        r"/(?:diary/films|diary|films/diary)/page/(\d+)/",
                        str(href),
                    )
                    if match:
                        pagination_pages.append(int(match.group(1)))
                if pagination_text:
                    body_text += "\n" + "\n".join(pagination_text)
                if self._letterboxd_challenge(body_text, await page.title()):
                    reader_result = await self._scrape_diary_reader_routes(
                        ctx.session, username, page_number
                    )
                    if (
                        reader_result[1]
                        or page_number <= 1
                        or not _allow_route_fallback
                    ):
                        return reader_result
                    alternate_url = (
                        f"{self.BASE}/{username}/films/diary/page/{page_number}/"
                    )
                    if alternate_url != url:
                        return await self._scrape_diary_page(
                            ctx,
                            username,
                            page_number,
                            _url=alternate_url,
                            _allow_route_fallback=False,
                        )
                    return reader_result
                if await page.query_selector(".error-message"):
                    raise commands.BadArgument(
                        f"No Letterboxd user found for **{username}**."
                    )
                rows = await page.locator(
                    "tr.diary-entry-row, li.diary-entry-row, div.diary-entry-row, "
                    "table.diary-table tbody tr, table.diary-table tr"
                ).all()
                entries: list[_DiaryFilm] = []
                for row in rows:
                    raw = await row.evaluate("""
                        row => {
                          const links = [...row.querySelectorAll('a[href]')];
                          const film = links.find(a => {
                            const href = a.getAttribute('href') || '';
                            return href.includes('/film/') && !href.includes('/diary/');
                          });
                          const diary = links.find(a => {
                            const href = a.getAttribute('href') || '';
                            return href.includes('/diary/');
                          });
                          const time = row.querySelector('time');
                          const dateCell = row.querySelector(
                            '.td-calendar, .td-day, [class*="date"]'
                          );
                          const dateLink = row.querySelector(
                            '.daydate, .td-calendar a[href*="/for/"], a[href*="/diary/for/"]'
                          );
                          const ratingNode = row.querySelector(
                            '.td-rating, .rating, [data-rating], [class*="rating"]'
                          );
                          const ratingIcon = row.querySelector(
                            '.td-rating svg[aria-label], .td-rating [aria-label], svg[aria-label]'
                          );
                          const poster = row.querySelector(
                            'img[src], img[data-src], img[data-original], source[srcset]'
                          );
                          const posterSrcset = poster?.getAttribute('srcset') ||
                            poster?.getAttribute('data-srcset') || '';
                          let posterUrl =
                            poster?.getAttribute('data-src') ||
                            poster?.getAttribute('data-original') ||
                            poster?.getAttribute('src') ||
                            posterSrcset.split(',').pop()?.trim().split(/\\s+/)[0] || '';
                          if (posterUrl.includes('empty-poster')) posterUrl = '';
                          const posterComponent = row.querySelector(
                            'div.react-component[data-postered-identifier], [data-postered-identifier]'
                          );
                          const posterIdentifier =
                            posterComponent?.getAttribute('data-postered-identifier') || '';
                          const posterFilmId =
                            (posterIdentifier.match(/\\"uid\\":\\"film:(\\d+)\\"/) || [])[1] || '';
                          const posterSlug =
                            posterComponent?.getAttribute('data-item-slug') || '';
                          if (!posterUrl && posterFilmId && posterSlug) {
                            const posterPath = posterFilmId.split('').join('/');
                            const posterName = posterSlug.replace(/-\\d{4}$/, '-');
                            posterUrl = `https://a.ltrbxd.com/resized/film-poster/${posterPath}/${posterFilmId}-${posterName}-0-300-0-450-crop.jpg`;
                          }
                          const review = links.find(a => {
                            const href = a.getAttribute('href') || '';
                            return href.includes('/diary/entry/') ||
                                   href.includes('/review/');
                          });
                          const rewatchCell = row.querySelector(
                            'td.col-rewatch.js-td-rewatch, ' +
                            '.col-rewatch.js-td-rewatch, .td-rewatch'
                          );
                          // The icon is meaningful only inside Letterboxd's
                          // dedicated rewatch column.  Generic controls can
                          // also use the ``icon-rewatch`` class elsewhere in
                          // the row, so do not treat those as a positive mark.
                          const rewatchCellIcon = !!rewatchCell?.querySelector(
                            '.icon-rewatch'
                          );
                          const rewatchNodes = Array.from(row.querySelectorAll(
                            '[data-rewatch], ' +
                            '[aria-label*="rewatch" i], [title*="rewatch" i], ' +
                            '[aria-label*="seen this" i], [title*="seen this" i]'
                          )).filter(node => node !== rewatchCell);
                          const rewatchValues = [
                            row.getAttribute('data-rewatch'),
                            rewatchCell?.getAttribute('data-rewatch'),
                            ...rewatchNodes.map(node =>
                              node.getAttribute('data-rewatch')
                            )
                          ];
                          const rewatchStateNodes = [
                            ...(rewatchCell ? Array.from(rewatchCell.querySelectorAll(
                              'input[type="checkbox"], [role="checkbox"], ' +
                              '[aria-checked], [aria-pressed], [data-rewatch], ' +
                              '[data-checked], [data-selected]'
                            )) : []),
                            ...rewatchNodes.filter(node =>
                              ['data-rewatch', 'aria-checked', 'aria-pressed',
                               'data-checked', 'data-selected'].some(attribute =>
                                node.hasAttribute(attribute)
                              )
                            )
                          ];
                          const rewatchActive = rewatchStateNodes.some(node => {
                            const states = [
                              node.getAttribute('aria-checked'),
                              node.getAttribute('aria-pressed'),
                              node.getAttribute('data-checked'),
                              node.getAttribute('data-selected')
                            ].filter(Boolean);
                            return node.checked === true ||
                              states.some(state =>
                                /^(?:true|1|yes|active|selected|checked)$/i.test(state)
                              );
                          });
                          const rewatchMarker = rewatchNodes.find(node => {
                            const classes = typeof node.className === 'string'
                              ? node.className
                              : '';
                            const label = [
                              node.getAttribute('aria-label'),
                              node.getAttribute('title'),
                              node.getAttribute('alt'),
                              node.textContent
                            ].filter(Boolean).join(' ');
                            return /^(?:1|true|yes)$/i.test(
                              node.getAttribute('data-rewatch') || ''
                            ) || /(?:^|\\s)(?:rewatched|(?:is|has|selected)[-_]?rewatch)(?:$|\\s)/i.test(
                              classes
                            ) || /(?:rewatched|seen this|watched this)\\s*(?:film|movie)?/i.test(
                              label
                            );
                          });
                          const rewatchText = (
                            [
                              rewatchCell?.textContent || '',
                              ...rewatchNodes.map(node => node.textContent || '')
                            ].join(' ')
                          ).replace(/\\s+/g, ' ').trim();
                          const rewatch = rewatchCellIcon || rewatchValues.some(value =>
                            /^(?:1|true|yes)$/i.test(value || '')
                          ) || rewatchActive || !!rewatchMarker ||
                            /^(?:1|true|yes)$/i.test(rewatchText) ||
                            /\\b(?:rewatched|rewatch\\s*[:\\-]?\\s*(?:yes|true)|(?:seen|watched)\\s+(?:this\\s+)?(?:film|movie)\\s+before)\\b/i.test(
                              rewatchText
                            );
                          const href = film?.getAttribute('href') || '';
                          const releaseLink = links.find(a => {
                            const value = a.getAttribute('href') || '';
                            return /\\/(?:films?\\/year|year)\\/\\d{4}\\/?/.test(value);
                          });
                          const releaseText = releaseLink?.textContent || '';
                          const year =
                            (releaseText.match(/\\b(?:18|19|20)\\d{2}\\b/) || [])[0] ||
                            (href.match(/-(\\d{4})\\/?$/) || [])[1] || '';
                          const rating = (
                            ratingIcon?.getAttribute('aria-label') ||
                            ratingNode?.getAttribute('aria-label') ||
                            ratingNode?.getAttribute('title') ||
                            ratingNode?.getAttribute('data-rating') ||
                            ratingNode?.textContent || ''
                          ).replace(/\\s+/g, ' ').trim();
                          return {
                            title: (
                              film?.getAttribute('data-film-name') ||
                              film?.textContent || ''
                            ).trim(),
                            film_url: href,
                            diary_url: diary?.getAttribute('href') || '',
                            date_href: dateLink?.getAttribute('href') || '',
                            watched_date: time?.getAttribute('datetime') ||
                              (dateCell?.textContent || '').trim(),
                            review_url: review?.getAttribute('href') || '',
                            rewatch,
                            year,
                            rating,
                            poster_url: posterUrl,
                          };
                        }
                        """)
                    if not isinstance(raw, dict):
                        continue
                    data = dict(raw)
                    film_url = str(data.get("film_url") or "")
                    if film_url.startswith("/"):
                        data["film_url"] = urljoin(self.BASE, film_url)
                    diary_url = str(data.get("diary_url") or "")
                    if diary_url.startswith("/"):
                        data["diary_url"] = urljoin(self.BASE, diary_url)
                    review_url = str(data.get("review_url") or "")
                    if review_url.startswith("/"):
                        data["review_url"] = urljoin(self.BASE, review_url)
                    poster_url = str(data.get("poster_url") or "")
                    if poster_url.startswith("//"):
                        data["poster_url"] = f"https:{poster_url}"
                    elif poster_url.startswith("/"):
                        data["poster_url"] = urljoin(self.BASE, poster_url)
                    entry = _films_parse_diary_row(data)
                    if entry.film_url and entry.title:
                        entries.append(entry)
                total = self._diary_total(
                    body_text,
                    page_number,
                    len(entries),
                    max(pagination_pages, default=0),
                )
                if not entries and page_number > 1 and _allow_route_fallback:
                    # Letterboxd has served both ``/diary/films/page/N/`` and
                    # ``/films/diary/page/N/`` over time.  Try the alternate
                    # public route before treating a page as the end of a
                    # diary.  This is especially important for entry 51,
                    # which must load the first row from diary page 2.
                    alternate_url = (
                        f"{self.BASE}/{username}/films/diary/page/{page_number}/"
                    )
                    if alternate_url != url:
                        return await self._scrape_diary_page(
                            ctx,
                            username,
                            page_number,
                            _url=alternate_url,
                            _allow_route_fallback=False,
                        )
                if not entries and page_number > 1:
                    # Some responses omit the row classes above or redirect
                    # the paginated route to a challenge page.  Jina exposes a
                    # stable markdown rendering, so use it as the final
                    # page-specific fallback before reporting no entry.
                    reader_result = await self._scrape_diary_reader_routes(
                        ctx.session, username, page_number
                    )
                    if reader_result[1]:
                        return reader_result
                return total, entries
            except commands.BadArgument:
                raise
            except Exception as error:
                self.bot.logger.info(
                    "Letterboxd diary page %s unavailable for %s: %s",
                    page_number,
                    username,
                    error,
                )
                return await self._scrape_diary_reader_routes(
                    ctx.session, username, page_number
                )
            finally:
                if browser is not None:
                    await browser.close()

    @staticmethod
    def _letterboxd_challenge(body: str, title: str) -> bool:
        marker = f"{title}\n{body}".casefold()
        return any(
            text in marker
            for text in (
                "just a moment",
                "cf-chl-",
                "checking your browser",
                "enable javascript and cookies",
            )
        )

    @staticmethod
    def _extract_diary_total(body: str) -> int | None:
        """Extract an explicitly labelled Diary count from profile text."""
        values: list[int] = []
        # The direct profile request is HTML while the Jina fallback is
        # markdown.  Normalize tags as a second pass so ``<a>Diary</a><b>455``
        # is handled the same way as ``Diary\\n455``.
        bodies = (body, html.unescape(re.sub(r"<[^>]+>", "\\n", body)))
        for pattern in (
            r"(?:^|\n)\s*diary\s*\n\s*([\d,]+)\b",
            r"\bdiary\s*[:\-]?\s*([\d,]+)\b",
            r"\b([\d,]+)\s+diary\s+entries?\b",
        ):
            for candidate_body in bodies:
                values.extend(
                    int(value.replace(",", ""))
                    for value in re.findall(pattern, candidate_body, re.IGNORECASE)
                    if value.replace(",", "").isdigit()
                )
        return max(values) if values else None

    @staticmethod
    def _diary_total(
        body: str,
        page_number: int,
        row_count: int,
        page_count: int = 0,
    ) -> int:
        """Extract the diary's total from pagination or its summary text."""
        diary_total = Letterboxd._extract_diary_total(body)
        # ``Showing 1-50 of 123`` is an explicit result count.  Do not use a
        # bare ``of N`` match because ``Page 1 of 12`` describes pages, not
        # diary entries.
        showing_matches = re.findall(
            r"\b(?:showing|displaying)\s+[\d,]+\s*"
            r"(?:[-–—]\s*[\d,]+)?\s+of\s+([\d,]+)\b",
            body,
            re.IGNORECASE,
        )
        explicit_totals = [
            int(value.replace(",", ""))
            for value in showing_matches
            if value.replace(",", "").isdigit()
        ]
        if diary_total is not None:
            total = diary_total
        elif explicit_totals:
            total = max(explicit_totals)
        else:
            total = row_count
            if not page_count:
                page_counts = [
                    int(value)
                    for value in re.findall(
                        r"\bpage\s+\d+\s+of\s+(\d+)\b",
                        body,
                        re.IGNORECASE,
                    )
                    if value.isdigit()
                ]
                page_count = max(page_counts, default=0)
        # A page can expose only a navigation count such as ``Page 1 of 3``.
        # Treat a small ``of`` value as a page count when the DOM also gave us
        # the final page link, while preserving an explicit total such as
        # ``Showing 1-50 of 123 films``.
        if diary_total is None and not explicit_totals and page_count:
            total = page_count * FILMS_PAGE_SIZE
        if page_number > 1:
            total = max(total, (page_number - 1) * FILMS_PAGE_SIZE + row_count)
        return max(total, row_count)

    async def _scrape_diary_reader_routes(
        self, session: aiohttp.ClientSession, username: str, page_number: int
    ) -> tuple[int, list[_DiaryFilm]]:
        """Try both public diary URL layouts through the text mirror."""
        result = await self._scrape_diary_reader(session, username, page_number)
        if result[1] or page_number <= 1:
            return result
        return await self._scrape_diary_reader(
            session,
            username,
            page_number,
            _suffix=f"/films/diary/page/{page_number}/",
        )

    async def _scrape_diary_reader(
        self,
        session: aiohttp.ClientSession,
        username: str,
        page_number: int,
        *,
        _suffix: str | None = None,
    ) -> tuple[int, list[_DiaryFilm]]:
        """Use Jina's read-only rendering when Letterboxd serves a challenge."""
        suffix = (_suffix or _films_diary_suffix(page_number)).lstrip("/")
        reader_url = f"https://r.jina.ai/http://letterboxd.com/{username}/{suffix}"
        try:
            async with session.get(
                reader_url,
                headers={"User-Agent": "Fishie Letterboxd diary lookup"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    return 0, []
                body = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            return 0, []

        entries: list[_DiaryFilm] = []
        seen: set[str] = set()
        matches = list(
            re.finditer(
                r"\[([^\]]+)\]\((https?://(?:www\.)?letterboxd\.com/film/[^)]+)\)",
                body,
                re.IGNORECASE,
            )
        )
        for index, match in enumerate(matches):
            title = html.unescape(match.group(1)).strip()
            film_url = match.group(2).strip()
            if film_url in seen or not title:
                continue
            seen.add(film_url)
            next_start = (
                matches[index + 1].start() if index + 1 < len(matches) else len(body)
            )
            segment = body[match.start() : next_start]
            before = body[max(0, match.start() - 500) : match.start()]
            rating_match = re.findall(r"[★☆]{1,5}½?", segment)
            release_match = re.search(r"\b(?:18|19|20)\d{2}\b", segment)
            poster_matches = re.findall(
                r"!\[[^\]]*\]\((https?://[^)]+)\)", before, re.IGNORECASE
            )
            review_match = re.search(
                r"\[[^\]]*(?:review|reviewed)[^\]]*\]\((https?://[^)]+)\)",
                segment,
                re.IGNORECASE,
            )
            entries.append(
                _films_parse_diary_row(
                    {
                        "title": title,
                        "film_url": film_url,
                        "year": release_match.group(0) if release_match else "",
                        "rating": rating_match[-1] if rating_match else "",
                        "poster_url": poster_matches[-1] if poster_matches else "",
                        "review_url": review_match.group(1) if review_match else "",
                        "rewatch": bool(
                            re.search(
                                r"\b(?:rewatched|rewatch\s*[:\-]?\s*(?:yes|true)|(?:seen|watched)\s+(?:this\s+)?(?:film|movie)\s+before)\b",
                                segment,
                                re.I,
                            )
                        ),
                    }
                )
            )
            if len(entries) >= FILMS_PAGE_SIZE:
                break
        total = self._diary_total(body, page_number, len(entries))
        return total, entries

    async def _films_fetch_omdb(self, entry: _DiaryFilm) -> dict[str, Any] | None:
        keys = self.bot.config["keys"].get("opendb", [])
        if isinstance(keys, str):
            keys = [keys]
        keys = [str(key).strip() for key in keys if str(key).strip()]
        if not keys:
            return None
        params: dict[str, str] = {
            "apikey": random.choice(keys),
            "plot": "full",
            "r": "json",
            "type": "movie",
            "t": _films_omdb_title(entry.title)[:255],
        }
        if entry.year and entry.year.isdigit():
            params["y"] = entry.year
        try:
            async with self.bot.session.get(
                "https://www.omdbapi.com/",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None
                value = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return (
            value
            if isinstance(value, dict) and value.get("Response") == "True"
            else None
        )

    async def _films_fetch_review(self, review_url: str) -> str | None:
        parsed = urlparse(review_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "letterboxd.com",
            "www.letterboxd.com",
        }:
            return None
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=("--disable-blink-features=AutomationControlled",),
                )
                context = await browser.new_context()
                if os.path.isfile(COOKIE_FILE):
                    await self._load_cookies(context, COOKIE_FILE)
                page = await context.new_page()
                await page.goto(
                    review_url, wait_until="domcontentloaded", timeout=20_000
                )
                for selector in (
                    ".js-review-body",
                    ".review .body-text",
                    ".body-text",
                    "[class*='review-body']",
                ):
                    element = await page.query_selector(selector)
                    if element:
                        text = (await element.inner_text()).strip()
                        if text:
                            return _movie_text(text, 2_500)
            except Exception as error:
                self.bot.logger.debug("Letterboxd review fetch failed: %s", error)
            finally:
                if browser is not None:
                    await browser.close()
        return None

    @commands.hybrid_command(name="letterboxd", aliases=("lbxd",))
    @app_commands.describe(
        username="Letterboxd username, profile URL, or Discord user."
    )
    async def letterboxd(
        self,
        ctx: Context,
        username: str = commands.param(
            default=commands.Author,
            description="A Letterboxd username, profile URL, or Discord user",
        ),
    ):
        """Show a Letterboxd profile with favourites and recent films."""
        if isinstance(username, str):
            if m := LBD_URL_RE.fullmatch(username.strip().rstrip("/")):
                username = m.group(1)
        converter = LetterboxdConverter()
        username = await converter.convert(ctx, username)
        if m := LBD_URL_RE.match(username):
            username = m.group(1)
        url = f"{self.BASE}/{username}/"
        async with ctx.typing():
            data = await self._scrape_profile(username, url, ctx.session)
            if not data:
                return
            fav_urls, recent_urls, av, stats, dn, bio = data
            embed = self._make_embed(ctx, dn or username, av, url, stats, bio)
            fav_file = None
            if fav_urls:
                fav_imgs = await self._download_posters(ctx.session, fav_urls)
                if fav_imgs:
                    fav_buf = await self._stitch_row(fav_imgs)
                    fav_file = discord.File(fav_buf, filename="favs.png")
                    embed.set_image(url="attachment://favs.png")
            view = ToggleView(
                self, ctx, username, url, dn, av, stats, bio, fav_file, recent_urls
            )
            send_kwargs: dict[str, Any] = {"embed": embed, "view": view}
            if fav_file is not None:
                send_kwargs["file"] = fav_file
            view.message = await ctx.send(**send_kwargs)

    @commands.hybrid_command(name="movie")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Movie title, year, IMDb ID, or IMDb URL.")
    async def movie(self, ctx: Context, *, query: str):
        """Look up a movie through OMDb."""
        await self._omdb_lookup(ctx, query, "movie")

    @commands.hybrid_command(name="show", aliases=("tv", "series"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="TV show title, year, IMDb ID, or IMDb URL.")
    async def show(self, ctx: Context, *, query: str):
        """Look up a TV show through OMDb."""
        await self._omdb_lookup(ctx, query, "series")

    async def _omdb_lookup(self, ctx: Context, query: str, media_type: str) -> None:
        query = query.strip()
        if not query:
            label = "TV show" if media_type == "series" else "movie"
            raise commands.BadArgument(f"Provide a {label} title to look up.")

        keys = self.bot.config["keys"].get("opendb", [])
        if isinstance(keys, str):
            keys = [keys]
        keys = [str(key).strip() for key in keys if str(key).strip()]
        if not keys:
            raise commands.CommandError("OMDb is not configured.")

        params = {
            "apikey": random.choice(keys),
            "plot": "full",
            "r": "json",
            "type": media_type,
        }
        imdb_match = re.search(r"imdb\.com/title/(tt\d+)", query, re.IGNORECASE)
        if imdb_match:
            params["i"] = imdb_match.group(1)
        elif re.fullmatch(r"tt\d+", query, re.IGNORECASE):
            params["i"] = query
        else:
            params["t"] = query[:255]

        try:
            async with ctx.typing():
                async with self.bot.session.get(
                    "https://www.omdbapi.com/",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        raise commands.BadArgument(
                            "OMDb could not process that lookup."
                        )
                    movie_data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise commands.BadArgument("OMDb could not process that lookup.") from exc

        if not isinstance(movie_data, dict) or movie_data.get("Response") != "True":
            error = _movie_available(
                movie_data.get("Error") if isinstance(movie_data, dict) else None
            )
            raise commands.BadArgument(
                _movie_text(error, 300)
                if error
                else "No result was found for that query."
            )

        view = self._movie_view(movie_data)
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    def _movie_view(self, movie_data: dict[str, Any]) -> discord.ui.LayoutView:
        title = _movie_text(
            _movie_available(movie_data.get("Title")) or "Unknown movie", 180
        )
        title_display = f"## {title}"

        plot = _movie_text(
            _movie_available(movie_data.get("Plot"))
            or "No plot description available.",
            1_800,
        )
        poster = _movie_safe_url(movie_data.get("Poster"))
        children: list[discord.ui.Item[Any]] = []
        if poster:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_display),
                    discord.ui.TextDisplay(plot),
                    accessory=discord.ui.Thumbnail(poster),
                )
            )
        else:
            children.extend(
                (
                    discord.ui.TextDisplay(title_display),
                    discord.ui.TextDisplay(plot),
                )
            )
        children.append(discord.ui.Separator())

        details: list[str] = []
        for label, key in (
            ("Rated", "Rated"),
            ("Runtime", "Runtime"),
            ("Genre", "Genre"),
            ("Director", "Director"),
            ("Writer", "Writer"),
            ("Actors", "Actors"),
            ("Language", "Language"),
            ("Production", "Production"),
            ("Box office", "BoxOffice"),
        ):
            value = _movie_available(movie_data.get(key))
            if value:
                if key == "Runtime":
                    value = _movie_runtime(value)
                details.append(f"**{label}:** {_movie_text(value, 800)}")
        if str(movie_data.get("Type", "")).casefold() == "series":
            seasons = _movie_available(movie_data.get("totalSeasons"))
            if seasons:
                details.append(f"**Seasons:** {_movie_text(seasons, 80)}")

        rating_values: list[str] = []
        ratings = movie_data.get("Ratings")
        if isinstance(ratings, list):
            for rating in ratings:
                if not isinstance(rating, dict):
                    continue
                source = _movie_text(_movie_available(rating.get("Source")), 120)
                value = _movie_text(_movie_available(rating.get("Value")), 120)
                if source and value:
                    if source.casefold() == "internet movie database":
                        source = "IMDb"
                    rating_values.append(f"{source}: {value}")
        if rating_values:
            details.append(f"**Ratings:** {' · '.join(rating_values)}")

        children.append(
            discord.ui.TextDisplay(
                "\n".join(details)[:3_500] or "No additional details were provided."
            )
        )
        imdb_id = movie_data.get("imdbID")
        release_timestamp = _movie_release_timestamp(movie_data.get("Released"))
        buttons: list[discord.ui.Button] = []
        if isinstance(imdb_id, str) and re.fullmatch(r"tt\d+", imdb_id):
            buttons.append(
                discord.ui.Button(
                    label="IMDb",
                    style=discord.ButtonStyle.link,
                    url=f"https://www.imdb.com/title/{imdb_id}/",
                )
            )
            buttons.append(
                discord.ui.Button(
                    label="Letterboxd",
                    style=discord.ButtonStyle.link,
                    url=f"https://letterboxd.com/imdb/{imdb_id}",
                )
            )
        footer_parts = [
            (
                f"IMDb ID: {_movie_text(imdb_id, 40)}"
                if imdb_id
                else "IMDb ID: Unavailable"
            )
        ]
        if release_timestamp is not None:
            footer_parts.append(f"Released: <t:{release_timestamp}:D>")
        footer_parts.append("Data from OMDb")
        children.append(discord.ui.TextDisplay(f"-# {' · '.join(footer_parts)}"))

        container = discord.ui.Container(*children, accent_color=self.bot.embedcolor)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        if buttons:
            view.add_item(discord.ui.ActionRow(*buttons))
        return view

    async def _scrape_profile(self, username, url, session):
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                if os.path.isfile(COOKIE_FILE):
                    await self._load_cookies(context, COOKIE_FILE)
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_selector("section#favourites", timeout=15000)
                    try:
                        await page.wait_for_function(
                            """() => Array.from(
                                document.querySelectorAll('section#favourites li.griditem img')
                            ).every(img => !img.src.includes('empty-poster'))""",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                except Exception as error:
                    # Letterboxd currently protects the profile home route
                    # with a Cloudflare challenge. Public child pages such as
                    # /films/ remain available, so use those pages for a
                    # read-only fallback instead of failing the command.
                    self.bot.logger.info(
                        "Letterboxd profile home unavailable for %s; using public child pages: %s",
                        username,
                        error,
                    )
                    return await self._scrape_profile_fallback(page, username, session)
                if await page.query_selector(".error-message"):
                    raise commands.BadArgument(
                        f"No Letterboxd user found for **{username}**."
                    )
                fav = await self._scrape_posters(page, "section#favourites")
                recent = await self._scrape_posters(page, "section#recent-activity")
                av = await self._scrape_avatar(page)
                stats = await self._scrape_stats(page)
                dn = await self._scrape_display_name(page)
                bio = await self._scrape_bio(page)
                return fav, recent, av, stats, dn, bio
            except commands.BadArgument:
                raise
            except Exception as error:
                self.bot.logger.exception("Letterboxd scrape failed for %s", username)
                raise commands.BadArgument(
                    f"Could not load profile for **{username}**."
                ) from error
            finally:
                if browser is not None:
                    await browser.close()

    async def _scrape_profile_fallback(self, page, username, session):
        """Read profile data from Letterboxd pages that bypass the home challenge."""
        reader_profile = await self._scrape_reader_profile(session, username)
        if reader_profile is not None:
            fav_urls, reader_recent, avatar, stats, display_name, bio = reader_profile
            recent = await self._scrape_rss_posters(session, username)
            if not recent:
                recent = reader_recent
            if "followers" not in stats or "following" not in stats:
                stats.update(await self._scrape_network_stats(session, username))
            return fav_urls[:4], recent[:4], avatar, stats, display_name, bio

        films_url = f"{self.BASE}/{username}/films/"
        try:
            await page.goto(films_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_selector("div.poster-grid", timeout=15000)
        except Exception as error:
            self.bot.logger.debug(
                "Letterboxd fallback navigation failed for %s: %s", username, error
            )
            raise commands.BadArgument(
                f"Could not load profile for **{username}**."
            ) from error
        if await page.query_selector(".error-message"):
            raise commands.BadArgument(f"No Letterboxd user found for **{username}**.")

        avatar = await self._scrape_avatar(page)
        display_name = await self._scrape_display_name(page)
        stats = await self._scrape_stats(page)
        fallback_recent = await self._scrape_posters(page, "div.poster-grid")
        # Letterboxd's four profile favourites are only exposed on the profile
        # home page (or through its official API). The films page is a watched
        # list, so liked or recent posters must not be presented as favourites.
        favourites: list[str] = []
        recent = await self._scrape_rss_posters(session, username)
        if not recent:
            recent = fallback_recent
        stats.update(await self._scrape_network_stats(session, username))
        bio = await self._scrape_profile_bio(session, username)
        return favourites, recent[:4], avatar, stats, display_name, bio

    async def _scrape_reader_profile(self, session, username):
        """Read public profile data through the read-only Jina page reader.

        Letterboxd sometimes protects only the profile home route with an edge
        challenge while leaving child pages available. The reader is used only
        as a fallback and returns the rendered public profile, including the
        four favourites that are not present on the films page.
        """
        try:
            async with session.get(
                f"{_PROFILE_READER}{username}/",
                headers={"User-Agent": "Fishie Letterboxd lookup"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    return None
                body = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            return None

        if "## Favorite films" not in body or "Just a moment" in body:
            return None

        def _section(start_marker, end_marker):
            start = body.find(start_marker)
            if start < 0:
                return ""
            start += len(start_marker)
            end = body.find(end_marker, start) if end_marker else -1
            return body[start:] if end < 0 else body[start:end]

        def _posters(section):
            urls = []
            for match in re.finditer(
                r"!\[[^]]*?Poster[^]]*\]\((https?://[^)]+)\)",
                section,
                re.IGNORECASE,
            ):
                candidate = html.unescape(match.group(1)).strip()
                parsed = urlparse(candidate)
                if parsed.hostname in _POSTER_HOSTS and candidate not in urls:
                    urls.append(candidate)
                if len(urls) >= 4:
                    break
            return urls

        stats = {}
        stat_matches = list(re.finditer(r"^#### \[([^]]+)\]\(", body, re.MULTILINE))
        for match in stat_matches:
            value = match.group(1)
            number = re.match(r"([\d,]+)\s+(.+)", value)
            if not number:
                continue
            label = number.group(2).strip().casefold()
            if label in {"films", "this year", "lists", "following", "followers"}:
                stats[label] = number.group(1)

        avatar_match = re.search(r"!\[[^]]*\]\((https?://[^)]+)\)", body, re.IGNORECASE)
        avatar = avatar_match.group(1) if avatar_match else None
        name_match = re.search(r"^Title:\s*(.+?)[’']s profile\s*$", body, re.MULTILINE)
        display_name = name_match.group(1).strip() if name_match else None

        stats_end = 0
        if stat_matches:
            line_end = body.find("\n", stat_matches[-1].end())
            stats_end = len(body) if line_end < 0 else line_end + 1
        bio_section = body[stats_end : body.find("*   [Profile]", stats_end)]
        bio_lines = []
        for line in bio_section.splitlines():
            line = line.strip()
            if not line or line.startswith("["):
                continue
            if line.startswith("!"):
                continue
            bio_lines.append(line)
        bio = "\n".join(bio_lines).strip()
        favourites = _posters(_section("## Favorite films", "## [Recent activity]"))
        recent = _posters(_section("## [Recent activity]", "## [Recent reviews]"))
        return favourites, recent, avatar, stats, display_name, bio

    async def _scrape_rss_posters(self, session, username):
        """Read recent poster URLs from Letterboxd's public RSS feed."""
        try:
            async with session.get(
                f"{self.BASE}/{username}/rss/",
                headers={"User-Agent": "Fishie Letterboxd lookup"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    return []
                body = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            return []

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []

        urls: list[str] = []
        for item in root.findall(".//item"):
            description = item.findtext("description") or ""
            match = re.search(
                r"<img[^>]+src=[\"']([^\"']+)",
                html.unescape(description),
                re.IGNORECASE,
            )
            if not match:
                continue
            candidate = html.unescape(match.group(1)).strip()
            parsed = urlparse(candidate)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in _POSTER_HOSTS
                and candidate not in urls
            ):
                urls.append(candidate)
            if len(urls) >= 4:
                break
        return urls

    async def _scrape_reviews_profile(self, username, url):
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                if os.path.isfile(COOKIE_FILE):
                    await self._load_cookies(context, COOKIE_FILE)
                page = await context.new_page()
                # The profile home route is currently challenged by
                # Letterboxd's edge protection. The reviews child page is
                # public and contains the same review sections.
                await page.goto(
                    f"{self.BASE}/{username}/reviews/",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await page.wait_for_selector("body", timeout=15000)
                if await page.query_selector(".error-message"):
                    raise commands.BadArgument(
                        f"No Letterboxd user found for **{username}**."
                    )
                return await self._scrape_reviews(page)
            except commands.BadArgument:
                raise
            except Exception as error:
                self.bot.logger.exception(
                    "Letterboxd review scrape failed for %s", username
                )
                raise commands.BadArgument(
                    f"Could not load reviews for **{username}**."
                ) from error
            finally:
                if browser is not None:
                    await browser.close()

    async def _scrape_posters(self, page, selector):
        section = await page.query_selector(selector)
        if not section:
            return []
        urls = []
        for li in (await section.query_selector_all("li.griditem"))[:4]:
            urls.append(await self._scrape_poster_item(li))
        return urls

    async def _scrape_network_stats(self, session, username):
        """Read public follower/following counts from Letterboxd network pages."""

        async def _fetch(kind):
            for host in (self.BASE, "https://embed.letterboxd.com"):
                try:
                    async with session.get(
                        f"{host}/{username}/{kind}/",
                        headers={"User-Agent": "Fishie Letterboxd lookup"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as response:
                        if response.status != 200:
                            continue
                        body = await response.text()
                except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
                    continue

                for anchor in re.findall(r"<a\b[^>]*>", body, re.IGNORECASE):
                    href_match = re.search(
                        r"\bhref=[\"']([^\"']+)[\"']", anchor, re.IGNORECASE
                    )
                    title_match = re.search(
                        r"\btitle=[\"']([^\"']+)[\"']", anchor, re.IGNORECASE
                    )
                    if not href_match or not title_match:
                        continue
                    path = href_match.group(1).split("?", 1)[0].strip("/").split("/")
                    if len(path) != 2 or path[0].casefold() != username.casefold():
                        continue
                    if path[1].casefold() != kind:
                        continue
                    count = re.search(
                        r"(\d[\d,]*)\s*(?:people|followers|following)?",
                        html.unescape(title_match.group(1)),
                        re.IGNORECASE,
                    )
                    if count:
                        return count.group(1)
                break
            return None

        following, followers = await asyncio.gather(
            _fetch("following"), _fetch("followers")
        )
        result = {}
        if followers is not None:
            result["followers"] = followers
        if following is not None:
            result["following"] = following
        return result

    async def _scrape_profile_bio(self, session, username):
        """Attempt to read the profile bio from public profile markup."""
        for host in (self.BASE, "https://embed.letterboxd.com"):
            try:
                async with session.get(
                    f"{host}/{username}/",
                    headers={"User-Agent": "Fishie Letterboxd lookup"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        continue
                    body = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
                continue

            for match in re.finditer(
                r"<(?:div|p)\b[^>]*class=[\"'][^\"']*"
                r"(?:js-bio-content|profile-bio|bio)[^\"']*[\"'][^>]*>"
                r"(.*?)</(?:div|p)>",
                body,
                re.IGNORECASE | re.DOTALL,
            ):
                value = re.sub(r"<br\s*/?>", "\n", match.group(1), flags=re.I)
                value = re.sub(r"<[^>]+>", "", value)
                value = html.unescape(value).strip()
                if value:
                    return value
        return ""

    async def _scrape_poster_item(self, item):
        img = await item.query_selector("img")
        if img:
            ss = await img.get_attribute("srcset")
            if ss:
                candidate = ss.split(",")[-1].strip().split()[0]
                if candidate:
                    return candidate
            source = await img.get_attribute("src") or ""
            if source and "empty-poster" not in source:
                return source
        component = await item.query_selector("div.react-component")
        if component:
            identifier = await component.get_attribute("data-postered-identifier")
            slug_value = await component.get_attribute("data-item-slug")
            film_id = None
            slug = str(slug_value) if slug_value else None
            if identifier:
                match = re.search(r'"uid":"film:(\d+)"', str(identifier))
                if match:
                    film_id = match.group(1)
            if film_id and slug:
                slug = re.sub(r"-\d{4}$", "-", slug)
                path = "/".join(film_id)
                return (
                    "https://a.ltrbxd.com/resized/film-poster/"
                    f"{path}/{film_id}-{slug}-0-300-0-450-crop.jpg"
                )
        return ""

    async def _scrape_avatar(self, page):
        img = await page.query_selector(".profile-avatar img, .profile-mini-person img")
        return await img.get_attribute("src") if img else None

    async def _scrape_display_name(self, page):
        for selector in (
            ".person-display-name .label",
            ".profile-mini-person .title-3 a",
        ):
            el = await page.query_selector(selector)
            if el:
                try:
                    return (await el.inner_text()).strip()
                except Exception:
                    pass
        return None

    async def _scrape_bio(self, page):
        for selector in (
            ".bio .js-bio-content",
            ".js-bio-content",
            ".profile-bio",
            "[data-bio]",
        ):
            el = await page.query_selector(selector)
            if not el:
                continue
            try:
                value = (await el.inner_text()).strip()
            except Exception:
                value = ""
            if not value:
                value = (await el.get_attribute("data-bio") or "").strip()
            if value:
                return value
        return ""

    async def _scrape_stats(self, page):
        r = {}
        for el in await page.query_selector_all(".profile-stats .statistic a"):
            ve = await el.query_selector(".value")
            de = await el.query_selector(".definition")
            if ve and de:
                try:
                    r[(await de.inner_text()).strip().lower()] = (
                        await ve.inner_text()
                    ).strip()
                except Exception:
                    pass
        if not r:
            heading = await page.query_selector("h1.section-heading .tooltip")
            if heading:
                title = (
                    await heading.get_attribute("title")
                    or await heading.get_attribute("data-original-title")
                    or ""
                )
                match = re.search(r"([\d,]+)\s+films?", title, re.IGNORECASE)
                if match:
                    r["films"] = match.group(1)
        for section in await page.query_selector_all("aside .section"):
            heading = await section.query_selector("h3.section-heading")
            if not heading:
                continue
            label = (await heading.inner_text()).strip().casefold()
            if label not in {"following", "followers"}:
                continue
            count = await section.query_selector("a.all-link")
            if count:
                value = (await count.inner_text()).strip()
                if value:
                    r[label] = value
        for link in await page.query_selector_all(
            'a[href$="/following/"][title], a[href$="/followers/"][title]'
        ):
            href = await link.get_attribute("href") or ""
            label = href.rstrip("/").rsplit("/", 1)[-1].casefold()
            if label not in {"following", "followers"}:
                continue
            title = html.unescape(await link.get_attribute("title") or "")
            match = re.search(r"(\d[\d,]*)", title)
            if match:
                r[label] = match.group(1)
        return r

    async def _scrape_reviews(self, page):
        recent = []
        popular = []
        for sec in await page.query_selector_all("section"):
            h2 = await sec.query_selector("h2")
            if not h2:
                continue
            try:
                t = ((await h2.inner_text()) or "").lower()
            except Exception:
                continue
            if "recent review" in t:
                target = recent
            elif "popular review" in t:
                target = popular
            else:
                continue
            for art in await sec.query_selector_all("article"):
                try:
                    title = ""
                    fu = ""
                    for sel in ("h2 a", ".primaryname a", "a[href*='/film/']"):
                        el = await art.query_selector(sel)
                        if el:
                            try:
                                title = (await el.inner_text()).strip()
                            except Exception:
                                pass
                            h = await el.get_attribute("href")
                            if h:
                                fu = (
                                    f"https://letterboxd.com{h}"
                                    if h.startswith("/")
                                    else h
                                )
                            break
                    rating = ""
                    svg = await art.query_selector("svg[aria-label]")
                    if svg:
                        rating = (await svg.get_attribute("aria-label") or "").strip()
                    txt = ""
                    for be in await art.query_selector_all(
                        ".body-text p, .js-review-body p"
                    ):
                        try:
                            t2 = (await be.inner_text()).strip()
                            if t2:
                                txt = t2
                                break
                        except Exception:
                            pass
                    ds = ""
                    te = await art.query_selector("time")
                    if te:
                        try:
                            ds = (await te.inner_text()).strip()
                        except Exception:
                            pass
                    if title:
                        target.append(_RE(title, fu, rating, txt, ds))
                except Exception:
                    pass
        return recent, popular

    @staticmethod
    async def _load_cookies(context, path):
        cookies = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = line.split("\t")
                if len(p) >= 7:
                    cookies.append(
                        dict(
                            name=p[5],
                            value=p[6],
                            domain=p[0],
                            path=p[2],
                            secure=p[3] == "TRUE",
                            httpOnly=False,
                        )
                    )
        if cookies:
            await context.add_cookies(cookies)

    @staticmethod
    def _make_embed(ctx, dn, av, url, stats, bio=""):
        e = discord.Embed(color=ctx.bot.embedcolor, url=url, description=bio or None)
        e.set_author(name=f"{dn}'s profile", url=url, icon_url=av or LBD_LOGO)
        _add_stats(e, stats)
        e.set_footer(text="Letterboxd", icon_url=LBD_LOGO)
        return e

    @staticmethod
    def _make_reviews_embed(ctx, dn, av, url, stats, bio, recent, popular):
        e = discord.Embed(color=ctx.bot.embedcolor, url=url, description=bio or None)
        e.set_author(name=f"{dn}'s Reviews", url=url, icon_url=av or LBD_LOGO)
        _add_stats(e, stats)
        if recent:
            lines = []
            for r in recent:
                s = f" {r.rating}" if r.rating else ""
                line = f"[**{r.title}**]({r.film_url}){s}"
                if r.text:
                    line += f"\n> {r.text[:150]}{'...' if len(r.text)>150 else ''}"
                lines.append(line)
            e.add_field(name="Recent Reviews", value="\n".join(lines), inline=False)
        if popular:
            lines = []
            for r in popular:
                s = f" {r.rating}" if r.rating else ""
                line = f"[**{r.title}**]({r.film_url}){s}"
                if r.text:
                    line += f"\n> {r.text[:150]}{'...' if len(r.text)>150 else ''}"
                lines.append(line)
            e.add_field(name="Popular Reviews", value="\n".join(lines), inline=False)
        e.set_footer(text="Letterboxd", icon_url=LBD_LOGO)
        return e

    @staticmethod
    def _decode_poster(data):
        with Image.open(BytesIO(data)) as source:
            if (
                source.width > MAX_IMAGE_DIMENSION
                or source.height > MAX_IMAGE_DIMENSION
                or source.width * source.height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("poster dimensions exceed the safe limit")
            image = source.convert("RGB")
            image.load()
            return image

    @staticmethod
    async def _download_posters(session, urls):
        async def _download(url):
            try:
                async with session.get(
                    url,
                    headers={"Referer": "https://letterboxd.com/"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        d = await _read_remote_image(resp)
                        if d is None:
                            return None
                        return await asyncio.to_thread(Letterboxd._decode_poster, d)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
            ):
                return None
            return None

        results = await asyncio.gather(*(_download(url) for url in urls))
        return [image for image in results if isinstance(image, Image.Image)]

    @staticmethod
    @to_thread
    def _stitch_row(images):
        h = POSTER_HEIGHT
        resized = []
        tw = 0
        for img in images:
            r = h / img.height
            w = int(img.width * r)
            resized.append(img.resize((w, h), Image.Resampling.LANCZOS))
            tw += w
        c = Image.new("RGB", (tw, h), (255, 255, 255))
        x = 0
        for img in resized:
            c.paste(img, (x, 0))
            x += img.width
        b = BytesIO()
        c.save(b, format="PNG")
        b.seek(0)
        return b


class FilmsPageSource:
    """Lazy Components V2 source for one Letterboxd diary film per page."""

    def __init__(self, cog: Letterboxd, ctx: Context, cache: _FilmsCache) -> None:
        self.cog = cog
        self.ctx = ctx
        self.cache = cache

    def get_max_pages(self) -> int:
        return self.cache.page_count

    async def prepare_page(self, page_number: int) -> bool:
        return await self.cog._films_prepare_page(
            self.ctx, self.cache, max(0, page_number)
        )

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        diary_page = _films_page_for_index(page_number, self.cache.page_size)
        entries = self.cache.pages.get(diary_page, [])
        offset = page_number % self.cache.page_size
        if offset >= len(entries):
            return [discord.ui.TextDisplay("That diary entry is not available.")]

        entry = entries[offset]
        safe_title = _movie_text(entry.title, 180)
        film_url = _movie_safe_url(entry.film_url)
        heading = f"## [{safe_title}]({film_url})" if film_url else f"## {safe_title}"
        details: list[str] = []
        if entry.rating:
            details.append(f"**Rating:** { _movie_text(entry.rating, 40) }")
        watched_timestamp = _films_date_timestamp(entry.watched_date)
        if watched_timestamp is not None:
            details.append(f"**Watched:** <t:{watched_timestamp}:D>")
        if entry.omdb:
            released = _movie_release_timestamp(entry.omdb.get("Released"))
            if released is not None:
                details.append(f"**Released:** <t:{released}:D>")
        if entry.year and not any(
            detail.startswith("**Released:**") for detail in details
        ):
            details.append(f"**Released:** {_movie_text(entry.year, 12)}")
        if entry.rewatch:
            details.append("**Rewatch:** Yes")
        if not details:
            details.append("No diary details were provided.")

        body = heading + "\n" + "\n".join(details)
        if entry.review_text:
            body += "\n\n**Review**\n" + _movie_text(entry.review_text, 2_400)

        poster = None
        if entry.omdb:
            poster = _movie_safe_url(entry.omdb.get("Poster"))
        if poster is None:
            poster = _movie_safe_url(entry.poster_url)
        items: list[discord.ui.Item[Any]] = []
        if poster:
            items.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(body[:3_900]),
                    accessory=discord.ui.Thumbnail(poster),
                )
            )
        else:
            items.append(discord.ui.TextDisplay(body[:3_900]))
        items.extend(
            (
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# Page {page_number + 1}/{self.get_max_pages()} · Letterboxd diary"
                ),
            )
        )
        return items


class _RE:
    __slots__ = ("title", "film_url", "rating", "text", "date")

    def __init__(self, t, fu, r, tx, d):
        self.title = t
        self.film_url = fu
        self.rating = r
        self.text = tx
        self.date = d


def _add_stats(embed, stats):
    p = []
    if "films" in stats:
        p.append(f"**{stats['films']}** total")
    if "this year" in stats:
        p.append(f"**{stats['this year']}** this year")
    if p:
        embed.add_field(name="Films", value="\n".join(p), inline=True)
    p = []
    if "followers" in stats:
        p.append(f"**{stats['followers']}** Followers")
    if "following" in stats:
        p.append(f"**{stats['following']}** Following")
    if p:
        embed.add_field(name="Network", value="\n".join(p), inline=True)


class ToggleView(discord.ui.View):
    message: discord.Message

    def __init__(
        self, cog, ctx, username, url, dn, av, stats, bio, fav_file, recent_urls
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.ctx = ctx
        self.username = username
        self.url = url
        self.dn = dn
        self.av = av
        self.stats = stats
        self.bio = bio
        self.recent_urls = recent_urls
        self._files = {}
        if fav_file is not None:
            self._files["favs"] = fav_file
        name = dn or username
        base = self._base_embed(name)
        self._fav_embed = base.copy()
        if fav_file is not None:
            self._fav_embed.set_image(url="attachment://favs.png")
        self._recent_embed = None
        self._reviews_embed = None
        self._update_buttons("favs")

    def _base_embed(self, name):
        return Letterboxd._make_embed(
            self.ctx, name, self.av, self.url, self.stats, self.bio
        )

    def _update_buttons(self, active):
        self.favs_btn.disabled = "favs" not in self._files or active == "favs"
        self.recent_btn.disabled = active == "recent"
        self.reviews_btn.disabled = active == "reviews"

    async def _load_recent(self, interaction):
        if self._recent_embed:
            return True
        if not self.recent_urls:
            await interaction.followup.send("No recent activity.", ephemeral=True)
            return False
        imgs = await Letterboxd._download_posters(
            self.ctx.bot.session, self.recent_urls
        )
        if not imgs:
            await interaction.followup.send(
                "Could not load recent posters.", ephemeral=True
            )
            return False
        buf = await Letterboxd._stitch_row(imgs)
        self._files["recent"] = discord.File(buf, filename="recent.png")
        self._recent_embed = (
            self._base_embed(self.dn or self.username)
            .copy()
            .set_image(url="attachment://recent.png")
        )
        return True

    async def _load_reviews(self, interaction):
        if self._reviews_embed:
            return True
        try:
            recent, popular = await self.cog._scrape_reviews_profile(
                self.username, self.url
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return False
        if not recent and not popular:
            self.reviews_btn.disabled = True
            self._update_buttons("favs")
            await interaction.followup.send("No reviews found.", ephemeral=True)
            return False
        self._reviews_embed = Letterboxd._make_reviews_embed(
            self.ctx,
            self.dn or self.username,
            self.av,
            self.url,
            self.stats,
            self.bio,
            recent,
            popular,
        )
        return True

    @discord.ui.button(label="Favorites", style=discord.ButtonStyle.blurple, row=0)
    async def favs_btn(self, interaction, button):
        await interaction.response.defer()
        f = self._files.get("favs")
        if f is None:
            return
        if hasattr(f.fp, "seek"):
            f.fp.seek(0)
        self._update_buttons("favs")
        await interaction.edit_original_response(
            embed=self._fav_embed, view=self, attachments=[f]
        )

    @discord.ui.button(label="Recent", style=discord.ButtonStyle.blurple, row=0)
    async def recent_btn(self, interaction, button):
        await interaction.response.defer()
        if not await self._load_recent(interaction):
            return
        f = self._files["recent"]
        if hasattr(f.fp, "seek"):
            f.fp.seek(0)
        self._update_buttons("recent")
        await interaction.edit_original_response(
            embed=self._recent_embed, view=self, attachments=[f]
        )

    @discord.ui.button(label="Reviews", style=discord.ButtonStyle.green, row=0)
    async def reviews_btn(self, interaction, button):
        await interaction.response.defer()
        if not await self._load_reviews(interaction):
            return
        self._update_buttons("reviews")
        await interaction.edit_original_response(
            embed=self._reviews_embed, view=self, attachments=[]
        )

    async def on_timeout(self):
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        message = getattr(self, "message", None)
        if message is not None:
            try:
                await message.edit(view=self)
            except discord.HTTPException:
                pass

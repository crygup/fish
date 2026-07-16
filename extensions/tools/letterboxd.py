from __future__ import annotations

import asyncio
import os
import re
from io import BytesIO
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image
from playwright.async_api import async_playwright

from core import Cog
from utils import to_thread
from utils.converters import LetterboxdConverter
from utils.regexes import LBD_URL_RE

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

POSTER_HEIGHT = 300
MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 8_192
LBD_LOGO = "https://a.ltrbxd.com/logos/letterboxd-decal-dots-pos-rgb-500px.png"
COOKIE_FILE = "files/cookies/letterboxd-cookies.txt"


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

    @commands.hybrid_command(name="letterboxd", aliases=("lbxd",))
    async def letterboxd(
        self,
        ctx: Context,
        username: str = commands.param(
            default=commands.Author,
            description="A Letterboxd username, profile URL, or Discord user",
        ),
    ):
        if isinstance(username, str):
            if m := LBD_URL_RE.fullmatch(username.strip().rstrip("/")):
                username = m.group(1)
        converter = LetterboxdConverter()
        username = await converter.convert(ctx, username)
        if m := LBD_URL_RE.match(username):
            username = m.group(1)
        url = f"{self.BASE}/{username}/"
        async with ctx.typing():
            data = await self._scrape_profile(username, url)
            if not data:
                return
            fav_urls, recent_urls, av, stats, dn, bio = data
            if not fav_urls:
                await ctx.send(f"**{username}** has no favourites on Letterboxd.")
                return
            fav_imgs = await self._download_posters(ctx.session, fav_urls)
            if not fav_imgs:
                await ctx.send("Could not load poster images.")
                return
            fav_buf = await self._stitch_row(fav_imgs)
            fav_file = discord.File(fav_buf, filename="favs.png")
            embed = self._make_embed(ctx, dn or username, av, url, stats, bio)
            embed.set_image(url="attachment://favs.png")
            view = ToggleView(
                self, ctx, username, url, dn, av, stats, bio, fav_file, recent_urls
            )
            view.message = await ctx.send(embed=embed, view=view, file=fav_file)

    async def _scrape_profile(self, username, url):
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
                    self.bot.logger.debug(
                        "Letterboxd profile navigation failed for %s: %s",
                        username,
                        error,
                    )
                    raise commands.BadArgument(
                        f"Could not load profile for **{username}**."
                    ) from error
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

    async def _scrape_reviews_profile(self, username, url):
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                if os.path.isfile(COOKIE_FILE):
                    await self._load_cookies(context, COOKIE_FILE)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
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
            img = await li.query_selector("img")
            if img:
                ss = await img.get_attribute("srcset")
                if ss:
                    urls.append(ss.split(",")[-1].strip().split()[0])
                    continue
                s = await img.get_attribute("src") or ""
                if s and "empty-poster" not in s:
                    urls.append(s)
                    continue
            rd = await li.query_selector("div.react-component")
            if rd:
                ident = await rd.get_attribute("data-postered-identifier")
                sv = await rd.get_attribute("data-item-slug")
                fid = None
                slug = str(sv) if sv else None
                if ident:
                    m = re.search(r'"uid":"film:(\d+)"', str(ident))
                    if m:
                        fid = m.group(1)
                if fid and slug:
                    us = re.sub(r"-\d{4}$", "-", slug)
                    d = "/".join(fid)
                    urls.append(
                        f"https://a.ltrbxd.com/resized/film-poster/{d}/{fid}-{us}-0-300-0-450-crop.jpg"
                    )
                    continue
            urls.append("")
        return urls

    async def _scrape_avatar(self, page):
        img = await page.query_selector(".profile-avatar img")
        return await img.get_attribute("src") if img else None

    async def _scrape_display_name(self, page):
        el = await page.query_selector(".person-display-name .label")
        if el:
            try:
                return (await el.inner_text()).strip()
            except Exception:
                return None
        return None

    async def _scrape_bio(self, page):
        el = await page.query_selector(".bio .js-bio-content p")
        if el:
            try:
                return (await el.inner_text()).strip()
            except Exception:
                pass
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
        self._files = {"favs": fav_file}
        name = dn or username
        base = self._base_embed(name)
        self._fav_embed = base.copy().set_image(url="attachment://favs.png")
        self._recent_embed = None
        self._reviews_embed = None
        self._update_buttons("favs")

    def _base_embed(self, name):
        return Letterboxd._make_embed(
            self.ctx, name, self.av, self.url, self.stats, self.bio
        )

    def _update_buttons(self, active):
        self.favs_btn.disabled = active == "favs"
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
        f = self._files["favs"]
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


async def setup(bot: Fishie):
    await bot.add_cog(Letterboxd())

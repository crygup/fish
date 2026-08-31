"""Raster profile-card rendering used by the default ``profile`` command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

import aiohttp
from discord.flags import PublicUserFlags
from PIL import Image, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from utils.rich_text import (
    draw_inline_tokens,
    inline_image_tokens,
    inline_image_url,
    resolve_inline_images,
    text_font,
    wrap_inline_text,
)

CARD_SIZE = (1050, 400)
CARD_RADIUS = 32
AVATAR_SIZE = 295
MAX_DESCRIPTION_LENGTH = 500
MAX_PROFILE_BADGES = 10
DISCORD_FLAG_KEYS = frozenset(PublicUserFlags.VALID_FLAGS)
# Profile cards currently use an intentionally neutral text colour.  The
# user's purchased profile colour is kept in ``ProfileCardData`` for future
# styling work, but must not tint the title or reputation text yet.
PROFILE_TEXT_COLOR = (255, 255, 255, 255)


def _is_discord_flag(entry: dict[str, Any]) -> bool:
    """Return whether an entry represents a native Discord user flag."""

    key = str(entry.get("_badge_key") or "")
    return key.startswith("flag:") and key[5:] in DISCORD_FLAG_KEYS


@dataclass(frozen=True, slots=True)
class ProfileCardData:
    """Values displayed on a generated profile card."""

    name: str
    title: str | None
    description: str | None
    badges: str
    rank: int
    xp: int
    reputation: int
    avatar: bytes
    accent: int
    badge_images: tuple[bytes, ...] = ()


def _safe_text(value: str | None, *, limit: int) -> str:
    """Drop control characters that PIL cannot render consistently."""

    text = "".join(
        character
        for character in str(value or "")
        if character in "\n\t" or ord(character) >= 32
    ).strip()
    return text[:limit]


def _first_frame_png(data: bytes) -> bytes | None:
    """Decode an emoji asset and return its first frame as a static PNG."""

    try:
        with Image.open(BytesIO(data)) as opened:
            opened.seek(0)
            frame = opened.convert("RGBA")
            output = BytesIO()
            frame.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, TypeError):
        return None


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    last = lines[-1]
    while last and draw.textlength(f"{last}…", font=font) > max_width:
        last = last[:-1].rstrip()
    lines[-1] = f"{last}…"
    return lines


def _rounded_avatar(data: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(data)) as opened:
            avatar = ImageOps.fit(
                opened.convert("RGBA"),
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
    except (UnidentifiedImageError, OSError, ValueError):
        avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (72, 64, 78, 255))

    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), radius=24, fill=255
    )
    avatar.putalpha(mask)
    return avatar


def _draw_shadow_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    *,
    font: Any,
    fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    stroke: tuple[int, int, int, int] = (27, 23, 32, 255),
    stroke_width: int = 1,
) -> None:
    x, y = position
    draw.text(
        (x + 3, y + 4),
        text,
        font=font,
        fill=(0, 0, 0, 255),
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 255),
    )
    draw.text(
        position,
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke,
    )


def _draw_shadow_right_text(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    text: str,
    *,
    font: Any,
    fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    stroke: tuple[int, int, int, int] = (27, 23, 32, 255),
) -> None:
    """Draw the shared text treatment while keeping the right edge inside."""

    bounds = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    text_width = int(bounds[2] - bounds[0])
    _draw_shadow_text(
        draw,
        (right - text_width, y),
        text,
        font=font,
        fill=fill,
        stroke=stroke,
        stroke_width=1,
    )


async def render_profile_card(session: Any, data: ProfileCardData) -> BytesIO:
    """Render a rounded, shadowed profile card as a PNG attachment."""

    width, height = CARD_SIZE

    image = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=CARD_RADIUS,
        fill=(47, 40, 55, 255),
    )

    # Keep a soft avatar shadow separate from the avatar mask so the edges
    # remain rounded even when Discord returns a transparent PNG/WebP.
    avatar = _rounded_avatar(data.avatar)
    shadow = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    shadow_mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        (0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), radius=24, fill=150
    )
    shadow.paste((0, 0, 0, 180), (52, 55), shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(11))
    image.alpha_composite(shadow)
    image.alpha_composite(avatar, (52, 48))

    content_x = 380
    content_width = 550
    title_font = text_font(data.name, 58)
    small_font = text_font(data.name, 25)
    body_font = text_font(data.description or "", 20)
    rank_font = text_font("Rank", 38)
    xp_font = text_font("XP", 20)
    rep_font = text_font("rep", 32)

    name = _safe_text(data.name, limit=64)
    # A long display name should never run into the rank block.
    name_lines = _wrap_text(draw, name, title_font, 525, 2)
    y = 46
    for line in name_lines:
        # Keep the display name free of an outline while retaining the same
        # opaque shadow used by the rest of the card.
        _draw_shadow_text(
            draw,
            (content_x, y),
            line,
            font=title_font,
            stroke_width=0,
        )
        y += 62

    title = _safe_text(data.title, limit=100)
    badges = _safe_text(data.badges, limit=300)
    if title:
        # Center the optional title beneath the username rather than leaving
        # it flush with the content column.  Use the widest rendered name
        # line so wrapped display names still produce a stable center point.
        name_width = max(
            (draw.textlength(line, font=title_font) for line in name_lines),
            default=0,
        )
        title_width = draw.textlength(title, font=small_font)
        title_x = content_x + max(0, round((name_width - title_width) / 2))
        _draw_shadow_text(
            draw,
            (title_x, y + 2),
            title,
            font=small_font,
            fill=PROFILE_TEXT_COLOR,
        )
        y += 36

    if data.badge_images:
        badge_size = 34
        badge_gap = 9
        badge_x = content_x + 4
        # Leave a small breathing gap below the username/title so the icons
        # do not visually touch the text above them.
        badge_y = y + 16
        right_edge = content_x + content_width
        for raw_badge in data.badge_images[:MAX_PROFILE_BADGES]:
            try:
                with Image.open(BytesIO(raw_badge)) as opened:
                    badge = ImageOps.contain(
                        opened.convert("RGBA"),
                        (badge_size, badge_size),
                        method=Image.Resampling.LANCZOS,
                    )
                icon = Image.new("RGBA", (badge_size, badge_size), (0, 0, 0, 0))
                icon.alpha_composite(
                    badge,
                    (
                        (badge_size - badge.width) // 2,
                        (badge_size - badge.height) // 2,
                    ),
                )
            except (UnidentifiedImageError, OSError, ValueError, TypeError):
                continue
            if badge_x + badge_size > right_edge:
                badge_x = content_x + 4
                badge_y += badge_size + 8
            image.alpha_composite(icon, (badge_x, badge_y))
            badge_x += badge_size + badge_gap
        y = badge_y + badge_size + 1
    elif badges:
        y += 16
        if session is None:
            assets = {}
        else:
            try:
                assets = await resolve_inline_images(session, [badges])
            except Exception:
                assets = {}
        badge_font = text_font(badges, 29)
        badge_lines = wrap_inline_text(
            badges,
            badge_font,
            29,
            assets,
            max(120, content_width - 25),
        )
        for line in badge_lines[:2]:
            # Never draw Discord markup such as ``<:badge:id>`` literally if
            # a CDN request fails.  A missing icon is preferable to exposing
            # implementation markup on the card.
            visible_line = [
                token
                for token in line
                if token.kind != "image" or token.value in assets
            ]
            if not visible_line:
                continue
            draw_inline_tokens(
                image,
                visible_line,
                (content_x + 4, y),
                font=badge_font,
                image_size=29,
                assets=assets,
                fill=(255, 255, 255, 255),
                stroke_width=1,
                stroke_fill=(27, 23, 32, 255),
            )
            y += 36

    description = _safe_text(data.description, limit=MAX_DESCRIPTION_LENGTH)
    if description:
        body_y = max(y + 28, 188)
        for line in _wrap_text(draw, description, body_font, content_width, 4):
            _draw_shadow_text(
                draw,
                (content_x + 4, body_y),
                line,
                font=body_font,
            )
            body_y += 31

    rank_text = f"Rank #{max(1, int(data.rank))}"
    rank_x = width - 35
    _draw_shadow_right_text(draw, rank_x, 34, rank_text, font=rank_font)
    _draw_shadow_right_text(
        draw, rank_x, 81, f"{max(0, int(data.xp)):,} XP", font=xp_font
    )
    _draw_shadow_right_text(
        draw,
        rank_x,
        height - 67,
        f"+{max(0, int(data.reputation)):,} rep",
        font=rep_font,
        fill=PROFILE_TEXT_COLOR,
    )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def inline_badge_markup(entries: list[dict[str, Any]]) -> str:
    """Return icon-only markup for profile badges except Discord flags."""

    icons: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        # Native Discord public flags (staff, partner, HypeSquad, and
        # similar) are intentionally omitted. Custom role/owner badges such
        # as Uploader, purchased badges, and stat-earned badges remain visible.
        if _is_discord_flag(entry):
            continue
        emoji_name = str(entry.get("emoji_name") or "").strip()
        if not emoji_name:
            continue
        emoji_id = entry.get("emoji_id")
        if entry.get("is_custom") and emoji_id:
            try:
                emoji_id = int(emoji_id)
            except (TypeError, ValueError):
                continue
            prefix = "a" if entry.get("animated") else ""
            token = f"<{prefix}:{emoji_name}:{emoji_id}>"
        elif not entry.get("is_custom"):
            token = emoji_name
        else:
            continue
        if token in seen:
            continue
        seen.add(token)
        icons.append(token)
        if len(icons) >= MAX_PROFILE_BADGES:
            break
    return " ".join(icons)


async def resolve_badge_images(
    session: Any,
    entries: list[dict[str, Any]],
    *,
    bot: Any = None,
    user_id: int | None = None,
) -> tuple[bytes, ...]:
    """Fetch badge icons in their display order.

    Custom emoji assets are first resolved through the normal CDN resolver;
    a cached Discord emoji is then tried as a fallback. Unicode badges use
    the same Twemoji source as other inline emoji rendering. A missing icon
    is skipped so one unavailable badge cannot break the profile card.
    """

    markup = inline_badge_markup(entries)
    if not markup:
        return ()

    logger = getattr(bot, "logger", None)
    tokens = inline_image_tokens(markup)
    log_prefix = "Profile badge assets"

    def log(level: str, message: str, *args: Any, **kwargs: Any) -> None:
        method = getattr(logger, level, None)
        if callable(method):
            method(message, *args, **kwargs)

    assets: dict[str, bytes] = {}
    if session is not None:
        try:
            assets.update(await resolve_inline_images(session, [markup]))
        except Exception:
            log(
                "warning",
                "%s normal resolver failed user_id=%s token_count=%d",
                log_prefix,
                user_id,
                len(tokens),
                exc_info=True,
            )

    async def fetch_trusted_asset(request_session: Any, token: str) -> None:
        """Fetch a known emoji CDN URL without public-peer revalidation.

        The URL is generated from an already validated custom emoji token or
        Unicode codepoint, and can only target Discord's CDN or Twemoji. The
        generic media fetcher deliberately rejects some proxy deployments;
        this bounded fallback keeps profile badges reliable without accepting
        arbitrary user URLs.
        """

        if token in assets:
            normalized = _first_frame_png(assets[token])
            if normalized is not None:
                assets[token] = normalized
                return
            # A stale inline-image cache entry can contain an HTTP error body
            # rather than an image. Drop it so the trusted static fallback is
            # attempted below.
            assets.pop(token, None)
        if request_session is None:
            return
        urls = [inline_image_url(token)]
        # Discord may report an emoji as animated while its current CDN
        # representation only accepts the static PNG route. Try that route
        # as a fallback so animated custom badges still have a usable icon.
        if token.startswith("<a:"):
            urls.append(urls[0].replace(".gif?", ".png?"))
        for url in urls:
            try:
                async with request_session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        continue
                    content_type = response.headers.get("Content-Type", "")
                    if content_type and not content_type.lower().startswith("image/"):
                        continue
                    data = await response.content.read(4 * 1024 * 1024 + 1)
            except Exception:
                continue
            if 0 < len(data) <= 4 * 1024 * 1024:
                normalized = _first_frame_png(data)
                if normalized is not None:
                    assets[token] = normalized
                return

    await asyncio.gather(*(fetch_trusted_asset(session, token) for token in tokens))

    missing_tokens = [token for token in tokens if token not in assets]
    if missing_tokens:
        # A long-lived bot session can retain a failed DNS/proxy connection.
        # Retry only the fixed, trusted emoji URLs with a fresh bounded
        # session; this path never accepts a user-provided URL.
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as fresh_session:
                await asyncio.gather(
                    *(
                        fetch_trusted_asset(fresh_session, token)
                        for token in missing_tokens
                    )
                )
        except Exception:
            log(
                "warning",
                "%s fresh resolver failed user_id=%s missing=%d",
                log_prefix,
                user_id,
                len(missing_tokens),
                exc_info=True,
            )

    async def read_cached(entry: dict[str, Any]) -> None:
        if bot is None or not entry.get("is_custom"):
            return
        emoji_id_value = entry.get("emoji_id")
        if emoji_id_value is None:
            return
        try:
            emoji_id = int(str(emoji_id_value))
        except (TypeError, ValueError):
            return
        token = f"{'a' if entry.get('animated') else ''}:{entry.get('emoji_name')}:{emoji_id}"
        token = f"<{token}>"
        if token in assets:
            return
        get_emoji = getattr(bot, "get_emoji", None)
        emoji = get_emoji(emoji_id) if callable(get_emoji) else None
        if emoji is None:
            return
        try:
            data = await asyncio.wait_for(cast(Any, emoji).url.read(), timeout=4)
        except Exception:
            return
        if isinstance(data, bytes):
            assets[token] = data

    await asyncio.gather(
        *(read_cached(entry) for entry in entries if not _is_discord_flag(entry))
    )
    # Cached Discord emoji objects are read after the HTTP attempts. Normalize
    # those too, so animated/custom assets always use their first frame.
    for token in tokens:
        raw = assets.get(token)
        if raw is None:
            continue
        normalized = _first_frame_png(raw)
        if normalized is None:
            assets.pop(token, None)
            log(
                "warning",
                "%s asset could not be decoded user_id=%s token=%s",
                log_prefix,
                user_id,
                token,
            )
        else:
            assets[token] = normalized
    unresolved = [token for token in tokens if token not in assets]
    log(
        "info" if not unresolved else "warning",
        "%s resolved user_id=%s requested=%d resolved=%d unresolved=%s",
        log_prefix,
        user_id,
        len(tokens),
        len(tokens) - len(unresolved),
        unresolved,
    )
    return tuple(assets[token] for token in tokens if token in assets)

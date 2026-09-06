"""Anime burger images and the owner-managed burger catalogue.

The burger collection is deliberately kept separate from the general image
catalogue.  It is a small, file-backed collection so adding an image does not
require a database migration or a deploy.  The checked-in catalogue acts as a
seed; production can point the writable catalogue and image directory at a
persistent volume with ``FISHIE_BURGERS_CATALOG_PATH`` and
``FISHIE_BURGERS_ROOT``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, cast
from urllib.parse import urlsplit

import discord
from discord.ext import commands
from PIL import Image, ImageOps

from utils.network import fetch_public_bytes, refresh_discord_attachment_url
from utils.paths import FILES_ROOT
from utils.vars import base_header

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


BURGER_SEED_ROOT = FILES_ROOT / "images" / "burgers"
BURGER_SEED_CATALOG = BURGER_SEED_ROOT / "catalog.json"

# ``src`` is mounted read-only in production.  This is the same data parent
# mounted by the production compose file, and is also a useful local fallback
# when an older deployment has not yet picked up the burger-specific
# environment variables.  Keep this runtime location separate from the
# checked-in seed: the seed is intentionally never a write target.
BURGER_RUNTIME_ROOT = Path(
    os.getenv(
        "FISHIE_BURGERS_RUNTIME_ROOT",
        str(FILES_ROOT.parents[1] / "data" / "burgers"),
    )
)
BURGER_RUNTIME_CATALOG = Path(
    os.getenv(
        "FISHIE_BURGERS_RUNTIME_CATALOG_PATH",
        str(FILES_ROOT.parents[1] / "data" / "burgers_catalog.json"),
    )
)

# Source files are read-only in the production container.  These optional
# paths let owner-managed additions live on the existing persistent data
# volume while retaining the bundled images as a fallback.  The runtime
# directory is the default even when the environment variables are absent;
# otherwise a stale deployment could silently select the read-only seed tree.
BURGER_ROOT = Path(os.getenv("FISHIE_BURGERS_ROOT", str(BURGER_RUNTIME_ROOT)))
BURGER_CATALOG = Path(
    os.getenv("FISHIE_BURGERS_CATALOG_PATH", str(BURGER_ROOT / "catalog.json"))
)

BURGER_MAX_BYTES = 50 * 1024 * 1024
BURGER_MAX_DIMENSION = 4_096

# A response from these CDNs can legitimately omit the transport peer after
# headers have been received.  They are still checked by
# ``validate_public_url`` before the request, so the exception is scoped to
# known public media hosts and never applies to arbitrary URLs.
BURGER_TRUSTED_CDN_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
        "i.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
        "pbs.twimg.com",
    }
)

_ACTIVE_BURGER_CATALOG: Path | None = None


@dataclass(frozen=True, slots=True)
class BurgerAsset:
    """One validated image in the burger catalogue."""

    id: int
    date_added: str
    path: Path
    source: str | None
    extra: dict[str, Any]


def _catalog_path() -> Path:
    """Return the writable catalogue path configured for this instance."""

    return BURGER_CATALOG


def _catalog_candidates() -> tuple[Path, ...]:
    """Return catalogue paths in the order in which they should be read.

    When no runtime path was configured, the bundled seed is the historical
    ``BURGER_CATALOG`` value.  Put the runtime catalogue first in that case so
    additions made by an older deployment remain visible after a restart.
    """

    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    if _ACTIVE_BURGER_CATALOG is not None:
        add(_ACTIVE_BURGER_CATALOG)
    if BURGER_CATALOG == BURGER_SEED_CATALOG:
        add(BURGER_RUNTIME_CATALOG)
        add(BURGER_CATALOG)
    else:
        add(BURGER_CATALOG)
        add(BURGER_RUNTIME_CATALOG)
        add(BURGER_SEED_CATALOG)
    if BURGER_SEED_CATALOG not in candidates:
        add(BURGER_SEED_CATALOG)
    return tuple(candidates)


def _catalog_document() -> tuple[Path, dict[str, Any]]:
    """Load the writable catalogue, falling back to the bundled seed."""

    for candidate in _catalog_candidates():
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("assets"), list):
            return candidate, payload
    return _catalog_path(), {"version": 1, "assets": []}


def _safe_asset_path(relative: object) -> Path | None:
    """Resolve a catalogue path without allowing traversal outside the roots."""

    value = str(relative or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return None

    roots: list[Path] = []
    for root in (BURGER_ROOT, BURGER_RUNTIME_ROOT, BURGER_SEED_ROOT):
        resolved = root.resolve()
        if resolved not in roots:
            roots.append(resolved)
    for root in roots:
        path = (root / candidate).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file() and path.suffix.casefold() == ".png":
            return path
    return None


def burger_catalog() -> tuple[BurgerAsset, ...]:
    """Return valid burger images sorted by their stable IDs."""

    _catalog_file, payload = _catalog_document()
    assets: list[BurgerAsset] = []
    seen_ids: set[int] = set()
    for raw in payload.get("assets", []):
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id")
        if not isinstance(raw_id, (int, str)) or isinstance(raw_id, bool):
            continue
        try:
            asset_id = int(raw_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if asset_id <= 0 or asset_id in seen_ids:
            continue
        path = _safe_asset_path(raw.get("path"))
        if path is None:
            continue
        seen_ids.add(asset_id)
        extra = raw.get("extra")
        assets.append(
            BurgerAsset(
                id=asset_id,
                date_added=str(raw.get("date_added") or ""),
                path=path,
                source=(str(raw["source"]).strip() if raw.get("source") else None),
                extra=dict(extra) if isinstance(extra, dict) else {},
            )
        )
    return tuple(sorted(assets, key=lambda item: item.id))


def _raw_assets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in payload.get("assets", []) if isinstance(item, dict)]


def _write_catalog(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a catalogue document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    # Include a random suffix so two bot processes writing the shared catalog
    # cannot clobber one another's temporary file when they happen to share a
    # PID namespace.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(
            {"version": int(payload.get("version") or 1), "assets": payload["assets"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_targets() -> tuple[tuple[Path, Path], ...]:
    """Return persistent ``(image_root, catalog)`` write locations.

    ``BURGER_SEED_ROOT``/``BURGER_SEED_CATALOG`` are deliberately excluded.
    They are bundled with the application and production mounts them under a
    read-only source tree.  If an explicitly configured target is unavailable
    we fall back to the persistent runtime target, then fail with a clear
    error; we never mutate the seed as a last resort.
    """

    candidates: list[tuple[Path, Path]] = []
    for root, catalog in (
        (BURGER_ROOT, BURGER_CATALOG),
        (BURGER_RUNTIME_ROOT, BURGER_RUNTIME_CATALOG),
    ):
        pair = (root, catalog)
        if pair in candidates:
            continue
        try:
            if root.resolve() == BURGER_SEED_ROOT.resolve():
                continue
            if catalog.resolve() == BURGER_SEED_CATALOG.resolve():
                continue
        except OSError:
            # A missing path still has a valid lexical path.  ``resolve`` may
            # fail only for an unusual filesystem race; leave selection to
            # the write probe below rather than treating it as the seed.
            pass
        if pair not in candidates:
            candidates.append(pair)
    return tuple(candidates)


def _select_write_target() -> tuple[Path, Path]:
    """Choose the first writable image/catalogue pair.

    The source tree is intentionally read-only in the production container.
    Probing a tiny file in the target directory catches both read-only mounts
    and host UID/GID mismatches before an image is downloaded and avoids the
    old generic failure after a successful fetch.
    """

    for root, catalog in _write_targets():
        probes: list[Path] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
            catalog.parent.mkdir(parents=True, exist_ok=True)
            for directory, label in (
                (root, "asset"),
                (catalog.parent, "catalog"),
            ):
                probe = directory / (
                    f".{catalog.name}.{label}.{secrets.token_hex(8)}.tmp"
                )
                probes.append(probe)
                probe.write_bytes(b"")
                probe.unlink(missing_ok=True)
            return root, catalog
        except OSError:
            for probe in probes:
                try:
                    # Cleanup can itself fail on a read-only mount (the
                    # original failure that caused this target to be skipped)
                    # so never let it prevent trying the next target.
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass
    raise commands.BadArgument(
        "The burger catalogue is not writable; configure a writable data directory."
    )


def _png_bytes(data: bytes) -> bytes:
    """Validate an image and return a normalized PNG representation."""

    with Image.open(BytesIO(data)) as image:
        image.verify()
    with Image.open(BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if image.width > BURGER_MAX_DIMENSION or image.height > BURGER_MAX_DIMENSION:
            image.thumbnail(
                (BURGER_MAX_DIMENSION, BURGER_MAX_DIMENSION), Image.Resampling.LANCZOS
            )
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            image = image.convert("RGBA")
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
    return output.getvalue()


_catalog_lock = asyncio.Lock()


async def add_burger_asset(
    bot: Fishie,
    image_url: str,
    source_url: str | None = None,
) -> BurgerAsset:
    """Download, normalize, and add one owner-managed burger image."""

    image_url = str(image_url).strip().strip("<>")
    if not image_url:
        raise commands.BadArgument("Provide an image URL.")
    image_url = await refresh_discord_attachment_url(bot, image_url)
    fetch_options = {
        "max_bytes": BURGER_MAX_BYTES,
        "allow_missing_peer_hosts": BURGER_TRUSTED_CDN_HOSTS,
        "headers": base_header,
    }
    try:
        fetched = await fetch_public_bytes(
            bot.session,
            image_url,
            allowed_content_prefixes=(
                "image/",
                "application/octet-stream",
                "binary/octet-stream",
            ),
            **fetch_options,
        )
    except commands.BadArgument as error:
        # Some image hosts return text/plain (or no MIME type) for a valid
        # image URL.  The bounded public fetch already completed the SSRF and
        # size checks; retry without a MIME gate and let Pillow be the final,
        # format-aware validator.  Do not retry HTTP/status or URL failures.
        if "supported media" not in str(error).casefold():
            raise
        fetched = await fetch_public_bytes(
            bot.session,
            image_url,
            allowed_content_prefixes=(),
            **fetch_options,
        )
    try:
        png = await asyncio.to_thread(_png_bytes, fetched.data)
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.UnidentifiedImageError,
    ) as error:
        raise commands.BadArgument("That URL did not return a valid image.") from error

    async with _catalog_lock:
        catalog_file, payload = _catalog_document()
        entries = _raw_assets(payload)
        ids = [int(item["id"]) for item in entries if str(item.get("id", "")).isdigit()]
        asset_id = max(ids, default=0) + 1
        filename = f"{secrets.token_hex(8)}.png"
        root, target = _select_write_target()
        path = root / filename
        try:
            await asyncio.to_thread(path.write_bytes, png)
            entry: dict[str, Any] = {
                "id": asset_id,
                "date_added": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "path": filename,
                "source": str(source_url).strip().strip("<>") if source_url else None,
                "extra": {},
            }
            entries.append(entry)
            payload["assets"] = entries
            await asyncio.to_thread(_write_catalog, target, payload)
            global _ACTIVE_BURGER_CATALOG
            _ACTIVE_BURGER_CATALOG = target
        except Exception:
            path.unlink(missing_ok=True)
            raise

    return BurgerAsset(
        id=asset_id,
        date_added=str(entry["date_added"]),
        path=path,
        source=entry.get("source"),
        extra={},
    )


async def remove_burger_asset(asset_id: int) -> bool:
    """Remove a burger entry and its managed file from the catalogue."""

    async with _catalog_lock:
        catalog_file, payload = _catalog_document()
        entries = _raw_assets(payload)
        selected = next(
            (entry for entry in entries if str(entry.get("id")) == str(int(asset_id))),
            None,
        )
        if selected is None:
            return False
        entries = [entry for entry in entries if entry is not selected]
        payload["assets"] = entries
        try:
            target = catalog_file
            # A seed catalogue can be readable but not writable.  In that
            # case write the edited document to the same runtime fallback used
            # by additions instead of failing after the database lookup.
            _root, fallback = _select_write_target()
            if target == BURGER_SEED_CATALOG:
                target = fallback
            await asyncio.to_thread(_write_catalog, target, payload)
            global _ACTIVE_BURGER_CATALOG
            _ACTIVE_BURGER_CATALOG = target
        except OSError as error:
            raise commands.BadArgument(
                "The burger catalogue is not writable; configure a writable data directory."
            ) from error
        # Never remove a bundled seed image when production is using a
        # separate writable directory; it may still be needed by the seed.
        path = _safe_asset_path(selected.get("path"))
        if path is not None and path.parent != BURGER_SEED_ROOT.resolve():
            path.unlink(missing_ok=True)
    return True


class BurgerPageView(discord.ui.LayoutView):
    """Components V2 image-only burger view with optional navigation."""

    def __init__(self, ctx: Context, assets: Iterable[BurgerAsset]) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.assets = tuple(assets)
        self.page = 0
        self.message: discord.Message | None = None
        self.previous = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary
        )
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.previous.callback = self._previous
        self.next.callback = self._next
        self._render()

    @property
    def asset(self) -> BurgerAsset:
        return self.assets[self.page]

    def _render(self) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.Container(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem("attachment://burger.png")
                ),
                accent_color=getattr(self.ctx, "embedcolor", None),
            )
        )
        if len(self.assets) > 1:
            self.previous.disabled = False
            self.next.disabled = False
            self.add_item(discord.ui.ActionRow(self.previous, self.next))

    def _file(self) -> discord.File:
        return discord.File(self.asset.path, filename="burger.png")

    async def start(self) -> None:
        self.message = await self.ctx.send(
            view=self,
            file=self._file(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this paginator can use its controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _set_page(self, interaction: discord.Interaction, page: int) -> None:
        if not self.assets:
            return
        self.page = page % len(self.assets)
        self._render()
        await interaction.response.defer()
        await interaction.edit_original_response(
            view=self,
            attachments=[self._file()],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._set_page(interaction, self.page - 1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._set_page(interaction, self.page + 1)

    async def on_timeout(self) -> None:
        self.previous.disabled = True
        self.next.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


class BurgerCommands:
    """Mixin for the random burger image commands."""

    bot: Fishie

    @cast(Any, commands.command)(name="burger", description="burger")
    async def burger(self, ctx: Context) -> None:
        """burger"""

        assets = burger_catalog()
        if not assets:
            raise commands.BadArgument("No burger images are available right now.")
        view = BurgerPageView(ctx, (random.choice(assets),))
        await view.start()

    @cast(Any, commands.command)(name="burgers", description="burger")
    async def burgers(self, ctx: Context) -> None:
        """burger"""

        assets = burger_catalog()
        if not assets:
            raise commands.BadArgument("No burger images are available right now.")
        await BurgerPageView(ctx, assets).start()


__all__ = [
    "BURGER_CATALOG",
    "BURGER_ROOT",
    "BURGER_RUNTIME_CATALOG",
    "BURGER_RUNTIME_ROOT",
    "BURGER_TRUSTED_CDN_HOSTS",
    "BurgerAsset",
    "BurgerCommands",
    "BurgerPageView",
    "add_burger_asset",
    "burger_catalog",
    "remove_burger_asset",
]

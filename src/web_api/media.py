"""Media helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import mimetypes
import re
from typing import Any

from discord.ext import commands
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)

from extensions.media_effects.audio_effects import (
    audio_effect_catalog as bundled_audio_effect_catalog,
)
from extensions.media_effects.audio_effects import (
    find_audio_effect,
)
from extensions.media_effects.commands import (
    PIPELINE_EFFECTS,
    _normalize_effect_options,
    _renderer_effect_options,
    refresh_discord_attachment_url,
)
from extensions.media_effects.processing import (
    render_combine_effect,
    render_image_effect,
    render_overlay_effect,
    render_text_effect,
    render_video_effect,
)
from utils.network import (
    fetch_public_bytes,
)
from utils.rich_text import resolve_inline_images

from . import state as api_state

router = APIRouter()


async def _read_bounded_request(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_media_api_key(supplied: str | None) -> None:
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    expected = str(
        api_state.bot_ref.config.get("keys", {}).get("media_api", "")
    ).strip()
    if not expected:
        raise HTTPException(503, "Media API is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            401,
            "Invalid media API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def _media_api_options(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        if len(value) > 8_192:
            raise HTTPException(400, "Effect options are too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise HTTPException(400, "Effect options must be valid JSON") from error
    if not isinstance(value, dict) or len(value) > 20:
        raise HTTPException(400, "Effect options must be a JSON object")
    options: dict[str, Any] = {}
    for key, option in value.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
            or not isinstance(option, (str, int, float, bool))
            or isinstance(option, float)
            and not math.isfinite(option)
            or isinstance(option, str)
            and len(option) > 256
        ):
            raise HTTPException(400, "Effect options contain an invalid value")
        options[key] = option
    return options


@router.get("/media/effects")
async def media_effect_catalog():
    """List the effects accepted by the authenticated media endpoint."""
    return {
        "effects": sorted(api_state.MEDIA_API_EFFECTS),
        "max_bytes": api_state.MAX_MEDIA_API_BYTES,
        "authentication": "X-API-Key",
        "documentation": "docs/media-effects-api.md in the Fish repository",
    }


@router.get("/media/audio-effects")
async def media_audio_effect_catalog(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """List the bundled audio clips accepted by the sound-effect processor."""
    _require_media_api_key(x_api_key)
    return {
        "effects": [
            {
                "id": effect.id,
                "name": effect.name,
                "display_name": effect.display_name,
                "category": effect.category,
                "duration": effect.duration,
            }
            for effect in bundled_audio_effect_catalog()
        ]
    }


@router.post("/media/effects/{effect}")
async def apply_media_effect_api(
    effect: str,
    request: Request,
    media_url: str | None = Query(None, max_length=2_048),
    secondary_media_url: str | None = Query(None, max_length=2_048),
    options: str = Query("{}"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """Apply one media effect to a public URL or a raw uploaded file."""
    _require_media_api_key(x_api_key)
    normalized = effect.casefold().strip()
    normalized = api_state.MEDIA_API_ALIASES.get(normalized, normalized)
    if normalized not in api_state.MEDIA_API_EFFECTS:
        raise HTTPException(404, "Unknown media effect")
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    body = b""
    effect_options: dict[str, Any]
    if content_type == "application/json":
        raw_json = await _read_bounded_request(request, api_state.MAX_REQUEST_BYTES)
        try:
            payload = json.loads(raw_json or b"{}")
        except json.JSONDecodeError as error:
            raise HTTPException(400, "Request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        payload_url = payload.get("media_url")
        payload_secondary_url = payload.get("secondary_media_url")
        if payload_url is not None and not isinstance(payload_url, str):
            raise HTTPException(400, "media_url must be a string")
        if isinstance(payload_url, str) and len(payload_url) > 2_048:
            raise HTTPException(400, "media_url is too long")
        if payload_secondary_url is not None and not isinstance(
            payload_secondary_url, str
        ):
            raise HTTPException(400, "secondary_media_url must be a string")
        if (
            isinstance(payload_secondary_url, str)
            and len(payload_secondary_url) > 2_048
        ):
            raise HTTPException(400, "secondary_media_url is too long")
        media_url = media_url or payload_url
        secondary_media_url = secondary_media_url or payload_secondary_url
        effect_options = _media_api_options(payload.get("options", options))
    else:
        body = await _read_bounded_request(request, api_state.MAX_MEDIA_API_BYTES)
        effect_options = _media_api_options(options)

    if media_url and body:
        raise HTTPException(400, "Provide a media URL or an uploaded file, not both")
    if media_url:
        try:
            media_url = await refresh_discord_attachment_url(
                api_state.bot_ref, media_url
            )
            fetched = await fetch_public_bytes(
                api_state.bot_ref.session,
                media_url,
                max_bytes=api_state.MAX_MEDIA_API_BYTES,
                allowed_content_prefixes=("image/", "video/", "audio/"),
            )
        except commands.BadArgument as error:
            raise HTTPException(400, str(error)) from error
        body = fetched.data
    elif not body:
        raise HTTPException(
            400,
            "Provide media_url in JSON/query parameters or upload raw media bytes",
        )
    elif content_type and not (
        content_type.startswith(("image/", "video/", "audio/"))
        or content_type == "application/octet-stream"
    ):
        raise HTTPException(415, "Upload an image, GIF, video, or audio file")

    secondary_data: bytes | None = None
    if normalized == "soundeffect":
        try:
            selected = find_audio_effect(str(effect_options.pop("effect", "random")))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        secondary_data = await asyncio.to_thread(selected.path.read_bytes)
    elif secondary_media_url:
        try:
            secondary_media_url = await refresh_discord_attachment_url(
                api_state.bot_ref,
                secondary_media_url,
            )
            secondary = await fetch_public_bytes(
                api_state.bot_ref.session,
                secondary_media_url,
                max_bytes=api_state.MAX_MEDIA_API_BYTES,
                allowed_content_prefixes=("image/", "video/", "audio/"),
            )
        except commands.BadArgument as error:
            raise HTTPException(400, str(error)) from error
        secondary_data = secondary.data

    if (
        normalized in {"overlay", "audiooverlay", "audioreplace", "combine"}
        and secondary_data is None
    ):
        raise HTTPException(
            400,
            "This effect requires secondary_media_url.",
        )

    effect_options, adjustments = _normalize_effect_options(
        normalized,
        effect_options,
    )
    renderer_options = _renderer_effect_options(normalized, effect_options)
    try:
        async with api_state.bot_ref.media_semaphore:
            async with asyncio.timeout(60):
                engine = PIPELINE_EFFECTS[normalized][0]
                if normalized == "text":
                    text = str(renderer_options.get("text", ""))
                    renderer_options["inline_images"] = await resolve_inline_images(
                        api_state.bot_ref.session, [text]
                    )
                    result = await render_text_effect(body, **renderer_options)
                elif normalized == "combine":
                    if secondary_data is None:
                        raise ValueError("Combine requires secondary media.")
                    result = await render_combine_effect(
                        body,
                        secondary_data,
                        **renderer_options,
                    )
                elif normalized == "overlay":
                    if secondary_data is None:
                        raise ValueError("Overlay requires secondary media.")
                    result = await render_overlay_effect(
                        body,
                        secondary_data,
                        **renderer_options,
                    )
                elif engine == "video" or normalized in {
                    "audiooverlay",
                    "audioreplace",
                    "soundeffect",
                }:
                    result = await render_video_effect(
                        body,
                        normalized,
                        second_data=secondary_data,
                        **renderer_options,
                    )
                else:
                    result = await render_image_effect(
                        body,
                        normalized,
                        **renderer_options,
                    )
    except TimeoutError as error:
        raise HTTPException(408, "The effect took longer than 60 seconds") from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error

    if len(result.data) > api_state.MAX_MEDIA_API_BYTES:
        raise HTTPException(413, "Generated media is too large")
    filename = result.filename.replace("/", "_").replace("\\", "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    if adjustments:
        headers["X-Fishie-Adjusted"] = json.dumps(adjustments, ensure_ascii=True)[:2048]
    return Response(
        result.data,
        media_type=media_type,
        headers=headers,
    )

"""Health helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse

from . import state as api_state

router = APIRouter()


class RequestBodyTooLarge(ValueError):
    """Raised when a streamed request exceeds its endpoint body limit."""


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """Read and cache a request body without trusting Content-Length."""

    cached = getattr(request, "_body", None)
    if isinstance(cached, bytes):
        if len(cached) > limit:
            raise RequestBodyTooLarge
        return cached

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RequestBodyTooLarge
        chunks.append(chunk)
    body = b"".join(chunks)
    setattr(request, "_body", body)
    return body


async def protect_cookie_requests(request: Request, call_next):
    """Reject cross-origin state changes authenticated by the session cookie."""
    path = request.url.path
    media_request = path.startswith("/media/effects/")
    webhook_request = path == "/twitch/eventsub"
    request_limit = (
        api_state.MAX_MEDIA_API_BYTES
        if media_request
        else (
            api_state.MAX_WEBHOOK_BYTES
            if webhook_request
            else api_state.MAX_REQUEST_BYTES
        )
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > request_limit:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )
        except ValueError:
            return JSONResponse(
                status_code=400, content={"detail": "Invalid body size"}
            )
    if not media_request:
        try:
            await read_bounded_body(request, request_limit)
        except RequestBodyTooLarge:
            return JSONResponse(
                status_code=413, content={"detail": "Request body is too large"}
            )
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.cookies.get(
        api_state.SESSION_COOKIE
    ):
        if request.headers.get("origin") not in api_state.WEB_ORIGINS:
            return JSONResponse(
                status_code=403, content={"detail": "Invalid request origin"}
            )
    return await call_next(request)


@router.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
async def health_ready():
    if (
        api_state.bot_ref is None
        or api_state.bot_ref.is_closed()
        or not api_state.bot_ref.is_ready()
    ):
        raise HTTPException(503, "Bot is not ready")
    try:
        await asyncio.wait_for(api_state._check_pool().fetchval("SELECT 1"), timeout=2)
    except Exception as error:
        raise HTTPException(503, "Database is not ready") from error
    return {"status": "ok"}

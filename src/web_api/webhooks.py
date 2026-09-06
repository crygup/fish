"""Webhooks helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import PlainTextResponse

from extensions.events.youtube import (
    youtube_websub_verify_token,
)

from . import state as api_state
from .health import RequestBodyTooLarge, read_bounded_body

router = APIRouter()


async def _claim_twitch_eventsub_message(message_id: str) -> bool:
    """Atomically claim a non-notification EventSub message.

    Keep the in-process lock for concurrent requests and persist a completed
    marker in the EventSub inbox for deployments with more than one API
    worker.  Callback/revocation payloads are intentionally marked ``done`` so
    the durable event worker never attempts to process their empty payload.
    """

    async with api_state._twitch_eventsub_replay_lock:
        if message_id in api_state._twitch_eventsub_replays:
            return False
        api_state._twitch_eventsub_replays[message_id] = True
    if api_state.bot_ref is None or not getattr(api_state.bot_ref, "pool", None):
        return True
    try:
        result = await api_state.bot_ref.pool.execute(
            "INSERT INTO twitch_eventsub_events (message_id, payload, status) "
            "VALUES ($1, '{}'::jsonb, 'done') "
            "ON CONFLICT (message_id) DO NOTHING",
            message_id,
        )
    except Exception:
        api_state._twitch_eventsub_replays.pop(message_id, None)
        raise
    if result == "INSERT 0 0":
        return False
    return True


def _verify_twitch_eventsub(request: Request, body: bytes) -> None:
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    keys = api_state.bot_ref.config["keys"]
    secret = keys.get("twitch_eventsub_secret")
    if not secret:
        raise HTTPException(503, "Twitch EventSub is not configured")

    message_id = request.headers.get("Twitch-Eventsub-Message-Id")
    timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp")
    supplied_signature = request.headers.get("Twitch-Eventsub-Message-Signature")
    if not message_id or not timestamp or not supplied_signature:
        raise HTTPException(403, "Missing Twitch EventSub signature headers")

    try:
        message_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(403, "Invalid Twitch EventSub timestamp")
    if message_time.tzinfo is None:
        message_time = message_time.replace(tzinfo=timezone.utc)
    if abs(time.time() - message_time.timestamp()) > api_state.TWITCH_EVENTSUB_MAX_AGE:
        raise HTTPException(403, "Expired Twitch EventSub message")

    expected_signature = (
        "sha256="
        + hmac.new(
            secret.encode(),
            message_id.encode() + timestamp.encode() + body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(403, "Invalid Twitch EventSub signature")


@router.post("/twitch/eventsub")
async def twitch_eventsub(request: Request):
    """Receive verified Twitch EventSub stream notifications."""
    try:
        body = await read_bounded_body(request, api_state.MAX_WEBHOOK_BYTES)
    except RequestBodyTooLarge as error:
        raise HTTPException(413, "Twitch EventSub payload is too large") from error
    _verify_twitch_eventsub(request, body)
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    bot = api_state.bot_ref

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid Twitch EventSub payload")

    message_type = request.headers.get("Twitch-Eventsub-Message-Type")
    message_id = request.headers.get("Twitch-Eventsub-Message-Id")
    bot.logger.info(
        "Received Twitch EventSub message type=%s", message_type or "unknown"
    )
    # Verification and revocation messages update subscription state directly
    # and therefore do not get the notification table's primary-key dedupe.
    # Claim their signed message ID before performing those updates so a replay
    # cannot repeat the side effect during Twitch's ten-minute validity window.
    if message_type in {"webhook_callback_verification", "revocation"}:
        if not message_id or not await _claim_twitch_eventsub_message(message_id):
            return {"ok": True, "duplicate": True}
    if message_type == "webhook_callback_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(400, "Missing Twitch EventSub challenge")
        subscription = payload.get("subscription")
        if isinstance(subscription, dict) and subscription.get("id"):
            await bot.pool.execute(
                "UPDATE twitch_eventsub_subscriptions "
                "SET status = 'enabled', updated_at = now() "
                "WHERE subscription_id = $1",
                subscription["id"],
            )
        return PlainTextResponse(challenge)

    subscription = payload.get("subscription")
    if not isinstance(subscription, dict):
        raise HTTPException(400, "Missing Twitch EventSub subscription")

    if message_type == "revocation":
        if api_state.bot_ref:
            await api_state.bot_ref.pool.execute(
                "UPDATE twitch_eventsub_subscriptions "
                "SET status = $2, updated_at = now() WHERE subscription_id = $1",
                subscription.get("id"),
                subscription.get("status", "revoked"),
            )
        return {"ok": True}

    if message_type != "notification":
        raise HTTPException(400, "Unsupported Twitch EventSub message type")

    event = payload.get("event")
    if not isinstance(event, dict) or not message_id:
        raise HTTPException(400, "Missing Twitch EventSub event")
    event = dict(event)
    event["type"] = subscription.get("type")

    events_cog: Any = api_state.bot_ref.get_cog("Events")
    if events_cog is None or not hasattr(events_cog, "handle_twitch_event"):
        raise HTTPException(503, "Twitch event handler is not ready")

    pool = api_state._check_pool()
    result = await pool.execute(
        "INSERT INTO twitch_eventsub_events (message_id, payload) VALUES ($1, $2::jsonb) "
        "ON CONFLICT (message_id) DO NOTHING",
        message_id,
        event,
    )
    if result == "INSERT 0 0":
        return {"ok": True, "duplicate": True}

    task = asyncio.create_task(events_cog.process_twitch_event_message(message_id))
    tasks = bot._eventsub_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"ok": True}


def _youtube_channel_from_topic(topic: str) -> str | None:
    parsed = urlsplit(topic)
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.youtube.com",
        "youtube.com",
    }:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None
    values = parse_qs(parsed.query).get("channel_id", [])
    channel_id = values[0] if len(values) == 1 else ""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        return None
    return channel_id


def _verify_youtube_websub_signature(body: bytes, supplied: str | None) -> None:
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    keys = api_state.bot_ref.config.get("keys", {})
    secret = keys.get("youtube_websub_secret")
    if not secret:
        raise HTTPException(503, "YouTube WebSub is not configured")
    if not supplied or "=" not in supplied:
        raise HTTPException(403, "Missing YouTube WebSub signature")
    algorithm, signature = supplied.split("=", 1)
    digest = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}.get(algorithm.lower())
    if digest is None:
        raise HTTPException(403, "Unsupported YouTube WebSub signature")
    expected = hmac.new(str(secret).encode(), body, digest).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise HTTPException(403, "Invalid YouTube WebSub signature")


@router.get("/youtube/websub", include_in_schema=False)
async def verify_youtube_websub(request: Request):
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    mode = request.query_params.get("hub.mode", "")
    topic = request.query_params.get("hub.topic", "")
    challenge = request.query_params.get("hub.challenge", "")
    channel_id = _youtube_channel_from_topic(topic)
    if mode not in {"subscribe", "unsubscribe"} or not challenge or not channel_id:
        raise HTTPException(400, "Invalid YouTube WebSub verification")
    keys = api_state.bot_ref.config.get("keys", {})
    secret = keys.get("youtube_websub_secret")
    if not secret:
        raise HTTPException(503, "YouTube WebSub is not configured")
    supplied_token = request.query_params.get("hub.verify_token", "")
    expected_token = youtube_websub_verify_token(str(secret), channel_id)
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(403, "Invalid YouTube WebSub verification token")
    known = await api_state.bot_ref.pool.fetchval(
        "SELECT 1 FROM youtube_websub_subscriptions "
        "WHERE youtube_channel_id = $1 "
        "UNION ALL SELECT 1 FROM youtube_follows "
        "WHERE youtube_channel_id = $1 LIMIT 1",
        channel_id,
    )
    if not known:
        raise HTTPException(404, "Unknown YouTube subscription")
    if mode == "unsubscribe":
        await api_state.bot_ref.pool.execute(
            "DELETE FROM youtube_websub_subscriptions WHERE youtube_channel_id = $1",
            channel_id,
        )
    else:
        try:
            lease_seconds = min(
                864000,
                max(
                    60,
                    int(request.query_params.get("hub.lease_seconds", "864000")),
                ),
            )
        except ValueError:
            raise HTTPException(400, "Invalid YouTube WebSub lease")
        await api_state.bot_ref.pool.execute(
            "INSERT INTO youtube_websub_subscriptions "
            "(youtube_channel_id, status, lease_expires_at, updated_at, last_error) "
            "VALUES ($1, 'enabled', now() + ($2 * interval '1 second'), now(), NULL) "
            "ON CONFLICT (youtube_channel_id) DO UPDATE SET status = 'enabled', "
            "lease_expires_at = EXCLUDED.lease_expires_at, updated_at = now(), "
            "last_error = NULL",
            channel_id,
            lease_seconds,
        )
    return PlainTextResponse(challenge)


@router.post("/youtube/websub", include_in_schema=False)
async def youtube_websub(request: Request):
    try:
        body = await read_bounded_body(request, api_state.MAX_WEBHOOK_BYTES)
    except RequestBodyTooLarge as error:
        raise HTTPException(413, "YouTube WebSub payload is too large") from error
    _verify_youtube_websub_signature(
        body,
        request.headers.get("X-Hub-Signature-256")
        or request.headers.get("X-Hub-Signature"),
    )
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise HTTPException(400, "Unsafe YouTube WebSub XML")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPException(400, "Invalid YouTube WebSub XML")
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    events_cog: Any = api_state.bot_ref.get_cog("Events")
    if events_cog is None or not hasattr(events_cog, "process_youtube_event"):
        raise HTTPException(503, "YouTube event handler is not ready")
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    inserted: list[str] = []
    body_hash = hashlib.sha256(body).hexdigest()
    for entry in root.findall("atom:entry", namespaces):
        video_id = (entry.findtext("yt:videoId", "", namespaces) or "").strip()
        channel_id = (entry.findtext("yt:channelId", "", namespaces) or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not re.fullmatch(
            r"UC[A-Za-z0-9_-]{22}", channel_id
        ):
            continue
        followed = await api_state.bot_ref.pool.fetchval(
            "SELECT 1 FROM youtube_follows WHERE youtube_channel_id = $1 LIMIT 1",
            channel_id,
        )
        if not followed:
            continue
        updated = entry.findtext("atom:updated", "", namespaces) or ""
        event_id = hashlib.sha256(
            f"{body_hash}:{channel_id}:{video_id}:{updated}".encode()
        ).hexdigest()
        payload = {
            "channel_id": channel_id,
            "video_id": video_id,
            "title": entry.findtext("atom:title", "", namespaces) or "",
            "published": entry.findtext("atom:published", "", namespaces) or "",
            "updated": updated,
        }
        result = await api_state.bot_ref.pool.execute(
            "INSERT INTO youtube_events "
            "(event_id, youtube_channel_id, video_id, payload) "
            "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (event_id) DO NOTHING",
            event_id,
            channel_id,
            video_id,
            payload,
        )
        if result != "INSERT 0 0":
            inserted.append(event_id)
    for event_id in inserted:
        task = asyncio.create_task(events_cog.process_youtube_event(event_id))
        api_state.bot_ref._eventsub_tasks.add(task)
        task.add_done_callback(api_state.bot_ref._eventsub_tasks.discard)
    return {"ok": True, "accepted": len(inserted)}

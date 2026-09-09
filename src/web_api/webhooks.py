"""Webhooks helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import PlainTextResponse

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

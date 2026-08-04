from __future__ import annotations

import json
from typing import Any, Iterable, TypeAlias

import discord

ActivityLike: TypeAlias = discord.BaseActivity | discord.Spotify


def activity_type_name(activity: ActivityLike) -> str:
    activity_type = getattr(activity, "type", None)
    if isinstance(activity_type, discord.ActivityType):
        return activity_type.name.replace("_", " ").title()
    return "Activity"


def activity_name(activity: ActivityLike) -> str:
    if isinstance(activity, discord.CustomActivity):
        return activity.name or activity.state or "Custom Status"
    return getattr(activity, "name", None) or "Unknown activity"


def activity_identity(
    activity: ActivityLike,
) -> tuple[int, str, int | None]:
    activity_type = getattr(activity, "type", discord.ActivityType.unknown)
    return (
        int(activity_type.value),
        activity_name(activity).casefold(),
        getattr(activity, "application_id", None),
    )


def activity_snapshot(activity: ActivityLike) -> str:
    try:
        payload: Any = activity.to_dict()
    except (AttributeError, TypeError):
        payload = {
            "name": activity_name(activity),
            "type": activity_type_name(activity),
        }

    def remove_progress_fields(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: remove_progress_fields(item)
                for key, item in value.items()
                if key not in {"timestamps", "created_at"}
            }
        if isinstance(value, list):
            return [remove_progress_fields(item) for item in value]
        return value

    return json.dumps(
        remove_progress_fields(payload),
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def activity_matches(activity: ActivityLike, query: str) -> bool:
    needle = query.casefold()
    values = (
        activity_name(activity),
        str(getattr(activity, "details", "") or ""),
        str(getattr(activity, "state", "") or ""),
    )
    return any(needle in value.casefold() for value in values)


def activity_image_url(activity: ActivityLike) -> str | None:
    for attribute in ("large_image_url", "small_image_url", "album_cover_url"):
        value = getattr(activity, attribute, None)
        if value and str(value).startswith(("https://", "http://")):
            return str(value)
    return None


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        return discord.utils.format_dt(value, "R")
    return None


def activity_details(activity: ActivityLike) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [
        ("Type", activity_type_name(activity)),
    ]
    application_id = getattr(activity, "application_id", None)
    if application_id:
        fields.append(("Application ID", str(application_id)))

    if isinstance(activity, discord.Spotify):
        if activity.artists:
            fields.append(("Artists", ", ".join(activity.artists)))
        if activity.album:
            fields.append(("Album", activity.album))
        if activity.track_id:
            fields.append(("Track ID", activity.track_id))

    for label, attribute in (("Details", "details"), ("State", "state")):
        value = getattr(activity, attribute, None)
        if value and str(value) != activity_name(activity):
            fields.append((label, str(value)))

    start = _format_timestamp(getattr(activity, "start", None))
    end = _format_timestamp(getattr(activity, "end", None))
    if start:
        fields.append(("Started", start))
    if end:
        fields.append(("Ends", end))
    if not start:
        created_at = _format_timestamp(getattr(activity, "created_at", None))
        if created_at:
            fields.append(("Created", created_at))

    url = getattr(activity, "url", None)
    if url:
        fields.append(("URL", str(url)))

    platform = getattr(activity, "platform", None)
    if platform:
        fields.append(("Platform", str(platform).replace("_", " ").title()))

    party = getattr(activity, "party", None)
    if isinstance(party, dict):
        party_id = party.get("id")
        party_size = party.get("size")
        values = []
        if party_id:
            values.append(f"ID: {party_id}")
        if isinstance(party_size, (list, tuple)) and len(party_size) == 2:
            values.append(f"Size: {party_size[0]}/{party_size[1]}")
        if values:
            fields.append(("Party", " • ".join(values)))

    buttons = getattr(activity, "buttons", None)
    if buttons:
        fields.append(("Buttons", ", ".join(map(str, buttons))))
    return fields


def activity_summary(activity: ActivityLike) -> str:
    details = activity_details(activity)
    lines = [f"**{activity_type_name(activity)}:** {activity_name(activity)}"]
    for label, value in details:
        if label == "Type":
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def loggable_activities(
    activities: Iterable[ActivityLike],
) -> tuple[ActivityLike, ...]:
    return tuple(
        activity
        for activity in activities
        if getattr(activity, "type", None) is not discord.ActivityType.custom
    )

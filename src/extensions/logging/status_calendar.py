from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

from utils.rich_text import text_font

STATUS_COLORS: dict[str, tuple[int, int, int]] = {
    "online": (67, 181, 129),
    "idle": (250, 166, 26),
    "dnd": (240, 71, 71),
    "offline": (116, 127, 141),
}
STATUS_ORDER = ("online", "idle", "dnd", "offline")


@dataclass(frozen=True, slots=True)
class StatusInterval:
    status: str
    started_at: datetime.datetime
    ended_at: datetime.datetime | None


def hourly_statuses(
    intervals: list[StatusInterval],
    start_date: datetime.date,
    *,
    days: int = 31,
    now: datetime.datetime | None = None,
) -> list[list[str | None]]:
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    window_start = datetime.datetime.combine(
        start_date,
        datetime.time.min,
        tzinfo=datetime.UTC,
    )
    window_end = min(window_start + datetime.timedelta(days=days), now)
    totals = [
        [{status: 0.0 for status in STATUS_ORDER} for _ in range(24)]
        for _ in range(days)
    ]

    for interval in intervals:
        if interval.status not in STATUS_COLORS:
            continue
        started_at = interval.started_at
        ended_at = interval.ended_at or now
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=datetime.UTC)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=datetime.UTC)
        cursor = max(started_at.astimezone(datetime.UTC), window_start)
        interval_end = min(ended_at.astimezone(datetime.UTC), window_end)
        while cursor < interval_end:
            day_index = (cursor.date() - start_date).days
            if day_index < 0 or day_index >= days:
                break
            next_hour = cursor.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) + datetime.timedelta(hours=1)
            segment_end = min(interval_end, next_hour)
            totals[day_index][cursor.hour][interval.status] += (
                segment_end - cursor
            ).total_seconds()
            cursor = segment_end

    result: list[list[str | None]] = []
    for day in totals:
        hours: list[str | None] = []
        for hour in day:
            longest = max(hour.values())
            if longest <= 0:
                hours.append(None)
                continue
            hours.append(
                next(status for status in STATUS_ORDER if hour[status] == longest)
            )
        result.append(hours)
    return result


def merged_hourly_statuses(
    intervals_by_guild: dict[int, list[StatusInterval]],
    primary_guild_id: int,
    start_date: datetime.date,
    *,
    days: int = 31,
    now: datetime.datetime | None = None,
) -> list[list[str | None]]:
    """Merge user-wide presence without double-counting shared guild events.

    Discord emits the same user presence through every shared guild. The
    current guild remains authoritative wherever it has an observation; empty
    hours are then filled from the most complete remaining guild timelines.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    timelines = {
        guild_id: hourly_statuses(
            intervals,
            start_date,
            days=days,
            now=now,
        )
        for guild_id, intervals in intervals_by_guild.items()
    }
    merged: list[list[str | None]] = [[None] * 24 for _ in range(days)]

    def observed_hours(timeline: list[list[str | None]]) -> int:
        return sum(status is not None for day in timeline for status in day)

    guild_order: list[int] = []
    if primary_guild_id in timelines:
        guild_order.append(primary_guild_id)
    guild_order.extend(
        sorted(
            (guild_id for guild_id in timelines if guild_id != primary_guild_id),
            key=lambda guild_id: (-observed_hours(timelines[guild_id]), guild_id),
        )
    )

    for guild_id in guild_order:
        timeline = timelines[guild_id]
        for day_index, day in enumerate(timeline):
            for hour, status in enumerate(day):
                if merged[day_index][hour] is None and status is not None:
                    merged[day_index][hour] = status
    return merged


def render_status_calendar(
    username: str,
    start_date: datetime.date,
    statuses: list[list[str | None]],
) -> BytesIO:
    columns = 7
    first_weekday = start_date.weekday()
    rows = math.ceil((first_weekday + len(statuses)) / columns)
    width = 920
    margin = 42
    gap = 10
    cell_width = (width - margin * 2 - gap * (columns - 1)) // columns
    cell_height = 86
    grid_y = 92
    height = grid_y + rows * cell_height + (rows - 1) * gap + 94
    canvas = Image.new("RGB", (width, height), (17, 18, 20))
    draw = ImageDraw.Draw(canvas)
    label_font = text_font("", 17)
    date_font = text_font("", 16)
    legend_font = text_font("", 16)

    for column, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        x = margin + column * (cell_width + gap)
        draw.text((x + 4, 50), label, font=label_font, fill=(174, 177, 185))

    for index, hours in enumerate(statuses):
        slot = first_weekday + index
        row, column = divmod(slot, columns)
        x = margin + column * (cell_width + gap)
        y = grid_y + row * (cell_height + gap)
        observed = {status for status in hours if status is not None}
        tile_width = cell_width + 1
        tile_height = cell_height + 1
        tile = Image.new("RGB", (tile_width, tile_height), (43, 45, 49))
        tile_draw = ImageDraw.Draw(tile)
        for hour, status in enumerate(hours):
            left = round(hour * tile_width / 24)
            right = round((hour + 1) * tile_width / 24)
            tile_draw.rectangle(
                (left, 0, max(left, right - 1), tile_height - 1),
                fill=STATUS_COLORS.get(status or "", (43, 45, 49)),
            )
        mask = Image.new("L", (tile_width, tile_height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, tile_width - 1, tile_height - 1),
            radius=8,
            fill=255,
        )
        canvas.paste(tile, (x, y), mask)
        day = start_date + datetime.timedelta(days=index)
        text_color = (250, 250, 250) if observed else (136, 139, 147)
        draw.text(
            (x + 10, y + 9),
            day.strftime("%b %d"),
            font=date_font,
            fill=text_color,
            stroke_width=2,
            stroke_fill=(17, 18, 20),
        )
        if not observed:
            draw.text(
                (x + 10, y + 49),
                "No data",
                font=date_font,
                fill=text_color,
            )

    legend_y = height - 62
    legend_x = margin
    for status in STATUS_ORDER:
        draw.rounded_rectangle(
            (legend_x, legend_y, legend_x + 22, legend_y + 22),
            radius=5,
            fill=STATUS_COLORS[status],
        )
        draw.text(
            (legend_x + 31, legend_y - 2),
            status.title(),
            font=legend_font,
            fill=(220, 221, 225),
        )
        legend_x += 142

    output = BytesIO()
    canvas.save(output, "PNG", optimize=True)
    output.seek(0)
    return output

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
    cell_height = 72
    grid_y = 108
    height = grid_y + rows * cell_height + (rows - 1) * gap + 105
    canvas = Image.new("RGB", (width, height), (17, 18, 20))
    draw = ImageDraw.Draw(canvas)
    title_font = text_font(username, 30)
    small_font = text_font("", 12)

    draw.text(
        (margin, 30),
        f"{username}'s status calendar",
        font=title_font,
        fill=(245, 245, 245),
    )

    for column, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        x = margin + column * (cell_width + gap)
        draw.text((x + 4, 79), label, font=small_font, fill=(174, 177, 185))

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
            (x + 10, y + 8),
            day.strftime("%b %d"),
            font=small_font,
            fill=text_color,
            stroke_width=2,
            stroke_fill=(17, 18, 20),
        )
        if not observed:
            draw.text(
                (x + 10, y + 40),
                "No data",
                font=small_font,
                fill=text_color,
            )

    legend_y = height - 58
    legend_x = margin
    for status in STATUS_ORDER:
        draw.rounded_rectangle(
            (legend_x, legend_y, legend_x + 18, legend_y + 18),
            radius=4,
            fill=STATUS_COLORS[status],
        )
        draw.text(
            (legend_x + 27, legend_y - 1),
            status.title(),
            font=small_font,
            fill=(220, 221, 225),
        )
        legend_x += 126

    output = BytesIO()
    canvas.save(output, "PNG", optimize=True)
    output.seek(0)
    return output

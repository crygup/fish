import datetime
from types import SimpleNamespace
from typing import Any, cast

from PIL import Image, ImageChops

from extensions.events.statuses import StatusCog
from extensions.events.tasks import Tasks
from extensions.logging.status_calendar import (
    STATUS_COLORS,
    StatusInterval,
    hourly_statuses,
    render_status_calendar,
)


def test_hourly_status_tracks_changes_within_each_day() -> None:
    start_date = datetime.date(2026, 7, 1)
    intervals = [
        StatusInterval(
            "online",
            datetime.datetime(2026, 7, 1, 0, tzinfo=datetime.UTC),
            datetime.datetime(2026, 7, 1, 18, tzinfo=datetime.UTC),
        ),
        StatusInterval(
            "idle",
            datetime.datetime(2026, 7, 1, 18, tzinfo=datetime.UTC),
            datetime.datetime(2026, 7, 2, 12, tzinfo=datetime.UTC),
        ),
        StatusInterval(
            "offline",
            datetime.datetime(2026, 7, 2, 12, tzinfo=datetime.UTC),
            None,
        ),
    ]

    statuses = hourly_statuses(
        intervals,
        start_date,
        days=2,
        now=datetime.datetime(2026, 7, 3, tzinfo=datetime.UTC),
    )

    assert statuses[0] == ["online"] * 18 + ["idle"] * 6
    assert statuses[1] == ["idle"] * 12 + ["offline"] * 12


def test_hourly_status_uses_the_longest_status_within_an_hour() -> None:
    intervals = [
        StatusInterval(
            "online",
            datetime.datetime(2026, 7, 1, 0, tzinfo=datetime.UTC),
            datetime.datetime(2026, 7, 1, 0, 40, tzinfo=datetime.UTC),
        ),
        StatusInterval(
            "idle",
            datetime.datetime(2026, 7, 1, 0, 40, tzinfo=datetime.UTC),
            datetime.datetime(2026, 7, 1, 2, tzinfo=datetime.UTC),
        ),
    ]

    statuses = hourly_statuses(
        intervals,
        datetime.date(2026, 7, 1),
        days=1,
        now=datetime.datetime(2026, 7, 2, tzinfo=datetime.UTC),
    )

    assert statuses[0][0] == "online"
    assert statuses[0][1] == "idle"


def test_hourly_status_leaves_hours_without_observations_empty() -> None:
    statuses = hourly_statuses(
        [],
        datetime.date(2026, 7, 1),
        days=3,
        now=datetime.datetime(2026, 7, 4, tzinfo=datetime.UTC),
    )

    assert statuses == [[None] * 24, [None] * 24, [None] * 24]


def test_status_calendar_renders_all_31_days() -> None:
    output = render_status_calendar(
        "Fishie",
        datetime.date(2026, 6, 27),
        [[status] * 24 for status in (["online", "idle", "dnd", "offline", None] * 6)]
        + [["online"] * 12 + ["idle"] * 12],
    )

    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 920
        assert image.height > 500


def test_mixed_day_hour_colours_fill_the_entire_cell() -> None:
    statuses = cast(list[list[str | None]], [["online"] * 12 + ["dnd"] * 12])
    output = render_status_calendar(
        "Fishie",
        datetime.date(2026, 7, 6),
        statuses,
    )

    with Image.open(output) as image:
        assert image.getpixel((70, 160)) == STATUS_COLORS["online"]
        assert image.getpixel((125, 160)) == STATUS_COLORS["dnd"]


def test_empty_day_displays_a_no_data_label() -> None:
    output = render_status_calendar(
        "Fishie",
        datetime.date(2026, 7, 6),
        [[None] * 24],
    )

    with Image.open(output) as image:
        label = image.crop((50, 148, 135, 175))
        background = Image.new("RGB", label.size, (43, 45, 49))
        assert ImageChops.difference(label, background).getbbox() is not None


class RecordingPool:
    def __init__(self) -> None:
        self.sql = ""

    async def execute(self, sql: str) -> str:
        self.sql = sql
        return "DELETE 7"


class AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_: object) -> None:
        return None


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def transaction(self) -> AsyncContext:
        return AsyncContext(self)

    async def execute(self, sql: str, *_: object) -> str:
        self.statements.append(sql)
        return "INSERT 0 1"


class RecordingConnectionPool:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> AsyncContext:
        return AsyncContext(self.connection)


async def test_status_transition_closes_before_opening_under_a_lock() -> None:
    connection = RecordingConnection()
    cog = StatusCog()
    cog.bot = cast(Any, SimpleNamespace(pool=RecordingConnectionPool(connection)))
    member = cast(Any, SimpleNamespace(id=42, guild=SimpleNamespace(id=99)))

    await cog.store_status(member, "online")

    assert len(connection.statements) == 4
    assert "pg_advisory_xact_lock" in connection.statements[0]
    assert "$1::bigint::text" in connection.statements[0]
    assert "$2::bigint::text" in connection.statements[0]
    assert connection.statements[1].lstrip().startswith("UPDATE user_status_history")
    assert (
        connection.statements[2].lstrip().startswith("INSERT INTO user_status_history")
    )
    assert connection.statements[3].lstrip().startswith("INSERT INTO user_statuses")


async def test_status_cleanup_keeps_latest_status_occurrence() -> None:
    pool = RecordingPool()
    cog = Tasks()
    cog.bot = cast(Any, SimpleNamespace(pool=pool))

    deleted = await cog.cleanup_status_history()

    assert deleted == 7
    assert "PARTITION BY user_id, guild_id, status" in pool.sql
    assert "status_rank > 1" in pool.sql
    assert "interval '31 days'" in pool.sql

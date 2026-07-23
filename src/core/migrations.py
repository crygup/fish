from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_LOCK_ID = 0x464953484945  # "FISHIE"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def available_migrations() -> tuple[Migration, ...]:
    migrations = [Migration(1, "baseline", ROOT / "schema.sql")]
    for path in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version_text, _, name = path.stem.partition("_")
        migrations.append(Migration(int(version_text), name, path))
    versions = [migration.version for migration in migrations]
    if versions != sorted(set(versions)):
        raise RuntimeError("Migration versions must be unique and increasing")
    return tuple(migrations)


async def migrate(connection: Any) -> list[Migration]:
    """Apply pending migrations under a database-wide advisory lock."""

    applied: list[Migration] = []
    await connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """)
    await connection.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
    try:
        existing = {
            row["version"]: row
            for row in await connection.fetch(
                "SELECT version, name, checksum FROM schema_migrations"
            )
        }
        for migration in available_migrations():
            record = existing.get(migration.version)
            if record:
                if record["checksum"] != migration.checksum:
                    raise RuntimeError(
                        f"Applied migration {migration.version} checksum has changed"
                    )
                continue
            if migration.sql.startswith("-- fishie: no-transaction"):
                # These files contain simple standalone statements such as
                # CREATE INDEX CONCURRENTLY, which PostgreSQL forbids inside a
                # transaction. Every statement must be independently idempotent.
                for statement in migration.sql.split(";"):
                    if statement.strip():
                        await connection.execute(statement)
                await connection.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) "
                    "VALUES($1, $2, $3)",
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
            else:
                async with connection.transaction():
                    await connection.execute(migration.sql)
                    await connection.execute(
                        "INSERT INTO schema_migrations(version, name, checksum) "
                        "VALUES($1, $2, $3)",
                        migration.version,
                        migration.name,
                        migration.checksum,
                    )
            applied.append(migration)
        return applied
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)


async def check_migrations(connection: Any) -> None:
    """Fail startup when the operator has not applied the exact migration set."""

    exists = await connection.fetchval("SELECT to_regclass('schema_migrations')")
    if not exists:
        raise RuntimeError(
            "Database is not migrated; run `python src/manage.py migrate`"
        )
    records = {
        row["version"]: row
        for row in await connection.fetch(
            "SELECT version, name, checksum FROM schema_migrations"
        )
    }
    for migration in available_migrations():
        record = records.get(migration.version)
        if record is None:
            raise RuntimeError(
                f"Database migration {migration.version} is pending; "
                "run `python src/manage.py migrate`"
            )
        if record["checksum"] != migration.checksum:
            raise RuntimeError(
                f"Database migration {migration.version} differs from the applied version"
            )

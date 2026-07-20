from __future__ import annotations

import argparse
import asyncio
import os
import tomllib
from pathlib import Path

import asyncpg

from core.migrations import migrate
from utils.credentials import encrypt_credential


def database_url() -> str:
    if value := os.getenv("DATABASE_URL"):
        return value
    config_path = Path(os.getenv("FISHIE_CONFIG", "config.toml"))
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    return str(config["databases"]["psql"])


async def migrate_command() -> None:
    connection = await asyncpg.connect(database_url())
    try:
        applied = await migrate(connection)
    finally:
        await connection.close()
    if applied:
        print("Applied: " + ", ".join(f"{item.version}_{item.name}" for item in applied))
    else:
        print("Database is up to date")


async def encrypt_credentials_command() -> None:
    """Encrypt credentials written before application-level encryption existed."""

    connection = await asyncpg.connect(database_url())
    changed = 0
    try:
        async with connection.transaction():
            account_columns = (
                "lastfm_session_key",
                "spotify_refresh_token",
                "anilist_access_token",
            )
            rows = await connection.fetch(
                "SELECT user_id, " + ", ".join(account_columns) + " FROM accounts FOR UPDATE"
            )
            for row in rows:
                encrypted = [
                    encrypt_credential(row[column]) if row[column] else None
                    for column in account_columns
                ]
                if any(encrypted[index] != row[column] for index, column in enumerate(account_columns)):
                    await connection.execute(
                        "UPDATE accounts SET lastfm_session_key = $2, "
                        "spotify_refresh_token = $3, anilist_access_token = $4 "
                        "WHERE user_id = $1",
                        row["user_id"],
                        *encrypted,
                    )
                    changed += 1

            sessions = await connection.fetch(
                "SELECT session_id_hash, discord_access_token FROM web_sessions FOR UPDATE"
            )
            for row in sessions:
                encrypted_token = encrypt_credential(row["discord_access_token"])
                if encrypted_token != row["discord_access_token"]:
                    await connection.execute(
                        "UPDATE web_sessions SET discord_access_token = $2 "
                        "WHERE session_id_hash = $1",
                        row["session_id_hash"],
                        encrypted_token,
                    )
                    changed += 1
    finally:
        await connection.close()
    print(f"Encrypted {changed} credential row(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fishie operational commands")
    parser.add_argument("command", choices=("migrate", "encrypt-credentials"))
    arguments = parser.parse_args()
    if arguments.command == "migrate":
        asyncio.run(migrate_command())
    elif arguments.command == "encrypt-credentials":
        asyncio.run(encrypt_credentials_command())

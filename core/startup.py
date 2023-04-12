import json
from typing import Any

import asyncpg


async def create_pool(connection_url: str) -> "asyncpg.Pool[asyncpg.Record]":
    def _encode_jsonb(value: Any) -> Any:
        return json.dumps(value)

    def _decode_jsonb(value: Any) -> Any:
        return json.loads(value)

    async def init(con: "asyncpg.Connection[Any]"):
        await con.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=_encode_jsonb,
            decoder=_decode_jsonb,
            format="text",
        )

    connection = await asyncpg.create_pool(connection_url, init=init)

    if connection is None:
        raise Exception("Failed to connect to database")

    return connection

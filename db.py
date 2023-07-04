import asyncio

import asyncpg
import tomllib


async def main():
    with open("config.toml", "rb") as fileObj:
        config = dict(**tomllib.load(fileObj))

    conn = await asyncpg.create_pool(config["databases"]["testing_psql"])
    if conn is None:
        raise asyncpg.ConnectionFailureError("Could not connect to database")

    with open("schema.sql") as f:
        await conn.execute(f.read())

    await conn.close()

    print("Done")


asyncio.run(main())

import asyncio
import asyncpg
import toml


async def main():
    config = toml.load("config.toml")
    conn = await asyncpg.create_pool(config["databases"]["testing_psql"])
    if conn is None:
        raise asyncpg.ConnectionFailureError("Could not connect to database")

    with open("schema.sql") as f:
        await conn.execute(f.read())

    await conn.close()

    print("Done")


asyncio.run(main())

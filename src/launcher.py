import argparse
import asyncio
import logging.handlers
import os
import sys
import tomllib
from pathlib import Path
from collections.abc import Callable
from typing import Any

import aiohttp
import uvicorn
from discord import gateway

from api import app as api_app
from api import init as api_init
from core import BotInstance, Fishie, normalize_bot_instance
from core.bot import _redact_error_text
from utils import (
    Config,
    base_header,
    create_pool,
    identify_mobile,
    validate_credential_key,
)
from utils.paths import REPOSITORY_ROOT
from utils.network import PublicTCPConnector
from utils.credentials import configured_secrets

gateway.DiscordWebSocket.identify = identify_mobile


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.redact: Callable[[str], str] = lambda text: text

    def format(self, record: logging.LogRecord) -> str:
        return _redact_error_text(super().format(record), self.redact)


def _env_bool(name: str, default: bool = True) -> bool:
    """Parse a boolean environment switch with a useful error message."""

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _resolve_instance(testing: bool, instance: str | None = None) -> BotInstance:
    """Resolve the process role while retaining ``--testing`` compatibility."""

    return normalize_bot_instance(instance, testing=testing)


def _token_for_instance(config: Config, instance: BotInstance) -> str:
    """Read the token matching the selected application without logging it."""

    env_names = {
        "legacy": "DISCORD_BOT_TOKEN",
        "new": "DISCORD_NEW_BOT_TOKEN",
        "testing": "DISCORD_TESTING_BOT_TOKEN",
    }
    config_names = {
        "legacy": "bot",
        "new": "new_bot",
        "testing": "testing_bot",
    }
    key = config_names[instance]
    token = os.getenv(env_names[instance])
    if token:
        return token
    configured = config.get("tokens", {}).get(key)
    return str(configured or "")


def _api_port(instance: BotInstance) -> int:
    """Return an instance-specific API port.

    ``FISHIE_API_PORT`` remains the highest-priority override for existing
    deployments.  Otherwise a role-specific variable is used, with the old
    8001 port retained for legacy/testing and 8002 reserved for the new bot.
    """

    default_ports = {"legacy": 8001, "new": 8002, "testing": 8001}
    value = os.getenv("FISHIE_API_PORT") or os.getenv(
        f"FISHIE_API_PORT_{instance.upper()}"
    )
    if value is None or not value.strip():
        return default_ports[instance]
    try:
        port = int(value)
    except ValueError as error:
        raise RuntimeError("FISHIE_API_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("FISHIE_API_PORT must be between 1 and 65535")
    return port


async def start(
    testing: bool = False,
    *,
    instance: str | None = None,
):
    selected_instance = _resolve_instance(testing, instance)
    logger = logging.getLogger("fishie")
    logger.setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)

    default_log_name = (
        "discord-new.log" if selected_instance == "new" else "discord.log"
    )
    log_path = Path(
        os.getenv("FISHIE_LOG_PATH", str(REPOSITORY_ROOT / default_log_name))
    )
    handlers = [
        logging.handlers.RotatingFileHandler(
            filename=log_path,
            encoding="utf-8",
            maxBytes=32 * 1024 * 1024,  # 32 MiB
            backupCount=5,  # Rotate through 5 files
        ),
        logging.StreamHandler(sys.stdout),
    ]

    formatter = RedactingFormatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{"
    )

    logger.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    logging.getLogger().handlers = handlers

    config_path = os.getenv("FISHIE_CONFIG", str(REPOSITORY_ROOT / "config.toml"))
    with open(config_path, "rb") as fileObj:
        config: Config = Config(**tomllib.load(fileObj))

    secrets = sorted(configured_secrets(config), key=len, reverse=True)

    def redact_config(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    formatter.redact = redact_config

    use_testing_database = selected_instance == "testing"
    api_enabled = _env_bool("FISHIE_API_ENABLED", True)
    api_port = _api_port(selected_instance) if api_enabled else None
    database_url = (
        os.getenv("DATABASE_URL")
        or config["databases"]["psql_testing" if use_testing_database else "psql"]
    )
    token = _token_for_instance(config, selected_instance)
    if not database_url:
        raise RuntimeError("A PostgreSQL URL is required")
    if not token:
        raise RuntimeError("A Discord bot token is required")
    validate_credential_key()

    jsk_envs = [
        "JISHAKU_RETAIN",
        "JISHAKU_HIDE",
        "JISHAKU_NO_DM_TRACEBACK",
        "JISHAKU_NO_UNDERSCORE",
        "JISHAKU_FORCE_PAGINATOR",
    ]

    for env in jsk_envs:
        os.environ[env] = "True"

    pool = await create_pool(database_url)
    logger.info("Connected to Postgres")

    timeout = aiohttp.ClientTimeout(total=30)
    async with (
        aiohttp.ClientSession(
            headers=base_header, timeout=timeout, connector=PublicTCPConnector()
        ) as session,
        Fishie(
            config=config,
            logger=logger,
            pool=pool,
            session=session,
            testing=use_testing_database,
            instance=selected_instance,
        ) as bot,
    ):
        formatter.redact = bot.redact
        api_server: uvicorn.Server | None = None
        api_task: asyncio.Task[object] | None = None
        if api_enabled:
            api_init(bot)
            api_cfg = uvicorn.Config(
                api_app,
                host=os.getenv("FISHIE_API_HOST", "127.0.0.1"),
                port=api_port or 8001,
                log_level=os.getenv("FISHIE_API_LOG_LEVEL", "warning"),
            )
            api_server = uvicorn.Server(api_cfg)
            api_task = asyncio.create_task(api_server.serve())
            logger.info(
                "Fishie API server started (instance=%s, port=%s)",
                selected_instance,
                api_cfg.port,
            )
        bot_task = asyncio.create_task(bot.start(token))
        tasks: set[asyncio.Task[object]] = {bot_task}
        if api_task is not None:
            tasks.add(api_task)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        try:
            for task in done:
                task.result()
        finally:
            if api_server is not None:
                api_server.should_exit = True
            if api_task is not None and not api_task.done():
                try:
                    await asyncio.wait_for(api_task, timeout=10)
                except asyncio.TimeoutError:
                    api_task.cancel()
            if not bot.is_closed():
                await bot.close()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testing", "-t", action="store_true")
    parser.add_argument(
        "--instance",
        choices=("legacy", "new", "testing"),
        help="Bot application to run (also configurable with FISHIE_BOT_INSTANCE)",
    )

    parsed = parser.parse_args()

    asyncio.run(start(parsed.testing, instance=parsed.instance))

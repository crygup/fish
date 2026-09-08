# Fishie

A Discord bot with media tools, music, games, and notifications.

## Setup

1. Copy `examples/config.toml` to `config.toml` and `.env.example` to `.env`.
2. Fill in the bot token, database settings, and credential key. Add API keys for the features you use.
3. Start the bot:

```bash
docker compose up -d --build
```

Docker sets up the database automatically. View logs with `docker compose logs -f fishie`.

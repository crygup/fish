# Fishie setup

This repository keeps the bot source in `src`, database migrations in
`migrations`, and the Docker setup in the repository root.

Copy `examples/config.toml` to `config.toml` and fill in the values you plan to
use.

Most third-party keys are optional until you use their related commands:

- Last.fm keys come from [Last.fm API accounts](https://www.last.fm/api/account/create)
- Spotify keys come from the [Spotify developer dashboard](https://developer.spotify.com/dashboard)
- AniList keys come from the developer section in AniList settings
- Twitch keys come from the [Twitch developer console](https://dev.twitch.tv/console)
- Google keys come from the [Google Cloud console](https://console.cloud.google.com/)
- Steam keys come from the [Steam Web API key page](https://steamcommunity.com/dev/apikey)

Create `.env` from `.env.example`.

## Run with Docker

The regular Compose file starts PostgreSQL, applies migrations, and then starts
Fishie:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f fishie
```

Restart the bot with:

```bash
docker compose restart fishie
```

Stop everything with:

```bash
docker compose down
```

The production Compose file expects an existing PostgreSQL server and the
credential key file used on the host. Set `FISHIE_UID` and `FISHIE_GID` to the
account that owns the project files, apply migrations, then start it:

```bash
sudo docker compose -f compose.production.yaml run --rm --no-deps fishie python manage.py migrate
sudo docker compose -f compose.production.yaml up -d --build fishie
```

The bot and API share one process. The API listens on port `8001` by default
and `/health/ready` reports whether Discord and PostgreSQL are ready.

## Run without Docker

Create a virtual environment, install the locked requirements, apply the
migrations, and launch from `src`:

```bash
python3.13 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cd src
../venv/bin/python manage.py migrate
../venv/bin/python launcher.py
```

FFmpeg, Chromium dependencies for Playwright, PostgreSQL, `config.toml`, and the
credential key must already be available when running this way.

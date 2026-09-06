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

## Production

Production runs through Docker Compose. Both the retiring `fishie` service and
replacement `fishie-new` service belong to `compose.production.yaml`. Only the
replacement serves the website API, on loopback port 8001.

The authoritative deployment guide for the two repositories is
[website/DEPLOYMENT.md](../website/DEPLOYMENT.md) in the sibling website checkout.
It covers backups, migrations, both bots, all website services, and Nginx.
Do not start the retired `fish.service` or `avatar-lookup.service` units.

## Local development without Docker

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

# Fishie

Fishie is a Discord bot and FastAPI dashboard backend. It includes user and guild history, moderation, reminders, media tools, third-party account integrations, and Twitch EventSub announcements.

## Requirements

- Python 3.13
- PostgreSQL
- FFmpeg and `rsvg-convert`
- Chromium installed through Playwright
- A Discord application with the privileged intents used by the bot

## Local setup

1. Copy `examples/config.toml` to `config.toml` and fill in the required Discord IDs, API credentials, and webhook URLs. The file is ignored by Git.
2. Create a virtual environment and install `requirements-dev.txt`.
3. Generate `FISHIE_CREDENTIAL_KEY` as shown in `.env.example`. Keep this key stable and backed up: losing it makes stored OAuth credentials unreadable.
4. Set `DATABASE_URL` or configure the appropriate PostgreSQL URL in `config.toml`.
5. Run `python manage.py migrate` as a release step.
6. Existing installations should run `python manage.py encrypt-credentials` once after backing up the database and encryption key.
7. Run `python launcher.py` (or `python launcher.py --testing`).

The application deliberately refuses to start when migrations are pending. Schema changes are never applied implicitly by a bot process.

## Containers

Copy `.env.example` to `.env`, supply the secrets, create `config.toml`, then run `docker compose up --build`. Compose runs the migration as a one-shot service before starting the bot. The API is exposed only on loopback; put a TLS reverse proxy in front of it.

## Security and operations

- Never commit `config.toml`, `.env`, browser cookies, database dumps, or the Fernet key.
- Apply outbound firewall rules that prevent the bot and browser from reaching private networks. Application URL checks reduce SSRF risk but are not a substitute for network egress controls.
- Configure the reverse proxy to cap request bodies at 2 MiB, rate-limit public `/send-message`, `/stats`, and OAuth endpoints, and pass forwarding headers only from proxy addresses listed in `FISHIE_TRUSTED_PROXIES`.
- Back up PostgreSQL and `FISHIE_CREDENTIAL_KEY` together. Test restores periodically.
- Use `/health/live` for process liveness and `/health/ready` for traffic readiness.
- Rotate Discord, OAuth, Twitch, webhook, database, and Fernet credentials after suspected exposure.

## Database migrations

`schema.sql` is migration 1 for compatibility with existing installations. Later migrations live in `migrations/`. Applied checksums are recorded in `schema_migrations`; never edit an applied migration. Add a new numbered migration instead.

## Tests

Run `ruff check .`, `pytest`, and `python -m compileall -q .`. CI also audits Python dependencies and builds the production container.

Media integration tests need FFmpeg, `rsvg-convert`, and Chromium. External Discord, Twitch, Spotify, Last.fm, Steam, and AniList calls should be tested in a non-production application before deployment.

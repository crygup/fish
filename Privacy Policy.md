# Privacy Policy for Fishie

Effective date: July 20, 2026

This policy explains the data Fishie stores when the Discord bot and its dashboard are used. It should be read together with the Terms of Service.

## Data Fishie processes

Depending on enabled server features and individual choices, Fishie may store:

- Discord user, guild, channel, message, role, and webhook identifiers;
- username, display-name, discriminator, nickname, server-tags, avatar, guild-name, guild-icon, membership, status, command-use, XP, reaction, pinboard, and Pokémon-solve history;
- reminders, highlight words, prefixes, moderation/logger settings, honeypot settings, opt-outs, and game state;
- account identifiers for Last.fm, Spotify, Steam, AniList, Roblox, Genshin, and Letterboxd;
- OAuth access, refresh, or session credentials needed for explicitly linked services;
- Twitch follow configuration, EventSub delivery records, and announcement delivery state;
- dashboard session identifiers, expiry times, and security metadata such as rate-limit IP addresses.

Fishie no longer writes raw message content to command logs. Reminder text, user-configured templates, highlight words, and content explicitly submitted to a feature are still stored because those features require it.

## Purpose and legal basis

Data is used to provide requested bot and dashboard features, remember server configuration, prevent abuse, recover background work after failures, diagnose operational errors, and honor opt-out or deletion requests. Server administrators decide which guild-level features to enable. Users explicitly authorize linked third-party accounts.

## Credentials and security

Opaque dashboard session IDs are stored as hashes. Stored Discord access tokens and supported third-party OAuth credentials are encrypted at rest with an application key kept separately from the database. Access is restricted to operators who need it. Network encryption, host access controls, backups, dependency updates, and monitoring should also be used by the deployment operator. No system can guarantee absolute security.

## Third parties

Fishie sends data to Discord and, when a corresponding feature is used, may communicate with Twitch, Spotify, Last.fm, Steam, AniList, Google, Roblox, Letterboxd, media hosts, or other URLs a user requests Fishie to process. Those services process data under their own policies. Fishie does not sell personal data.

## Retention

Dashboard and OAuth state records expire automatically. Completed webhook inbox records and temporary media files are removed by cleanup jobs. Other history and configuration remains until it is deleted, the relevant guild removes it, or an operator applies a documented retention policy. Backups may retain deleted data until they age out and are protected from normal application access.

## Choices, access, and deletion

The dashboard and bot settings provide per-category opt-outs for supported history including avatars, names, nicknames, primary server tags, joins, status, XP, commands, Pokémon solves, and corn reactions. An opt-out prevents new collection for that category; it does not erase existing rows.

Avatar, username, display-name, and legacy discriminator histories are publicly searchable through the dashboard. Primary server-tag history is publicly searchable through the bot. Account settings, linked services, and deletion controls remain restricted to the authenticated account owner.

Authenticated users can inspect and delete their own stored data. A full user deletion removes account links, sessions, settings, histories, reminders, game state, subscriptions, and references where the user is an actor or target. Server managers can delete guild-owned history and configuration. Some security or backup records may persist temporarily where needed to prevent abuse or complete backup expiry.

To request help with access, correction, deletion, or a failed self-service request, contact the Fishie developers through the bot's support Discord. The operator may ask for proof that the requester controls the relevant Discord account or server.

## Children and regional rights

Users must meet Discord's minimum age and any higher age required in their country. Depending on location, users may have additional rights such as objection, restriction, portability, or complaint to a supervisory authority; contact the operator to exercise them.

## Changes

Material changes will be posted through the bot, dashboard, repository, or support server. The effective date above identifies the current version.

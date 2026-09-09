"""Recoverable privacy deletion, shared by bot commands and the website."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg

from .privacy import erase_user

RESTORE_NOTICE = "Hidden now. You have 31 days to restore this data with `settings restore` or the website."
PRIVACY_LOCK = 0x4649534844454C


def _json(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


@asynccontextmanager
async def deletion(
    pool: Any, subject_id: int, *, scope: str = "user", full: bool = False
):
    """Archive only explicit privacy deletes, atomically with their existing SQL."""
    if scope not in {"user", "guild"}:
        raise ValueError("Invalid deletion scope")
    async with pool.acquire() as conn:
        async with conn.transaction():
            # ponytail: global lock for rare privacy operations; use per-subject
            # locks if concurrent deletion throughput ever matters.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", PRIVACY_LOCK)
            await conn.execute("SELECT privacy_expire_deletions()")
            batch = await conn.fetchval(
                "INSERT INTO privacy_deletions(scope, subject_id, full_account) VALUES($1,$2,$3) RETURNING id",
                scope,
                subject_id,
                full,
            )
            if full:
                await conn.execute(
                    """INSERT INTO privacy_deletion_holds(row_id, deletion_id)
                       SELECT id, $1 FROM privacy_deleted_rows
                       WHERE privacy_row_mentions(before_data, $2, $3)
                       ON CONFLICT DO NOTHING""",
                    batch,
                    scope,
                    subject_id,
                )
            await conn.execute(
                "SELECT set_config('fishie.deletion_id', $1, true)", str(batch)
            )
            yield conn
            await conn.execute("SELECT set_config('fishie.deletion_id', '', true)")
            await conn.execute(
                "SELECT pg_notify('fishie_privacy', $1)",
                json.dumps(
                    {"scope": scope, "id": subject_id, "full": full, "restored": False}
                ),
            )


async def delete_account(bot: Any, user_id: int) -> int:
    from utils.vars import remove_user_badge

    await bot.currency._flush_click_rewards(user_id)
    async with deletion(bot.pool, user_id, full=True) as conn:
        # Do not remove the backing record of an in-progress financial action.
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM currency_wagers WHERE user_id=$1 AND status='open')",
            user_id,
        ):
            raise ValueError(
                "Finish your active wagered game before deleting your account."
            )
        await archive_badge_document(conn, user_id)
        count = await erase_user(conn, user_id)
    remove_user_badge(user_id)
    await invalidate(bot, {"scope": "user", "id": user_id, "full": True})
    return count


async def archive_badge_document(conn: Any, user_id: int) -> None:
    from utils.vars import refresh_user_badges

    document = refresh_user_badges().get("users", {}).get(str(user_id))
    if document:
        await conn.execute(
            """INSERT INTO user_badge_documents VALUES($1,$2::jsonb)
               ON CONFLICT(user_id) DO UPDATE SET document=EXCLUDED.document""",
            user_id,
            json.dumps({"badges": document}),
        )
    await conn.execute("DELETE FROM user_badge_documents WHERE user_id=$1", user_id)


async def pending_status(
    pool: Any, subject_id: int, scope: str = "user"
) -> dict[str, Any]:
    row = await pool.fetchrow(
        """SELECT COUNT(DISTINCT r.id) AS records, MIN(d.expires_at) AS next_expiry
           FROM privacy_deletions d JOIN privacy_deleted_rows r ON r.deletion_id=d.id
             OR EXISTS(SELECT 1 FROM privacy_deletion_holds h WHERE h.row_id=r.id AND h.deletion_id=d.id)
           WHERE d.scope=$1 AND d.subject_id=$2 AND d.expires_at>now()""",
        scope,
        subject_id,
    )
    return {"records": row["records"], "next_expiry": row["next_expiry"]}


async def _restore_row(conn: Any, row: Any) -> bool:
    table = row["table_name"]
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError("Invalid archived table")
    # Only tables carrying the privacy trigger can be restored, never sessions
    # or SQL identifiers supplied by a website request.
    columns = await conn.fetch(
        """SELECT a.attname FROM pg_attribute a
           WHERE a.attrelid=to_regclass($1) AND a.attnum>0 AND NOT a.attisdropped
             AND a.attgenerated=''
             AND EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=a.attrelid AND t.tgname='privacy_archive')
           ORDER BY a.attnum""",
        "public." + table,
    )
    if not columns:
        return False
    names = [c["attname"] for c in columns]
    data = _json(row["before_data"])
    key = _json(row["identity_data"])
    if row["after_data"] is not None:
        after = _json(row["after_data"])
        changed = [name for name in names if data.get(name) != after.get(name)]
        if not changed:
            return True
        # Restore anonymized fields only if they still have the deletion value.
        sets = ", ".join(f'"{name}"=old."{name}"' for name in changed)
        guards = " AND ".join(
            f'target."{name}" IS NOT DISTINCT FROM expected."{name}"'
            for name in changed
        )
        result = await conn.execute(
            f"""UPDATE public."{table}" target SET {sets}
                FROM jsonb_populate_record(NULL::public."{table}", $1::jsonb) old,
                     jsonb_populate_record(NULL::public."{table}", $2::jsonb) expected
                WHERE to_jsonb(target) @> $3::jsonb AND {guards}""",
            json.dumps(data),
            json.dumps(after),
            json.dumps(key),
        )
        return result != "UPDATE 0"

    # Expired one-shot work must not be executed again after a restore.
    if table == "social_marriages" and await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM social_marriage_members
           WHERE user_id=ANY($1::bigint[]) AND marriage_id<>$2)""",
        [data["proposer_id"], data["recipient_id"]],
        data["id"],
    ):
        return False
    if table == "social_marriage_members" and await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM social_marriage_members WHERE user_id=$1 AND marriage_id<>$2)",
        data["user_id"],
        data["marriage_id"],
    ):
        return False
    if table == "lottery_tickets" and not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM lottery_rounds WHERE round_start=$1 AND status='open')",
        datetime.fromisoformat(data["round_start"]),
    ):
        return False
    if (
        table == "reminders"
        and data.get("expires")
        and await conn.fetchval(
            "SELECT $1::timestamptz <= now()", datetime.fromisoformat(data["expires"])
        )
    ):
        return False
    if table in {"user_colors", "user_titles", "user_racing_emojis", "user_rings"}:
        field = "equipped_count" if table == "user_rings" else "equipped"
        if field in data and await conn.fetchval(
            f'SELECT EXISTS(SELECT 1 FROM public."{table}" WHERE user_id=$1 AND "{field}"=$2)',
            data["user_id"],
            1 if field == "equipped_count" else True,
        ):
            data[field] = 0 if field == "equipped_count" else False
    conflict = "DO NOTHING"
    if table == "currency_wallets":
        conflict = "ON CONSTRAINT currency_wallets_pkey DO UPDATE SET balance=currency_wallets.balance+EXCLUDED.balance"
    elif table == "user_rings":
        conflict = "ON CONSTRAINT user_rings_pkey DO UPDATE SET quantity=user_rings.quantity+EXCLUDED.quantity"
    elif table == "currency_daily_rewards":
        conflict = "ON CONSTRAINT currency_daily_rewards_pkey DO UPDATE SET amount=currency_daily_rewards.amount+EXCLUDED.amount"
    elif table == "accounts":
        fields = ", ".join(
            f'"{n}"=COALESCE(accounts."{n}", EXCLUDED."{n}")'
            for n in names
            if n != "user_id"
        )
        conflict = "ON CONSTRAINT accounts_pkey DO UPDATE SET " + fields
    elif table == "currency_gambling_stats":
        sums = ", ".join(
            f"{n}=currency_gambling_stats.{n}+EXCLUDED.{n}"
            for n in ("total_wagered", "total_earned", "total_lost", "wins", "losses")
        )
        conflict = "ON CONSTRAINT currency_gambling_stats_pkey DO UPDATE SET " + sums
    elif table == "message_xp":
        conflict = "ON CONSTRAINT message_xp_pkey DO UPDATE SET xp=COALESCE(message_xp.xp,0)+COALESCE(EXCLUDED.xp,0), messages=COALESCE(message_xp.messages,0)+COALESCE(EXCLUDED.messages,0)"
    quoted = ", ".join(f'"{name}"' for name in names)
    result = await conn.execute(
        f"""INSERT INTO public."{table}" ({quoted}) OVERRIDING SYSTEM VALUE
            SELECT {quoted} FROM jsonb_populate_record(NULL::public."{table}", $1::jsonb)
            ON CONFLICT {conflict}""",
        json.dumps(data),
    )
    # A natural-key duplicate represents newer state, which takes precedence.
    return result.startswith("INSERT")


async def restore(pool: Any, subject_id: int, *, scope: str = "user") -> dict[str, int]:
    restored = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", PRIVACY_LOCK)
            await conn.execute("SELECT privacy_expire_deletions()")
            batches = await conn.fetch(
                """UPDATE privacy_deletions SET restored_at=now()
                   WHERE scope=$1 AND subject_id=$2 AND expires_at>now() RETURNING id, full_account""",
                scope,
                subject_id,
            )
            ids = [b["id"] for b in batches]
            released = await conn.fetch(
                "DELETE FROM privacy_deletion_holds WHERE deletion_id=ANY($1::bigint[]) RETURNING row_id",
                ids,
            )
            rows = await conn.fetch(
                """SELECT r.* FROM privacy_deleted_rows r
                   WHERE (r.deletion_id=ANY($1::bigint[]) OR r.deletion_id IN (
                       SELECT deletion_id FROM privacy_deleted_rows WHERE id=ANY($2::bigint[])))
                     AND NOT EXISTS(SELECT 1 FROM privacy_deletion_holds h WHERE h.row_id=r.id)
                   ORDER BY r.id DESC""",
                ids,
                [r["row_id"] for r in released],
            )
            # Retry child rows after their parents; savepoints keep one stale FK
            # or changed catalog item from preventing unrelated history recovery.
            while rows:
                deferred = []
                for row in rows:
                    try:
                        async with conn.transaction():
                            if not await _restore_row(conn, row):
                                deferred.append(row)
                                continue
                            await conn.execute(
                                "DELETE FROM privacy_deleted_rows WHERE id=$1",
                                row["id"],
                            )
                            restored += 1
                    except asyncpg.IntegrityConstraintViolationError:
                        deferred.append(row)
                if len(deferred) == len(rows):
                    break
                rows = deferred
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM privacy_deleted_rows WHERE deletion_id=ANY($1::bigint[])",
                ids,
            )
            await conn.execute(
                "SELECT pg_notify('fishie_privacy', $1)",
                json.dumps(
                    {
                        "scope": scope,
                        "id": subject_id,
                        "full": any(b["full_account"] for b in batches),
                        "restored": True,
                    }
                ),
            )
    return {"restored": restored, "pending": pending}


async def invalidate(bot: Any, event: dict[str, Any]) -> None:
    subject = int(event["id"])
    if event["scope"] == "user":
        from utils.vars import (
            refresh_user_badges,
            save_user_badges_document,
            remove_user_badge,
        )

        latest = await bot.pool.fetchval(
            """SELECT restored_at IS NOT NULL FROM privacy_deletions
               WHERE scope='user' AND subject_id=$1 AND full_account
               ORDER BY GREATEST(created_at, COALESCE(restored_at, created_at)) DESC LIMIT 1""",
            subject,
        )
        document = await bot.pool.fetchval(
            "SELECT document FROM user_badge_documents WHERE user_id=$1", subject
        )
        if document:
            catalog = refresh_user_badges()
            catalog.setdefault("users", {}).setdefault(
                str(subject), _json(document)["badges"]
            )
            save_user_badges_document(catalog)
        elif latest is False or await bot.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM privacy_deleted_rows r
               JOIN privacy_deletions d ON d.id=r.deletion_id
               WHERE d.scope='user' AND d.subject_id=$1 AND d.expires_at>now()
                 AND r.table_name='user_badge_documents')""",
            subject,
        ):
            remove_user_badge(subject)
        tools = bot.get_cog("Tools")
        if tools:
            for name in ("_highlight_cache", "_timezone_cache"):
                cache = getattr(tools, name, None)
                if cache is not None:
                    cache.clear()
            if hasattr(tools, "_have_data"):
                tools._have_data.set()
    # Full erasure affects many small configuration caches; reuse the startup
    # loader rather than maintain a second list that can miss new features.
    if not hasattr(bot, "_privacy_cache_lock"):
        bot._privacy_cache_lock = asyncio.Lock()
    async with bot._privacy_cache_lock:
        await bot.populate_cache()


async def privacy_listener(bot: Any) -> None:
    """Invalidate both instances after committed deletion/restore; prune hourly."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def received(_conn, _pid, _channel, payload):
        queue.put_nowait(json.loads(payload))

    while True:
        try:
            async with bot.pool.acquire() as conn:
                await conn.add_listener("fishie_privacy", received)
                try:
                    # Reconcile the file mirror if a process stopped after the
                    # database commit but before invalidating its badge cache.
                    subjects = await conn.fetch(
                        """SELECT subject_id FROM privacy_deletions
                           WHERE scope='user' AND full_account
                           UNION SELECT user_id FROM user_badge_documents
                           UNION SELECT d.subject_id FROM privacy_deletions d
                           JOIN privacy_deleted_rows r ON r.deletion_id=d.id
                           WHERE d.scope='user' AND r.table_name='user_badge_documents'"""
                    )
                    for subject in subjects:
                        await invalidate(bot, {"scope": "user", "id": subject["subject_id"], "full": True})
                    await conn.execute("SELECT privacy_expire_deletions()")
                    next_cleanup = asyncio.get_running_loop().time() + 3600
                    while True:
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=30)
                            await invalidate(bot, event)
                        except asyncio.TimeoutError:
                            await conn.execute("SELECT 1")
                        if asyncio.get_running_loop().time() >= next_cleanup:
                            await conn.execute("SELECT privacy_expire_deletions()")
                            next_cleanup = asyncio.get_running_loop().time() + 3600
                finally:
                    await conn.remove_listener("fishie_privacy", received)
        except Exception:
            bot.logger.exception("Privacy cache listener disconnected")
            await asyncio.sleep(5)

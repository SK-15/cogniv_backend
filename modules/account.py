"""
Account data export and deletion (DPDP / GDPR data-subject requests).

Deletion keeps two things on purpose:
  * public.purchases — payment records are retained for tax/accounting.
  * the neon_auth."user" row, anonymized — purchases.user_id references it
    with ON DELETE CASCADE, so deleting the row would delete the purchases.
    Instead its email/name/image are scrubbed and its login methods
    (neon_auth.account) and sessions are deleted, so nobody can sign in to it.
"""

from modules.database import get_pool


async def export_user_data(user_id: str) -> dict | None:
    """Everything stored about the user, or None if the account doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        account = await conn.fetchrow(
            'SELECT id, email, name, "createdAt" AS created_at FROM neon_auth."user" WHERE id = $1::uuid',
            user_id,
        )
        if not account:
            return None
        profiles = await conn.fetch(
            "SELECT * FROM public.interview_profiles WHERE user_id = $1::uuid ORDER BY created_at",
            user_id,
        )
        sessions = await conn.fetch(
            "SELECT * FROM public.interview_sessions WHERE user_id = $1::uuid ORDER BY started_at",
            user_id,
        )
        responses = await conn.fetch(
            """
            SELECT r.*
            FROM public.interview_responses r
            JOIN public.interview_sessions s ON s.id = r.session_id
            WHERE s.user_id = $1::uuid
            ORDER BY r.created_at
            """,
            user_id,
        )
        purchases = await conn.fetch(
            """
            SELECT plan_id, sessions, amount, order_id, payment_id, created_at
            FROM public.purchases
            WHERE user_id = $1::uuid
            ORDER BY created_at
            """,
            user_id,
        )
        subscription = await conn.fetchrow(
            "SELECT * FROM public.subscriptions WHERE user_id = $1::uuid",
            user_id,
        )
        free_usage = await conn.fetchrow(
            "SELECT * FROM public.free_usage WHERE user_id = $1",
            user_id,
        )
        site_events = await conn.fetch(
            "SELECT * FROM public.site_events WHERE user_id = $1::uuid ORDER BY created_at",
            user_id,
        )
        # Legacy web chat (threads / chat_history) and per-day usage counters.
        threads = await conn.fetch(
            "SELECT * FROM public.threads WHERE user_id = $1::uuid ORDER BY created_at",
            user_id,
        )
        chat_history = await conn.fetch(
            """
            SELECT c.*
            FROM public.chat_history c
            JOIN public.threads t ON t.id = c.thread_id
            WHERE t.user_id = $1::uuid
            ORDER BY c.created_at
            """,
            user_id,
        )
        usage_daily = await conn.fetch(
            "SELECT * FROM public.usage_daily WHERE user_id = $1::uuid ORDER BY day",
            user_id,
        )
    return {
        "account": dict(account),
        "interview_profiles": [dict(r) for r in profiles],
        "interview_sessions": [dict(r) for r in sessions],
        "interview_responses": [dict(r) for r in responses],
        "purchases": [dict(r) for r in purchases],
        "subscription": dict(subscription) if subscription else None,
        "free_usage": dict(free_usage) if free_usage else None,
        "site_events": [dict(r) for r in site_events],
        "chat_threads": [dict(r) for r in threads],
        "chat_history": [dict(r) for r in chat_history],
        "usage_daily": [dict(r) for r in usage_daily],
    }


async def delete_user_data(user_id: str) -> None:
    """Delete the user's data and anonymize the account, in one transaction."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                DELETE FROM public.interview_responses
                WHERE session_id IN (
                    SELECT id FROM public.interview_sessions WHERE user_id = $1::uuid
                )
                """,
                user_id,
            )
            await conn.execute("DELETE FROM public.interview_sessions WHERE user_id = $1::uuid", user_id)
            await conn.execute("DELETE FROM public.interview_profiles WHERE user_id = $1::uuid", user_id)
            await conn.execute(
                """
                DELETE FROM public.chat_history
                WHERE thread_id IN (SELECT id FROM public.threads WHERE user_id = $1::uuid)
                """,
                user_id,
            )
            await conn.execute("DELETE FROM public.threads WHERE user_id = $1::uuid", user_id)
            await conn.execute("DELETE FROM public.usage_daily WHERE user_id = $1::uuid", user_id)
            await conn.execute("DELETE FROM public.subscriptions WHERE user_id = $1::uuid", user_id)
            await conn.execute("DELETE FROM public.free_usage WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM public.refresh_tokens WHERE user_id = $1::uuid", user_id)
            # Analytics rows stay for aggregate counts but lose the link to the person.
            await conn.execute(
                "UPDATE public.site_events SET user_id = NULL, ip = NULL WHERE user_id = $1::uuid",
                user_id,
            )
            # Neon Auth: drop login methods (password hash, Google link), live
            # sessions and pending reset/verification tokens, then scrub the user row.
            await conn.execute('DELETE FROM neon_auth.session WHERE "userId" = $1::uuid', user_id)
            await conn.execute('DELETE FROM neon_auth.account WHERE "userId" = $1::uuid', user_id)
            await conn.execute("DELETE FROM neon_auth.verification WHERE value = $1", user_id)
            await conn.execute(
                """
                UPDATE neon_auth."user"
                SET email = 'deleted-' || id::text || '@deleted.cogniv.invalid',
                    name = 'Deleted user',
                    image = NULL,
                    "emailVerified" = false,
                    banned = true,
                    "banReason" = 'account deleted',
                    "updatedAt" = now()
                WHERE id = $1::uuid
                """,
                user_id,
            )

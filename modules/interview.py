from uuid import UUID

from modules.database import get_pool


async def get_interview_profiles(
    user_id: str,
    *,
    include_resume: bool = True,
) -> list[dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if include_resume:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, name, role, resume_text, is_default, sort_order,
                           created_at, updated_at
                    FROM public.interview_profiles
                    WHERE user_id = $1::uuid
                    ORDER BY is_default DESC, sort_order ASC, created_at DESC
                    """,
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, name, role, is_default, sort_order,
                           created_at, updated_at
                    FROM public.interview_profiles
                    WHERE user_id = $1::uuid
                    ORDER BY is_default DESC, sort_order ASC, created_at DESC
                    """,
                    user_id,
                )
        out = [dict(r) for r in rows]
        if not include_resume:
            for d in out:
                d["resume_text"] = None
        return out
    except Exception as e:
        print(f"Error fetching interview profiles: {e}")
        return []


async def list_interview_sessions(
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """
    Past interview sessions for the user, newest first.
    Joins profile name for dashboard display.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    s.id,
                    s.user_id,
                    s.profile_id,
                    s.started_at,
                    s.ended_at,
                    s.job_title,
                    s.job_description,
                    p.name AS profile_name
                FROM public.interview_sessions s
                LEFT JOIN public.interview_profiles p ON p.id = s.profile_id
                WHERE s.user_id = $1::uuid
                ORDER BY s.started_at DESC NULLS LAST, s.id DESC
                LIMIT $2 OFFSET $3
                """,
                user_id,
                limit,
                offset,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"Error listing interview sessions: {e}")
        return []


async def save_interview_profile(
    user_id: str,
    job_role: str,
    name: str,
    resume_text: str,
    is_default: bool,
) -> dict | None:
    """
    Maps API job_role -> DB column `role`.
    `resume_text` stores extracted plain text from the uploaded resume file.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if is_default:
                    await conn.execute(
                        """
                        UPDATE public.interview_profiles
                        SET is_default = false
                        WHERE user_id = $1::uuid
                        """,
                        user_id,
                    )
                row = await conn.fetchrow(
                    """
                    INSERT INTO public.interview_profiles
                        (user_id, name, role, resume_text, is_default)
                    VALUES ($1::uuid, $2, $3, $4, $5)
                    RETURNING id, user_id, name, role, resume_text, is_default,
                              sort_order, created_at, updated_at
                    """,
                    user_id,
                    name,
                    job_role,
                    resume_text,
                    is_default,
                )
        return dict(row) if row else None
    except Exception as e:
        print(f"Error saving interview profile: {e}")
        return None


async def create_interview_session(
    user_id: str,
    profile_id: UUID,
    job_title: str,
    job_description: str,
) -> UUID | None:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.interview_sessions
                    (user_id, profile_id, job_title, job_description)
                SELECT $1::uuid, $2, $3, $4
                FROM public.interview_profiles p
                WHERE p.id = $2 AND p.user_id = $1::uuid
                RETURNING id
                """,
                user_id,
                profile_id,
                job_title,
                job_description,
            )
        return row["id"] if row else None
    except Exception as e:
        print(f"Error creating interview session: {e}")
        return None


async def interview_session_belongs_to_user(user_id: str, session_id: UUID) -> bool:
    """True if interview_sessions row exists for this id and user."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM public.interview_sessions
                WHERE id = $1 AND user_id = $2::uuid
                """,
                session_id,
                user_id,
            )
        return row is not None
    except Exception as e:
        print(f"Error checking interview session: {e}")
        return False


async def get_session_prompt_context(user_id: str, session_id: UUID) -> dict | None:
    """
    Returns resume + job context for a session owned by user.
    Shape: { "resume_text": str|None, "job_title": str|None, "job_description": str|None }
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    p.resume_text,
                    s.job_title,
                    s.job_description
                FROM public.interview_sessions s
                LEFT JOIN public.interview_profiles p ON p.id = s.profile_id
                WHERE s.id = $1 AND s.user_id = $2::uuid
                """,
                session_id,
                user_id,
            )
        return dict(row) if row else None
    except Exception as e:
        print(f"Error fetching interview session context: {e}")
        return None


async def insert_interview_response(
    user_id: str,
    session_id: UUID,
    query: str,
    response: str,
    response_type: str,
) -> dict | None:
    """
    response_type is stored in column `query_type` (e.g. 'screen', 'transcript').
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.interview_responses
                    (session_id, query, response, query_type)
                SELECT $2, $3, $4, $5
                FROM public.interview_sessions s
                WHERE s.id = $2 AND s.user_id = $1::uuid
                RETURNING id, session_id, query, response, query_type, created_at
                """,
                user_id,
                session_id,
                query,
                response,
                response_type,
            )
        return dict(row) if row else None
    except Exception as e:
        print(f"Error inserting interview response: {e}")
        return None

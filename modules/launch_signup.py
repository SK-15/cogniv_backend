from modules.database import get_pool


async def insert_launch_signup(
    email: str,
    phone: str,
    name: str,
    profession: str,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO public.launch_signup (email, phone, name, profession)
            VALUES (lower(trim($1)), trim($2), trim($3), trim($4))
            RETURNING id, email, phone, name, profession, created_at
            """,
            email,
            phone,
            name,
            profession,
        )
    if not row:
        raise RuntimeError("Launch signup insert returned no row")
    return dict(row)

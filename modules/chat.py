from modules.database import get_pool

async def get_user_threads(user_id: str):
    """
    Fetch all threads for a specific user, ordered by most recently updated.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, created_at, updated_at
                FROM public.threads
                WHERE user_id = $1
                ORDER BY updated_at DESC
                """,
                user_id,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"Error fetching threads: {e}")
        return []

async def get_thread_chats(user_id: str, thread_id: str):
    """
    Fetch all chat messages for a specific thread.
    Ensures the thread belongs to the user.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # First verify thread ownership
            thread_row = await conn.fetchrow(
                """
                SELECT id
                FROM public.threads
                WHERE id = $2 AND user_id = $1
                """,
                user_id,
                thread_id,
            )
            if not thread_row:
                return []

            # Fetch chats for the verified thread
            rows = await conn.fetch(
                """
                SELECT id, query, response, created_at
                FROM public.chat_history
                WHERE thread_id = $1
                ORDER BY created_at ASC
                """,
                thread_id,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"Error fetching chats: {e}")
        return []

async def create_thread(user_id: str, title: str = "New Chat"):
    """
    Create a new thread for the user.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.threads (user_id, title)
                VALUES ($1, $2)
                RETURNING id, title, created_at, updated_at
                """,
                user_id,
                title,
            )
        return dict(row) if row else None
    except Exception as e:
        print(f"Error creating thread: {e}")
        return None

async def save_chat_message(user_id: str, thread_id: str, prompt: str, response: str):
    """
    Save the user prompt and AI response to the database.
    Also update the thread's updated_at timestamp.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Ensure the thread belongs to the user and update its timestamp.
                updated_thread = await conn.fetchrow(
                    """
                    UPDATE public.threads
                    SET updated_at = now()
                    WHERE id = $2 AND user_id = $1
                    RETURNING id
                    """,
                    user_id,
                    thread_id,
                )
                if not updated_thread:
                    return False

                # Insert the chat message
                await conn.execute(
                    """
                    INSERT INTO public.chat_history (thread_id, query, response)
                    VALUES ($1, $2, $3)
                    """,
                    thread_id,
                    prompt,
                    response,
                )

                return True
    except Exception as e:
        print(f"Error saving chat: {e}")
        return False

async def delete_thread(user_id: str, thread_id: str):
    """
    Delete a specific thread and its chats.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Delete chat history first to satisfy FK constraints.
                await conn.execute(
                    """
                    DELETE FROM public.chat_history ch
                    USING public.threads t
                    WHERE ch.thread_id = t.id
                      AND t.user_id = $1
                      AND t.id = $2
                    """,
                    user_id,
                    thread_id,
                )

                deleted_thread = await conn.fetchrow(
                    """
                    DELETE FROM public.threads
                    WHERE user_id = $1 AND id = $2
                    RETURNING id
                    """,
                    user_id,
                    thread_id,
                )
                return bool(deleted_thread)
    except Exception as e:
        print(f"Error deleting thread: {e}")
        return False

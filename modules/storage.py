import logging
import uuid
from pathlib import Path

from fastapi import UploadFile


logger = logging.getLogger(__name__)

UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"


async def upload_to_supabase(file: UploadFile, base_url: str) -> str | None:
    """
    Upload a file to local disk and return a public URL served by FastAPI.

    Note: function name kept as `upload_to_supabase` to avoid touching callers.
    """
    logger.info(
        "[upload_to_supabase] Starting upload for file='%s', content_type='%s'",
        file.filename,
        file.content_type,
    )

    try:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename).suffix.lstrip(".")
        filename = f"{uuid.uuid4()}" + (f".{ext}" if ext else "")

        content = await file.read()
        if not content:
            logger.warning("[upload_to_supabase] File content is empty! Upload aborted.")
            return None

        dest_path = UPLOADS_DIR / filename
        dest_path.write_bytes(content)

        url = f"{base_url.rstrip('/')}/uploads/{filename}"
        logger.info("[upload_to_supabase] Upload complete. url='%s'", url)
        return url
    except Exception as e:
        logger.error(
            "[upload_to_supabase] Exception during upload: %s: %s",
            type(e).__name__,
            e,
            exc_info=True,
        )
        return None

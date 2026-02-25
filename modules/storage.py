from fastapi import UploadFile
from modules.database import supabase
import uuid
import logging

logger = logging.getLogger(__name__)

async def upload_to_supabase(file: UploadFile, bucket: str = "user") -> str:
    """
    Upload a file to Supabase storage and return its public URL.
    """
    logger.info(f"[upload_to_supabase] Starting upload for file: '{file.filename}', content_type: '{file.content_type}', bucket: '{bucket}'")
    try:
        # Generate a unique filename
        file_extension = file.filename.split(".")[-1] if "." in file.filename else ""
        filename = f"{uuid.uuid4()}.{file_extension}"
        logger.info(f"[upload_to_supabase] Generated storage filename: '{filename}'")

        # Read file content
        content = await file.read()
        logger.info(f"[upload_to_supabase] Read {len(content)} bytes from file")

        if len(content) == 0:
            logger.warning("[upload_to_supabase] File content is empty! Upload aborted.")
            return None

        # Upload to Supabase
        logger.info(f"[upload_to_supabase] Uploading to Supabase bucket '{bucket}' at path '{filename}'...")
        try:
            response = supabase.storage.from_(bucket).upload(
                path=filename,
                file=content,
                file_options={"content-type": file.content_type}
            )
            logger.info(f"[upload_to_supabase] Supabase upload successful. Path: {response}")
        except Exception as upload_err:
            logger.error(f"[upload_to_supabase] Supabase storage upload failed: {upload_err}")
            # Try to get more info if it's a supabase-specific error
            if hasattr(upload_err, 'message'):
                logger.error(f"[upload_to_supabase] Error message: {upload_err.message}")
            return None

        # Get public URL
        public_url = supabase.storage.from_(bucket).get_public_url(filename)
        logger.info(f"[upload_to_supabase] Public URL obtained: {public_url}")
        return public_url

    except Exception as e:
        logger.error(f"[upload_to_supabase] Exception during upload: {type(e).__name__}: {e}", exc_info=True)
        return None

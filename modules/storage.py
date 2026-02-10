from fastapi import UploadFile
from modules.database import supabase
import uuid

async def upload_to_supabase(file: UploadFile, bucket: str = "user") -> str:
    """
    Upload a file to Supabase storage and return its public URL.
    """
    try:
        # Generate a unique filename
        file_extension = file.filename.split(".")[-1] if "." in file.filename else ""
        filename = f"{uuid.uuid4()}.{file_extension}"
        
        # Read file content
        content = await file.read()
        
        # Upload to Supabase
        response = supabase.storage.from_(bucket).upload(
            path=filename,
            file=content,
            file_options={"content-type": file.content_type}
        )
        
        # Get public URL
        public_url = supabase.storage.from_(bucket).get_public_url(filename)
        return public_url
    except Exception as e:
        print(f"Error uploading file: {e}")
        return None

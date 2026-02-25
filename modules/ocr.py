import base64
import json
import logging
from typing import Optional
import google.generativeai as genai
import openai
from modules.config import settings

logger = logging.getLogger(__name__)

# Configure APIs
genai.configure(api_key=settings.gemini_api_key)

async def extract_structured_text(
    image_bytes: bytes,
    mime_type: str,
    provider: str = "gemini",
    prompt: Optional[str] = None
) -> Optional[dict]:
    """
    Extract structured text from an image using the specified LLM provider.
    Returns a dictionary (parsed JSON) or None if extraction fails.
    """
    default_prompt = (
        "Extract all the text and structured information from this image. "
        "Return the data in a clear, nested JSON format. "
        "Include all headers, tables, and key-value pairs you can identify."
    )
    
    active_prompt = prompt if prompt else default_prompt

    try:
        if provider.lower() == "gemini":
            return await _extract_gemini(image_bytes, mime_type, active_prompt)
        elif provider.lower() == "openai":
            return await _extract_openai(image_bytes, mime_type, active_prompt)
        else:
            logger.error(f"Unsupported OCR provider: {provider}")
            return None
    except Exception as e:
        logger.error(f"OCR extraction error with {provider}: {e}", exc_info=True)
        return None

async def _extract_gemini(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    """Internal helper for Gemini OCR."""
    # Using gemini-1.5-flash for speed and strong multimodal performance
    model = genai.GenerativeModel("gemini-1.5-flash")
    
    # Format the content for Gemini
    content = [
        prompt,
        {
            "mime_type": mime_type,
            "data": image_bytes
        }
    ]
    
    # We use generation_config to encourage JSON output
    response = await model.generate_content_async(
        content,
        generation_config={"response_mime_type": "application/json"}
    )
    
    if not response or not response.text:
        return None
        
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON text: {response.text[:200]}...")
        # Fallback: attempt to find JSON block in text
        return _extract_json_block(response.text)

async def _extract_openai(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    """Internal helper for OpenAI OCR."""
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    
    # OpenAI requires base64 for images in the chat completions API
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_image}"
                    }
                }
            ]
        }
    ]
    
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_object"}
    )
    
    content = response.choices[0].message.content
    if not content:
        return None
        
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"OpenAI returned non-JSON text: {content[:200]}...")
        return _extract_json_block(content)

def _extract_json_block(text: str) -> Optional[dict]:
    """Helper to extract a JSON block from freeform text."""
    try:
        # Look for ```json ... ``` blocks
        import re
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        
        # Fallback: look for the first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
    except Exception:
        pass
    return None

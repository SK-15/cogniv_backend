import base64
import json
import logging
import re
from typing import Optional

import openai
from google import genai
from google.genai import types
import anthropic
from modules.config import settings

logger = logging.getLogger(__name__)

_gemini_client = genai.Client(api_key=settings.gemini_api_key)
_anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
_openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"


async def extract_structured_text(
    image_bytes: bytes,
    mime_type: str,
    provider: str = "gemini",
    prompt: Optional[str] = None,
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
        elif provider.lower() in ("anthropic", "claude"):
            return await _extract_anthropic(image_bytes, mime_type, active_prompt)
        else:
            logger.error(f"Unsupported OCR provider: {provider}")
            return None
    except Exception as e:
        logger.error(f"OCR extraction error with {provider}: {e}", exc_info=True)
        return None


async def extract_text(
    image_bytes: bytes,
    mime_type: str,
    provider: str = "gemini",
    prompt: Optional[str] = None,
) -> Optional[str]:
    """
    Extract raw text from an image using the specified LLM provider.
    Returns a string containing the text or None if extraction fails.
    """
    default_prompt = (
        "Extract all visible text from this image exactly as it appears. "
        "Do not add any conversational text or markdown formatting outside of the extracted text itself."
    )
    active_prompt = prompt if prompt else default_prompt

    try:
        if provider.lower() == "gemini":
            return await _extract_gemini_text(image_bytes, mime_type, active_prompt)
        elif provider.lower() == "openai":
            return await _extract_openai_text(image_bytes, mime_type, active_prompt)
        elif provider.lower() in ("anthropic", "claude"):
            return await _extract_anthropic_text(image_bytes, mime_type, active_prompt)
        else:
            logger.error(f"Unsupported OCR provider: {provider}")
            return None
    except Exception as e:
        logger.error(f"Text extraction error with {provider}: {e}", exc_info=True)
        return None


def _image_part(mime_type: str, data: bytes) -> types.Part:
    return types.Part(inline_data=types.Blob(mime_type=mime_type, data=data))


async def _extract_gemini_text(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[str]:
    contents = [
        types.Content(role="user", parts=[
            types.Part(text=prompt),
            _image_part(mime_type, image_bytes),
        ])
    ]
    response = await _gemini_client.aio.models.generate_content(
        model="gemini-1.5-flash",
        contents=contents,
    )
    return response.text or None


async def _extract_openai_text(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[str]:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}},
            ],
        }
    ]
    response = await _openai_client.chat.completions.create(model="gpt-4o", messages=messages)
    return response.choices[0].message.content


async def _extract_anthropic_text(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[str]:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    response = await _anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64_image,
                        },
                    },
                ],
            }
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text or None


async def _extract_gemini(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    contents = [
        types.Content(role="user", parts=[
            types.Part(text=prompt),
            _image_part(mime_type, image_bytes),
        ])
    ]
    response = await _gemini_client.aio.models.generate_content(
        model="gemini-1.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    if not response or not response.text:
        return None
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON text: {response.text[:200]}...")
        return _extract_json_block(response.text)


async def _extract_openai(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}},
            ],
        }
    ]
    response = await _openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"OpenAI returned non-JSON text: {content[:200]}...")
        return _extract_json_block(content)


async def _extract_anthropic(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    response = await _anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt + "\n\nRespond with valid JSON only."},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64_image,
                        },
                    },
                ],
            }
        ],
    )
    content = "".join(block.text for block in response.content if block.type == "text")
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"Anthropic returned non-JSON text: {content[:200]}...")
        return _extract_json_block(content)


def _extract_json_block(text: str) -> Optional[dict]:
    try:
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
    except Exception:
        pass
    return None

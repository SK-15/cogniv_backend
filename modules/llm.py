import openai
from google import genai
from google.genai import types
import anthropic
from modules.config import settings

openai.api_key = settings.openai_api_key

_openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
_gemini_client = genai.Client(api_key=settings.gemini_api_key)
_anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"

DIAGRAM_KEYWORDS = {"flow", "diagram", "sequence", "architecture", "visualize", "chart", "graph", "steps", "flowchart"}

DIAGRAM_SYSTEM_ADDENDUM = """
When a visual diagram would help, output it as a mermaid code block:
```mermaid
graph TD
    A --> B
```
Use mermaid syntax only. Supported: graph, sequenceDiagram, flowchart, classDiagram, erDiagram.
"""

def needs_diagram_hint(prompt: str) -> bool:
    return bool(DIAGRAM_KEYWORDS & set(prompt.lower().split()))


async def stream_openai(prompt: str, history: list = None, system_prompt: str | None = None):
    client = _openai_client

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        for chat in history:
            messages.append({"role": "user", "content": chat["query"]})
            messages.append({"role": "assistant", "content": chat["response"]})
    messages.append({"role": "user", "content": prompt})

    response = await _openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        stream=True,
    )
    async for chunk in response:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def stream_gemini(prompt: str, history: list = None, system_prompt: str | None = None):
    contents = []
    if history:
        for chat in history:
            contents.append(types.Content(role="user", parts=[types.Part(text=chat["query"])]))
            contents.append(types.Content(role="model", parts=[types.Part(text=chat["response"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    config = types.GenerateContentConfig(system_instruction=system_prompt) if system_prompt else None

    async for chunk in _gemini_client.aio.models.generate_content_stream(
        model="gemini-2.0-flash",
        contents=contents,
        config=config,
    ):
        if chunk.text:
            yield chunk.text


async def stream_anthropic(prompt: str, history: list = None, system_prompt: str | None = None):
    messages = []
    if history:
        for chat in history:
            messages.append({"role": "user", "content": chat["query"]})
            messages.append({"role": "assistant", "content": chat["response"]})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    async with _anthropic_client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            if text:
                yield text


def select_stream(provider: str):
    """Return the streaming generator function for the given provider."""
    p = (provider or "").lower()
    if p == "gemini":
        return stream_gemini
    if p in ("anthropic", "claude"):
        return stream_anthropic
    return stream_openai



async def generate_response(prompt: str, model_type: str = "openai", history: list = None):
    """
    Generate a non-streaming response from the specified LLM model.
    """
    try:
        if model_type == "openai":
            messages = []
            if history:
                for chat in history:
                    messages.append({"role": "user", "content": chat["query"]})
                    messages.append({"role": "assistant", "content": chat["response"]})
            messages.append({"role": "user", "content": prompt})

            response = await _openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                stream=False,
            )
            return response.choices[0].message.content

        elif model_type == "gemini":
            contents = []
            if history:
                for chat in history:
                    contents.append(types.Content(role="user", parts=[types.Part(text=chat["query"])]))
                    contents.append(types.Content(role="model", parts=[types.Part(text=chat["response"])]))
            contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

            response = await _gemini_client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
            )
            return response.text

    except Exception as e:
        print(f"Error generating response from {model_type}: {e}")
        return None

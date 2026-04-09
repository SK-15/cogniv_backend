import openai
from google import genai
from google.genai import types
from modules.config import settings

openai.api_key = settings.openai_api_key

_gemini_client = genai.Client(api_key=settings.gemini_api_key)


async def stream_openai(prompt: str, history: list = None, system_prompt: str | None = None):
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        for chat in history:
            messages.append({"role": "user", "content": chat["query"]})
            messages.append({"role": "assistant", "content": chat["response"]})
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
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


async def generate_response(prompt: str, model_type: str = "openai", history: list = None):
    """
    Generate a non-streaming response from the specified LLM model.
    """
    try:
        if model_type == "openai":
            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            messages = []
            if history:
                for chat in history:
                    messages.append({"role": "user", "content": chat["query"]})
                    messages.append({"role": "assistant", "content": chat["response"]})
            messages.append({"role": "user", "content": prompt})

            response = await client.chat.completions.create(
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

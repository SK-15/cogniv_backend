import openai
import google.generativeai as genai
from modules.config import settings

# Configure OpenAI
openai.api_key = settings.openai_api_key

# Configure Gemini
genai.configure(api_key=settings.gemini_api_key)

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
    model = genai.GenerativeModel("gemini-pro")
    
    chat_history = []
    if history:
        for chat in history:
            chat_history.append({"role": "user", "parts": [chat["query"]]})
            chat_history.append({"role": "model", "parts": [chat["response"]]})

    chat = model.start_chat(history=chat_history)
    final_prompt = prompt
    if system_prompt:
        final_prompt = f"{system_prompt}\n\nUser question:\n{prompt}"
    response = await chat.send_message_async(final_prompt, stream=True)
    async for chunk in response:
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
            model = genai.GenerativeModel("gemini-pro")
            chat_history = []
            if history:
                for chat in history:
                    chat_history.append({"role": "user", "parts": [chat["query"]]})
                    chat_history.append({"role": "model", "parts": [chat["response"]]})
            
            chat = model.start_chat(history=chat_history)
            response = await chat.send_message_async(prompt)
            return response.text
    except Exception as e:
        print(f"Error generating response from {model_type}: {e}")
        return None

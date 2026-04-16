import asyncio
import hashlib
import json
from typing import AsyncGenerator

from modules.llm import stream_openai, stream_gemini, _openai_client
from modules.websearch import web_search_task

# ── Intent classification ────────────────────────────────────────────────────

INTENT_SYSTEM = """
Classify user intent. Respond with JSON only, no explanation.
Schema: { "intent": "simple_chat"|"web_search"|"file_analysis"|"multi_step", "reasoning": "..." }
"""

_intent_cache: dict[str, dict] = {}


async def classify_intent(prompt: str) -> dict:
    """Fast intent classification using lightweight model, with in-process cache."""
    key = hashlib.md5(prompt.encode()).hexdigest()
    if key in _intent_cache:
        return _intent_cache[key]

    try:
        resp = await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=80,
        )
        result = json.loads(resp.choices[0].message.content)
    except Exception:
        result = {"intent": "simple_chat"}

    _intent_cache[key] = result
    return result


# ── Multi-step planner ───────────────────────────────────────────────────────

PLANNER_SYSTEM = """
Break the user request into 2-4 sequential steps.
Respond JSON only: { "steps": ["step1", "step2", ...] }
"""


async def _multi_step_stream(prompt: str, history: list, model: str) -> AsyncGenerator[str, None]:
    try:
        plan_resp = await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=150,
        )
        steps = json.loads(plan_resp.choices[0].message.content).get("steps", [prompt])
    except Exception:
        steps = [prompt]

    streamer = stream_openai if model == "openai" else stream_gemini

    for i, step in enumerate(steps, 1):
        yield f"**Step {i}:** {step}\n\n"
        async for token in streamer(step, history=history):
            yield token
        yield "\n\n"


# ── Agent stream ─────────────────────────────────────────────────────────────

async def agent_stream(
    prompt: str,
    history: list = None,
    model: str = "openai",
    system_prompt: str | None = None,
    file_context: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Main agent entry point. Streams tokens with near-zero perceived latency.
    """
    intent_data = await classify_intent(prompt)
    intent = intent_data.get("intent", "simple_chat")

    streamer = stream_openai if model == "openai" else stream_gemini

    if intent == "simple_chat":
        async for token in streamer(prompt, history=history, system_prompt=system_prompt):
            yield token
        return

    if intent == "web_search":
        yield "Searching the web...\n\n"
        try:
            results = await asyncio.wait_for(web_search_task(prompt), timeout=15.0)
        except asyncio.TimeoutError:
            yield "Web search timed out. Answering from knowledge.\n\n"
            async for token in streamer(prompt, history=history, system_prompt=system_prompt):
                yield token
            return

        augmented = (
            f"User question: {prompt}\n\n"
            f"Web search results:\n{results}\n\n"
            "Answer the user's question using the search results above."
        )
        async for token in streamer(augmented, history=history, system_prompt=system_prompt):
            yield token
        return

    if intent == "file_analysis":
        if not file_context:
            yield "No file provided. Please upload a file first.\n"
            return

        yield "Analyzing your document...\n\n"
        augmented = (
            f"User question: {prompt}\n\n"
            f"Document content:\n{file_context}\n\n"
            "Answer based on the document."
        )
        async for token in streamer(augmented, history=history, system_prompt=system_prompt):
            yield token
        return

    if intent == "multi_step":
        async for token in _multi_step_stream(prompt, history or [], model):
            yield token
        return

    # Fallback
    async for token in streamer(prompt, history=history, system_prompt=system_prompt):
        yield token

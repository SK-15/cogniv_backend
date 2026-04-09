# Gemini API Key Endpoint & AI Answer System Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GET `/gemini/api_key` authenticated endpoint that returns the Gemini API key from `.env`, and update the `/ai-answer` system prompt to respond concisely in bullet points without fluff.

**Architecture:** Both changes are isolated to `app/main.py`. The Gemini key endpoint follows the exact same pattern as the existing `/deepgram/api_key` endpoint (line 151-156). The system prompt update replaces the current interview-style prompt with a concise, bullet-point-first prompt that still uses resume + job description context.

**Tech Stack:** FastAPI, Pydantic-settings (`settings.gemini_api_key` already exists in `modules/config.py`)

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `app/main.py` | Modify | Add `GET /gemini/api_key` endpoint; replace `system_prompt` string in `/ai-answer` handler |
| `API_DOC.md` | Modify | Add documentation for the new endpoint |

---

### Task 1: Add `GET /gemini/api_key` endpoint

**Files:**
- Modify: `app/main.py:151-156` (insert after the existing `/deepgram/api_key` endpoint)

**Context:** `settings.gemini_api_key` is already populated from env via `AliasChoices("GEMINI_API_KEY", "GENAI_API_KEY")` in `modules/config.py:9`. The `require_user_id` dependency (line 61-68) handles auth. No new imports needed.

- [ ] **Step 1: Add the endpoint to `app/main.py`**

Open `app/main.py`. After the `/deepgram/api_key` endpoint (currently ending at line 156), add:

```python
@app.get("/gemini/api_key")
async def get_gemini_api_key(_user_id: str = Depends(require_user_id)):
    """Return the Gemini API key for client-side use. Requires a valid Bearer token."""
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured")
    return {"gemini_api_key": settings.gemini_api_key}
```

- [ ] **Step 2: Manually verify the endpoint appears after line ~156**

Read `app/main.py` lines 150-170 and confirm the new endpoint is present and indented correctly.

- [ ] **Step 3: Smoke-test the endpoint locally**

Run:
```bash
cd /home/saurav/Projects/chatbot/backend && python -m uvicorn app.main:app --port 8001 --reload &
sleep 2
# Replace <TOKEN> with a valid Bearer token from login
curl -s http://localhost:8001/gemini/api_key -H "Authorization: Bearer <TOKEN>"
```
Expected: `{"gemini_api_key": "AIza..."}` or a 401 if token is missing/invalid.

- [ ] **Step 4: Commit**

```bash
cd /home/saurav/Projects/chatbot/backend
git add app/main.py
git commit -m "feat: add authenticated GET /gemini/api_key endpoint"
```

---

### Task 2: Update `/ai-answer` system prompt

**Files:**
- Modify: `app/main.py:598-610` (the `system_prompt` string inside the `/ai-answer` handler)

**Context:** The current prompt (lines 598-610) tells the AI to respond as an interview candidate in first-person paragraphs using STAR method. The new requirement is: answer the question directly, no fluff, in bullet points, using the resume and job description only when relevant. The surrounding code (context fetching, streaming, persistence) is unchanged.

- [ ] **Step 1: Replace the `system_prompt` string in `/ai-answer`**

In `app/main.py`, locate the `system_prompt = (` block starting at line ~598. Replace the **entire `system_prompt = (...)` assignment** with:

```python
        system_prompt = (
            "You are a precise, concise assistant helping someone answer interview questions. "
            "Answer the question directly and completely, in bullet points, with no fluff, preamble, or filler phrases.\n\n"
            "Rules:\n"
            "- Always respond in bullet points (use '-' prefix).\n"
            "- No introductory sentences like 'Great question' or 'Sure, here is...'.\n"
            "- No closing remarks or summaries.\n"
            "- Be specific and substantive — avoid vague platitudes.\n"
            "- Use the candidate's resume and job description below ONLY when they add relevant detail. "
            "Do not mention or reference that you were given a resume or job description.\n\n"
            f"Target role title: {job_title or '[not provided]'}\n\n"
            f"Target role description:\n{job_description or '[not provided]'}\n\n"
            f"Candidate resume:\n{resume_text or '[not provided]'}\n"
        )
```

- [ ] **Step 2: Verify the surrounding code is intact**

Read `app/main.py` lines 580-640 and confirm:
- `resume_text`, `job_title`, `job_description` are still fetched and truncated above the prompt.
- The `generate_and_save()` function below still passes `system_prompt` to `stream_gemini` / `stream_openai`.
- No other code was accidentally removed.

- [ ] **Step 3: Smoke-test `/ai-answer` locally**

```bash
# Server should already be running from Task 1, or start it:
# python -m uvicorn app.main:app --port 8001 --reload &
# Replace SESSION_UUID and TOKEN with real values
curl -s -X POST http://localhost:8001/ai-answer \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<SESSION_UUID>","question":"Tell me about yourself","provider":"gemini"}' \
  --no-buffer
```
Expected: Streamed response starting with bullet points (`- ...`), no preamble, no closing sentence.

- [ ] **Step 4: Commit**

```bash
cd /home/saurav/Projects/chatbot/backend
git add app/main.py
git commit -m "feat: update /ai-answer system prompt to bullet-point concise answers"
```

---

### Task 3: Update API documentation

**Files:**
- Modify: `API_DOC.md` — add section for new endpoint; update `/ai-answer` notes

- [ ] **Step 1: Add Gemini API key endpoint docs to `API_DOC.md`**

After the `### 1.` (Deepgram key) endpoint entry — or as a new numbered section before the WebSocket entry — insert:

```markdown
### 17. Get Gemini API Key
Return the server-side Gemini API key for client-side use.

*   **URL**: `/gemini/api_key`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "gemini_api_key": "AIza..."
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 503 Service Unavailable (key not configured in .env)
```

- [ ] **Step 2: Add a note to the `/ai-answer` section**

In the existing `### 15. AI Answer` section, add under the description:

```
**Response style:** Answers are formatted as bullet points with no preamble or fluff. Resume and job description context is used when relevant.
```

- [ ] **Step 3: Commit**

```bash
cd /home/saurav/Projects/chatbot/backend
git add API_DOC.md
git commit -m "docs: add /gemini/api_key endpoint docs and note /ai-answer response style"
```

---

## Self-Review

**Spec coverage:**
- [x] `/gemini/api_key` endpoint — Task 1
- [x] Endpoint is authenticated (`require_user_id` dependency) — Task 1
- [x] Returns Gemini key from `.env` via `settings.gemini_api_key` — Task 1
- [x] System prompt for `/ai-answer` answers without fluff in bullet points — Task 2
- [x] System prompt uses resume + job description as context — Task 2 (context variables unchanged, still passed in)

**Placeholder scan:** No TBDs, no "implement later", all code blocks are complete.

**Type consistency:** No new types introduced. `settings.gemini_api_key` is `str` (from `config.py:9`), consistent with how `settings.deepgram_api_key` is used in the existing endpoint.

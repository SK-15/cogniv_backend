# Supabase LLM Chatbot API Documentation

This API provides authentication via Supabase and streaming chat responses from OpenAI and Gemini.

## Base URL
- **Production**: `https://adcrkz336r.ap-south-1.awsapprunner.com`
- **Local**: `http://localhost:8000`

## Security

### CORS
Only `https://cogniv.co.in` is allowed by default. Configurable via `CORS_ORIGINS` env var (comma-separated for multiple origins, e.g. local dev).

### Rate Limiting
Applied per IP address. Returns `429 Too Many Requests` with `Retry-After` header on breach.

| Endpoint | Limit |
|----------|-------|
| `POST /login` | 10 req/min |
| `POST /signup` | 5 req/min |
| `POST /payment/create-order` | 10 req/min |

### Security Headers
All responses include:
```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
```

### Error Normalization
Auth endpoints return opaque error codes — no internal details or user enumeration signals leaked:
- `/login` always returns `"invalid_credentials"` on 401 (wrong email OR wrong password)
- `/signup` returns `"email_taken"` (409) or `"signup_failed"` (400)

---

## Authentication
Most endpoints are public, but the `/chat` endpoint requires a valid Supabase access token passed in the `Authorization` header.

Header format:
`Authorization: Bearer <your_access_token>`

---

## Endpoints

### 1. User Signup
Create a new user account in Supabase.

*   **URL**: `/signup`
*   **Method**: `POST`
*   **Rate limit**: 5 requests/minute per IP
*   **Request Body**:
    ```json
    {
      "email": "user@example.com",
      "password": "yourpassword"
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "message": "User created successfully",
          "user_id": "uuid-string"
        }
        ```
*   **Error Responses**:
    *   **Code**: 400 `{"detail": "signup_failed"}` (invalid email or other error)
    *   **Code**: 409 `{"detail": "email_taken"}` (email already registered)
    *   **Code**: 429 Too Many Requests (rate limit exceeded)

### 2. User Login
Authenticate a user and receive access tokens.

*   **URL**: `/login`
*   **Method**: `POST`
*   **Rate limit**: 10 requests/minute per IP
*   **Request Body**:
    ```json
    {
      "email": "user@example.com",
      "password": "yourpassword"
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "access_token": "jwt-token-string",
          "refresh_token": "refresh-token-string",
          "user_id": "uuid-string"
        }
        ```
        `refresh_token` is always present; it may be `null` if the identity provider did not return a refresh token.
*   **Error Responses**:
    *   **Code**: 401 `{"detail": "invalid_credentials"}` (wrong email or password — intentionally indistinct)
    *   **Code**: 429 Too Many Requests (rate limit exceeded)

### 3. Refresh session tokens
Exchange a **refresh token** for new `access_token` / `refresh_token` values. Neon Auth (email/password) and the app’s Google OAuth (Electron loopback) use different refresh tokens; set `provider` or use the default `auto` behavior.

*   **URL**: `/refresh`
*   **Method**: `POST`
*   **Request Body**:
    ```json
    {
      "refresh_token": "string",
      "provider": "auto"
    }
    ```
    *   `refresh_token`: **Required**.
    *   `provider`: Optional. One of `neon` (Neon Auth only), `google` (Google OAuth refresh only), or `auto` / omitted (try Neon Auth first, then Google).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "access_token": "jwt-or-session-token-string",
          "refresh_token": "new-or-same-refresh-token",
          "user_id": "uuid-string",
          "provider": "neon"
        }
        ```
        `provider` is `neon` or `google` so clients can store which flow to use on the next refresh.
*   **Error Response**:
    *   **Code**: 401 Unauthorized (invalid or expired refresh token)
    *   **Code**: 422 Unprocessable Entity (missing `refresh_token` or invalid `provider`)

For Neon Auth, the backend calls your Better Auth base URL (see `NEON_AUTH_BASE_URL`). You can optionally set **`NEON_AUTH_REFRESH_URL`** in the environment to the full refresh endpoint if it differs from the defaults (`…/refresh`, `…/refresh-token`).

### 4. Create New Chat (Thread)
Create a new conversation thread.

*   **URL**: `/new_chat`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Request Body**:
    ```json
    {
      "title": "My New Chat"
    }
    ```
    *   `title`: Optional. Default is "New Chat".
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "id": "thread-uuid",
          "user_id": "user-uuid",
          "title": "My New Chat",
          "created_at": "timestamp",
          "updated_at": "timestamp"
        }
        ```

### 5. Chat (Streaming)
Send a message to a thread and get a streaming response.

*   **URL**: `/chat`
*   **Method**: `POST`
*   **Headers**: 
    *   `Authorization: Bearer <access_token>`
*   **Request Body**:
    ```json
    {
      "prompt": "Tell me a joke.",
      "thread_id": "thread-uuid",
      "provider": "openai" 
    }
    ```
    *   `thread_id`: **Required**. The ID of the thread to post to.
    *   `provider`: Optional. Can be `"openai"` (default) or `"gemini"`.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content-Type**: `text/event-stream`
    *   **Body**: A stream of text chunks.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 500 Internal Server Error

### 6. List Threads
Get all conversation threads for the user.

*   **URL**: `/threads`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "threads": [
            {
              "id": "thread-uuid",
              "title": "My New Chat",
              "created_at": "timestamp",
              "updated_at": "timestamp"
            }
          ]
        }
        ```

### 7. Get Thread History
Get all chat messages for a specific thread.

*   **URL**: `/threads/{thread_id}/chats`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "chats": [
            {
              "id": "chat-uuid",
              "query": "User prompt",
              "response": "AI response",
              "created_at": "timestamp"
            }
          ]
        }
        ```

### 8. Delete Thread
Delete a specific thread and all associated messages.

*   **URL**: `/threads/{thread_id}`
*   **Method**: `DELETE`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "message": "Thread deleted successfully"
        }
        ```
*   **Error Response**:
    *   **Code**: 404 Not Found
    *   **Code**: 401 Unauthorized
    *   **Code**: 500 Internal Server Error
    *   **Code**: 500 Internal Server Error

### 9. Web Search
Perform a web search and get a synthesized answer.

*   **URL**: `/websearch`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Request Body**:
    ```json
    {
      "query": "Current status of solid state batteries"
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "answer": "Solid state batteries are currently..."
        }
        ```
*   **Error Response**:
    *   **Code**: 500 Internal Server Error

### 10. Upload File
Upload a file to a specific thread.

*   **URL**: `/upload`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Query Parameters**:
    *   `thread_id`: **Required**. The ID of the thread to associate the file with.
*   **Request Body**:
    *   `file`: The file to upload (in `multipart/form-data` form).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "message": "File uploaded and processed",
          "url": "http://localhost:8000/uploads/<FILENAME>"
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 500 Internal Server Error

### 11. OCR (Structured Text Extraction)
Perform OCR on an image and extract structured text as JSON.

*   **URL**: `/ocr`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Request Body**:
    *   `file`: The image file to process (in `multipart/form-data` form).
    *   `provider`: Optional. Can be `"gemini"` (default) or `"openai"`.
    *   `prompt`: Optional. A custom prompt to guide the extraction.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: A JSON object containing the structured data.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 500 Internal Server Error

### 12. Save Interview Profile
Save an interview profile (job role, display name, resume file, default flag) for the authenticated user. The server reads the uploaded file **in-memory**, extracts plain text from the resume (PDF via PyMuPDF, DOCX via python-docx), and writes the extracted text into `interview_profiles.resume_text`. (The API form field `job_role` is stored in DB column `role`.)

*   **URL**: `/save_profile`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Request Body** (form fields):
    *   `job_role`: **Required**. Text.
    *   `name`: **Required**. Profile label.
    *   `is_default`: Optional. String interpreted as boolean (`true`, `1`, `yes`, `on`). Default `false`. If true, other profiles for this user are cleared from default before insert.
    *   `resume`: **Required**. File — `.pdf` or `.docx`.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: Row from `interview_profiles` (`id`, `user_id`, `name`, `role`, `resume_text`, `is_default`, `sort_order`, `created_at`, `updated_at`).
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 422 Unprocessable Entity (invalid resume type)
    *   **Code**: 500 Internal Server Error

### 13. Get Current User (Account)
Return the authenticated user’s account row from `neon_auth."user"` (for dashboards: display name and email).

*   **URL**: `/user/me`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "id": "user-uuid",
          "email": "user@example.com",
          "name": "Jane Doe"
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (user row missing)

### 14. List Past Interview Sessions
Return previous `interview_sessions` for the authenticated user, **newest first**, with the linked profile’s display name.

*   **URL**: `/interview/sessions`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Query Parameters**:
    *   `limit`: Optional. Integer, default `50`, min `1`, max `100`.
    *   `offset`: Optional. Integer, default `0`, min `0` (pagination).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "sessions": [
            {
              "id": "session-uuid",
              "user_id": "user-uuid",
              "profile_id": "profile-uuid",
              "started_at": "timestamp",
              "ended_at": "timestamp or null",
              "job_title": "Senior Backend Engineer",
              "job_description": "Job description text...",
              "profile_name": "Primary"
            }
          ],
          "limit": 50,
          "offset": 0
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized

### 15. List Interview Profiles
Get all interview profiles for the authenticated user.

*   **URL**: `/interview/profiles`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Query Parameters**:
    *   `include_resume`: Optional. Boolean, default `true`. If `false`, each profile still includes a `resume_text` field set to `null` (omits loading full extracted text for lighter list views).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "profiles": [
            {
              "id": "profile-uuid",
              "user_id": "user-uuid",
              "name": "Primary",
              "role": "Backend Engineer",
              "resume_text": "Extracted resume text...",
              "is_default": true,
              "sort_order": 0,
              "created_at": "timestamp",
              "updated_at": "timestamp"
            }
          ]
        }
        ```
        When `include_resume=false`, `resume_text` is always `null`.
*   **Error Response**:
    *   **Code**: 401 Unauthorized

### 16. Start Interview Session
Create a session for a given profile and job context. The profile must belong to the authenticated user.

*   **URL**: `/interview/session`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "profile_id": "uuid-of-interview-profile",
      "job_title": "Senior Backend Engineer",
      "job_description": "Full job description text..."
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "session_id": "uuid-of-new-session"
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (profile missing or not owned by user)
    *   **Code**: 422 Unprocessable Entity (invalid `profile_id`)

### 17. Analyse Screen (Text Extraction & Q&A, Streaming)
Extract raw text from an uploaded image, then **stream** an LLM answer for any questions identified in that text (same mechanism as `/chat`: `Content-Type: text/event-stream`, chunked body). Persists `query` (extracted text), `response` (full LLM answer after the stream completes), and `query_type` = `screen` on `interview_responses` for the given session.

*   **URL**: `/analyse-screen`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Query Parameters**:
    *   `provider`: Optional. LLM for OCR and answering. `"openai"` (default), `"gemini"`, or `"anthropic"` (Claude).
*   **Request Body** (form fields):
    *   `file`: The image file to analyze.
    *   `session_id`: **Required**. UUID of an `interview_sessions` row owned by the user.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content-Type**: `text/event-stream`
    *   **Body**:
        1. **First line**: a single JSON object (UTF-8) ending with a newline, e.g. `{"extracted_text":"..."}\n`. This is the OCR result; parse it before treating the rest of the body as streamed text.
        2. **Remainder**: streamed answer text (same style as `/chat`—incremental chunks, not a second JSON envelope).
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 422 Unprocessable Entity (invalid `session_id`)
    *   **Code**: 500 Internal Server Error

### 18. AI Answer (Streaming)
Answer a question using OpenAI or Gemini with a **streaming** response (same as `/chat`). Persists `query` (question), `response` (full answer after the stream completes), and `query_type` = `transcript` on `interview_responses` for the given session.

**Response style:** Answers are formatted as bullet points with no preamble or fluff. Resume and job description context from the session is used when relevant.

*   **URL**: `/ai-answer`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "session_id": "uuid-of-interview-session",
      "question": "What is the capital of France?",
      "provider": "openai"
    }
    ```
    *   `session_id`: **Required**.
    *   Provide exactly one of `question`, `query`, or `text` for the prompt.
    *   `provider`: Optional. `"openai"` (default) or `"gemini"`.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content-Type**: `text/event-stream`
    *   **Body**: A stream of text chunks (same as `/chat`).
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 422 Unprocessable Entity (invalid `session_id` or missing question field)
    *   **Code**: 500 Internal Server Error

### 19. Get Gemini API Key
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

### 20. End Interview Session
Mark a session as ended and store its duration.

*   **URL**: `/interview/session/{session_id}/end`
*   **Method**: `PATCH`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "duration_seconds": 420
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "ok": true,
          "duration_seconds": 420
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found, already ended, or not owned by user)
    *   **Code**: 422 Unprocessable Entity (`duration_seconds` < 0)

---

### 21. Google OAuth — Start (Electron)
Redirect to Google's consent screen. For use by the Electron desktop app only.

*   **URL**: `/auth/google/start`
*   **Method**: `GET`
*   **Query Parameters**:
    *   `local_port`: **Required**. Loopback port the Electron app is listening on.
*   **Success Response**:
    *   **Code**: 302 Redirect → Google OAuth consent URL

---

### 22. Google OAuth — Callback (Electron)
Receives the OAuth code from Google, exchanges it, and redirects to the Electron loopback server.

*   **URL**: `/auth/google/callback`
*   **Method**: `GET`
*   **Query Parameters** (set by Google):
    *   `code`, `state`, `error`
*   **Success Response**:
    *   **Code**: 302 Redirect → `http://127.0.0.1:{local_port}/callback?access_token=...&refresh_token=...&user_id=...`
*   **Error Response**:
    *   **Code**: 302 Redirect → `http://127.0.0.1:{local_port}/callback?error=<message>`

---

### 23. Google OAuth — Start (Web)
Redirect to Google's consent screen. For use by the web app at cogniv.co.in.

*   **URL**: `/auth/google/start/web`
*   **Method**: `GET`
*   **Success Response**:
    *   **Code**: 302 Redirect → Google OAuth consent URL

---

### 24. Google OAuth — Callback (Web)
Receives the OAuth code from Google, exchanges it, and redirects back to the web frontend with tokens.

*   **URL**: `/auth/google/callback/web`
*   **Method**: `GET`
*   **Query Parameters** (set by Google):
    *   `code`, `state`, `error`
*   **Success Response**:
    *   **Code**: 302 Redirect → `https://cogniv.co.in/auth/callback#access_token=...&refresh_token=...&user_id=...`
    *   Tokens are in the **URL fragment** (`#`), not query params — fragments are never sent to servers or logged.
    *   Frontend reads via `new URLSearchParams(window.location.hash.slice(1))`
*   **Error Response**:
    *   **Code**: 302 Redirect → `https://cogniv.co.in/auth/callback?error=<message>` (errors via query param, not fragment)

---

### 25. Subscription Status
Return the authenticated user's current plan, usage, and session quota.

*   **URL**: `/subscription/status`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "plan_tier": "free",
          "status": "active",
          "sessions_used": 2,
          "sessions_remaining": 1,
          "free_session_limit": 3,
          "current_period_end": null
        }
        ```
        *   `plan_tier`: `"free"` or `"pro"`
        *   `status`: `"active"`, `"cancelled"`, or `"past_due"`
        *   `sessions_used`, `sessions_remaining`, `free_session_limit`: `null` when on Pro plan
        *   `current_period_end`: ISO timestamp or `null` (Pro only)
*   **Error Response**:
    *   **Code**: 401 Unauthorized

---

### 26. Create Payment Order
Create a Razorpay subscription and return credentials to open the checkout modal.

*   **URL**: `/payment/create-order`
*   **Method**: `POST`
*   **Rate limit**: 10 requests/minute per IP
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "subscription_id": "sub_...",
          "key_id": "rzp_..."
        }
        ```
        Pass `subscription_id` to `Razorpay({ subscription_id })` on the frontend. Never hardcode `key_id`.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (user not found)
    *   **Code**: 429 Too Many Requests (rate limit exceeded)

---

### 27. Verify Payment
Verify Razorpay payment signature after checkout modal success and activate Pro plan.

*   **URL**: `/payment/verify`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "razorpay_payment_id": "pay_...",
      "razorpay_subscription_id": "sub_...",
      "razorpay_signature": "signature-string"
    }
    ```
    All three fields come from the Razorpay checkout `handler` callback.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "ok": true,
          "plan_tier": "pro"
        }
        ```
*   **Error Response**:
    *   **Code**: 400 Bad Request (signature mismatch)
    *   **Code**: 401 Unauthorized

---

### 28. Cancel Subscription
Cancel the active Pro subscription at the end of the current billing period.

*   **URL**: `/subscription/cancel`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "ok": true,
          "message": "Subscription will cancel at end of current billing period."
        }
        ```
*   **Error Response**:
    *   **Code**: 400 Bad Request (no active subscription found)
    *   **Code**: 401 Unauthorized

---

### 29. Razorpay Webhook
Receives Razorpay subscription lifecycle events. No auth — validated by Razorpay HMAC signature header.

*   **URL**: `/subscription/webhook`
*   **Method**: `POST`
*   **Headers**:
    *   `x-razorpay-signature`: Razorpay HMAC-SHA256 signature over the raw body
*   **Events handled**:
    *   `subscription.charged` → sets `plan_tier='pro'`, updates `current_period_end`
    *   `subscription.halted` → sets `status='past_due'`
    *   `subscription.cancelled` → downgrades to `plan_tier='free'`
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: `{ "received": true }`
*   **Error Response**:
    *   **Code**: 400 Bad Request (invalid signature)

---

### 30. Live Audio Transcription (WebSocket)
Real-time audio transcription using Deepgram. Accepts raw audio bytes and streams back transcribed text.

*   **URL**: `/listen`
*   **Protocol**: `WebSocket`
*   **Connection URL**: `ws://localhost:8000/listen`
*   **Audio Format**: 
    - **Encoding**: `linear16` (Raw PCM)
    - **Sample Rate**: `16000 Hz`
    - **Channels**: `1` (Mono)
*   **Request (Binary Frames)**: Send raw audio bytes according to the format above.
*   **Response (Text Frames)**: Real-time transcript strings.
*   **Error Handling**:
    - Disconnects on internal error or Deepgram timeout.
    - Sends specialized error text if API keys are missing.

---

## Testing with cURL

### Signup
```bash
curl -X POST http://localhost:8000/signup \
     -H "Content-Type: application/json" \
     -d '{"email": "test@example.com", "password": "Password123!"}'
```

### Login
```bash
curl -X POST http://localhost:8000/login \
     -H "Content-Type: application/json" \
     -d '{"email": "test@example.com", "password": "Password123!"}'
```

### Refresh tokens
```bash
curl -X POST http://localhost:8000/refresh \
     -H "Content-Type: application/json" \
     -d '{"refresh_token":"<REFRESH_TOKEN>","provider":"auto"}'
```

### Create New Chat
```bash
curl -X POST http://localhost:8000/new_chat \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"title": "Test Chat"}'
```

### Chat
```bash
curl -X POST http://localhost:8000/chat \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Hi!", "thread_id": "<THREAD_ID>", "provider": "openai"}' \
     --no-buffer
```

### List Threads
```bash
curl -X GET http://localhost:8000/threads \
     -H "Authorization: Bearer <TOKEN>"
```


### Get Chats for Thread
```bash
curl -X GET http://localhost:8000/threads/<THREAD_ID>/chats \
     -H "Authorization: Bearer <TOKEN>"
```

### Delete Thread
```bash
curl -X DELETE http://localhost:8000/threads/<THREAD_ID> \
     -H "Authorization: Bearer <TOKEN>"
```

### Web Search
```bash
curl -X POST http://localhost:8000/websearch \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"query": "Latest news on AI"}'
```

### Upload File
```bash
curl -X POST "http://localhost:8000/upload?thread_id=<THREAD_ID>" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "file=@/path/to/your/file.txt"
```

### OCR (Structured Text Extraction)
```bash
curl -X POST "http://localhost:8000/ocr" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "file=@/path/to/your/image.jpg" \
     -F "provider=gemini"
```

### Save interview profile
```bash
curl -X POST "http://localhost:8000/save_profile" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "job_role=Engineer" \
     -F "name=Primary" \
     -F "is_default=true" \
     -F "resume=@/path/to/resume.pdf"
```

### Get current user (dashboard account)
```bash
curl -X GET "http://localhost:8000/user/me" \
     -H "Authorization: Bearer <TOKEN>"
```

### List past interview sessions
```bash
curl -X GET "http://localhost:8000/interview/sessions?limit=50&offset=0" \
     -H "Authorization: Bearer <TOKEN>"
```

### List interview profiles
```bash
curl -X GET "http://localhost:8000/interview/profiles" \
     -H "Authorization: Bearer <TOKEN>"
```

### List interview profiles (without resume text)
```bash
curl -X GET "http://localhost:8000/interview/profiles?include_resume=false" \
     -H "Authorization: Bearer <TOKEN>"
```

### Start interview session
```bash
curl -X POST "http://localhost:8000/interview/session" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"profile_id":"<PROFILE_UUID>","job_title":"Role","job_description":"..."}'
```

### Analyse screen
First line is JSON with `extracted_text`; the rest streams the LLM answer. Use `--no-buffer` so chunks arrive as they are generated.

```bash
curl -X POST "http://localhost:8000/analyse-screen?provider=openai" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "file=@/path/to/your/screenshot.png" \
     -F "session_id=<SESSION_UUID>" \
     --no-buffer
```

### AI answer
Streams plain text chunks like `/chat`.

```bash
curl -X POST "http://localhost:8000/ai-answer" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"session_id":"<SESSION_UUID>","question":"Summarize STAR method","provider":"openai"}' \
     --no-buffer
```

### End interview session
```bash
curl -X PATCH "http://localhost:8000/interview/session/<SESSION_UUID>/end" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"duration_seconds": 420}'
```

### Google OAuth — start (web, opens in browser)
```bash
open "http://localhost:8000/auth/google/start/web"
```

### Subscription status
```bash
curl -X GET "http://localhost:8000/subscription/status" \
     -H "Authorization: Bearer <TOKEN>"
```

### Create payment order
```bash
curl -X POST "http://localhost:8000/payment/create-order" \
     -H "Authorization: Bearer <TOKEN>"
```

### Verify payment
```bash
curl -X POST "http://localhost:8000/payment/verify" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"razorpay_payment_id":"pay_...","razorpay_subscription_id":"sub_...","razorpay_signature":"..."}'
```

### Cancel subscription
```bash
curl -X POST "http://localhost:8000/subscription/cancel" \
     -H "Authorization: Bearer <TOKEN>"
```

---

## Database Schema

### `threads` Table
| Column       | Type                     | Description |
|--------------|--------------------------|-------------|
| `id`         | `uuid` (PK)              | Unique thread ID |
| `user_id`    | `uuid` (FK)              | Reference to `neon_auth."user"` |
| `title`      | `text`                   | Title of the conversation |
| `created_at` | `timestamp with timezone`| Creation timestamp |
| `updated_at` | `timestamp with timezone`| Last update timestamp |

### `chat_history` Table
| Column       | Type                     | Description |
|--------------|--------------------------|-------------|
| `id`         | `uuid` (PK)              | Unique message ID |
| `thread_id`  | `uuid` (FK)              | Reference to `threads.id` |
| `query`      | `text`                   | User's message |
| `response`   | `text`                   | AI's response |
| `created_at` | `timestamp with timezone`| Timestamp of the message |

### `interview_profiles` Table
| Column       | Type                     | Description |
|--------------|--------------------------|-------------|
| `id`         | `uuid` (PK)              | Profile ID |
| `user_id`    | `uuid`                   | Authenticated user |
| `name`       | `text`                   | Profile label |
| `role`       | `text`                   | Job role (`job_role` in the API maps here) |
| `resume_text`| `text`                   | Resume body or uploaded file URL |
| `is_default` | `boolean`                | Default profile flag |
| `sort_order` | `integer`                | Display order |
| `created_at` | `timestamp with timezone`| Creation timestamp |
| `updated_at` | `timestamp with timezone`| Last update timestamp |

### `interview_sessions` Table
| Column            | Type                     | Description |
|-------------------|--------------------------|-------------|
| `id`              | `uuid` (PK)              | Session ID |
| `user_id`         | `uuid`                   | Authenticated user |
| `profile_id`      | `uuid` (FK, nullable)    | `interview_profiles.id` |
| `started_at`      | `timestamp with timezone`| Session start |
| `ended_at`        | `timestamp with timezone`| Session end (optional) |
| `job_title`       | `text`                   | Job title for this session |
| `job_description` | `text`                   | Job description |

### `interview_responses` Table
| Column       | Type                     | Description |
|--------------|--------------------------|-------------|
| `id`         | `uuid` (PK)              | Row ID |
| `session_id` | `uuid`                   | `interview_sessions.id` |
| `query`      | `text`                   | Extracted text (screen) or user question (transcript) |
| `response`   | `text`                   | LLM answer |
| `query_type` | `text`                   | `screen` or `transcript` |
| `created_at` | `timestamp with timezone`| Creation timestamp |

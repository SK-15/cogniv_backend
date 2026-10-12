# Cogniv API Documentation

This API provides authentication via Supabase and streaming chat responses from OpenAI and Gemini.

## Base URL
- **Production**: `https://chatbot-apis-deo7.onrender.com`
- **Local**: `http://localhost:8000`

## Security

### CORS
Only `https://cogniv.co.in` is allowed by default. Configurable via `CORS_ORIGINS` env var (comma-separated for multiple origins, e.g. local dev).

### Rate Limiting
Applied per client IP address, taken from Cloudflare's `CF-Connecting-IP` header (Render passes client-sent `X-Forwarded-For` through unfiltered, so it is not trusted). Returns `429 Too Many Requests` on breach.

| Endpoint | Limit |
|----------|-------|
| `POST /login` | 10 req/min |
| `POST /signup` | 5 req/min |
| `POST /refresh` | 30 req/min |
| `POST /auth/google/mobile` | 10 req/min |
| `POST /auth/password-reset/request` | 5 req/min |
| `POST /auth/password-reset/confirm` | 10 req/min |
| `POST /payment/create-order` | 10 req/min |
| `POST /payment/verify` | 10 req/min |
| `POST /ai-answer` | 60 req/min |
| `POST /analyse-screen` | 20 req/min |
| `POST /deepgram/token` | 30 req/min |
| `POST /launch_signup` | 5 req/min |
| `POST /track` | 120 req/min |

### Request Size Limits
| Input | Limit | Over the limit |
|-------|-------|----------------|
| Resume upload (`/save_profile`) | 5 MB | `413` |
| Screen image (`/analyse-screen`) | 8 MB | `413` |
| Question text (`/ai-answer` `question` / `query` / `text`) | 4000 characters | `422` |

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
- Unexpected server errors return a generic message; details are only logged.
- `422` validation errors are logged with the path and error locations only — never the request body or the offending values.

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

### 2a. Google Sign-In (mobile / native app)
Sign in with Google from a native mobile app. The app obtains a Google **`id_token`** on-device using the Google Sign-In SDK (Android/iOS) and posts it here. The backend verifies the token against Google's public certificates (signature, `iss`, `aud`, expiry), find-or-creates the user, and returns the same unified session tokens as `/login`.

*   **URL**: `/auth/google/mobile`
*   **Method**: `POST`
*   **Rate limit**: 10 requests/minute per IP
*   **Request Body**:
    ```json
    {
      "id_token": "google-id-token-string"
    }
    ```
    *   `id_token`: **Required**. The `idToken` returned by the Google Sign-In SDK on-device.
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
        The returned tokens behave exactly like those from `/login`: refresh via `/refresh` and revoke via `/logout`.
*   **Error Responses**:
    *   **Code**: 401 `{"detail": "invalid_id_token"}` (or a specific reason such as `invalid_audience`, `invalid_issuer`, `email_not_verified`)
    *   **Code**: 422 `{"detail": "id_token is required"}`
    *   **Code**: 429 Too Many Requests
*   **Email verification**: the token's `email_verified` claim must be true. Every Google flow (mobile, desktop, web, legacy refresh) refuses an unverified email, because sign-in links to any existing account with that email.
*   **Configuration**: Set the native OAuth client IDs in the environment so their values are accepted as the token's `aud`:
    *   `GOOGLE_ANDROID_CLIENT_ID` — Android OAuth client ID (registered with the app's SHA-1 fingerprint).
    *   `GOOGLE_IOS_CLIENT_ID` — iOS OAuth client ID (registered with the app's bundle ID).
    *   `GOOGLE_CLIENT_ID` is also accepted (useful when the app requests an id_token for a Web/server client ID).

### 3. Refresh session tokens
Exchange a **refresh token** for new `access_token` / `refresh_token` values. Neon Auth (email/password) and the app’s Google OAuth (Electron loopback) use different refresh tokens; set `provider` or use the default `auto` behavior.

*   **URL**: `/refresh`
*   **Method**: `POST`
*   **Rate limit**: 30 requests/minute per IP
*   **Rotation**: app refresh tokens are single-use. The old token is revoked and the new one issued atomically, so two concurrent refreshes with the same token succeed only once. **Reuse detection:** presenting a token that was already used (or revoked by logout) revokes *all* of that user's refresh tokens — every device must sign in again. Clients must store the new `refresh_token` from each response and never retry a refresh with the old one.
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
    *   **Code**: 401 Unauthorized (invalid, expired or reused refresh token)
    *   **Code**: 422 Unprocessable Entity (missing `refresh_token` or invalid `provider`)
    *   **Code**: 429 Too Many Requests

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
    *   `resume`: **Required**. File — `.pdf` or `.docx`, at most 5 MB.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: Row from `interview_profiles` (`id`, `user_id`, `name`, `role`, `resume_text`, `is_default`, `sort_order`, `created_at`, `updated_at`).
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 413 Payload Too Large (resume over 5 MB)
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
Create a session for a given profile and job context. The profile must belong to the authenticated user. Starting a session spends one session credit.

**Session length cap (enforced server-side):** 10 minutes on the free tier, 30 minutes on any paid tier, plus a 2-minute grace period. Past that, `/ai-answer`, `/analyse-screen` and `/deepgram/token` return `410 session_ended` and the session is closed at its cap.

**Unended previous session:** if the user's latest session is still open, the same request (same `profile_id`, `job_title`, `job_description`) within its cap returns that session with `"reused": true` and no charge (double-submit / app restart). Otherwise the old session is closed and a new one is created and charged.

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
        or, for a reused open session, `{ "session_id": "...", "reused": true }`.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 402 Payment Required (`{"detail": {"error": "quota_exceeded", ...}}` — no session credits left)
    *   **Code**: 404 Not Found (profile missing or not owned by user; the credit is refunded)
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
    *   `file`: The image file to analyze, at most 8 MB.
    *   `session_id`: **Required**. UUID of an `interview_sessions` row owned by the user.
*   **Rate limit**: 20 requests/minute per IP
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content-Type**: `text/event-stream`
    *   **Body**:
        1. **First line**: a single JSON object (UTF-8) ending with a newline, e.g. `{"extracted_text":"..."}\n`. This is the OCR result; parse it before treating the rest of the body as streamed text.
        2. **Remainder**: streamed answer text (same style as `/chat`—incremental chunks, not a second JSON envelope).
        3. If the LLM fails mid-stream, the body ends with the line `\n\n[Answer interrupted — please try again.]` (still HTTP 200, since headers are already sent). The partial answer is saved.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 410 Gone — the session has ended, or ran past its length cap (it is closed at the cap):
        ```json
        { "detail": { "error": "session_ended", "message": "This session has ended. Start a new session to continue." } }
        ```
    *   **Code**: 413 Payload Too Large (image over 8 MB)
    *   **Code**: 422 Unprocessable Entity (invalid `session_id`)
    *   **Code**: 429 Too Many Requests
    *   **Code**: 500 Internal Server Error (generic message; details are logged)

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
    *   Provide exactly one of `question`, `query`, or `text` for the prompt (max 4000 characters).
    *   `provider`: Optional. `"openai"` (default), `"gemini"`, or `"anthropic"` / `"claude"`.
*   **Rate limit**: 60 requests/minute per IP
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content-Type**: `text/event-stream`
    *   **Body**: A stream of text chunks (same as `/chat`). If the LLM fails mid-stream, the body ends with `\n\n[Answer interrupted — please try again.]` (still HTTP 200). The partial answer is saved.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 410 Gone — the session has ended, or ran past its length cap (it is closed at the cap):
        ```json
        { "detail": { "error": "session_ended", "message": "This session has ended. Start a new session to continue." } }
        ```
    *   **Code**: 422 Unprocessable Entity (invalid `session_id`, missing question field, or question over 4000 characters)
    *   **Code**: 429 Too Many Requests
    *   **Code**: 500 Internal Server Error (generic message; details are logged)

### 19. Deepgram Transcription Token
Short-lived Deepgram token for streaming transcription straight to Deepgram. The account API key never leaves the server (the old `GET /deepgram/api_key` and `GET /gemini/api_key` endpoints are removed). The token only has to be valid when the WebSocket opens (60 s TTL); the stream keeps running after it expires. Requires an active interview session.

*   **URL**: `/deepgram/token`
*   **Method**: `POST`
*   **Rate limit**: 30 requests/minute per IP
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    { "session_id": "uuid-of-active-interview-session" }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        { "access_token": "eyJ...", "expires_in": 60 }
        ```
*   **Using the token**: open `wss://api.deepgram.com/v1/listen?...` with header `Authorization: Bearer <access_token>` (server/Node), or WebSocket subprotocols `['bearer', <access_token>]` in a browser.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 410 Gone — the session has ended, or ran past its length cap (it is closed at the cap):
        ```json
        { "detail": { "error": "session_ended", "message": "This session has ended. Start a new session to continue." } }
        ```
    *   **Code**: 422 Unprocessable Entity (invalid `session_id`)
    *   **Code**: 502 Bad Gateway (Deepgram refused or could not be reached)
    *   **Code**: 503 Service Unavailable (`DEEPGRAM_API_KEY` not configured)

### 20. End Interview Session
Mark a session as ended and store its duration. The stored `duration_seconds` is the client's value, capped at the time actually elapsed since the session started.

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
    *   `nonce`: Optional (recommended). A fresh random value per login, 32–128 URL-safe characters (`A-Z a-z 0-9 _ -`). It is signed into the OAuth `state` and echoed back on the loopback redirect; the app should reject any callback whose `nonce` doesn't match the one it sent (login CSRF protection). Omit it only for older app builds.
*   **Success Response**:
    *   **Code**: 302 Redirect → Google OAuth consent URL
*   **Error Response**:
    *   **Code**: 422 Unprocessable Entity (`nonce` malformed)

---

### 22. Google OAuth — Callback (Electron)
Receives the OAuth code from Google, exchanges it, and redirects to the Electron loopback server.

*   **URL**: `/auth/google/callback`
*   **Method**: `GET`
*   **Query Parameters** (set by Google):
    *   `code`, `state`, `error`
*   **Success Response**:
    *   **Code**: 302 Redirect → `http://127.0.0.1:{local_port}/callback?access_token=...&refresh_token=...&user_id=...&nonce=<nonce>` (`nonce` only when the start request sent one)
*   **Error Response**:
    *   **Code**: 302 Redirect → `http://127.0.0.1:{local_port}/callback?error=<message>&nonce=<nonce>` (`nonce` present when the `state` was valid and carried one)
    *   `error` values include `invalid_state`, `token_exchange_failed`, `no_email_from_google`, `email_not_verified`, or Google's own error (e.g. `access_denied`).

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
    *   **Code**: 302 Redirect → `https://cogniv.co.in/auth/callback?error=<message>` (errors via query param, not fragment). Includes `email_not_verified` when Google reports the email as unverified.

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
Confirm a Razorpay checkout and credit the plan's sessions. Called from the checkout `handler` callback.

The checkout signature is verified, then the order and payment are fetched from Razorpay: the plan, price and owner come from the order (created by `/payment/create-order` with `notes.plan_id` and `notes.user_id`), and the payment must be `captured` (an `authorized` payment is captured here). If Razorpay cannot be reached nothing is credited — the webhook (§29) credits the order once Razorpay delivers it. Crediting is idempotent per `order_id`, so verify and the webhook never double-credit.

*   **URL**: `/payment/verify`
*   **Method**: `POST`
*   **Rate limit**: 10 requests/minute per IP
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "razorpay_order_id": "order_...",
      "razorpay_payment_id": "pay_...",
      "razorpay_signature": "signature-string",
      "plan_id": "starter"
    }
    ```
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        { "ok": true, "sessions_credited": 7 }
        ```
*   **Error Response**:
    *   **Code**: 400 Bad Request (signature mismatch, unknown plan, `plan_id` differs from the order, or the payment is unpaid / wrong amount)
    *   **Code**: 401 Unauthorized
    *   **Code**: 403 Forbidden (the order was created by another account)
    *   **Code**: 429 Too Many Requests
    *   **Code**: 502 Bad Gateway (Razorpay unreachable — nothing credited; the webhook will credit it)
    *   A payment Razorpay reports as unpaid / wrong amount returns `{"detail": "Payment not verified"}` — the specific reason is logged, not returned.

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
Credits a paid order even if the client never reaches `/payment/verify` (app closed, network drop). No auth — validated by the Razorpay HMAC signature header. Applies the same Razorpay checks as §27 and credits the user in the order's `notes.user_id`.

**Setup:** Razorpay Dashboard → Settings → Webhooks → URL `https://<host>/payment/webhook`, events `payment.captured` and `order.paid` (optionally `payment.authorized` if auto-capture is off), and set the same secret as `RAZORPAY_WEBHOOK_SECRET` on the server.

*   **URL**: `/payment/webhook`
*   **Method**: `POST`
*   **Headers**:
    *   `X-Razorpay-Signature`: HMAC-SHA256 of the raw body with `RAZORPAY_WEBHOOK_SECRET`
*   **Events handled**: `payment.authorized`, `payment.captured`, `order.paid`; others are acknowledged and ignored.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: `{ "ok": true, "credited": true }` (`false` if already credited), or `{ "ok": true, "ignored": true }`
*   **Error Response**:
    *   **Code**: 400 Bad Request (invalid signature or JSON)
    *   **Code**: 503 Service Unavailable (secret not configured, or Razorpay unreachable — Razorpay retries)

---

### 31. Admin — Adjust User Sessions
**Admin-only.** Grant or revoke purchased sessions for any user. The caller's account must have `role = "admin"`.

*   **URL**: `/admin/users/{target_user_id}/sessions`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>` (must belong to an admin)
    *   `Content-Type: application/json`
*   **Path Parameters**:
    *   `target_user_id`: **Required**. UUID of the user whose sessions are being adjusted.
*   **Request Body**:
    ```json
    { "delta": 5 }
    ```
    *   `delta`: **Required**. Integer. Positive **adds** sessions, negative **removes** them. The stored `sessions_purchased` total is clamped at `0` (cannot go negative).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "user_id": "uuid",
          "sessions_used": 3,
          "sessions_purchased": 10,
          "sessions_remaining": 8,
          "plan_tier": "free"
        }
        ```
        *   Same shape used elsewhere for quota display — `sessions_remaining = max(0, free_session_limit + sessions_purchased - sessions_used)`.
*   **Error Responses**:
    *   **Code**: 403 Forbidden — `{ "detail": "admin_required" }` (caller is not an admin)
    *   **Code**: 422 Unprocessable Entity — `{ "detail": "invalid user_id" }` (malformed UUID)
    *   **Code**: 404 Not Found — `{ "detail": "User not found" }`
    *   **Code**: 401 Unauthorized — missing/invalid token

---

### 32. Track Site Event (Analytics)
Records a marketing-site event — a page visit or a download button click — together with the visitor's details. **Public and anonymous: no auth required.**

*   **URL**: `/track`
*   **Method**: `POST`
*   **Headers**:
    *   `Content-Type: application/json`
    *   `Authorization: Bearer <access_token>` — *optional*. If a valid token is sent, `user_id` is attached to the event. An invalid token is **ignored**, not rejected.
*   **Rate Limit**: 120 requests / minute per IP.
*   **Sources**: the marketing site sends `source: "web"`; the desktop app sends `source: "app"` with its `app_version` and a Bearer token, so app events attach to `user_id`.
*   **Request Body**:
    ```json
    {
      "event": "page_view",
      "path": "/?utm_source=google",
      "referrer": "https://google.com",
      "visitor_id": "b1f0...",
      "session_id": "8c2a...",
      "utm_source": "google",
      "utm_medium": "cpc",
      "utm_campaign": "launch",
      "utm_term": null,
      "utm_content": null,
      "language": "en-IN",
      "timezone": "Asia/Kolkata",
      "screen_w": 1920,
      "screen_h": 1080,
      "meta": {},
      "source": "web",
      "app_version": null
    }
    ```
    *   `event`: **Required**. Web: `page_view`, `download_click`. Desktop app: `app_launch`, `session_start`, `session_end`, `answer_requested`, `screen_analysed`, `quota_exceeded`, `upgrade_clicked`. Anything else is rejected.
    *   `source`: `web` (default) or `app`. Anything else is rejected.
    *   `app_version`: Desktop app version, e.g. `1.0.3`. Ignored for web events.
    *   `visitor_id`: Browser-generated UUID kept in `localStorage` — stable across visits.
    *   `session_id`: Browser-generated UUID kept in `sessionStorage` — one per tab session.
    *   `meta`: Free-form JSON. The download button sends `{ "href": ..., "label": ..., "id": ... }`.
    *   Every other field is optional.
*   **Captured server-side** (not sent by the client):
    *   `ip` — the client IP from Cloudflare's `CF-Connecting-IP` header. A client-sent `X-Forwarded-For` is not trusted.
    *   `user_agent` — from the request header.
    *   `country` / `region` / `city` — resolved from the IP in a **background task** (never blocks the response), cached per-IP for 24h.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**: `{ "ok": true, "id": "uuid" }`
*   **Error Responses**:
    *   **Code**: 422 Unprocessable Entity — unknown `event` value.
    *   **Code**: 429 Too Many Requests — rate limit exceeded.
    *   A database failure returns **200** with `{ "ok": false }` — analytics must never break the page.

---

### 33. Admin — Dashboard Stats
**Admin-only.** Everything behind the admin dashboard at `/admin` on the website: acquisition funnel, daily trend, revenue and traffic. Read-only.

*   **URL**: `/admin/stats`
*   **Method**: `GET`
*   **Headers**:
    *   `Authorization: Bearer <access_token>` (must belong to an admin — `role = 'admin'` on the caller's `neon_auth."user"` row)
*   **Query Parameters**:
    *   `days`: Optional, default `30`, range `1`–`365`. Size of the reporting window.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "generated_at": "2026-10-10T15:21:29+00:00",
          "range_days": 30,
          "tracking_started_on": "2026-09-12",
          "totals": {
            "users": 105, "users_new_7d": 4, "users_new_window": 21,
            "sessions": 159, "sessions_window": 19, "sessions_ended": 1,
            "avg_session_seconds": 40, "page_views": 278, "downloads": 34
          },
          "funnel": {
            "visitors": 248, "download_clicks": 34, "unique_downloaders": 25,
            "signups": 21, "sessions_started": 19,
            "rates": { "visitor_to_download": 10.1, "download_to_signup": 84.0, "signup_to_session": 90.5 }
          },
          "timeseries": [ { "date": "2026-10-09", "signups": 1, "visitors": 24, "downloads": 1, "sessions": 2 } ],
          "revenue": {
            "total_paise": 309700, "paying_users": 3,
            "by_plan": [ { "plan_id": "starter", "purchases": 2, "amount_paise": 159800, "sessions_sold": 14 } ]
          },
          "traffic": {
            "referrers": [ { "referrer": "direct", "views": 209 } ],
            "pages":     [ { "path": "/", "views": 180 } ],
            "campaigns": [ { "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "launch", "views": 4 } ],
            "countries": [ { "country": "India", "visitors": 58 } ]
          },
          "app_events": [ { "event": "session_start", "n": 12, "installs": 3 } ]
        }
        ```
*   **Notes**:
    *   `timeseries` is zero-filled across the whole window, so a quiet day is a `0` rather than a missing entry.
    *   `totals.users`, `totals.sessions` and the whole `revenue` block are all-time. Everything else is scoped to `days`.
    *   Website figures only exist from `tracking_started_on` onward; earlier traffic was never recorded. `app_events` stays empty until a desktop build carrying analytics is released.
    *   Amounts are **paise** (`309700` = ₹3,097).
*   **Error Responses**:
    *   **Code**: 403 Forbidden — `{ "detail": "admin_required" }` (caller is not an admin)
    *   **Code**: 401 Unauthorized — missing/invalid token
    *   **Code**: 422 Unprocessable Entity — `days` outside 1–365

---

### 34. Health Check
Liveness probe for Render / uptime monitors. Runs `SELECT 1` against the database with a short timeout (~3 s).

*   **URL**: `/health`
*   **Method**: `GET`
*   **Success Response**: **Code**: 200 OK — `{ "ok": true }`
*   **Error Response**: **Code**: 503 Service Unavailable — `{ "ok": false }` (database unreachable or slow)

---

### 35. Password Reset — Request
Email the user a password-reset link via Neon Auth (Better Auth `POST /request-password-reset`). The link sends the user to `PASSWORD_RESET_REDIRECT_URL` (default `https://www.cogniv.co.in/reset-password/`) with `?token=<token>` appended (or `?error=INVALID_TOKEN` if the link is invalid/expired). Neon Auth reset links expire after 15 minutes.

*   **URL**: `/auth/password-reset/request`
*   **Method**: `POST`
*   **Rate limit**: 5 requests/minute per IP
*   **Request Body**:
    ```json
    { "email": "user@example.com" }
    ```
*   **Success Response**: **Code**: 200 OK — `{ "ok": true }`
    *   Always 200, whether or not an account exists for the email (no account enumeration). Upstream failures are logged, not returned.
*   **Error Response**:
    *   **Code**: 422 Unprocessable Entity (missing `email`)
    *   **Code**: 429 Too Many Requests
*   **Configuration**: the redirect URL's origin must be a trusted origin / allowed domain in the Neon Auth project settings, and email/password sign-in must be enabled.

---

### 36. Password Reset — Confirm
Set a new password using the token from the reset link (Better Auth `POST /reset-password`). On success, all of the user's app refresh tokens are revoked, so every device must sign in again.

*   **URL**: `/auth/password-reset/confirm`
*   **Method**: `POST`
*   **Rate limit**: 10 requests/minute per IP
*   **Request Body**:
    ```json
    { "token": "token-from-reset-link", "new_password": "at-least-8-chars" }
    ```
    *   `new_password`: 8–128 characters.
*   **Success Response**: **Code**: 200 OK — `{ "ok": true }`
*   **Error Response**:
    *   **Code**: 400 Bad Request — `{ "detail": "invalid_or_expired_token" }`
    *   **Code**: 422 Unprocessable Entity (`new_password` shorter than 8 or longer than 128 characters, or missing fields)
    *   **Code**: 429 Too Many Requests
    *   **Code**: 502 Bad Gateway — `{ "detail": "password_reset_unavailable" }` (Neon Auth unreachable)

---

### 37. Export My Data
Download everything stored about the authenticated user as a JSON file (DPDP / GDPR access request).

*   **URL**: `/user/me/export`
*   **Method**: `GET`
*   **Headers**: `Authorization: Bearer <access_token>`
*   **Success Response**:
    *   **Code**: 200 OK, `Content-Disposition: attachment; filename="cogniv-data.json"`
    *   **Content**:
        ```json
        {
          "account": { "id": "uuid", "email": "user@example.com", "name": "Jane", "created_at": "2026-01-01T00:00:00+00:00" },
          "interview_profiles": [ { "id": "uuid", "name": "...", "role": "...", "resume_text": "..." } ],
          "interview_sessions": [ { "id": "uuid", "job_title": "...", "started_at": "...", "ended_at": "..." } ],
          "interview_responses": [ { "session_id": "uuid", "query": "...", "response": "...", "query_type": "transcript", "created_at": "..." } ],
          "purchases": [ { "plan_id": "starter", "sessions": 7, "amount": 79900, "order_id": "order_...", "payment_id": "pay_...", "created_at": "..." } ],
          "subscription": { "plan_tier": "starter", "sessions_purchased": 7 },
          "free_usage": { "sessions_used": 3 },
          "site_events": [ { "event": "app_launch", "source": "app", "created_at": "..." } ],
          "chat_threads": [ { "id": "uuid", "title": "...", "created_at": "..." } ],
          "chat_history": [ { "thread_id": "uuid", "query": "...", "response": "...", "created_at": "..." } ],
          "usage_daily": [ { "day": "2026-10-01", "ai_answer_count": 4, "screen_analysis_count": 1 } ]
        }
        ```
        Row objects contain every column of their table (abbreviated above). `site_events` lists analytics events linked to the account; events recorded before sign-in are anonymous and not linked.
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (user row missing)

---

### 38. Delete My Account
Permanently delete the authenticated user's data, in one transaction. **Irreversible.**

*   **URL**: `/user/me`
*   **Method**: `DELETE`
*   **Headers**: `Authorization: Bearer <access_token>`, `Content-Type: application/json`
*   **Request Body**:
    ```json
    { "confirm": "DELETE" }
    ```
*   **What happens**:
    *   **Deleted**: interview responses, interview sessions, interview profiles (incl. resume text), legacy chat threads and history, daily usage counters, subscription, free-usage counter, all refresh tokens, Neon Auth sessions, Neon Auth login methods (password hash, Google link) and pending reset/verification tokens.
    *   **Anonymized**: analytics events lose their `user_id` and `ip`; the `neon_auth.user` row is kept as a tombstone with its email replaced by `deleted-<id>@deleted.cogniv.invalid`, name `Deleted user`, no image.
    *   **Retained**: purchase records (`purchases`: plan, amount, Razorpay order/payment IDs, date) — payment records are retained as legally required for tax/accounting. They stay linked to the anonymized tombstone (deleting the user row would cascade-delete them).
    *   The user can no longer sign in. Signing up again with the same email creates a new, empty account. Already-issued access tokens stay valid until they expire (≤1h) but no data remains behind them.
*   **Success Response**: **Code**: 200 OK — `{ "ok": true }`
*   **Error Response**:
    *   **Code**: 400 Bad Request (`confirm` is not exactly `"DELETE"`)
    *   **Code**: 401 Unauthorized
    *   **Code**: 500 Internal Server Error (nothing was deleted — the transaction rolled back)

---

### 39. Launch Signup
Record a pre-launch signup from the marketing site. Public.

*   **URL**: `/launch_signup`
*   **Method**: `POST`
*   **Rate limit**: 5 requests/minute per IP
*   **Request Body**:
    ```json
    { "email": "user@example.com", "phone": "+91...", "name": "Jane", "profession": "Engineer" }
    ```
*   **Success Response**: **Code**: 200 OK — `{ "message": "Launch signup recorded", "id": "uuid" }` (the submitted details are not echoed back)
*   **Error Response**:
    *   **Code**: 422 Unprocessable Entity (missing fields)
    *   **Code**: 429 Too Many Requests
    *   **Code**: 500 Internal Server Error (generic message)

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
     -d '{"razorpay_order_id":"order_...","razorpay_payment_id":"pay_...","razorpay_signature":"...","plan_id":"starter"}'
```

### Cancel subscription
```bash
curl -X POST "http://localhost:8000/subscription/cancel" \
     -H "Authorization: Bearer <TOKEN>"
```

### Admin — adjust user sessions
```bash
curl -X POST "http://localhost:8000/admin/users/<TARGET_USER_ID>/sessions" \
     -H "Authorization: Bearer <ADMIN_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"delta": 5}'
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

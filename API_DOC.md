# Supabase LLM Chatbot API Documentation

This API provides authentication via Supabase and streaming chat responses from OpenAI and Gemini.

## Base URL
`http://localhost:8000`

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
*   **Error Response**:
    *   **Code**: 400 Bad Request (if email is invalid or signup fails)

### 2. User Login
Authenticate a user and receive access tokens.

*   **URL**: `/login`
*   **Method**: `POST`
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
*   **Error Response**:
    *   **Code**: 401 Unauthorized (invalid credentials)

### 3. Create New Chat (Thread)
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

### 4. Chat (Streaming)
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

### 5. List Threads
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

### 6. Get Thread History
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

### 7. Delete Thread
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

### 8. Web Search
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

### 9. Upload File
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

### 10. OCR (Structured Text Extraction)
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

### 11. Save Interview Profile
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

### 12. Start Interview Session
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

### 13. Analyse Screen (Text Extraction & Q&A)
Extract raw text from an uploaded image and then use an LLM to answer any questions identified within that text. Persists `query` (extracted text), `response` (LLM answer), and `query_type` = `screen` on `interview_responses` for the given session.

*   **URL**: `/analyse-screen`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Query Parameters**:
    *   `provider`: Optional. LLM for OCR and answering. `"openai"` (default) or `"gemini"`.
*   **Request Body** (form fields):
    *   `file`: The image file to analyze.
    *   `session_id`: **Required**. UUID of an `interview_sessions` row owned by the user.
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "extracted_text": "The raw text found in the image...",
          "answers": "The step-by-step answers generated by the LLM..."
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 422 Unprocessable Entity (invalid `session_id`)
    *   **Code**: 500 Internal Server Error

### 14. AI Answer
Answer a question using OpenAI. Persists `query` (question), `response` (answer), and `query_type` = `transcript` on `interview_responses` for the given session.

*   **URL**: `/ai-answer`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: application/json`
*   **Request Body**:
    ```json
    {
      "session_id": "uuid-of-interview-session",
      "question": "What is the capital of France?"
    }
    ```
    *   `session_id`: **Required**.
    *   Provide exactly one of `question`, `query`, or `text` for the prompt (same as before).
*   **Success Response**:
    *   **Code**: 200 OK
    *   **Content**:
        ```json
        {
          "answer": "The capital of France is Paris."
        }
        ```
*   **Error Response**:
    *   **Code**: 401 Unauthorized
    *   **Code**: 404 Not Found (session not found or not owned)
    *   **Code**: 422 Unprocessable Entity (invalid `session_id` or missing question field)
    *   **Code**: 500 Internal Server Error

### 15. Live Audio Transcription (WebSocket)
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

### Start interview session
```bash
curl -X POST "http://localhost:8000/interview/session" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"profile_id":"<PROFILE_UUID>","job_title":"Role","job_description":"..."}'
```

### Analyse screen
```bash
curl -X POST "http://localhost:8000/analyse-screen?provider=openai" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "file=@/path/to/your/screenshot.png" \
     -F "session_id=<SESSION_UUID>"
```

### AI answer
```bash
curl -X POST "http://localhost:8000/ai-answer" \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"session_id":"<SESSION_UUID>","question":"Summarize STAR method"}'
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

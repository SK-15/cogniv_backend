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
          "url": "https://supabase-storage-url..."
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

### 11. Analyse Screen (Text Extraction & Q&A)
Extract raw text from an uploaded image and then use an LLM to answer any questions identified within that text.

*   **URL**: `/analyse_screen`
*   **Method**: `POST`
*   **Headers**:
    *   `Authorization: Bearer <access_token>`
    *   `Content-Type: multipart/form-data`
*   **Request Body**:
    *   `file`: The image file to analyze (in `multipart/form-data` form).
    *   `provider`: Optional. The LLM provider to use for both OCR and answering. Can be `"gemini"` (default) or `"openai"`.
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
    *   **Code**: 500 Internal Server Error

### 12. AI Answer
Answer a question provided in the request body using OpenAI.

*   **URL**: `/ai-answer`
*   **Method**: `POST`
*   **Request Body**:
    ```json
    {
      "question": "What is the capital of France?"
    }
    ```
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
    *   **Code**: 500 Internal Server Error

### 13. Live Audio Transcription (WebSocket)
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

### Analyse Screen
```bash
curl -X POST "http://localhost:8000/analyse_screen" \
     -H "Authorization: Bearer <TOKEN>" \
     -F "file=@/path/to/your/screenshot.png" \
     -F "provider=gemini"
```

---

## Database Schema

### `threads` Table
| Column       | Type                     | Description |
|--------------|--------------------------|-------------|
| `id`         | `uuid` (PK)              | Unique thread ID |
| `user_id`    | `uuid` (FK)              | Reference to `auth.users` |
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

from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Query, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from typing import Optional
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from modules.config import settings

import asyncio

import logging
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
from modules.auth import sign_up_user, login_user, get_user
from modules.chat import get_user_threads, get_thread_chats, create_thread, save_chat_message, delete_thread
from modules.llm import stream_openai, stream_gemini
from modules.websearch import web_search_task
from modules.storage import upload_to_supabase
from modules.ocr import extract_structured_text, extract_text
from modules.llm import generate_response
from modules.resume_text import extract_resume_text, ResumeTextExtractionError
from modules.interview import (
    save_interview_profile,
    create_interview_session,
    insert_interview_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Supabase LLM Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

ALLOWED_RESUME_SUFFIXES = {".pdf", ".docx"}


async def require_user_id(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.split(" ", 1)[1]
    user_res = await get_user(token)
    if not user_res.user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user_res.user.id


def _parse_form_bool(value: str) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _resume_filename_allowed(filename: str | None) -> bool:
    if not filename:
        return False
    return Path(filename).suffix.lower() in ALLOWED_RESUME_SUFFIXES

class AuthRequest(BaseModel):
    email: str
    password: str

class ChatRequest(BaseModel):
    prompt: str
    thread_id: str
    provider: str = "openai"  # "openai" or "gemini"

class NewChatRequest(BaseModel):
    title: str = "New Chat"

class WebSearchRequest(BaseModel):
    query: str

class OCRRequest(BaseModel):
    provider: str = "gemini"
    prompt: Optional[str] = None

class InterviewSessionRequest(BaseModel):
    profile_id: str
    job_title: str
    job_description: str


class AIAnswerRequest(BaseModel):
    session_id: str
    question: Optional[str] = None
    query: Optional[str] = None
    text: Optional[str] = None

    def get_question(self) -> str:
        q = self.question or self.query or self.text
        if not q:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Provide 'question', 'query', or 'text' in the request body.")
        return q

@app.post("/signup")
async def signup(request: AuthRequest):
    try:
        response = await sign_up_user(request.email, request.password)
        if response.user:
            return {"message": "User created successfully", "user_id": response.user.id}
        raise HTTPException(status_code=400, detail="Signup failed")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/login")
async def login(request: AuthRequest):
    try:
        response = await login_user(request.email, request.password)
        if response.session:
            return {
                "access_token": response.session.access_token,
                "refresh_token": response.session.refresh_token,
                "user_id": response.user.id
            }
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

@app.post("/new_chat")
async def new_chat(request: NewChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        thread = await create_thread(user_res.user.id, request.title)
        if not thread:
            raise HTTPException(status_code=500, detail="Failed to create thread")
            
        return thread
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat")
async def chat(request: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        # Verify user with Supabase
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_res.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication failed")

    # Fetch chat history
    history = await get_thread_chats(user_id, request.thread_id)

    async def generate_and_save():
        full_response = ""
        
        if request.provider == "gemini":
            generator_func = stream_gemini(request.prompt, history=history)
        else:
            generator_func = stream_openai(request.prompt, history=history)
            
        try:
            async for chunk in generator_func:
                full_response += chunk
                yield chunk
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            # Save persistence after stream is done or interrupted
            if full_response:
                # Use asyncio.create_task to ensure it runs even if request is cancelled
                asyncio.create_task(save_chat_message(user_id, request.thread_id, request.prompt, full_response))

    return StreamingResponse(generate_and_save(), media_type="text/event-stream")

@app.post("/upload")
async def upload_file(
    request: Request,
    thread_id: str,
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    logger.info(f"[/upload] Request received — filename: '{file.filename}', content_type: '{file.content_type}', thread_id: '{thread_id}'")

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("[/upload] Missing or invalid Authorization header")
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = authorization.split(" ")[1]
    logger.info("[/upload] Token extracted, verifying user...")

    try:
        user_res = await get_user(token)
        if not user_res.user:
            logger.warning("[/upload] get_user returned no user — invalid session")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_res.user.id
        logger.info(f"[/upload] Authenticated user_id: {user_id}")

        # Upload to Supabase Storage
        logger.info(f"[/upload] Calling upload_to_supabase for '{file.filename}'...")
        public_url = await upload_to_supabase(file, base_url=str(request.base_url))
        logger.info(f"[/upload] upload_to_supabase returned: {public_url}")

        if not public_url:
            logger.error("[/upload] upload_to_supabase returned None — raising 500")
            raise HTTPException(status_code=500, detail="Failed to upload file")

        # Parse file content (assuming text/markdown/code for now)
        # We need to reset the file cursor because upload_to_supabase read it
        logger.info("[/upload] Seeking file back to 0 and re-reading content...")
        await file.seek(0)
        content = await file.read()
        logger.info(f"[/upload] Re-read {len(content)} bytes from file")

        try:
            text_content = content.decode("utf-8")
            logger.info(f"[/upload] File decoded as UTF-8, length: {len(text_content)} chars")
        except UnicodeDecodeError:
            # If binary, just use the URL
            logger.info("[/upload] File is binary — using URL reference as text content")
            text_content = f"I have uploaded a file: {file.filename}. Access it here: {public_url}"
        else:
            # If text, include a snippet or full content
            text_content = f"I have uploaded a file '{file.filename}'.\n\nContent:\n{text_content}"

        ai_acknowledgement = f"Received file: {file.filename}."

        logger.info(f"[/upload] Saving chat message for user_id={user_id}, thread_id={thread_id}...")
        try:
            save_success = await save_chat_message(user_id, thread_id, text_content, ai_acknowledgement)
            if save_success:
                logger.info("[/upload] Chat message saved successfully")
            else:
                logger.error("[/upload] save_chat_message returned False — check database logs or thread_id validity")
        except Exception as db_err:
            logger.error(f"[/upload] Failed to save chat message to database: {db_err}", exc_info=True)

        logger.info(f"[/upload] Upload complete. Returning URL: {public_url}")
        return {"message": "File uploaded and processed", "url": public_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[/upload] Unexpected exception: {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/threads")
async def get_threads(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        user_id = user_res.user.id
        threads = await get_user_threads(user_id)
        return {"threads": threads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/threads/{thread_id}/chats")
async def get_chats(thread_id: str, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        user_id = user_res.user.id
        # In a real app we might verify thread ownership here or rely on RLS
        chats = await get_thread_chats(user_id, thread_id)
        return {"chats": chats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/threads/{thread_id}")
async def delete_thread_endpoint(thread_id: str, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        user_id = user_res.user.id
        success = await delete_thread(user_id, thread_id)
        
        if not success:
            raise HTTPException(status_code=404, detail="Thread not found or could not be deleted")
            
        return {"message": "Thread deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/websearch")
async def websearch(request: WebSearchRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        
        # Perform web search
        answer = await web_search_task(request.query)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ocr")
async def ocr_endpoint(
    file: UploadFile = File(...),
    provider: str = "gemini",
    prompt: Optional[str] = None,
    authorization: str = Header(None)
):
    """
    Perform OCR on an uploaded image and return structured text as JSON.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
            
        # Read image bytes
        image_bytes = await file.read()
        mime_type = file.content_type or "image/jpeg"
        
        # Extract structured text
        structured_data = await extract_structured_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider=provider,
            prompt=prompt
        )
        
        if structured_data is None:
            raise HTTPException(status_code=500, detail="OCR extraction failed")
            
        return structured_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OCR endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/save_profile")
async def save_profile(
    job_role: str = Form(...),
    name: str = Form(...),
    is_default: str = Form("false"),
    resume: UploadFile = File(...),
    user_id: str = Depends(require_user_id),
):
    if not _resume_filename_allowed(resume.filename):
        raise HTTPException(
            status_code=422,
            detail="Resume must be a PDF or DOCX file (.pdf, .docx)",
        )

    resume_bytes = await resume.read()
    if not resume_bytes:
        raise HTTPException(status_code=422, detail="Resume file is empty.")

    try:
        resume_text = await asyncio.to_thread(
            extract_resume_text, filename=resume.filename, data=resume_bytes
        )
    except ResumeTextExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    row = await save_interview_profile(
        user_id=user_id,
        job_role=job_role,
        name=name,
        resume_text=resume_text,
        is_default=_parse_form_bool(is_default),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to save profile")
    return row


@app.post("/interview/session")
async def start_interview_session(
    body: InterviewSessionRequest,
    user_id: str = Depends(require_user_id),
):
    try:
        profile_uuid = UUID(body.profile_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid profile_id")

    session_id = await create_interview_session(
        user_id,
        profile_uuid,
        body.job_title,
        body.job_description,
    )
    if not session_id:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"session_id": str(session_id)}


@app.post("/analyse-screen")
async def analyse_screen(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    provider: str = Query("openai"),
    user_id: str = Depends(require_user_id),
):
    """
    Extract text from an image and use an LLM to answer any questions found within.
    """
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    try:
        # 1. Read image bytes
        image_bytes = await file.read()
        mime_type = file.content_type or "image/jpeg"
        
        # 2. Extract plain text from image
        logger.info(f"[/analyse_screen] Extracting text using {provider}")
        extracted_text = await extract_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider=provider
        )
        
        if not extracted_text:
            raise HTTPException(status_code=500, detail="Failed to extract text from the image")
            
        # 3. Use LLM to answer questions
        logger.info("[/analyse_screen] Text extracted, querying LLM for answers")
        prompt = (
            "Here is the text extracted from an image:\n\n"
            f"```text\n{extracted_text}\n```\n\n"
            "Please carefully review this text, identify any questions or problems present in it, and provide step-by-step answers or solutions to those questions."
        )
        
        llm_response = await generate_response(prompt, model_type=provider)
        
        if not llm_response:
            raise HTTPException(status_code=500, detail="LLM failed to generate a response")

        saved = await insert_interview_response(
            user_id,
            session_uuid,
            extracted_text,
            llm_response,
            "screen",
        )
        if not saved:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "extracted_text": extracted_text,
            "answers": llm_response,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Analyse Screen endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ai-answer")
async def ai_answer(
    request: AIAnswerRequest,
    user_id: str = Depends(require_user_id),
):
    try:
        try:
            session_uuid = UUID(request.session_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid session_id")

        question = request.get_question()
        answer = await generate_response(question, model_type="openai")
        if answer is None:
            raise HTTPException(status_code=500, detail="Failed to generate AI response")

        saved = await insert_interview_response(
            user_id,
            session_uuid,
            question,
            answer,
            "transcript",
        )
        if not saved:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI Answer endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/listen")
async def websocket_endpoint(websocket: WebSocket, token: str = None):
    """
    WebSocket endpoint for live audio transcription using Deepgram.
    Accepts raw audio bytes from the client and streams back transcribed text.
    
    Connect via: ws://host/listen
    Send: raw LINEAR16 PCM audio at 16000 Hz, mono, as binary frames
    Receive: streamed transcript strings as text frames
    """
    await websocket.accept()
    logger.info("[/listen] WebSocket client connected")

    if not settings.deepgram_api_key:
        await websocket.send_text("Error: Deepgram API key not configured.")
        await websocket.close()
        return

    deepgram = DeepgramClient(api_key=settings.deepgram_api_key)
    loop = asyncio.get_event_loop()

    # Thread-safe queue: async WS receiver puts audio → sync thread sends to Deepgram
    import queue as thread_queue
    import threading

    audio_q: thread_queue.Queue = thread_queue.Queue()
    stop_event = threading.Event()

    def deepgram_worker():
        """
        Runs in a background thread.
        Opens a Deepgram v1 streaming connection, then:
          - Thread A: calls start_listening() — blocks, reads transcripts, fires callbacks
          - Thread B: drains audio_q and sends audio bytes to Deepgram
        """
        try:
            with deepgram.listen.v1.connect(
                model="nova-2",
                encoding="linear16",
                sample_rate=16000,
                channels=1,
                smart_format="true",
                interim_results="true",
            ) as connection:
                logger.info("[/listen] Deepgram connection opened")

                def on_message(message):
                    try:
                        transcript = None
                        channel = getattr(message, "channel", None)
                        if channel:
                            alts = getattr(channel, "alternatives", None)
                            if alts:
                                transcript = alts[0].transcript
                        if not transcript:
                            transcript = getattr(message, "transcript", None)
                        if transcript:
                            logger.info(f"[/listen] Transcript: {transcript}")
                            asyncio.run_coroutine_threadsafe(
                                websocket.send_text(transcript), loop
                            )
                    except Exception as e:
                        logger.error(f"[/listen] on_message error: {e}")

                connection.on(EventType.MESSAGE, on_message)
                connection.on(EventType.ERROR, lambda e: logger.error(f"[/listen] Deepgram error: {e}"))
                connection.on(EventType.OPEN, lambda _: logger.info("[/listen] Deepgram stream started"))
                connection.on(EventType.CLOSE, lambda _: logger.info("[/listen] Deepgram stream closed"))

                # Thread A: blocking receive — reads transcripts from Deepgram
                listen_thread = threading.Thread(target=connection.start_listening, daemon=True)
                listen_thread.start()

                # Thread B (this thread): send audio chunks to Deepgram
                while not stop_event.is_set():
                    try:
                        data = audio_q.get(timeout=0.5)
                        connection._send(data)
                    except thread_queue.Empty:
                        continue
                    except Exception as e:
                        logger.error(f"[/listen] send error: {e}")
                        break

                # Signal Deepgram we're done, wait for listen thread
                try:
                    connection._send(b"")  # empty close message
                except Exception:
                    pass
                listen_thread.join(timeout=3.0)

        except Exception as e:
            logger.error(f"[/listen] Deepgram worker error: {e}")
            asyncio.run_coroutine_threadsafe(
                websocket.close(code=1011, reason=str(e)), loop
            )

    try:
        # Run Deepgram worker in background thread
        thread_future = loop.run_in_executor(None, deepgram_worker)

        try:
            while True:
                data = await websocket.receive_bytes()
                audio_q.put(data)
        except WebSocketDisconnect:
            logger.info("[/listen] Client disconnected")
        except Exception as e:
            logger.error(f"[/listen] Receive error: {e}")
        finally:
            stop_event.set()
            await asyncio.wait_for(asyncio.wrap_future(thread_future), timeout=10.0)

    except Exception as e:
        logger.error(f"[/listen] Fatal error: {e}")
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

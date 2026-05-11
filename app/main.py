from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Query, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from typing import Optional
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from modules.config import settings
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import asyncio
import json

import logging
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path
from modules.auth import sign_up_user, login_user, get_user, get_auth_user_row, refresh_neon_auth_session
from modules.auth_google import router as google_router, refresh_google_oauth_tokens
from modules.chat import get_user_threads, get_thread_chats, create_thread, save_chat_message, delete_thread
from modules.llm import stream_openai, stream_gemini, needs_diagram_hint, DIAGRAM_SYSTEM_ADDENDUM, _openai_client
from modules.agent import agent_stream
from modules.websearch import web_search_task
from modules.storage import upload_to_supabase
from modules.ocr import extract_structured_text, extract_text
from modules.resume_text import extract_resume_text, ResumeTextExtractionError
from modules.interview import (
    get_interview_profiles,
    list_interview_sessions,
    save_interview_profile,
    create_interview_session,
    insert_interview_response,
    interview_session_belongs_to_user,
    get_session_prompt_context,
)
from modules.launch_signup import insert_launch_signup
from modules.database import fetch_one
from modules.billing import (
    provision_free_subscription,
    check_can_start_session,
    increment_free_sessions_used,
    end_interview_session,
    get_subscription,
    get_free_usage,
    create_order,
    verify_payment_signature,
    credit_sessions,
    get_purchases,
    FREE_SESSION_LIMIT,
    PLAN_CONFIG,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Supabase LLM Chatbot API")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

app.include_router(google_router)


@app.on_event("startup")
async def warm_llm():
    """Pre-establish OpenAI connection pool to eliminate first-request latency."""
    try:
        await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
    except Exception:
        pass


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


class RefreshTokenRequest(BaseModel):
    refresh_token: str
    provider: Optional[str] = Field(
        None,
        description="`neon`, `google`, or omit for `auto` (try Neon Auth, then Google).",
    )

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


class LaunchSignupRequest(BaseModel):
    email: str
    phone: str
    name: str
    profession: str


class EndSessionRequest(BaseModel):
    duration_seconds: int

class CreateOrderRequest(BaseModel):
    plan_id: str

class VerifyPaymentRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str
    plan_id: str

class AIAnswerRequest(BaseModel):
    session_id: str
    question: Optional[str] = None
    query: Optional[str] = None
    text: Optional[str] = None
    provider: str = "openai"  # "openai" or "gemini"

    def get_question(self) -> str:
        q = self.question or self.query or self.text
        if not q:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Provide 'question', 'query', or 'text' in the request body.")
        return q

@app.post("/signup")
@limiter.limit("5/minute")
async def signup(request: Request, body: AuthRequest):
    try:
        response = await sign_up_user(body.email, body.password)
        if response.user:
            await provision_free_subscription(response.user.id)
            return {"message": "User created successfully", "user_id": response.user.id}
        raise HTTPException(status_code=400, detail="signup_failed")
    except HTTPException:
        raise
    except Exception as e:
        err = str(e).lower()
        if "already" in err or "duplicate" in err or "exists" in err or "taken" in err:
            raise HTTPException(status_code=409, detail="email_taken")
        raise HTTPException(status_code=400, detail="signup_failed")

@app.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, body: AuthRequest):
    try:
        response = await login_user(body.email, body.password)
        if response.session:
            return {
                "access_token": response.session.access_token,
                "refresh_token": response.session.refresh_token,
                "user_id": response.user.id,
            }
        raise HTTPException(status_code=401, detail="invalid_credentials")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_credentials")


@app.post("/refresh")
async def refresh_session(body: RefreshTokenRequest):
    """
    Obtain new access (and possibly refresh) tokens using a refresh token.
    Neon Auth (email/password) and Google OAuth (Electron loopback) use different refresh tokens;
    pass `provider` or leave `auto` to try Neon Auth first, then Google.
    """
    rt = (body.refresh_token or "").strip()
    if not rt:
        raise HTTPException(status_code=422, detail="refresh_token is required")

    mode = (body.provider or "auto").strip().lower()
    if mode not in ("auto", "neon", "google"):
        raise HTTPException(
            status_code=422,
            detail="provider must be 'auto', 'neon', or 'google'",
        )

    if mode == "google":
        out = await refresh_google_oauth_tokens(rt)
        if not out:
            raise HTTPException(status_code=401, detail="Refresh failed")
        return {**out, "provider": "google"}

    if mode == "neon":
        response = await refresh_neon_auth_session(rt)
        if not response or not response.session or not response.user:
            raise HTTPException(status_code=401, detail="Refresh failed")
        return {
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "user_id": response.user.id,
            "provider": "neon",
        }

    response = await refresh_neon_auth_session(rt)
    if response and response.session and response.user:
        return {
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "user_id": response.user.id,
            "provider": "neon",
        }

    out = await refresh_google_oauth_tokens(rt)
    if out:
        return {**out, "provider": "google"}

    raise HTTPException(status_code=401, detail="Refresh failed")


@app.get("/deepgram/api_key")
async def get_deepgram_api_key(_user_id: str = Depends(require_user_id)):
    """Return the Deepgram API key for client-side streaming. Requires a valid Bearer token."""
    if not settings.deepgram_api_key:
        raise HTTPException(status_code=503, detail="Deepgram API key not configured")
    return {"deepgram_api_key": settings.deepgram_api_key}


@app.get("/gemini/api_key")
async def get_gemini_api_key(_user_id: str = Depends(require_user_id)):
    """Return the Gemini API key for client-side use. Requires a valid Bearer token."""
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured")
    return {"gemini_api_key": settings.gemini_api_key}


@app.post("/launch_signup")
async def launch_signup(request: LaunchSignupRequest):
    try:
        row = await insert_launch_signup(
            request.email,
            request.phone,
            request.name,
            request.profession,
        )
    except Exception as e:
        logger.exception("launch_signup insert failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "message": "Launch signup recorded",
        "id": str(row["id"]),
        "email": row["email"],
        "phone": row["phone"],
        "name": row["name"],
        "profession": row["profession"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


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

    system_prompt = DIAGRAM_SYSTEM_ADDENDUM if needs_diagram_hint(request.prompt) else None

    async def generate_and_save():
        full_response = ""
        try:
            async for chunk in agent_stream(
                prompt=request.prompt,
                history=history,
                model=request.provider,
                system_prompt=system_prompt,
            ):
                full_response += chunk
                yield chunk
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            if full_response:
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


@app.get("/user/me")
async def get_current_user_profile(user_id: str = Depends(require_user_id)):
    """
    Auth account for the dashboard (email, name from neon_auth.user).
    """
    row = await get_auth_user_row(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": str(row["id"]),
        "email": row.get("email"),
        "name": row.get("name"),
    }


@app.get("/interview/profiles")
async def list_interview_profiles(
    user_id: str = Depends(require_user_id),
    include_resume: bool = Query(
        True,
        description="If false, omits resume text (lighter payload for listings).",
    ),
):
    profiles = await get_interview_profiles(user_id, include_resume=include_resume)
    return {"profiles": profiles}


@app.get("/interview/sessions")
async def list_past_interview_sessions(
    user_id: str = Depends(require_user_id),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Previous interview sessions for the authenticated user, newest first.
    """
    sessions = await list_interview_sessions(user_id, limit=limit, offset=offset)
    return {"sessions": sessions, "limit": limit, "offset": offset}


@app.post("/interview/session")
async def start_interview_session(
    body: InterviewSessionRequest,
    user_id: str = Depends(require_user_id),
):
    quota = await check_can_start_session(user_id)
    if not quota["allowed"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "free_quota_exceeded",
                "message": "Free sessions exhausted. Upgrade to Pro for unlimited sessions.",
                "sessions_used": FREE_SESSION_LIMIT,
                "sessions_remaining": 0,
            },
        )

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

    if quota["reason"] == "free":
        await increment_free_sessions_used(user_id)

    return {"session_id": str(session_id)}


@app.patch("/interview/session/{session_id}/end")
async def end_session(
    session_id: str,
    body: EndSessionRequest,
    user_id: str = Depends(require_user_id),
):
    if body.duration_seconds < 0:
        raise HTTPException(status_code=422, detail="duration_seconds must be >= 0")
    updated = await end_interview_session(user_id, session_id, body.duration_seconds)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found, already ended, or not owned by user")
    return {"ok": True, "duration_seconds": body.duration_seconds}


@app.get("/subscription/status")
async def subscription_status(user_id: str = Depends(require_user_id)):
    sub = await get_subscription(user_id)
    usage = await get_free_usage(user_id)
    sessions_used = usage["sessions_used"] if usage else 0
    sessions_purchased = sub["sessions_purchased"] if sub else 0
    total_limit = FREE_SESSION_LIMIT + sessions_purchased
    sessions_remaining = max(0, total_limit - sessions_used)
    return {
        "sessions_used": sessions_used,
        "sessions_purchased": sessions_purchased,
        "sessions_remaining": sessions_remaining,
        "free_session_limit": FREE_SESSION_LIMIT,
    }


@app.get("/subscription/purchases")
async def subscription_purchases(user_id: str = Depends(require_user_id)):
    rows = await get_purchases(user_id)
    return {
        "purchases": [
            {
                "plan_id":    r["plan_id"],
                "sessions":   r["sessions"],
                "amount_inr": r["amount"] / 100,
                "order_id":   r["order_id"],
                "payment_id": r["payment_id"],
                "purchased_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@app.post("/payment/create-order")
@limiter.limit("10/minute")
async def payment_create_order(body: CreateOrderRequest, request: Request, user_id: str = Depends(require_user_id)):
    if body.plan_id not in PLAN_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan_id}")
    try:
        data = await create_order(body.plan_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Razorpay error: {e}")
    return data  # {order_id, amount, currency, key_id}


@app.post("/payment/verify")
async def payment_verify(
    body: VerifyPaymentRequest,
    user_id: str = Depends(require_user_id),
):
    if body.plan_id not in PLAN_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan_id}")
    valid = verify_payment_signature(
        body.razorpay_order_id,
        body.razorpay_payment_id,
        body.razorpay_signature,
    )
    if not valid:
        raise HTTPException(status_code=400, detail="Signature mismatch — payment not verified")
    await provision_free_subscription(user_id)
    await credit_sessions(user_id, body.plan_id, body.razorpay_order_id, body.razorpay_payment_id)
    sessions = PLAN_CONFIG[body.plan_id]["sessions"]
    return {"ok": True, "sessions_credited": sessions}


@app.post("/analyse-screen")
async def analyse_screen(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    provider: str = Query("openai"),
    user_id: str = Depends(require_user_id),
):
    """
    Extract text from an image and stream an LLM answer (same transport as /chat).
    The first line of the response body is a JSON object: {"extracted_text": "..."}.
    Remaining bytes are the streamed answer text.
    """
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    if not await interview_session_belongs_to_user(user_id, session_uuid):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        image_bytes = await file.read()
        mime_type = file.content_type or "image/jpeg"

        logger.info(f"[/analyse_screen] Extracting text using {provider}")
        extracted_text = await extract_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider=provider,
        )

        if not extracted_text:
            raise HTTPException(status_code=500, detail="Failed to extract text from the image")

        logger.info("[/analyse_screen] Text extracted, streaming LLM answers")
        prompt = (
            "Here is the text extracted from an image:\n\n"
            f"```text\n{extracted_text}\n```\n\n"
            "Please carefully review this text, identify any questions or problems present in it, and provide step-by-step answers or solutions to those questions."
        )

        async def generate_and_save():
            full_response = ""
            yield json.dumps({"extracted_text": extracted_text}, ensure_ascii=False) + "\n"
            generator_func = stream_gemini(prompt) if provider == "gemini" else stream_openai(prompt)
            try:
                async for chunk in generator_func:
                    full_response += chunk
                    yield chunk
            except Exception as e:
                logger.error(f"[/analyse_screen] Stream error: {e}")
            finally:
                if full_response:
                    asyncio.create_task(
                        insert_interview_response(
                            user_id,
                            session_uuid,
                            extracted_text,
                            full_response,
                            "screen",
                        )
                    )

        return StreamingResponse(generate_and_save(), media_type="text/event-stream")

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

        if not await interview_session_belongs_to_user(user_id, session_uuid):
            raise HTTPException(status_code=404, detail="Session not found")

        question = request.get_question()
        ctx = await get_session_prompt_context(user_id, session_uuid)
        if not ctx:
            raise HTTPException(status_code=404, detail="Session not found")

        resume_text = (ctx.get("resume_text") or "").strip()
        job_title = (ctx.get("job_title") or "").strip()
        job_description = (ctx.get("job_description") or "").strip()

        # Keep prompts bounded to reduce token blow-ups.
        if len(resume_text) > 6000:
            resume_text = resume_text[:6000] + "\n\n[...resume truncated...]"
        if len(job_description) > 4000:
            job_description = job_description[:4000] + "\n\n[...job description truncated...]"

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

        async def generate_and_save():
            full_response = ""
            generator_func = (
                stream_gemini(question, system_prompt=system_prompt)
                if request.provider == "gemini"
                else stream_openai(question, system_prompt=system_prompt)
            )
            try:
                async for chunk in generator_func:
                    full_response += chunk
                    yield chunk
            except Exception as e:
                logger.error(f"[/ai-answer] Stream error: {e}")
            finally:
                if full_response:
                    asyncio.create_task(
                        insert_interview_response(
                            user_id,
                            session_uuid,
                            question,
                            full_response,
                            "transcript",
                        )
                    )

        return StreamingResponse(generate_and_save(), media_type="text/event-stream")
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

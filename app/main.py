from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Query, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.exceptions import RequestValidationError
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
from modules.auth import sign_up_user, login_user, get_user, get_auth_user_row, get_user_role
from modules.auth_google import router as google_router, refresh_google_oauth_tokens
from modules.app_tokens import mint_access_token, issue_refresh_token, rotate_refresh_token, revoke_refresh_token
from modules.llm import stream_openai, stream_gemini, select_stream, _openai_client
from modules.ocr import extract_text
from modules.resume_text import extract_resume_text, ResumeTextExtractionError
from modules.interview import (
    get_interview_profiles,
    list_interview_sessions,
    save_interview_profile,
    create_interview_session,
    get_open_interview_session,
    insert_interview_response,
    interview_session_belongs_to_user,
    get_session_prompt_context,
)
from modules.launch_signup import insert_launch_signup
from modules.database import fetch_one, get_pool
from modules.billing import (
    FREE_SESSION_LIMIT,
    PLAN_CONFIG,
    adjust_user_sessions,
    check_can_purchase,
    check_can_start_session,
    create_order,
    credit_sessions,
    end_interview_session,
    get_free_usage,
    get_purchases,
    get_subscription,
    increment_free_sessions_used,
    provision_free_subscription,
    rz_client,
    verify_payment_signature,
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


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    try:
        body = await request.body()
        body_text = body.decode("utf-8", errors="replace")
    except Exception:
        body_text = "<unreadable>"
    logger.error(
        "[422] Validation error on %s %s | body: %s | errors: %s",
        request.method,
        request.url.path,
        body_text,
        exc.errors(),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

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
    """Pre-establish OpenAI + DB connection pools to eliminate first-request latency."""
    try:
        await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
    except Exception:
        pass
    try:
        # Open the asyncpg pool now so the first real request doesn't pay for it.
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        pass


ALLOWED_RESUME_SUFFIXES = {".pdf", ".docx"}


async def require_user_id(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.split(" ", 1)[1]
    user_res = await get_user(token)
    if not user_res.user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user_res.user.id


async def require_admin(user_id: str = Depends(require_user_id)) -> str:
    role = await get_user_role(user_id)
    if (role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user_id


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

class AdjustSessionsRequest(BaseModel):
    delta: int = Field(
        ...,
        description="Sessions to add (positive) or remove (negative). Final value is clamped at 0.",
    )

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
        if not response.user:
            raise HTTPException(status_code=401, detail="invalid_credentials")

        user_id = response.user.id
        access_token = mint_access_token(user_id, body.email)
        refresh_token = await issue_refresh_token(user_id)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id,
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_credentials")


@app.post("/refresh")
async def refresh_session(body: RefreshTokenRequest):
    """
    Exchange a refresh token for a new access/refresh token pair.

    Primary path: app-issued opaque refresh tokens (rotated on every use).
    Legacy fallback: Google OAuth refresh tokens issued before the unified
    token system; on success the client is migrated to an app refresh token.
    """
    rt = (body.refresh_token or "").strip()
    if not rt:
        raise HTTPException(status_code=422, detail="refresh_token is required")

    rotated = await rotate_refresh_token(rt)
    if rotated:
        user_id, new_refresh = rotated
        email = ""
        row = await get_auth_user_row(user_id)
        if row:
            email = row.get("email") or ""
        return {
            "access_token": mint_access_token(user_id, email),
            "refresh_token": new_refresh,
            "user_id": user_id,
        }

    # Legacy Google refresh token: validate, then migrate to an app refresh token.
    out = await refresh_google_oauth_tokens(rt)
    if out:
        new_refresh = await issue_refresh_token(out["user_id"])
        return {
            "access_token": out["access_token"],
            "refresh_token": new_refresh,
            "user_id": out["user_id"],
        }

    raise HTTPException(status_code=401, detail="Refresh failed")


@app.post("/logout")
async def logout(body: RefreshTokenRequest):
    """
    Revoke a refresh token so it can no longer be used to mint new sessions.

    Always returns 200 (idempotent): logging out an unknown or already-revoked
    token is a no-op. Existing access tokens remain valid until they expire (1h).
    """
    rt = (body.refresh_token or "").strip()
    if not rt:
        raise HTTPException(status_code=422, detail="refresh_token is required")
    await revoke_refresh_token(rt)
    return {"message": "logged_out"}


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
                "error": "quota_exceeded",
                "message": "No sessions remaining. Purchase a plan to start a new session.",
                "sessions_remaining": 0,
            },
        )

    # Guard against double-submit: if the user already has an unended session,
    # return it instead of creating (and charging for) a duplicate.
    open_session_id = await get_open_interview_session(user_id)
    if open_session_id:
        return {"session_id": str(open_session_id), "reused": True}

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

    # Session started successfully — consume one credit (free or purchased).
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
    period_end = sub["current_period_end"] if sub else None
    return {
        "sessions_used": sessions_used,
        "sessions_purchased": sessions_purchased,
        "sessions_remaining": sessions_remaining,
        "free_session_limit": FREE_SESSION_LIMIT,
        "plan_tier": sub["plan_tier"] if sub else "free",
        "current_period_end": period_end.isoformat() if period_end else None,
    }


@app.post("/admin/users/{target_user_id}/sessions")
async def admin_adjust_user_sessions(
    target_user_id: str,
    body: AdjustSessionsRequest,
    _admin_id: str = Depends(require_admin),
):
    """
    Admin-only: grant or revoke purchased sessions for a user.
    Body: {"delta": <int>} — positive adds, negative removes (clamped at 0).
    """
    try:
        UUID(target_user_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid user_id")

    if not await get_auth_user_row(target_user_id):
        raise HTTPException(status_code=404, detail="User not found")

    return await adjust_user_sessions(target_user_id, body.delta)


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
    await check_can_purchase(user_id, body.plan_id)
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
    # Verify plan_id matches the order to prevent plan forgery
    try:
        order = rz_client.order.fetch(body.razorpay_order_id)
        server_plan_id = order.get("notes", {}).get("plan_id")
        if server_plan_id and server_plan_id != body.plan_id:
            raise HTTPException(
                status_code=400,
                detail="Plan mismatch — payment was for a different plan",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # If Razorpay fetch fails, proceed (signature already verified)
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

    # Read the upload and verify ownership concurrently — they're independent.
    image_bytes, owns_session = await asyncio.gather(
        file.read(),
        interview_session_belongs_to_user(user_id, session_uuid),
    )

    if not owns_session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
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
            generator_func = select_stream(provider)(prompt)
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

        question = request.get_question()
        # get_session_prompt_context already filters by user_id, so it doubles
        # as the ownership check — no separate belongs-to-user query needed.
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
            "You are an expert interview coach helping a candidate prepare thorough, impressive answers to interview questions.\n\n"
            "For each question, produce a well-structured answer using markdown formatting:\n"
            "- Start with a bold **definition or headline** of the core concept.\n"
            "- Use ## section headers to break the answer into logical parts (e.g. 'What it is', 'How it works', 'Why it's used').\n"
            "- Use numbered lists for sequential steps or processes.\n"
            "- Use bullet points with bold lead-ins (e.g. '**Reduces Hallucinations:** ...') for feature/benefit lists.\n"
            "- Include a concrete analogy or real-world example where it adds clarity.\n"
            "- Aim for depth: 60-80% of what a strong written explainer would cover — not a one-liner, not an essay.\n\n"
            "Rules:\n"
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
            generator_func = select_stream(request.provider)(question, system_prompt=system_prompt)
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

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File
import asyncio
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from modules.auth import sign_up_user, login_user, get_user
from modules.chat import get_user_threads, get_thread_chats, create_thread, save_chat_message, delete_thread
from modules.llm import stream_openai, stream_gemini
from modules.websearch import web_search_task
from modules.storage import upload_to_supabase

app = FastAPI(title="Supabase LLM Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    thread_id: str,
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.split(" ")[1]
    try:
        user_res = await get_user(token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_res.user.id
        
        # Upload to Supabase Storage
        public_url = await upload_to_supabase(file)
        if not public_url:
            raise HTTPException(status_code=500, detail="Failed to upload file")
            
        # Parse file content (assuming text/markdown/code for now)
        # We need to reset the file cursor because upload_to_supabase read it
        await file.seek(0)
        content = await file.read()
        
        try:
            text_content = content.decode("utf-8")
        except UnicodeDecodeError:
            # If binary, just use the URL
            text_content = f"I have uploaded a file: {file.filename}. Access it here: {public_url}"
        else:
            # If text, include a snippet or full content
            text_content = f"I have uploaded a file '{file.filename}'.\n\nContent:\n{text_content}"

        # Save as a user message in the chat history
        # We save it as a "query" with a system/placeholder response or empty response
        # Actually, best to save it as a query, and have the AI acknowledge it?
        # For now, we'll just insert it into chat_history.
        # But wait, `save_chat_message` expects a response.
        # We can simulate an AI acknowledgement.
        
        ai_acknowledgement = f"Received file: {file.filename}."
        
        await save_chat_message(user_id, thread_id, text_content, ai_acknowledgement)
        
        return {"message": "File uploaded and processed", "url": public_url}

    except Exception as e:
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

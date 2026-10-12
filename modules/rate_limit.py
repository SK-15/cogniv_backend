from fastapi import Request
from slowapi import Limiter


def client_ip(request: Request) -> str:
    """
    The caller's IP, for rate limiting and analytics.

    Render's proxy appends to X-Forwarded-For instead of replacing it, so its
    leftmost entry is whatever the client sent. Cloudflare, in front of
    Render, sets CF-Connecting-IP and overwrites any client-supplied value —
    use that. Without it (local runs) fall back to the socket peer.
    """
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=client_ip)

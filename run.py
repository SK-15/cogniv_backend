#!/usr/bin/env python3
"""Run script for the Cogniv API."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        # Auto-reload is for local development only (DEV=1).
        reload=os.environ.get("DEV") == "1",
        # Client IPs come from CF-Connecting-IP (modules/rate_limit.py), not
        # X-Forwarded-For, which Render passes through unfiltered.
        proxy_headers=False,
    )

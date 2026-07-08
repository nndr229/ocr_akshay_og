"""Deployment entrypoint — runs the AP Invoice Console.

Platforms like Pella run `python main.py` (or you set it as the start
command). Host/port come from the environment, falling back to local dev
defaults. All app config is env-var driven: ANTHROPIC_API_KEY, APP_USERNAME,
APP_PASSWORD, CLAUDE_MODEL, DATA_DIR, SESSION_SECRET.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=os.environ.get("PORT", 8700),
    )

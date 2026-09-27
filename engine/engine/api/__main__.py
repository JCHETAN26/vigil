"""Run the dashboard API locally, bound to 127.0.0.1 only (never 0.0.0.0)."""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "engine.api.app:app",
        host="127.0.0.1",
        port=int(os.getenv("VIGIL_API_PORT", "8080")),
        reload=False,
    )

"""Console entry point: ``atlas-backend``."""

from __future__ import annotations

import argparse

import uvicorn

from atlas_backend.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-backend", description="Run the ATLAS backend.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="developer auto-reload")
    args = parser.parse_args()

    settings = get_settings()
    uvicorn.run(
        "atlas_backend.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

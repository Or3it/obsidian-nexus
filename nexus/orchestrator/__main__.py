"""Entrypoint: `python -m orchestrator --vault /path/to/vault`"""
import argparse
from pathlib import Path

import uvicorn

from .server import build_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus")
    parser.add_argument("--vault", type=Path, required=True, help="Path to Obsidian vault root")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app = build_app(args.vault)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

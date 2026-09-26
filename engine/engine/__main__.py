"""``python -m engine`` entrypoint — delegates to the CLI (design §10)."""

from __future__ import annotations

from engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

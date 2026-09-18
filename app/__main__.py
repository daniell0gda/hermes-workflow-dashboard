"""Container entrypoint: ``python -m app``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import ConfigError, load_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        root_path=settings.root_path,
        access_log=True,
        forwarded_allow_ips="*",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

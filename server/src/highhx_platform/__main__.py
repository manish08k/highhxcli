"""Run the platform: `python -m highhx_platform` (or `highhx-platform`)."""

from __future__ import annotations

import os


def main() -> None:
    import sys

    if sys.argv[1:2] == ["migrate"]:
        from highhx_platform.config import Settings
        from highhx_platform.db import Database

        database = Database(Settings.from_env().database_url)
        database.migrate()
        print(f"database schema at {database.schema_revision()}")
        return
    import uvicorn

    uvicorn.run(
        "highhx_platform.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_graceful_shutdown=int(os.environ.get("HIGHHX_SHUTDOWN_TIMEOUT", "30")),
    )


if __name__ == "__main__":
    main()

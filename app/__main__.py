"""`python -m app` starts the server."""

import uvicorn

from .config import load_settings


def main() -> None:
    cfg = load_settings()
    uvicorn.run(
        "app.api:create_app", factory=True, host=cfg.host, port=cfg.port,
        log_level="info", access_log=False, proxy_headers=False,
        timeout_graceful_shutdown=3,  # open dashboards hold a live-update stream; don't wait for them
    )


if __name__ == "__main__":
    main()

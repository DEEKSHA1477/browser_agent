"""Start the backend:  python run.py

Uvicorn is started programmatically on the default (Proactor) event loop.
On Windows, Playwright needs the Proactor loop to launch the browser process;
`uvicorn --reload` switches to the Selector loop and breaks that, so reload
is intentionally not used.
"""
import asyncio
import sys

import uvicorn

from config import settings


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    config = uvicorn.Config("main:app", host=settings.host, port=settings.port, log_level="info", ws_ping_interval=20)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()

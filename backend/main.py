"""NOVA - FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from browser.browser_manager import browser_manager
from config import settings
from routes import approval_routes, task_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("nova").info(
        "NOVA backend ready (LLM: %s/%s, configured=%s, headless=%s)",
        settings.llm_provider, settings.llm_model, settings.llm_configured, settings.headless,
    )
    yield
    await browser_manager.shutdown()


app = FastAPI(title="NOVA - AI Browser Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(task_routes.router)
app.include_router(task_routes.ws_router)
app.include_router(approval_routes.router)

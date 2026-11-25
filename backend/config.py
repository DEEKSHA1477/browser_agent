"""Application configuration loaded from environment variables.

All secrets (LLM API keys) live in environment variables / a local .env file and
are only ever read by the backend. Nothing here is exposed to the frontend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# Support a .env either at the repository root or inside backend/.
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # --- LLM -----------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").strip().lower())
    gemini_api_key: str | None = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None
    )
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
    # Planning and verification try these first, saving the main model's (small, per-model) free-tier quota.
    gemini_utility_models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv("GEMINI_UTILITY_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",")
        if m.strip()])
    # Tried in order when the main model is overloaded (503), out of quota (429) or retired (404).
    gemini_fallback_models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS",
                  "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",")
        if m.strip()])
    # Ollama: local models through Ollama's OpenAI-compatible API (no key, no quota).
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    ollama_vision: bool = field(default_factory=lambda: _bool("OLLAMA_VISION", False))  # only for vision models
    # Local CPU inference is slow (~1-2 min per agent step on a laptop without a dedicated GPU).
    ollama_timeout_s: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT_S", "300")))
    # Used when the main provider fails completely (e.g. every Gemini model is rate-limited): "" | ollama
    llm_fallback_provider: str = field(default_factory=lambda: os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower())
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "60")))
    # always | auto | off - when to attach a screenshot to the LLM's observation.
    vision_mode: str = field(default_factory=lambda: os.getenv("VISION_MODE", "auto").strip().lower())

    # --- Agent -----------------------------------------------------------------
    max_steps: int = field(default_factory=lambda: int(os.getenv("MAX_STEPS", "40")))
    max_consecutive_failures: int = field(default_factory=lambda: int(os.getenv("MAX_CONSECUTIVE_FAILURES", "4")))
    max_reapprovals: int = 3
    max_instruction_length: int = 1500  # free text is capped at 1000 by the API; launcher-composed tasks can be longer

    # --- Browser ---------------------------------------------------------------
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", False))
    slow_mo_ms: int = field(default_factory=lambda: int(os.getenv("BROWSER_SLOW_MO_MS", "80")))
    navigation_timeout_ms: int = field(default_factory=lambda: int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000")))
    screenshot_interval_s: float = field(default_factory=lambda: float(os.getenv("SCREENSHOT_INTERVAL_S", "1.0")))
    max_browser_sessions: int = field(default_factory=lambda: int(os.getenv("MAX_BROWSER_SESSIONS", "3")))
    locale: str = field(default_factory=lambda: os.getenv("BROWSER_LOCALE", "en-IN"))
    timezone: str = field(default_factory=lambda: os.getenv("BROWSER_TIMEZONE", "Asia/Kolkata"))
    search_engine: str = field(default_factory=lambda: os.getenv("SEARCH_ENGINE", "duckduckgo").strip().lower())
    download_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DOWNLOAD_DIR") or (Path.home() / "Downloads" / "nova-agent"))
    )

    # --- Navigation policy -------------------------------------------------------
    # The agent is general-purpose, so navigation is open by default but:
    #  * only http(s) URLs are allowed,
    #  * private / loopback networks are blocked unless explicitly enabled,
    #  * BLOCKED_DOMAINS are always refused,
    #  * if ALLOWED_DOMAINS is set, navigation is restricted to that allowlist.
    allowed_domains: list[str] = field(default_factory=lambda: _list("ALLOWED_DOMAINS"))
    blocked_domains: list[str] = field(default_factory=lambda: _list("BLOCKED_DOMAINS"))
    allow_private_network: bool = field(default_factory=lambda: _bool("ALLOW_PRIVATE_NETWORK", False))

    # --- Server ----------------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    cors_origins: list[str] = field(
        default_factory=lambda: _list("CORS_ORIGINS") or ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "ollama":
            return True
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.gemini_api_key)

    @property
    def llm_model(self) -> str:
        return {"openai": self.openai_model, "ollama": self.ollama_model}.get(self.llm_provider, self.gemini_model)


settings = Settings()

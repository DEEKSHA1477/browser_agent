"""Thin provider-agnostic LLM client (Gemini or OpenAI) returning JSON.

API keys are read from the environment on the server only.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import re
import time
from typing import Any, Protocol

from config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMNotConfigured(LLMError):
    pass


class LLMClient(Protocol):
    async def complete_json(self, system: str, prompt: str, image: bytes | None = None) -> dict[str, Any]: ...


def parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("The model did not return JSON.")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"The model returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("The model returned JSON that is not an object.")
    return value


# True while making a "utility" call (planning, verification) that a Lite model handles well.
_UTILITY: contextvars.ContextVar[bool] = contextvars.ContextVar("nova_llm_utility", default=False)

_UNAVAILABLE = re.compile(r"\b(503|429|404)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|NOT_FOUND|overloaded|high demand", re.I)


class GeminiClient:
    """Gemini with automatic model fallback.

    If the configured model is overloaded (503), out of quota (429) or retired
    for this key (404), the next model in GEMINI_FALLBACK_MODELS is used and
    the failing one is skipped for a cool-down period.
    """

    COOLDOWN_S = 120.0

    def __init__(self, *, wait_when_limited: bool = True) -> None:
        from google import genai  # imported lazily so the app starts without the key

        self._genai = genai
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._models = list(dict.fromkeys([settings.gemini_model, *settings.gemini_fallback_models,
                                           *settings.gemini_utility_models]))
        self._cooling: dict[str, float] = {}
        self.last_model: str | None = None
        # With another provider configured as fallback (e.g. local Ollama), fail fast instead of
        # waiting out rate limits, so that provider takes over immediately.
        self.wait_when_limited = wait_when_limited

    def _candidates(self) -> list[str]:
        now = time.monotonic()
        models = self._models
        if _UTILITY.get():
            # Planning / verification: use the Lite models first (separate per-model quota),
            # keeping the main Flash quota for the step-by-step browser decisions.
            lite = [m for m in settings.gemini_utility_models if m in models]
            models = lite + [m for m in models if m not in lite]
        ready = [m for m in models if self._cooling.get(m, 0) <= now]
        if ready:
            return ready
        return models if self.wait_when_limited else []  # all cooling down: retry them, or skip to the fallback

    async def complete_json(self, system: str, prompt: str, image: bytes | None = None) -> dict[str, Any]:
        from google.genai import types

        parts = []
        if image:
            parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=prompt))
        config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", temperature=0.2)
        last: Exception | None = None
        for round_ in range(2):
            for model in self._candidates():
                try:
                    response = await self._client.aio.models.generate_content(
                        model=model, contents=[types.Content(role="user", parts=parts)], config=config)
                except Exception as exc:
                    if not _UNAVAILABLE.search(str(exc)):
                        raise
                    last = exc
                    self._cooling[model] = time.monotonic() + self._cooldown_for(exc)
                    logger.warning("Gemini model %s unavailable (%s); trying the next model.", model, str(exc)[:80])
                    continue
                if model != self.last_model:
                    logger.info("Using Gemini model %s", model)
                    self.last_model = model
                return parse_json(response.text or "")
            # Every model is busy or rate-limited. Per-minute quotas reset quickly, so wait for the
            # earliest one instead of failing the task (asyncio.sleep is cancelled by Stop).
            wait = min(self._cooling.values(), default=0) - time.monotonic()
            if self.wait_when_limited and round_ == 0 and 0 < wait <= self.MAX_WAIT_S:
                logger.warning("All Gemini models are rate-limited; waiting %.0fs.", wait)
                await asyncio.sleep(wait + 1)
                continue
            break
        if last is not None:
            self._last_error = str(last)
        detail = str(last) if last is not None else f"all are cooling down after: {getattr(self, '_last_error', 'rate limits')}"
        raise LLMError(f"All Gemini models are unavailable right now: {detail}")

    MAX_WAIT_S = 90.0

    def _cooldown_for(self, exc: Exception) -> float:
        """Use Google's retryDelay for per-minute quota errors; otherwise a fixed cool-down."""
        m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
        if m:
            return float(m.group(1)) + 1
        if re.search(r"\b404\b|NOT_FOUND", str(exc)):
            return 3600.0  # retired for this key
        return self.COOLDOWN_S


class OpenAIClient:
    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def complete_json(self, system: str, prompt: str, image: bytes | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image:
            b64 = base64.b64encode(image).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}})
        response = await self._client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        return parse_json(response.choices[0].message.content or "")


class OllamaClient:
    """Local model served by Ollama, via its OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=f"{settings.ollama_base_url}/v1", api_key="ollama",
                                   timeout=settings.ollama_timeout_s, max_retries=0)

    async def complete_json(self, system: str, prompt: str, image: bytes | None = None) -> dict[str, Any]:
        content: Any = prompt
        if image and settings.ollama_vision:
            b64 = base64.b64encode(image).decode()
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
        try:
            response = await self._client.chat.completions.create(
                model=settings.ollama_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
        except Exception as exc:
            if "connect" in str(exc).lower():
                raise LLMError(f"Cannot reach Ollama at {settings.ollama_base_url}. Is `ollama serve` running?") from exc
            if "not found" in str(exc).lower():
                raise LLMError(f"Ollama model '{settings.ollama_model}' is not installed. Run: ollama pull {settings.ollama_model}") from exc
            raise
        return parse_json(response.choices[0].message.content or "")


class FallbackClient:
    """Main provider first; if it fails completely, the fallback provider (e.g. local Ollama)."""

    def __init__(self, primary: LLMClient, fallback: LLMClient, name: str) -> None:
        self.primary, self.fallback, self.name = primary, fallback, name
        self.used_fallback = False
        self.fallback_reason: str | None = None

    async def complete_json(self, system: str, prompt: str, image: bytes | None = None) -> dict[str, Any]:
        try:
            out = await self.primary.complete_json(system, prompt, image)
            self.used_fallback = False
            return out
        except (LLMError, asyncio.TimeoutError) as exc:
            logger.warning("Main LLM unavailable (%s); using %s.", str(exc)[:100], self.name)
            self.fallback_reason = _explain_unavailable(str(exc))
            out = await self.fallback.complete_json(system, prompt, image)
            self.used_fallback = True
            return out


def _explain_unavailable(error: str) -> str:
    if "PerDay" in error:
        return "the Gemini free-tier daily limit (20 requests per model) is used up"
    if "429" in error or "RESOURCE_EXHAUSTED" in error or "cooling down" in error:
        return "Gemini's rate limit was reached"
    if "503" in error or "UNAVAILABLE" in error:
        return "Gemini is overloaded right now"
    return "Gemini is unavailable"


def _timeout_for(client: LLMClient) -> float:
    """Overall time budget for one call, including Gemini's rate-limit waits and slow local models."""
    if isinstance(client, FallbackClient):
        return _timeout_for(client.primary) + _timeout_for(client.fallback)
    if isinstance(client, OllamaClient):
        return settings.ollama_timeout_s
    if isinstance(client, GeminiClient):
        return settings.llm_timeout_s + (GeminiClient.MAX_WAIT_S if client.wait_when_limited else 0)
    return settings.llm_timeout_s


def _make_client(provider: str, *, has_fallback: bool = False) -> LLMClient:
    if provider == "openai":
        return OpenAIClient()
    if provider == "ollama":
        return OllamaClient()
    return GeminiClient(wait_when_limited=not has_fallback)


class LLMService:
    """Retrying wrapper. ``override`` lets tests inject a scripted client."""

    def __init__(self) -> None:
        self._client: LLMClient | None = None
        self.override: LLMClient | None = None

    @property
    def available(self) -> bool:
        return self.override is not None or settings.llm_configured

    def _get(self) -> LLMClient:
        if self.override is not None:
            return self.override
        if not settings.llm_configured:
            key = "OPENAI_API_KEY" if settings.llm_provider == "openai" else "GEMINI_API_KEY"
            raise LLMNotConfigured(f"No LLM API key configured. Set {key} in the .env file and restart the backend.")
        if self._client is None:
            fb = settings.llm_fallback_provider
            has_fallback = bool(fb) and fb != settings.llm_provider
            client = _make_client(settings.llm_provider, has_fallback=has_fallback)
            if has_fallback:
                client = FallbackClient(client, _make_client(fb), fb)
            self._client = client
        return self._client

    def last_used(self) -> tuple[str, bool, str | None]:
        """(model that answered the last call, whether it is the slow local fallback, why the fallback was used)."""
        client = self._client
        if self.override is not None or client is None:
            return settings.llm_model, settings.llm_provider == "ollama", None
        if isinstance(client, FallbackClient):
            if client.used_fallback:
                return f"{settings.ollama_model} (local Ollama)", True, client.fallback_reason
            client = client.primary
        if isinstance(client, GeminiClient):
            return client.last_model or settings.gemini_model, False, None
        if isinstance(client, OllamaClient):
            return f"{settings.ollama_model} (local Ollama)", True, None
        return settings.llm_model, False, None

    async def complete_json(self, system: str, prompt: str, image: bytes | None = None, attempts: int = 3,
                            *, utility: bool = False) -> dict[str, Any]:
        token = _UTILITY.set(utility)
        try:
            return await self._complete_json(system, prompt, image, attempts)
        finally:
            _UTILITY.reset(token)

    async def _complete_json(self, system: str, prompt: str, image: bytes | None, attempts: int) -> dict[str, Any]:
        client = self._get()
        slow = isinstance(client, (OllamaClient, FallbackClient))
        if slow:
            attempts = min(attempts, 2)  # each attempt may already include a slow local model call
        timeout = _timeout_for(client)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                # Fallback waits (rate limits) happen inside the client, so allow for them in the timeout.
                return await asyncio.wait_for(client.complete_json(system, prompt, image), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except LLMNotConfigured:
                raise
            except Exception as exc:  # rate limits, 5xx, timeouts, bad JSON
                if isinstance(exc, asyncio.TimeoutError):
                    exc = LLMError(f"no answer within {timeout:.0f}s")
                last = exc
                logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, attempts, exc)
                if attempt < attempts - 1:
                    await asyncio.sleep(2 ** attempt * 1.5)
        raise LLMError(f"LLM request failed after {attempts} attempts: {last}")


llm_service = LLMService()

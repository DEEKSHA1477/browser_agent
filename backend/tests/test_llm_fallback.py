"""Gemini model fallback: busy / rate-limited / retired models are skipped, quotas are waited out."""
import time
from types import SimpleNamespace

import pytest

from services.llm_service import GeminiClient, LLMError

BUSY = Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
RPM = Exception("429 RESOURCE_EXHAUSTED. {'details': [{'retryDelay': '2s'}]}")
GONE = Exception("404 NOT_FOUND. This model is no longer available to new users.")


def make_client(behaviour, wait=True):
    """behaviour: model -> list of exceptions/strings returned on successive calls."""
    calls = []

    async def generate_content(model, contents, config):
        calls.append(model)
        outcome = behaviour[model].pop(0) if len(behaviour[model]) > 1 else behaviour[model][0]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    client = object.__new__(GeminiClient)
    client._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    client._models = list(behaviour)
    client._cooling = {}
    client.last_model = None
    client.wait_when_limited = wait
    return client, calls


async def test_falls_back_and_remembers_busy_model():
    client, calls = make_client({"main": [BUSY], "backup": ['{"ok": true}']})
    assert await client.complete_json("s", "p") == {"ok": True}
    assert calls == ["main", "backup"] and client.last_model == "backup"
    await client.complete_json("s", "p")
    assert calls[-1] == "backup" and calls.count("main") == 1  # cooling down, not retried every step


async def test_retired_model_cooldown_is_long_and_rpm_uses_retry_delay():
    client, _ = make_client({"a": [GONE], "b": [RPM], "c": ['{"x": 1}']})
    await client.complete_json("s", "p")
    now = time.monotonic()
    assert client._cooling["a"] - now > 3000
    assert 1 < client._cooling["b"] - now < 5


async def test_waits_when_every_model_is_rate_limited():
    client, calls = make_client({"a": [RPM, '{"ok": 1}'], "b": [RPM]})
    start = time.monotonic()
    assert await client.complete_json("s", "p") == {"ok": 1}
    assert 2 <= time.monotonic() - start < 10 and calls[:2] == ["a", "b"]


async def test_with_a_fallback_provider_gemini_fails_fast_instead_of_waiting():
    client, calls = make_client({"a": [RPM, '{"ok": 1}'], "b": [RPM]}, wait=False)
    start = time.monotonic()
    with pytest.raises(LLMError):
        await client.complete_json("s", "p")
    assert time.monotonic() - start < 1
    # Next step: every model is still cooling down -> no Gemini calls at all, straight to the fallback.
    with pytest.raises(LLMError, match="cooling down"):
        await client.complete_json("s", "p")
    assert calls == ["a", "b"]


async def test_utility_calls_prefer_lite_models():
    from services import llm_service as ls

    original = ls.settings.gemini_utility_models
    object.__setattr__(ls.settings, "gemini_utility_models", ["lite"])  # settings is a frozen dataclass
    client, calls = make_client({"flash": ['{"a": 1}'], "lite": ['{"b": 2}']})
    token = ls._UTILITY.set(True)
    try:
        assert await client.complete_json("s", "p") == {"b": 2}
    finally:
        ls._UTILITY.reset(token)
        object.__setattr__(ls.settings, "gemini_utility_models", original)
    assert await client.complete_json("s", "p") == {"a": 1}  # normal agent steps use the main model
    assert calls == ["lite", "flash"]


async def test_daily_limit_fallback_is_explained():
    from services import llm_service as ls

    DAILY = Exception("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier "
                      "{'retryDelay': '73506s'}")
    gem, _ = make_client({"flash": [DAILY]}, wait=False)

    class Local:
        async def complete_json(self, system, prompt, image=None):
            return {"ok": 1}

    fb = ls.FallbackClient(gem, Local(), "ollama")
    assert await fb.complete_json("s", "p") == {"ok": 1}
    assert fb.used_fallback and "daily limit" in fb.fallback_reason
    assert gem._cooling["flash"] - time.monotonic() > 70000  # skipped until the quota resets
    # Next call: Gemini is skipped instantly, and the reason is still the daily limit.
    assert await fb.complete_json("s", "p") == {"ok": 1}
    assert "daily limit" in fb.fallback_reason


async def test_other_errors_are_not_swallowed():
    client, _ = make_client({"a": [ValueError("bad request: invalid argument")], "b": ['{"ok": 1}']})
    with pytest.raises(ValueError):
        await client.complete_json("s", "p")


async def test_all_down_raises_llm_error():
    client, _ = make_client({"a": [BUSY], "b": [BUSY]})
    with pytest.raises(LLMError, match="All Gemini models"):
        await client.complete_json("s", "p")

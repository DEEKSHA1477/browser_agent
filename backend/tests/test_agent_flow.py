"""End-to-end LangGraph flow with a scripted LLM and real Chromium on a local page.

Checks the human-in-the-loop guarantees on the full agent loop:
  * the agent claims "Place order" is NOT sensitive -> the backend still pauses,
  * the price changes while the approval card is open -> the approval is
    invalidated during re-validation and a fresh approval is requested,
  * rejection stops the action and nothing is submitted,
  * completion is reported only after verification.
"""
import asyncio
import http.server
import re
import threading

import pytest

SHOP = b"""<!doctype html><html><head><title>Fixture Shop</title></head><body>
<h1>Laptop X</h1><p id="price">Price: \xe2\x82\xb954,990</p>
<button id="order" onclick="document.title='ordered'">Place order</button>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SHOP)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def shop_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/shop"
    server.shutdown()


class ScriptedLLM:
    def __init__(self, shop_url):
        self.shop_url = shop_url

    async def complete_json(self, system, prompt, image=None):
        if system.startswith("You are the planning module"):
            return {"refuse": False, "goal": "Order Laptop X", "category": "shopping", "needs_clarification": False,
                    "plan": ["Open the shop", "Place the order (needs approval)"], "success_criteria": ["Page title is 'ordered'"]}
        if system.startswith("You are the verification module"):
            ok = "Title: ordered" in prompt
            return {"completed": ok, "evidence": "Title shows ordered" if ok else "Not ordered", "missing": None}
        if "Title: ordered" in prompt:
            return {"thought": "done", "action": {"name": "done", "params": {"success": True, "summary": "Order placed."}}}
        if "Fixture Shop" not in prompt:
            return {"thought": "open shop", "action": {"name": "navigate", "params": {"url": self.shop_url}}}
        idx = re.search(r'\[(\d+)\] <button[^>]*> "Place order"', prompt).group(1)
        # The model (wrongly) says this is harmless - the backend gate must still require approval.
        return {"thought": "order", "next_goal": "Place the order", "action": {"name": "click", "params": {"element": int(idx)}},
                "sensitive": {"is_sensitive": False}}


@pytest.fixture
async def env(shop_url, monkeypatch):
    import browser.browser_actions as ba
    import browser.browser_manager as bm
    from agent.workflow import runner
    from browser.browser_manager import browser_manager
    from services.llm_service import llm_service

    allow_local = lambda url, internal_ok=True: (True, None)  # noqa: E731 - fixture is served on 127.0.0.1
    monkeypatch.setattr(ba, "check_url", allow_local)
    monkeypatch.setattr(bm, "check_url", allow_local)
    llm_service.override = ScriptedLLM(shop_url)
    yield runner
    llm_service.override = None
    await browser_manager.shutdown()


async def wait_for(predicate, timeout=60):
    for _ in range(int(timeout / 0.2)):
        if predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("timed out waiting for condition")


@pytest.mark.browser
async def test_order_requires_fresh_approval_after_price_change(env):
    from agent.state import task_store
    from browser.browser_manager import browser_manager
    from services.approval_service import approval_service

    runner = env
    record = task_store.create("Buy Laptop X")
    runner.start(record)
    await wait_for(lambda: record.status.value == "awaiting_approval")
    first = approval_service.get(record.approval_id)
    assert first.kind.value == "order" and "policy" in first.sources
    assert any("54,990" in d.value for d in first.details)

    # Price changes while the user is looking at the card.
    page = browser_manager.get(record.id).page
    await page.evaluate("document.getElementById('price').textContent = 'Price: ₹59,990'")
    approval_service.decide(first.id, decision="approve", fingerprint=first.fingerprint)
    await runner.wait_until_suspended(record.id)
    runner.resume(record, {"approval_id": first.id})

    await wait_for(lambda: record.approval_id not in (None, first.id) and record.status.value == "awaiting_approval")
    second = approval_service.get(record.approval_id)
    assert approval_service.get(first.id).status.value == "superseded"
    assert any("59,990" in d.value for d in second.details)
    assert await page.title() == "Fixture Shop"  # nothing executed yet

    approval_service.decide(second.id, decision="approve", fingerprint=second.fingerprint)
    await runner.wait_until_suspended(record.id)
    runner.resume(record, {"approval_id": second.id})
    await wait_for(lambda: record.is_terminal)
    assert record.status.value == "completed" and record.verification.completed
    assert approval_service.get(second.id).status.value == "consumed"


@pytest.mark.browser
async def test_rejection_stops_action(env):
    from agent.state import task_store
    from browser.browser_manager import browser_manager
    from services.approval_service import approval_service

    runner = env
    record = task_store.create("Buy Laptop X")
    runner.start(record)
    await wait_for(lambda: record.status.value == "awaiting_approval")
    approval = approval_service.get(record.approval_id)
    approval_service.decide(approval.id, decision="reject", fingerprint=approval.fingerprint)
    await runner.wait_until_suspended(record.id)
    runner.resume(record, {"approval_id": approval.id})
    await wait_for(lambda: record.is_terminal)
    assert "rejected" in record.summary
    assert await browser_manager.get(record.id).page.title() == "Fixture Shop"

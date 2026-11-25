"""Selecting a result on the dashboard: links come from the page, and NOVA acts on the chosen item."""
import asyncio
import http.server
import re
import threading

import pytest

LIST = """<!doctype html><html><head><title>Song list</title></head><body><h1>Results</h1>
<ul>
 <li><a href="/watch/1">Tum Hi Ho - Official</a> <span>4:22</span></li>
 <li><a href="/watch/2">Tum Hi Ho - Acoustic cover</a> <span>3:58</span></li>
 <li><a href="/watch/3">Tum Hi Ho - Lyrics</a> <span>4:30</span></li>
</ul></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        m = re.match(r"^/watch/(\d)$", self.path)
        body = f"<title>Watching {m.group(1)}</title><h1>Now playing video {m.group(1)}</h1>" if m else LIST
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def act(name, **params):
    return {"next_goal": name, "action": {"name": name, "params": params}}


def extract_action(prompt):
    rows = []
    for title in ("Official", "Acoustic cover", "Lyrics"):
        idx = int(re.search(r'\[(\d+)\] <a> "Tum Hi Ho - ' + title, prompt).group(1))
        rows.append({"fields": {"title": f"Tum Hi Ho - {title}"}, "element": idx})
    # A record whose link the model "made up": it must not survive as a clickable link.
    rows.append({"fields": {"title": "Tum Hi Ho - Lyrics"}, "url": "https://evil.example/phish"})
    return act("extract", title="Videos", columns=["title"], items=rows)


class LLM:
    def __init__(self, base, ask):
        self.base, self.ask = base, ask

    async def complete_json(self, system, prompt, image=None):
        if system.startswith("You are the planning module"):
            return {"goal": "g", "plan": ["p"], "success_criteria": ["s"]}
        if system.startswith("You are the verification module"):
            return {"completed": True, "evidence": "fixture"}
        if "Title: Watching" in prompt:
            return act("done", success=True, summary="Playing the selected video.")
        link = re.search(r"\((http://127\.0\.0\.1:\d+/watch/\d)\)", prompt)  # from the selection / answer
        if link and "Title: Song list" in prompt and ("I selected" in prompt or "I choose" in prompt):
            return act("navigate", url=link.group(1))
        if "Title: Song list" not in prompt:
            return act("navigate", url=self.base + "/list")
        if "Videos" not in prompt.split("RESULTS COLLECTED SO FAR")[1][:400]:
            return extract_action(prompt)
        if self.ask:
            return act("ask_user", question="Which video should I play?")
        return act("done", success=True, summary="Listed 3 videos.")


@pytest.fixture
async def env(monkeypatch):
    import browser.browser_actions as ba
    import browser.browser_manager as bm
    import routes.task_routes as tr
    from browser.browser_manager import browser_manager
    from services.llm_service import llm_service

    allow = lambda url, internal_ok=True: (True, None)  # noqa: E731 - fixture is on 127.0.0.1
    for mod in (ba, bm, tr):
        monkeypatch.setattr(mod, "check_url", allow)
    yield llm_service
    llm_service.override = None
    await browser_manager.shutdown()


async def wait_for(predicate, timeout=60):
    for _ in range(int(timeout / 0.1)):
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


@pytest.mark.browser
async def test_select_after_finish_opens_item_in_same_browser(env, base_url):
    from agent.state import task_store
    from agent.workflow import runner
    from browser.browser_manager import browser_manager
    from models.task_models import SelectRequest
    from routes.task_routes import select_result

    env.override = LLM(base_url, ask=False)
    first = task_store.create("Find Tum Hi Ho videos", capability="music")
    runner.start(first)
    await wait_for(lambda: first.is_terminal)
    coll = first.results[0]
    urls = [i.url for i in coll.items]
    assert urls[:3] == [f"{base_url}/watch/1", f"{base_url}/watch/2", f"{base_url}/watch/3"]
    assert "https://evil.example/phish" not in urls  # invented link dropped
    session = browser_manager.get(first.id)

    second_item = coll.items[1]
    out = await select_result(first.id, SelectRequest(item_id=second_item.id))
    follow = task_store.get(out["task_id"])
    assert follow.id != first.id and follow.details["Selected"] == "Tum Hi Ho - Acoustic cover"
    assert "play" in follow.instruction.lower()
    assert browser_manager.get(follow.id) is session and browser_manager.get(first.id) is None  # same window
    await wait_for(lambda: follow.is_terminal)
    assert follow.status.value == "completed"
    assert await session.page.title() == "Watching 2"


@pytest.mark.browser
async def test_select_answers_a_pending_choice(env, base_url):
    from agent.state import task_store
    from agent.workflow import runner
    from browser.browser_manager import browser_manager
    from models.task_models import SelectRequest
    from routes.task_routes import select_result

    env.override = LLM(base_url, ask=True)
    record = task_store.create("Let me choose a Tum Hi Ho video", capability="music")
    runner.start(record)
    await wait_for(lambda: record.status.value == "awaiting_clarification")
    third = record.results[0].items[2]
    out = await select_result(record.id, SelectRequest(item_id=third.id))
    assert out["task_id"] == record.id  # same task continues
    await wait_for(lambda: record.is_terminal)
    assert record.status.value == "completed"
    assert await browser_manager.get(record.id).page.title() == "Watching 3"


async def test_select_rejected_while_running(env):
    from fastapi import HTTPException

    from agent.state import task_store
    from models.task_models import ResultCollection, ResultItem, SelectRequest, TaskStatus
    from routes.task_routes import select_result

    record = task_store.create("x")
    record.status = TaskStatus.RUNNING
    record.results = [ResultCollection(id="c", title="t", columns=["title"], extracted_at=0,
                                       items=[ResultItem(id="i1", fields={"title": "A"})])]
    with pytest.raises(HTTPException) as exc:
        await select_result(record.id, SelectRequest(item_id="i1"))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await select_result(record.id, SelectRequest(item_id="nope"))
    assert exc.value.status_code == 404

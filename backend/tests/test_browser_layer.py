"""Real Chromium against a local fixture page (no network needed).

Covers: generic DOM/accessibility observation (incl. shadow DOM), typing,
select, overlay-intercepted clicks (keyboard fallback), the navigation guard,
and that the safety gate sees the form context of a sign-up submit button.
"""
import pytest

from agent.state import TaskRecord
from browser.browser_actions import BrowserActions
from browser.browser_manager import browser_manager
from browser.page_extractor import element_context, observe
from services.safety_policy import assess

FIXTURE = """<!doctype html><html><head><title>Fixture</title></head><body>
<form role="search" onsubmit="event.preventDefault(); document.getElementById('out').textContent='searched:' + this.q.value">
  <input name="q" aria-label="Search products" placeholder="Search">
  <button type="submit">Search</button>
</form>
<p id="out"></p>
<div style="position:relative;width:300px;height:40px">
  <div role="link" tabindex="0" aria-label="Open result: Laptop X for ₹54,990" id="lnk"
       style="position:absolute;inset:0" onclick="document.title='activated'"
       onkeydown="if(event.key==='Enter'){document.title='activated'}"></div>
  <div style="position:absolute;inset:0;z-index:5;background:#eee">Laptop X  ₹54,990</div>
</div>
<select aria-label="Sort by"><option>Relevance</option><option>Price: low to high</option></select>
<custom-card></custom-card>
<form id="signup" onsubmit="event.preventDefault(); document.title='submitted'">
  <label>Full name <input name="fullname"></label>
  <label>Email <input type="email" name="email"></label>
  <button type="submit">Create account</button>
</form>
<a href="file:///C:/Windows/win.ini" id="bad">Local file</a>
<script>
customElements.define('custom-card', class extends HTMLElement {
  constructor(){ super(); const r = this.attachShadow({mode:'open'});
    r.innerHTML = '<button onclick="document.title=\\'shadow\\'">Add to cart</button>'; }
});
</script>
</body></html>"""


@pytest.fixture
async def session():
    record = TaskRecord("fixture test")
    s = await browser_manager.ensure_session(record)
    await s.page.set_content(FIXTURE)
    yield s
    await browser_manager.shutdown()


def find(obs, name):
    return next(e for e in obs.elements if e.name.startswith(name))


@pytest.mark.browser
async def test_observe_and_interact(session):
    page = session.page
    obs = await observe(page)
    names = [e.name for e in obs.elements]
    assert "Search products" in names and "Add to cart" in names  # shadow DOM included
    actions = BrowserActions(session, obs)

    r = await actions.type_text(find(obs, "Search products").index, "gaming laptop", submit=True)
    assert r.success
    assert "searched:gaming laptop" in await page.inner_text("#out")

    obs = await observe(page)
    actions = BrowserActions(session, obs)
    r = await actions.click(find(obs, "Open result").index)  # covered by an overlay
    assert r.success and await page.title() == "activated", r.message

    obs = await observe(page)
    actions = BrowserActions(session, obs)
    assert (await actions.select_option(find(obs, "Sort by").index, "Price: low to high")).success
    assert (await actions.click(find(obs, "Add to cart").index)).success
    assert await page.title() == "shadow"

    r = await actions.click(999)
    assert not r.success and "not in the latest observation" in r.message


@pytest.mark.browser
async def test_navigation_guard(session):
    actions = BrowserActions(session, await observe(session.page))
    r = await actions.navigate("http://127.0.0.1:8000/api/tasks")
    assert not r.success and "security policy" in r.message
    r = await actions.navigate("file:///C:/Windows/win.ini")
    assert not r.success


@pytest.mark.browser
async def test_gate_sees_signup_form(session):
    page = session.page
    await page.fill("input[name=fullname]", "Asha Rao")
    await page.fill("input[name=email]", "asha@example.com")
    obs = await observe(page)
    button = find(obs, "Create account")
    ctx = await element_context(page, button.index)
    a = assess({"name": "click", "params": {"element": button.index}}, element=button, context=ctx, page_url=obs.url, agent_flag=None)
    assert a.requires_approval and a.kind.value == "sensitive_form"
    search = find(obs, "Search")
    ctx = await element_context(page, search.index)
    assert not assess({"name": "click", "params": {"element": search.index}}, element=search, context=ctx,
                      page_url=obs.url, agent_flag=None).requires_approval

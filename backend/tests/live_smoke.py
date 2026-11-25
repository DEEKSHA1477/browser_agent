"""Live smoke test against real websites (needs internet, no LLM key).

Drives real sites through the exact observation + action layer the agent uses,
choosing elements with simple generic rules instead of an LLM, and checks that
the observation exposes what the LLM needs to make decisions.

    cd backend
    ..\\.venv\\Scripts\\python.exe tests\\live_smoke.py            # headless
    ..\\.venv\\Scripts\\python.exe tests\\live_smoke.py --headed   # visible browser

Not collected by pytest (file name does not start with test_).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["HEADLESS"] = "false" if "--headed" in sys.argv else "true"
os.environ.setdefault("BROWSER_SLOW_MO_MS", "0")

from agent.state import TaskRecord  # noqa: E402
from browser.browser_actions import BrowserActions  # noqa: E402
from browser.browser_manager import browser_manager  # noqa: E402
from browser.page_extractor import observe  # noqa: E402
from services import comparison_service, safety_policy  # noqa: E402

PRICE = re.compile(r"₹\s?[\d,]{3,}")
results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


async def fresh(session):
    for attempt in range(4):  # same retry the agent's observe node uses (redirects destroy the JS context)
        try:
            obs = await observe(session.page)
            break
        except Exception:
            if attempt == 3:
                raise
            await asyncio.sleep(1.0 + attempt)
    obs.tabs = session.tabs()
    return obs, BrowserActions(session, obs)


async def youtube(session) -> None:
    obs, act = await fresh(session)
    r = await act.navigate("https://www.youtube.com/results?search_query=arijit+singh+songs")
    obs, act = await fresh(session)
    if obs.blocker:
        return report("YouTube", False, f"blocked: {obs.blocker}")
    videos = [e for e in obs.elements if e.href and "/watch?v=" in e.href and len(e.name) > 15]
    if not videos:
        return report("YouTube", False, f"no video links found ({r.message}); consent page? title={obs.title!r}")
    target = videos[0]
    await act.click(target.index)
    media = None
    for _ in range(12):  # ads / buffering
        await asyncio.sleep(1.5)
        obs, act = await fresh(session)
        media = obs.media
        if media and not media["paused"] and media["current_time"] > 0:
            break
    ok = bool(media) and not media["paused"]
    line = next((ln for ln in obs.to_prompt().splitlines() if ln.startswith("Media:")), "no media line")
    report("YouTube play", ok, f"clicked '{target.name[:50]}' -> {obs.url[:60]} | {line}")


async def amazon(session) -> None:
    obs, act = await fresh(session)
    r = await act.navigate("https://www.amazon.in/s?k=wireless+headphones")
    obs, _ = await fresh(session)
    if obs.blocker:
        return report("Amazon", False, f"blocked: {obs.blocker}")
    prices = PRICE.findall(obs.text)
    report("Amazon search", len(prices) >= 3, f"{r.message[:60]} | {len(obs.elements)} elements, {len(prices)} prices e.g. {prices[:3]}")
    # Anti-fabrication on real data: a real product line is kept, an invented one dropped.
    product = next((e for e in obs.elements if e.href and "/dp/" in e.href and len(e.name) > 25), None)
    if product and prices:
        name, price = product.name, prices[0]
        coll = comparison_service.build_collection(
            {"title": "Headphones", "columns": ["name", "price"],
             "items": [{"fields": {"name": name, "price": price}},
                       {"fields": {"name": "Imaginary SuperPhones Z9000", "price": "₹1,23,457"}}]},
            corpus=obs.corpus, source_url=obs.url, source_title=obs.title)
        report("Amazon extraction verification", len(coll.items) == 1 and coll.dropped_unverified == 1,
               f"kept {[i.fields['name'][:40] for i in coll.items]}, dropped {coll.dropped_unverified}")
    # Safety gate on a real page: "Add to cart" is not gated, "Buy Now" would be.
    add = next((e for e in obs.elements if re.search(r"add to cart", e.name, re.I)), None)
    if add:
        a = safety_policy.assess({"name": "click", "params": {"element": add.index}}, element=add, context=None,
                                 page_url=obs.url, agent_flag=None)
        report("Amazon 'Add to cart' not gated", not a.requires_approval, add.descriptor)


async def web_search(session) -> None:
    engines = {
        "google": ("https://www.google.com/search?q=latest+AI+research+papers&hl=en", "google."),
        "bing": ("https://www.bing.com/search?q=latest+AI+research+papers", "bing.com"),
        "duckduckgo": ("https://duckduckgo.com/?q=latest+AI+research+papers", "duckduckgo"),
    }
    for engine, (url, own) in engines.items():
        obs, act = await fresh(session)
        await act.navigate(url)
        await asyncio.sleep(2)
        obs, _ = await fresh(session)
        links = [e for e in obs.elements if e.href and own not in e.href and len(e.name) > 20]
        detail = f"blocker={obs.blocker}" if obs.blocker else (f"{len(links)} result links, e.g. '{links[0].name[:50]}'" if links else obs.title[:60])
        report(f"Web search ({engine})", len(links) >= 5 and not obs.blocker, detail)


async def arxiv(session) -> None:
    obs, act = await fresh(session)
    await act.navigate("https://arxiv.org/search/?query=large+language+models&searchtype=all&order=-announced_date_first")
    obs, _ = await fresh(session)
    papers = [e for e in obs.elements if e.href and "/abs/" in e.href]
    report("arXiv papers", len(papers) >= 5, f"{len(papers)} paper links")


async def flights(session) -> None:
    obs, act = await fresh(session)
    await act.navigate("https://www.google.com/travel/flights?q=Flights%20from%20Bengaluru%20to%20Delhi%20one%20way&curr=INR&hl=en")
    for _ in range(6):
        await asyncio.sleep(2)
        obs, _ = await fresh(session)
        if len(PRICE.findall(obs.corpus)) >= 3 or obs.blocker:
            break
    if obs.blocker:
        return report("Google Flights", False, f"blocked: {obs.blocker}")
    fares = PRICE.findall(obs.corpus)
    report("Google Flights", len(fares) >= 3, f"{len(fares)} fares in observation, e.g. {fares[:3]} | {obs.title[:50]}")


async def jobs(session) -> None:
    obs, act = await fresh(session)
    await act.navigate("https://www.naukri.com/python-developer-fresher-jobs")
    await asyncio.sleep(3)
    obs, _ = await fresh(session)
    titles = [e for e in obs.elements if e.href and "job-listings" in e.href]
    report("Jobs (Naukri)", len(titles) >= 3, f"{len(titles)} job links, e.g. '{titles[0].name[:60]}'" if titles else f"title={obs.title!r} blocker={obs.blocker}")


async def main() -> int:
    record = TaskRecord("live smoke test")
    session = await browser_manager.ensure_session(record)
    try:
        for check in (youtube, amazon, web_search, arxiv, flights, jobs):
            try:
                await asyncio.wait_for(check(session), timeout=90)
            except Exception as exc:  # report and continue with the next site
                report(check.__name__, False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}")
    finally:
        await browser_manager.shutdown()
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} live checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))

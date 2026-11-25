"""Launches and manages one visible Chromium browser per task.

Responsibilities:
  * launch a headed (visible) Chromium via Playwright,
  * enforce the navigation policy on every document request (CDP Fetch
    interception limited to Document requests, so page performance and HTTP
    caching are unaffected), with a post-navigation safety net,
  * follow new tabs / popups,
  * stream live screenshots to the frontend over WebSocket,
  * save downloads into the configured download directory,
  * detect browser crashes so the agent can relaunch on retry.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Download, Page, Playwright, async_playwright

from browser.security import check_url
from config import settings
from realtime.connection_manager import manager

if TYPE_CHECKING:
    from agent.state import TaskRecord

logger = logging.getLogger(__name__)

DANGEROUS_DOWNLOAD_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jar", ".scr", ".apk", ".dmg", ".pkg", ".sh", ".com", ".hta"}


class BrowserCrashed(Exception):
    pass


class BrowserSession:
    def __init__(self, record: "TaskRecord") -> None:
        self.record = record
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.pages: list[Page] = []
        self.active_page: Page | None = None
        self.crashed = False
        self.closed = False
        self.allow_dangerous_download_once = False
        self._stream_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    @property
    def alive(self) -> bool:
        return not self.closed and not self.crashed and self.browser is not None and self.browser.is_connected()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def start(self, pw: Playwright) -> None:
        self.browser = await pw.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo_ms)
        self.browser.on("disconnected", lambda _: self._on_disconnected())
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale=settings.locale,
            timezone_id=settings.timezone,
            accept_downloads=True,
        )
        self.context.set_default_timeout(15000)
        self.context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        self.context.on("page", lambda page: self._spawn(self._register_page(page)))
        page = await self.context.new_page()
        await self._register_page(page)
        self._stream_task = asyncio.create_task(self._stream_screenshots())

    def _on_disconnected(self) -> None:
        if not self.closed:
            self.crashed = True
            self.record.browser_open = False
            self._spawn(self.record.log("The browser closed or crashed unexpectedly.", "error"))

    async def _register_page(self, page: Page) -> None:
        if page in self.pages:
            return
        self.pages.append(page)
        self.active_page = page
        page.on("close", lambda p: self._on_page_closed(p))
        page.on("download", lambda d: self._spawn(self._handle_download(d)))
        page.on("framenavigated", lambda frame: self._spawn(self._after_navigation(page, frame)))
        try:
            cdp = await self.context.new_cdp_session(page)  # type: ignore[union-attr]
            cdp.on("Fetch.requestPaused", lambda params: self._spawn(self._on_request_paused(cdp, params)))
            await cdp.send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]},
            )
        except Exception as exc:  # the post-navigation check still applies
            logger.warning("Could not enable navigation interception: %s", exc)
        if len(self.pages) > 1:
            await self.record.log("A new tab was opened; following it.", "info")

    def _on_page_closed(self, page: Page) -> None:
        if page in self.pages:
            self.pages.remove(page)
        if self.active_page is page:
            self.active_page = self.pages[-1] if self.pages else None

    async def _on_request_paused(self, cdp, params: dict) -> None:
        url = params.get("request", {}).get("url", "")
        allowed, reason = check_url(url)
        try:
            if allowed:
                await cdp.send("Fetch.continueRequest", {"requestId": params["requestId"]})
            else:
                await cdp.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"})
                await self.record.log(f"Blocked navigation to {url[:120]} ({reason}).", "warning")
        except Exception:
            pass  # page closed mid-request

    async def _after_navigation(self, page: Page, frame) -> None:
        if frame != page.main_frame:
            return
        allowed, reason = check_url(frame.url)
        if not allowed:
            await self.record.log(f"Left disallowed page {frame.url[:120]} ({reason}).", "warning")
            try:
                await page.goto("about:blank")
            except Exception:
                pass

    async def _handle_download(self, download: Download) -> None:
        name = re.sub(r"[^\w.\- ()]", "_", download.suggested_filename or "download")[:150]
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext in DANGEROUS_DOWNLOAD_EXT and not self.allow_dangerous_download_once:
            await download.cancel()
            await self.record.log(f"Cancelled download of potentially dangerous file '{name}' (not approved).", "warning")
            return
        self.allow_dangerous_download_once = False
        try:
            settings.download_dir.mkdir(parents=True, exist_ok=True)
            target = settings.download_dir / name
            if target.exists():
                target = settings.download_dir / f"{int(time.time())}_{name}"
            await download.save_as(target)
            self.record.downloads.append({"name": name, "path": str(target), "url": download.url, "ts": time.time()})
            await self.record.log(f"Downloaded '{name}' to {target}", "success")
        except Exception as exc:
            await self.record.log(f"Download of '{name}' failed: {exc}", "error")
            self.record.downloads.append({"name": name, "path": None, "url": download.url, "error": str(exc), "ts": time.time()})

    @property
    def page(self) -> Page:
        if not self.alive:
            raise BrowserCrashed("The browser is not running.")
        if self.active_page is None or self.active_page.is_closed():
            if not self.pages:
                raise BrowserCrashed("No open browser tab.")
            self.active_page = self.pages[-1]
        return self.active_page

    async def ensure_page(self) -> bool:
        """Re-open a tab if the user closed every tab/window but the browser is still running.

        Returns True if a new tab had to be opened."""
        if not self.alive:
            raise BrowserCrashed("The browser is not running.")
        if any(not p.is_closed() for p in self.pages):
            return False
        try:
            page = await self.context.new_page()  # type: ignore[union-attr]
        except Exception as exc:  # the window/context itself is gone
            self.crashed = True
            raise BrowserCrashed(f"The browser window was closed: {exc}") from exc
        await self._register_page(page)
        return True

    def tabs(self) -> list[dict]:
        out = []
        for i, p in enumerate(self.pages):
            try:
                out.append({"index": i, "url": p.url, "title": "", "active": p is self.active_page})
            except Exception:
                continue
        return out

    async def switch_to(self, index: int) -> Page:
        if index < 0 or index >= len(self.pages):
            raise IndexError(f"No tab with index {index}")
        self.active_page = self.pages[index]
        await self.active_page.bring_to_front()
        return self.active_page

    async def screenshot(self, quality: int = 60) -> bytes:
        return await self.page.screenshot(type="jpeg", quality=quality, timeout=8000)

    async def _stream_screenshots(self) -> None:
        last_url = None
        while not self.closed and not self.crashed:
            try:
                if self.active_page and not self.active_page.is_closed():
                    page = self.active_page
                    url = page.url
                    if url != last_url:
                        last_url = url
                        self.record.current_url = url
                        try:
                            self.record.page_title = await page.title()
                        except Exception:
                            pass
                        await self.record.publish()
                    if manager.has_listeners(self.record.id):
                        shot = await page.screenshot(type="jpeg", quality=50, timeout=5000)
                        await self.record.publish_screenshot(base64.b64encode(shot).decode(), url, self.record.page_title)
            except Exception:
                pass  # navigation in progress, page closing, etc.
            await asyncio.sleep(settings.screenshot_interval_s)

    async def close(self) -> None:
        self.closed = True
        if self._stream_task:
            self._stream_task.cancel()
        try:
            if self.browser and self.browser.is_connected():
                await self.browser.close()
        except Exception:
            pass
        self.record.browser_open = False


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def _playwright(self) -> Playwright:
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self._pw

    def get(self, task_id: str) -> BrowserSession | None:
        return self._sessions.get(task_id)

    async def ensure_session(self, record: "TaskRecord") -> BrowserSession:
        """Return a live session for the task, relaunching after a crash."""
        async with self._lock:
            session = self._sessions.get(record.id)
            if session and session.alive:
                return session
            if session:
                await session.close()
            await self._evict_if_needed(exclude=record.id)
            session = BrowserSession(record)
            await session.start(await self._playwright())
            self._sessions[record.id] = session
            record.browser_open = True
            return session

    async def adopt(self, from_task_id: str, record: "TaskRecord") -> bool:
        """Hand a finished task's open browser to its follow-up task (e.g. after the user selects a result)."""
        async with self._lock:
            session = self._sessions.get(from_task_id)
            if session is None or not session.alive:
                return False
            self._sessions.pop(from_task_id, None)
            session.record.browser_open = False
            session.record = record
            self._sessions[record.id] = session
            record.browser_open = True
            return True

    async def _evict_if_needed(self, exclude: str) -> None:
        open_ids = [tid for tid, s in self._sessions.items() if s.alive and tid != exclude]
        while len(open_ids) >= settings.max_browser_sessions:
            oldest = open_ids.pop(0)
            await self.close(oldest)

    async def close(self, task_id: str) -> None:
        session = self._sessions.pop(task_id, None)
        if session:
            await session.close()

    async def shutdown(self) -> None:
        for task_id in list(self._sessions):
            await self.close(task_id)
        if self._pw:
            await self._pw.stop()
            self._pw = None


browser_manager = BrowserManager()

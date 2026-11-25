"""Low-level, site-agnostic browser actions.

Every action:
  * passes the task's cooperative checkpoint first (so Stop / Pause apply
    immediately, even mid-plan),
  * resolves elements by the index assigned during the latest observation,
  * reports a structured ``ActionResult`` instead of raising for ordinary
    failures, so the agent can observe what went wrong and adapt.

These functions perform NO sensitivity decisions themselves. They are only
reachable through the agent executor, which runs every action through the
safety policy and the approval service first.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from browser.browser_manager import BrowserSession
from browser.page_extractor import Observation
from browser.security import check_url, normalize_user_url
from config import settings

ALLOWED_KEYS = re.compile(
    r"^((Control|Shift|Alt|Meta)\+)*(Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|"
    r"PageUp|PageDown|Home|End|Space|F5|[A-Za-z0-9])$"
)


@dataclass
class ActionResult:
    success: bool
    message: str
    extra: dict[str, Any] = field(default_factory=dict)


class BrowserActions:
    def __init__(self, session: BrowserSession, observation: Observation | None) -> None:
        self.session = session
        self.obs = observation

    @property
    def page(self) -> Page:
        return self.session.page

    async def _checkpoint(self) -> None:
        await self.session.record.control.checkpoint()

    def _locator(self, index: int) -> Locator:
        if self.obs is None or self.obs.element(index) is None:
            raise LookupError(f"Element [{index}] is not in the latest observation. Use an index from the current element list.")
        return self.page.locator(f'[data-nova-id="{index}"]').first

    async def _settle(self, quick: bool = False) -> None:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000 if not quick else 3000)
        except PlaywrightError:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=1500 if quick else 3000)
        except PlaywrightError:
            pass  # many sites (YouTube, streaming) never go fully idle
        await page.wait_for_timeout(400 if quick else 700)

    # -- navigation ---------------------------------------------------------------

    async def navigate(self, url: str, new_tab: bool = False) -> ActionResult:
        await self._checkpoint()
        url = normalize_user_url(url)
        allowed, reason = check_url(url, internal_ok=False)
        if not allowed:
            return ActionResult(False, f"Navigation refused by security policy: {reason}.")
        if new_tab:
            page = await self.session.context.new_page()  # type: ignore[union-attr]
            await self.session._register_page(page)
        page = self.page
        last_error = None
        for attempt in range(3):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
                await self._settle()
                status = response.status if response else None
                if status and status >= 400:
                    return ActionResult(False, f"Page loaded with HTTP {status}.", {"status": status})
                return ActionResult(True, f"Opened {page.url[:150]}", {"status": status})
            except PlaywrightTimeout as exc:
                last_error = f"timed out loading {url}"
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    await self._checkpoint()
            except PlaywrightError as exc:
                msg = str(exc).split("\n")[0]
                if "ERR_BLOCKED_BY_CLIENT" in msg:
                    return ActionResult(False, "Navigation was blocked by the security policy.")
                last_error = msg
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    await self._checkpoint()
        return ActionResult(False, f"Failed to load page after 3 attempts: {last_error}")

    async def go_back(self) -> ActionResult:
        await self._checkpoint()
        try:
            await self.page.go_back(timeout=15000)
            await self._settle()
            return ActionResult(True, f"Went back to {self.page.url[:150]}")
        except PlaywrightError as exc:
            return ActionResult(False, f"Could not go back: {str(exc).splitlines()[0]}")

    async def switch_tab(self, index: int) -> ActionResult:
        await self._checkpoint()
        try:
            page = await self.session.switch_to(index)
            return ActionResult(True, f"Switched to tab {index}: {page.url[:120]}")
        except IndexError as exc:
            return ActionResult(False, str(exc))

    # -- element interaction ----------------------------------------------------------

    async def click(self, index: int) -> ActionResult:
        await self._checkpoint()
        try:
            loc = self._locator(index)
        except LookupError as exc:
            return ActionResult(False, str(exc))
        pages_before = len(self.session.pages)
        try:
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except PlaywrightError:
                pass
            await loc.click(timeout=5000)
            method = "click"
        except PlaywrightError as exc:
            # Overlays / intercepted pointer events are common on modern sites
            # (e.g. result cards whose children capture clicks). Activate the
            # element through the keyboard, as assistive technology would.
            try:
                await loc.focus(timeout=3000)
                await self.page.keyboard.press("Enter")
                method = "keyboard activation"
            except PlaywrightError:
                try:
                    await loc.dispatch_event("click", timeout=3000)
                    method = "dispatched click"
                except PlaywrightError as exc2:
                    return ActionResult(False, f"Could not click element [{index}]: {str(exc).splitlines()[0]}")
        await self._settle()
        note = " A new tab opened and is now active." if len(self.session.pages) > pages_before else ""
        return ActionResult(True, f"Clicked [{index}] via {method}.{note}")

    async def type_text(self, index: int, text: str, clear: bool = True, submit: bool = False) -> ActionResult:
        await self._checkpoint()
        try:
            loc = self._locator(index)
        except LookupError as exc:
            return ActionResult(False, str(exc))
        try:
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except PlaywrightError:
                pass
            try:
                if clear:
                    await loc.fill("", timeout=4000)
                await loc.press_sequentially(text, delay=35, timeout=max(8000, len(text) * 80))
            except PlaywrightError:
                # Not a native input (e.g. a div[role=combobox] or contenteditable)
                await loc.click(timeout=5000)
                if clear:
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                await self.page.keyboard.type(text, delay=35)
            if submit:
                await self.page.keyboard.press("Enter")
                await self._settle()
            else:
                await self.page.wait_for_timeout(600)  # let autocomplete suggestions render
            return ActionResult(True, f"Typed into [{index}]" + (" and pressed Enter." if submit else "."))
        except PlaywrightError as exc:
            return ActionResult(False, f"Could not type into [{index}]: {str(exc).splitlines()[0]}")

    async def select_option(self, index: int, option: str) -> ActionResult:
        await self._checkpoint()
        try:
            loc = self._locator(index)
        except LookupError as exc:
            return ActionResult(False, str(exc))
        try:
            try:
                await loc.select_option(label=option, timeout=4000)
            except PlaywrightError:
                await loc.select_option(value=option, timeout=4000)
            await self._settle(quick=True)
            return ActionResult(True, f"Selected '{option}' in [{index}].")
        except PlaywrightError as exc:
            return ActionResult(False, f"Could not select '{option}' in [{index}]: {str(exc).splitlines()[0]}. "
                                "If this is a custom dropdown, click it and then click the option.")

    async def press_key(self, key: str) -> ActionResult:
        await self._checkpoint()
        if not ALLOWED_KEYS.match(key):
            return ActionResult(False, f"Key '{key}' is not allowed.")
        await self.page.keyboard.press(key)
        await self._settle(quick=True)
        return ActionResult(True, f"Pressed {key}.")

    async def scroll(self, direction: str = "down", amount: float = 1.0, index: int | None = None) -> ActionResult:
        await self._checkpoint()
        page = self.page
        if index is not None:
            try:
                await self._locator(index).scroll_into_view_if_needed(timeout=4000)
                await page.wait_for_timeout(500)
                return ActionResult(True, f"Scrolled element [{index}] into view.")
            except (LookupError, PlaywrightError) as exc:
                return ActionResult(False, f"Could not scroll to [{index}]: {exc}")
        amount = max(0.2, min(float(amount or 1), 5))
        height = await page.evaluate("window.innerHeight")
        delta = int(height * 0.85 * amount) * (-1 if direction == "up" else 1)
        before = await page.evaluate("window.scrollY")
        await page.mouse.move(640, 400)
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(900)
        after = await page.evaluate("window.scrollY")
        if after == before:
            # Scrollable inner container (common in web apps)
            await page.evaluate("(d) => { const el = document.scrollingElement; window.scrollBy(0, d); }", delta)
            await page.wait_for_timeout(500)
            after = await page.evaluate("window.scrollY")
        moved = "" if after != before else " (page position did not change - may be at the end or using an inner scroll area)"
        return ActionResult(True, f"Scrolled {direction}{moved}.")

    async def wait(self, seconds: float) -> ActionResult:
        seconds = max(0.5, min(float(seconds), 10))
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            await self._checkpoint()
            await asyncio.sleep(0.25)
        return ActionResult(True, f"Waited {seconds:.1f}s.")

    async def download(self, index: int) -> ActionResult:
        await self._checkpoint()
        before = len(self.session.record.downloads)
        result = await self.click(index)
        if not result.success:
            return result
        for _ in range(60):  # up to 30s
            if len(self.session.record.downloads) > before:
                item = self.session.record.downloads[-1]
                if item.get("path"):
                    return ActionResult(True, f"Downloaded '{item['name']}' to {item['path']}.", {"download": item})
                return ActionResult(False, f"Download failed: {item.get('error')}")
            await asyncio.sleep(0.5)
            await self._checkpoint()
        return ActionResult(False, "Clicked, but no download started within 30 seconds (or it was cancelled by policy).")

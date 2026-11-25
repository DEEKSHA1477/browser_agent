"""Modular tool registry - the agent's capabilities.

Every capability the LLM can use is a registered tool with a name, a short
description, a parameter spec (shown to the LLM) and a handler. Adding a new
browser capability means registering one more tool here; the planner, the
executor loop, the safety gate and the UI stay unchanged.

Tools never decide whether they are allowed to run: the executor runs the
safety policy + approval gate *before* calling any handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote_plus

from browser.browser_actions import ActionResult, BrowserActions
from browser.browser_manager import BrowserSession
from browser.page_extractor import Observation
from config import settings
from services import comparison_service


@dataclass
class ToolContext:
    record: Any  # TaskRecord
    session: BrowserSession
    actions: BrowserActions
    observation: Observation | None


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ActionResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    params: str
    uses_element: bool = False
    control: bool = False  # handled by the workflow (done / ask_user), not a browser action
    handler: Handler | None = None


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, params: str = "{}", *, uses_element: bool = False, control: bool = False):
    def decorator(fn: Handler | None):
        REGISTRY[name] = ToolSpec(name, description, params, uses_element, control, fn)
        return fn

    return decorator


def describe_tools() -> str:
    return "\n".join(f"- {t.name}: {t.description}\n    params: {t.params}" for t in REGISTRY.values())


def validate_action(action: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(action, dict):
        return None, "Action must be an object with 'name' and 'params'."
    name = action.get("name")
    params = action.get("params") or {}
    if name not in REGISTRY:
        return None, f"Unknown action '{name}'. Available: {', '.join(REGISTRY)}."
    if not isinstance(params, dict):
        return None, "'params' must be an object."
    spec = REGISTRY[name]
    if spec.uses_element:
        try:
            params["element"] = int(params.get("element"))
        except (TypeError, ValueError):
            return None, f"Action '{name}' requires an integer 'element' index from the element list."
    return {"name": name, "params": params}, None


def describe_action(action: dict[str, Any], observation: Observation | None, *, mask_text: bool = False) -> str:
    """Short human-readable label for the timeline / approval card."""
    name, p = action["name"], action.get("params") or {}
    el = observation.element(p["element"]) if observation and "element" in p else None
    target = el.descriptor if el else (f"element [{p.get('element')}]" if "element" in p else "")
    text = "••••••" if mask_text else str(p.get("text", ""))[:60]
    return {
        "navigate": f"Open {str(p.get('url', ''))[:80]}",
        "web_search": f"Search the web for \"{str(p.get('query', ''))[:60]}\"",
        "click": f"Click {target}",
        "type": f"Type \"{text}\" into {target}" + (" and submit" if p.get("submit") else ""),
        "select": f"Select \"{p.get('option')}\" in {target}",
        "press": f"Press {p.get('key')}",
        "scroll": f"Scroll {p.get('direction', 'down')}" + (f" to {target}" if el else ""),
        "go_back": "Go back",
        "switch_tab": f"Switch to tab {p.get('tab')}",
        "wait": f"Wait {p.get('seconds', 2)}s",
        "download": f"Download via {target}",
        "extract": f"Extract \"{str(p.get('title', 'results'))[:50]}\" ({len(p.get('items') or [])} items)",
        "request_human_help": f"Ask you to handle: {str(p.get('reason', ''))[:80]}",
        "ask_user": "Ask you a question",
        "done": "Finish task",
    }.get(name, name)


# --------------------------------------------------------------------------- tools


@tool("navigate", "Open a URL in the current tab (or a new tab).", '{"url": str, "new_tab": bool = false}')
async def _navigate(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.navigate(str(p.get("url", "")), bool(p.get("new_tab", False)))


@tool("web_search", "Run a web search and open the results page.", '{"query": str}')
async def _web_search(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    q = quote_plus(str(p.get("query", "")))
    url = {
        "google": f"https://www.google.com/search?q={q}&hl=en",
        "bing": f"https://www.bing.com/search?q={q}",
    }.get(settings.search_engine, f"https://duckduckgo.com/?q={q}")
    return await ctx.actions.navigate(url)


@tool("click", "Click an element (link, button, result, tab, checkbox, option...).", '{"element": int}', uses_element=True)
async def _click(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.click(p["element"])


@tool(
    "type",
    "Type text into an input/textarea/editable element. Set submit=true to press Enter afterwards (e.g. search boxes).",
    '{"element": int, "text": str, "clear": bool = true, "submit": bool = false}',
    uses_element=True,
)
async def _type(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.type_text(p["element"], str(p.get("text", "")), bool(p.get("clear", True)), bool(p.get("submit", False)))


@tool("select", "Choose an option in a native <select> dropdown by its visible label.", '{"element": int, "option": str}', uses_element=True)
async def _select(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.select_option(p["element"], str(p.get("option", "")))


@tool("press", "Press a keyboard key or shortcut in the focused element (Enter, Escape, Tab, ArrowDown, PageDown...).", '{"key": str}')
async def _press(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.press_key(str(p.get("key", "")))


@tool(
    "scroll",
    "Scroll the page up/down by a number of screens, or scroll a specific element into view.",
    '{"direction": "down"|"up" = "down", "amount": float = 1, "element": int | null}',
)
async def _scroll(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    element = p.get("element")
    return await ctx.actions.scroll(str(p.get("direction", "down")), float(p.get("amount", 1) or 1), int(element) if element not in (None, "") else None)


@tool("go_back", "Go back to the previous page.")
async def _go_back(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.go_back()


@tool("switch_tab", "Switch to another open tab by index.", '{"tab": int}')
async def _switch_tab(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.switch_tab(int(p.get("tab", 0)))


@tool("wait", "Wait for content to load or a video ad to finish (max 10s).", '{"seconds": float = 2}')
async def _wait(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.wait(float(p.get("seconds", 2) or 2))


@tool("download", "Click an element that downloads a file and wait for the download to finish.", '{"element": int}', uses_element=True)
async def _download(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    return await ctx.actions.download(p["element"])


@tool(
    "extract",
    "Record structured results you can SEE on the current page (products, flights, jobs, videos, papers...) for the comparison "
    "table the user selects from. Copy values verbatim from the page; never estimate or invent. Unverifiable records are "
    "discarded. For each item give 'element' = the index of its main link/title so the user can open or select it.",
    '{"title": str, "columns": [str], "items": [{"fields": {column: value}, "element": int | null}], '
    '"recommended_index": int | null, "recommendation_reason": str | null}',
)
async def _extract(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:
    obs = ctx.observation
    if obs is None:
        return ActionResult(False, "Nothing observed yet.")
    # Links come from the page's own elements, never from free text the model (or a page) supplied.
    page_links = {e.href for e in obs.elements if e.href}
    for item in p.get("items") or []:
        if not isinstance(item, dict):
            continue
        el = None
        try:
            el = obs.element(int(item.get("element"))) if item.get("element") is not None else None
        except (TypeError, ValueError):
            el = None
        if el is not None and el.href:
            item["url"] = el.href
        elif item.get("url") not in page_links:
            item["url"] = None
    collection = comparison_service.build_collection(p, corpus=obs.corpus, source_url=obs.url, source_title=obs.title)
    if not collection.items:
        return ActionResult(
            False,
            f"None of the {collection.dropped_unverified} records could be verified against the page text. Extract only values visible on this page.",
        )
    ctx.record.results = comparison_service.merge_into(ctx.record.results, collection)
    msg = f"Recorded {len(collection.items)} verified record(s) in '{collection.title}'."
    if collection.dropped_unverified:
        msg += f" Discarded {collection.dropped_unverified} record(s) whose values were not found on the page."
    return ActionResult(True, msg, {"verified": len(collection.items), "dropped": collection.dropped_unverified})


@tool(
    "request_human_help",
    "Hand control to the user for something only a human may do: solving a CAPTCHA, logging in, entering secrets/OTP "
    "that the user did not give you. The agent waits until the user clicks 'Done'.",
    '{"kind": "captcha"|"login"|"other", "reason": str}',
)
async def _request_human_help(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:  # implemented in executor
    raise NotImplementedError


@tool("ask_user", "Ask the user a question when essential information is missing or a choice is genuinely theirs.", '{"question": str}', control=True)
async def _ask_user(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:  # routed by the workflow
    raise NotImplementedError


@tool(
    "done",
    "Finish the task. Use success=false if it could not be completed. The summary must state only what you observed.",
    '{"success": bool, "summary": str}',
    control=True,
)
async def _done(ctx: ToolContext, p: dict[str, Any]) -> ActionResult:  # routed by the workflow
    raise NotImplementedError

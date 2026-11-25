"""Generic page observation built from the DOM and accessibility information.

No site-specific selectors are used. On every step the agent:
  1. collects visible interactive elements (links, buttons, inputs, ARIA roles,
     open shadow roots included) and tags each with ``data-nova-id=<index>``,
  2. computes an accessible name for each one (aria-label, aria-labelledby,
     <label>, text, title/alt/placeholder...),
  3. captures the visible text, media playback state, scroll position and any
     CAPTCHA / human-verification blockers,
  4. optionally captures a screenshot for visual reasoning.

The LLM refers to elements only by index; actions then resolve the index to
``[data-nova-id="N"]`` (Playwright CSS pierces open shadow DOM).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

OBSERVE_JS = r"""
({maxElements, maxText}) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],' +
    '[role=menuitemcheckbox],[role=option],[role=combobox],[role=switch],[role=textbox],' +
    '[role=searchbox],[role=slider],[role=spinbutton],[contenteditable="true"],[contenteditable=""],[onclick]';
  const vh = window.innerHeight, vw = window.innerWidth;
  const cands = [];
  const ariaParts = [];
  const walk = (root) => {
    root.querySelectorAll('[data-nova-id]').forEach(e => e.removeAttribute('data-nova-id'));
    root.querySelectorAll(SEL).forEach(e => cands.push(e));
    root.querySelectorAll('[aria-label]').forEach(e => ariaParts.push(e.getAttribute('aria-label')));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const byId = (el, id) => {
    const root = el.getRootNode();
    return (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
  };
  const accName = (el) => {
    let t = el.getAttribute('aria-label');
    if (!t) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) t = lb.split(/\s+/).map(id => { const n = byId(el, id); return n ? (n.innerText || n.textContent) : ''; }).join(' ');
    }
    if (!t && el.labels && el.labels.length) t = Array.from(el.labels).map(l => l.innerText).join(' ');
    if (!t && !['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) t = el.innerText || el.textContent;
    if (!t) t = el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('placeholder') || el.getAttribute('name');
    if (!t && el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) t = el.value;
    if (!t && el.querySelector) {
      const img = el.querySelector('img[alt],svg[aria-label],[title]');
      if (img) t = img.getAttribute('alt') || img.getAttribute('aria-label') || img.getAttribute('title');
    }
    return clean(t).slice(0, 200);
  };
  const box = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) return null;
    return r;
  };
  const items = [];
  const seen = new Set();
  for (const el of cands) {
    const r = box(el);
    if (!r) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    const type = (el.getAttribute('type') || '').toLowerCase();
    const isField = ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable ||
      ['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(role);
    const name = accName(el);
    if (!name && !isField) continue;
    const key = tag + '|' + name + '|' + Math.round(r.left) + '|' + Math.round(r.top);
    if (seen.has(key)) continue;
    seen.add(key);
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    items.push({ el, r, name, tag, role, type, inView, isField });
  }
  items.sort((a, b) => (b.inView - a.inView) || (a.r.top - b.r.top) || (a.r.left - b.r.left));
  const elements = [];
  for (const it of items.slice(0, maxElements)) {
    const idx = elements.length + 1;
    const el = it.el;
    el.setAttribute('data-nova-id', String(idx));
    let value = null;
    if (it.tag === 'select') value = el.options[el.selectedIndex] ? clean(el.options[el.selectedIndex].text) : '';
    else if (it.isField && 'value' in el) value = it.type === 'password' ? (el.value ? '********' : '') : String(el.value || '').slice(0, 100);
    else if (el.isContentEditable) value = clean(el.innerText).slice(0, 100);
    elements.push({
      index: idx, tag: it.tag, role: it.role, type: it.type, name: it.name, value,
      placeholder: el.getAttribute('placeholder') || null,
      href: it.tag === 'a' ? (el.href || '').slice(0, 1500) : null,
      checked: (it.type === 'checkbox' || it.type === 'radio') ? String(!!el.checked) :
        (el.getAttribute('aria-checked') || el.getAttribute('aria-selected') || null),
      expanded: el.getAttribute('aria-expanded'),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      in_viewport: it.inView,
      options: it.tag === 'select' ? Array.from(el.options).slice(0, 30).map(o => clean(o.text)) : null,
    });
  }
  const body = document.body;
  const rawText = body ? (body.innerText || '') : '';
  const text = rawText.replace(/[ \t  ]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  const lower = text.slice(0, 5000).toLowerCase();
  let blocker = null;
  if (/google\.[a-z.]+\/sorry\//.test(location.href) || lower.includes('unusual traffic from your computer')) blocker = 'captcha';
  const capFrames = Array.from(document.querySelectorAll('iframe')).filter(f => {
    const s = f.src || '';
    const r = f.getBoundingClientRect();
    return r.width > 60 && r.height > 60 && /recaptcha\/(api2|enterprise)\/(anchor|bframe)|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs|funcaptcha/.test(s);
  });
  if (capFrames.length) blocker = 'captcha';
  if (/verify you are (a )?human|checking (if the site connection is secure|your browser)|press (&|and) hold|complete the security check/.test(lower)) blocker = 'captcha';
  const v = document.querySelector('video');
  const media = v ? { paused: v.paused, ended: v.ended, current_time: Math.round(v.currentTime || 0),
    duration: isFinite(v.duration) ? Math.round(v.duration) : null, muted: v.muted } : null;
  return {
    url: location.href, title: document.title, elements,
    total_candidates: items.length,
    text: text.slice(0, maxText), text_length: text.length,
    corpus: (text + '\n' + ariaParts.join('\n')).slice(0, 300000),
    scroll: { y: Math.round(window.scrollY), height: document.documentElement.scrollHeight, viewport: vh },
    media, blocker,
  };
}
"""

ELEMENT_CONTEXT_JS = r"""
(idx) => {
  const find = (root) => {
    let el = root.querySelector(`[data-nova-id="${idx}"]`);
    if (el) return el;
    for (const e of root.querySelectorAll('*')) { if (e.shadowRoot) { el = find(e.shadowRoot); if (el) return el; } }
    return null;
  };
  const el = find(document);
  if (!el) return null;
  const form = el.form || el.closest('form');
  const fields = form ? Array.from(form.querySelectorAll('input,textarea,select')).filter(f => f.type !== 'hidden').slice(0, 40) : [];
  const desc = (f) => [(f.type || f.tagName || '').toLowerCase(), f.name || '', f.id || '', f.getAttribute('autocomplete') || '',
    f.getAttribute('aria-label') || '', f.getAttribute('placeholder') || ''].join(' ').toLowerCase();
  let container = el, ctext = '';
  for (let i = 0; i < 6 && container.parentElement; i++) {
    container = container.parentElement;
    const t = container.innerText || '';
    if (t.length > 800) break;
    ctext = t;
  }
  return {
    tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(), role: el.getAttribute('role') || '',
    descriptor: desc(el), href: el.href || null,
    in_form: !!form, form_action: form ? (form.getAttribute('action') || '') : null,
    form_role: form ? (form.getAttribute('role') || '') : null,
    form_fields: fields.map(f => ({ descriptor: desc(f), filled: !!(f.value && String(f.value).trim()) })),
    container_text: ctext.replace(/\s+/g, ' ').trim().slice(0, 800),
  };
}
"""


@dataclass
class ElementInfo:
    index: int
    tag: str
    role: str
    type: str
    name: str
    value: str | None = None
    placeholder: str | None = None
    href: str | None = None
    checked: str | None = None
    expanded: str | None = None
    disabled: bool = False
    in_viewport: bool = True
    options: list[str] | None = None

    @property
    def descriptor(self) -> str:
        """Human-readable description used in the timeline and approval cards."""
        kind = self.role or (f"{self.tag}[{self.type}]" if self.type else self.tag)
        label = self.name or self.placeholder or "(unlabelled)"
        return f'{kind} "{label[:80]}"'

    def to_line(self) -> str:
        kind = self.tag + (f" type={self.type}" if self.type else "") + (f" role={self.role}" if self.role else "")
        parts = [f"[{self.index}] <{kind}>", f'"{self.name}"' if self.name else '""']
        if self.placeholder and self.placeholder != self.name:
            parts.append(f'placeholder="{self.placeholder[:60]}"')
        if self.value not in (None, ""):
            parts.append(f'value="{self.value}"')
        if self.href:
            parts.append(f"href={_short_href(self.href)}")
        if self.checked not in (None, "", "null"):
            parts.append(f"checked={self.checked}")
        if self.expanded not in (None, ""):
            parts.append(f"expanded={self.expanded}")
        if self.options:
            parts.append("options=" + "|".join(o[:30] for o in self.options[:15]))
        if self.disabled:
            parts.append("(disabled)")
        if not self.in_viewport:
            parts.append("(off-screen)")
        return " ".join(parts)


def _short_href(href: str) -> str:
    href = re.sub(r"^https?://(www\.)?", "", href)
    return href[:110]


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ElementInfo]
    text: str
    text_length: int
    corpus: str
    scroll: dict[str, Any]
    media: dict[str, Any] | None
    blocker: str | None
    tabs: list[dict[str, Any]] = field(default_factory=list)
    total_candidates: int = 0
    screenshot: bytes | None = None

    def element(self, index: int) -> ElementInfo | None:
        return next((e for e in self.elements if e.index == index), None)

    def to_prompt(self) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if len(self.tabs) > 1:
            lines.append("Open tabs: " + "; ".join(f"[{t['index']}]{'*' if t['active'] else ''} {t['title'][:40]}" for t in self.tabs))
        s = self.scroll
        lines.append(f"Scroll: {s.get('y', 0)}px of {s.get('height', 0)}px (viewport {s.get('viewport', 0)}px)")
        if self.media:
            m = self.media
            state = "ended" if m.get("ended") else ("paused" if m.get("paused") else "PLAYING")
            lines.append(f"Media: video {state} at {m.get('current_time')}s / {m.get('duration')}s{' (muted)' if m.get('muted') else ''}")
        if self.blocker:
            lines.append(f"BLOCKER DETECTED: {self.blocker}")
        shown = len(self.elements)
        lines.append(f"\nInteractive elements ({shown} shown of {self.total_candidates}):")
        lines.extend(e.to_line() for e in self.elements)
        truncated = " (truncated)" if self.text_length > len(self.text) else ""
        lines.append(f"\nVisible page text{truncated}:\n{self.text}")
        return "\n".join(lines)


async def observe(page: Page, *, max_elements: int = 160, max_text: int = 7000) -> Observation:
    raw = await page.evaluate(OBSERVE_JS, {"maxElements": max_elements, "maxText": max_text})
    elements = [ElementInfo(**e) for e in raw["elements"]]
    return Observation(
        url=raw["url"],
        title=raw["title"] or "",
        elements=elements,
        text=raw["text"],
        text_length=raw["text_length"],
        corpus=raw["corpus"],
        scroll=raw["scroll"],
        media=raw["media"],
        blocker=raw["blocker"],
        total_candidates=raw["total_candidates"],
    )


async def element_context(page: Page, index: int) -> dict[str, Any] | None:
    return await page.evaluate(ELEMENT_CONTEXT_JS, index)

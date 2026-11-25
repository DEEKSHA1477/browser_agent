"""Builds comparison tables from extracted records and guards against fabrication.

The LLM transcribes records it sees on the page. Before anything is shown to
the user, every record is checked against the page's own text (visible text +
accessibility labels). Records whose name or numeric values (prices, ratings,
times...) do not appear on the page are dropped as unverified.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from models.task_models import ResultCollection, ResultItem

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9ऀ-ॿ]+", re.I)

PRICE_COL = re.compile(r"price|cost|fare|amount|mrp|rate\b|per night|total", re.I)
RATING_COL = re.compile(r"rating|stars|score|review score", re.I)
DURATION_COL = re.compile(r"duration|journey|travel time|time taken", re.I)
NAME_COL = re.compile(r"^(name|title|product|airline|hotel|job|role|position|company|video|paper|item|flight)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace(" ", " ").replace(" ", " "))


def _digits_corpus(corpus: str) -> str:
    return re.sub(r"(?<=\d),(?=\d)", "", corpus)


def _numbers(value: str) -> list[str]:
    out = []
    for n in _NUM.findall(value or ""):
        n = n.replace(",", "")
        if n.endswith(".0"):
            n = n[:-2]
        if len(n.replace(".", "")) >= 2 or "." in n:  # ignore lone digits (too ambiguous to verify)
            out.append(n)
    return out


def _text_present(value: str, corpus: str) -> bool:
    words = [w for w in _WORD.findall(value.lower()) if len(w) > 1]
    if not words:
        return True
    hits = sum(1 for w in words if w in corpus)
    return hits / len(words) >= 0.6


def verify_item(fields: dict[str, str], corpus_norm: str, corpus_digits: str, name_key: str | None) -> bool:
    if name_key and fields.get(name_key):
        if not _text_present(fields[name_key], corpus_norm):
            return False
    for key, value in fields.items():
        for number in _numbers(value):
            if number not in corpus_digits:
                return False
    return True


def parse_number(value: str | None) -> float | None:
    if not value:
        return None
    m = _NUM.search(value.replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_duration_minutes(value: str | None) -> float | None:
    if not value:
        return None
    v = value.lower()
    h = re.search(r"(\d+)\s*(h|hr|hrs|hour)", v)
    m = re.search(r"(\d+)\s*(m|min|mins|minute)", v)
    if h or m:
        return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    return parse_number(value)


def build_collection(
    params: dict[str, Any],
    *,
    corpus: str,
    source_url: str | None,
    source_title: str | None,
) -> ResultCollection:
    title = str(params.get("title") or "Results")[:120]
    raw_items = params.get("items") or []
    columns: list[str] = [str(c)[:40] for c in (params.get("columns") or [])][:10]
    if not columns:
        for item in raw_items:
            for key in (item.get("fields") or {}):
                if key not in columns:
                    columns.append(str(key)[:40])
        columns = columns[:10]

    corpus_norm = _norm(corpus)
    corpus_digits = _digits_corpus(corpus_norm)
    name_key = next((c for c in columns if NAME_COL.search(c)), columns[0] if columns else None)

    items: list[ResultItem] = []
    dropped = 0
    for raw in raw_items[:60]:
        fields = {str(k)[:40]: str(v)[:200] for k, v in (raw.get("fields") or {}).items() if v not in (None, "")}
        if not fields:
            continue
        ok = verify_item(fields, corpus_norm, corpus_digits, name_key)
        if not ok:
            dropped += 1
            continue
        url = raw.get("url")
        items.append(ResultItem(id=uuid.uuid4().hex[:8], fields=fields, url=str(url)[:1500] if url else None))

    recommendation_id = None
    rec_index = params.get("recommended_index")
    if isinstance(rec_index, int) and 0 <= rec_index < len(raw_items):
        # map the raw index to a verified item, if it survived verification
        rec_fields = {str(k)[:40]: str(v)[:200] for k, v in (raw_items[rec_index].get("fields") or {}).items() if v not in (None, "")}
        match = next((i for i in items if i.fields == rec_fields), None)
        if match:
            recommendation_id = match.id
            match.tags.append("recommended")

    tag_extremes(items, columns)
    return ResultCollection(
        id=uuid.uuid4().hex[:8],
        title=title,
        source_url=source_url,
        source_title=source_title,
        columns=columns,
        items=items,
        recommendation_id=recommendation_id,
        recommendation_reason=(str(params.get("recommendation_reason"))[:400] if recommendation_id and params.get("recommendation_reason") else None),
        dropped_unverified=dropped,
        extracted_at=time.time(),
    )


def tag_extremes(items: list[ResultItem], columns: list[str]) -> None:
    def tag(col_re: re.Pattern[str], label: str, parser, pick_min: bool) -> None:
        col = next((c for c in columns if col_re.search(c)), None)
        if not col:
            return
        scored = [(parser(i.fields.get(col)), i) for i in items]
        scored = [(v, i) for v, i in scored if v is not None]
        if len(scored) < 2:
            return
        best = min(scored, key=lambda x: x[0]) if pick_min else max(scored, key=lambda x: x[0])
        for value, item in scored:
            if value == best[0] and label not in item.tags:
                item.tags.append(label)

    tag(PRICE_COL, "cheapest", parse_number, True)
    tag(RATING_COL, "top_rated", parse_number, False)
    if any(PRICE_COL.search(c) for c in columns):  # "fastest" only makes sense for priced trips, not video lengths
        tag(DURATION_COL, "fastest", parse_duration_minutes, True)


_AMOUNT = re.compile(r"(?:₹|rs\.?\s?|inr\s?)\s?(\d[\d,]*(?:\.\d+)?)", re.I)


def ungrounded_amounts(text: str, results: list[ResultCollection], corpus: str) -> list[str]:
    """Currency amounts in ``text`` that appear neither in the verified results nor on the current page.

    Used to stop a final summary from citing prices the agent never actually saw.
    """
    known = _digits_corpus(_norm(corpus))
    for coll in results:
        for item in coll.items:
            known += " " + _digits_corpus(_norm(" ".join(item.fields.values())))
    missing = []
    for m in _AMOUNT.finditer(text or ""):
        number = m.group(1).replace(",", "")
        if number.endswith(".00"):
            number = number[:-3]
        if number and number not in known and m.group(0).strip() not in missing:
            missing.append(m.group(0).strip())
    return missing


def results_digest(results: list[ResultCollection], max_items: int = 12, max_chars: int = 3500) -> str:
    """Compact text of the verified records, so the model reports from data instead of memory."""
    if not results:
        return "none"
    lines = []
    for coll in results:
        lines.append(f"'{coll.title}' ({coll.source_url or 'unknown source'}), {len(coll.items)} verified records:")
        for item in coll.items[:max_items]:
            tags = f" [{', '.join(item.tags)}]" if item.tags else ""
            lines.append("  - " + " | ".join(f"{k}: {v}" for k, v in item.fields.items()) + tags)
        if len(coll.items) > max_items:
            lines.append(f"  ... and {len(coll.items) - max_items} more")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n  ... (truncated)"


def merge_into(existing: list[ResultCollection], new: ResultCollection) -> list[ResultCollection]:
    """Extracting the same table again (e.g. after scrolling) appends, deduplicated."""
    for coll in existing:
        if coll.title.lower() == new.title.lower() and coll.columns == new.columns:
            seen = {tuple(sorted(i.fields.items())) for i in coll.items}
            for item in new.items:
                key = tuple(sorted(item.fields.items()))
                if key not in seen:
                    coll.items.append(item)
                    seen.add(key)
            coll.dropped_unverified += new.dropped_unverified
            if new.recommendation_id:
                for i in coll.items:
                    if "recommended" in i.tags and i.id != new.recommendation_id:
                        i.tags.remove("recommended")
                coll.recommendation_id = new.recommendation_id
                coll.recommendation_reason = new.recommendation_reason
            for i in coll.items:
                i.tags = [t for t in i.tags if t == "recommended"]
            tag_extremes(coll.items, coll.columns)
            return existing
    return existing + [new]

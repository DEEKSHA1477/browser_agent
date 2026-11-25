"""Deterministic sensitive-action classifier.

This is the backend guardrail that decides whether an action proposed by the
LLM needs human approval. It looks at *what the action would actually do* in
the browser - the target element's accessible name, its form context, the
destination URL, the values being typed - not at what the LLM claims.

The LLM may additionally flag an action as sensitive. The final decision is
the stricter of the two: the LLM can add an approval requirement but can
never remove one raised by this policy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from browser.page_extractor import ElementInfo
from models.approval_models import ApprovalKind

# Ordered: the first matching category wins (most severe first).
_RULES: list[tuple[ApprovalKind, re.Pattern[str]]] = [
    (ApprovalKind.PAYMENT, re.compile(
        r"\b(pay(\s+now|\s+securely|\s+₹|\s+rs\.?|\s+inr|\s+\$|\s+with)?|make\s+(a\s+)?payment|proceed\s+to\s+pay(ment)?|"
        r"complete\s+payment|confirm\s+(and\s+|&\s+)?pay|place\s+order\s+(and|&)\s+pay|authori[sz]e\s+payment|"
        r"add\s+money|transfer\s+(money|funds|now))\b", re.I)),
    (ApprovalKind.ORDER, re.compile(
        r"\b(place\s+(your\s+)?order|buy\s+now|buy\s+it\s+now|purchase|complete\s+(your\s+)?(order|purchase)|"
        r"confirm\s+(your\s+)?(order|purchase)|submit\s+order|one[-\s]click\s+buy)\b", re.I)),
    (ApprovalKind.FLIGHT_BOOKING, re.compile(
        r"\b(book\s+now|book\s+(this|flight|hotel|room|ticket|tickets|seat|table|appointment|slot)|continue\s+to\s+book|"
        r"confirm\s+(booking|reservation|appointment|slot)|complete\s+(booking|reservation)|reserve(\s+now)?|"
        r"schedule\s+(appointment|now)|book\s+with)\b", re.I)),
    (ApprovalKind.JOB_APPLICATION, re.compile(
        r"\b(submit\s+(your\s+)?application|apply\s+now|easy\s+apply|send\s+application|apply\s+for\s+(this\s+)?(job|role|position)|^apply$)",
        re.I)),
    (ApprovalKind.MESSAGE, re.compile(
        r"(^send$|\bsend\s+(email|mail|message|now|invite|invitation|reply|request)\b|^post$|\bpost\s+(comment|reply|now)\b|"
        r"^publish$|\bpublish\s+now\b|^tweet$|^reply$|\bsubmit\s+(comment|review|feedback)\b|^share$)", re.I)),
    (ApprovalKind.CANCELLATION, re.compile(
        r"\bcancel\s+(my\s+|this\s+)?(booking|reservation|order|subscription|ticket|appointment|membership|plan|flight)\b", re.I)),
    (ApprovalKind.DELETION, re.compile(
        r"(\bdelete\b|\bremove\s+(account|permanently|forever)\b|\bpermanently\b|\berase\b|\bclear\s+(all\s+)?(history|data)\b|"
        r"\bempty\s+(trash|bin)\b)", re.I)),
    (ApprovalKind.ACCOUNT_CHANGE, re.compile(
        r"\b((save|update)\s+(changes|settings|profile|password|account|preferences)|change\s+(password|email|phone)|"
        r"deactivate|close\s+(my\s+)?account|unsubscribe|enable\s+two|disable\s+two|revoke|log\s*out\s+of\s+all)\b", re.I)),
]

# Words that make a form submission count as "submitting" (as opposed to searching).
_SUBMIT_WORDS = re.compile(
    r"\b(submit|continue|next|sign\s*up|register|create\s+(an\s+)?account|join|save|confirm|done|finish|send|apply|proceed|get\s+started|subscribe)\b",
    re.I,
)
_PERSONAL_FIELD = re.compile(
    r"(password|passwd|e-?mail|phone|mobile|tel\b|name|address|street|city|zip|postal|pin\s*code|dob|birth|aadhaar|pan\b|"
    r"passport|ssn|card|cvv|cvc|expiry|upi|account|otp)", re.I,
)
_SECRET_FIELD = re.compile(r"(password|passwd|cc-number|card.?number|cardnumber|cvv|cvc|csc|security.?code|otp|one.?time|pin\b|upi.?pin)", re.I)
_CARD_FIELD = re.compile(r"(cc-|card|cvv|cvc|csc|expiry|exp-)", re.I)
_SENSITIVE_URL = re.compile(
    r"(/pay(ment)?s?(/|$|\?)|/checkout/(pay|confirm|place)|/delete|/remove|/cancel|/unsubscribe|/deactivate|/close-account|"
    r"[?&](action|op)=(delete|remove|cancel|pay|purchase))", re.I,
)
_EXECUTABLE = re.compile(r"\.(exe|msi|bat|cmd|ps1|vbs|jar|scr|apk|dmg|pkg|sh|hta)(\?|#|$)", re.I)
_CURRENCY = re.compile(r"(₹|rs\.?|inr|\$|usd|€|eur|£|gbp)\s?[\d,]+(\.\d+)?|[\d,]+(\.\d+)?\s?(₹|inr|usd|eur|gbp|indian rupees)", re.I)

AGENT_CATEGORY_MAP = {k.value: k for k in ApprovalKind}


@dataclass
class SafetyAssessment:
    requires_approval: bool
    kind: ApprovalKind | None = None
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    secret_values: bool = False  # typed value must be masked in UI / logs


def _classify_text(text: str) -> ApprovalKind | None:
    text = (text or "").strip()
    if not text:
        return None
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return None


def extract_amounts(text: str) -> list[str]:
    seen: list[str] = []
    for m in _CURRENCY.finditer(text or ""):
        val = re.sub(r"\s+", " ", m.group(0).strip())
        if val not in seen:
            seen.append(val)
    return seen[:6]


def _is_search_form(ctx: dict[str, Any] | None, element: ElementInfo | None) -> bool:
    if not ctx:
        return False
    if (ctx.get("form_role") or "").lower() == "search":
        return True
    fields = ctx.get("form_fields") or []
    text_fields = [f for f in fields if not re.match(r"^(submit|button|reset|checkbox|radio|image)\b", f["descriptor"])]
    if len(text_fields) <= 2 and any(re.search(r"\b(search|q|query|keyword|search_query|k)\b", f["descriptor"]) for f in text_fields):
        return True
    if element is not None and (element.type == "search" or element.role == "searchbox"):
        return True
    return False


def _form_has_personal_data(ctx: dict[str, Any] | None) -> list[str]:
    if not ctx:
        return []
    hits = []
    for f in ctx.get("form_fields") or []:
        if f.get("filled") and _PERSONAL_FIELD.search(f["descriptor"]):
            hits.append(f["descriptor"].split()[0])
    return hits


def assess(
    action: dict[str, Any],
    *,
    element: ElementInfo | None,
    context: dict[str, Any] | None,
    page_url: str,
    agent_flag: dict[str, Any] | None,
) -> SafetyAssessment:
    name = action.get("name")
    params = action.get("params") or {}
    result = SafetyAssessment(requires_approval=False)

    def flag(kind: ApprovalKind, reason: str) -> None:
        if not result.requires_approval:
            result.kind = kind
        result.requires_approval = True
        result.reasons.append(reason)
        if "policy" not in result.sources:
            result.sources.append("policy")

    label = " ".join(filter(None, [element.name if element else "", (element.value or "") if element and element.type in ("submit", "button") else ""]))

    if name in ("click", "download") and element is not None:
        kind = _classify_text(label)
        if kind:
            kind_text = kind.value.replace("_", " ")
            article = "an" if kind_text[0] in "aeiou" else "a"
            flag(kind, f"The target '{label[:80]}' looks like {article} {kind_text} action.")
        href = element.href or (context or {}).get("href") or ""
        if href and _SENSITIVE_URL.search(href):
            flag(ApprovalKind.OTHER, f"The link leads to a sensitive URL ({href[:100]}).")
        if name == "download" and href and _EXECUTABLE.search(href):
            flag(ApprovalKind.DOWNLOAD, "This downloads an executable file.")
        is_submitter = element.type in ("submit", "image") or (
            element.tag == "button" and (context or {}).get("in_form") and element.type in ("", "submit")
        )
        if (is_submitter or _SUBMIT_WORDS.search(label)) and (context or {}).get("in_form") and not _is_search_form(context, element):
            personal = _form_has_personal_data(context)
            if personal:
                flag(ApprovalKind.SENSITIVE_FORM, f"Submits a form containing personal data ({', '.join(sorted(set(personal)))}).")

    if name == "type" and element is not None:
        descriptor = (context or {}).get("descriptor", "") + " " + element.type + " " + element.name
        if element.type == "password" or _SECRET_FIELD.search(descriptor):
            kind = ApprovalKind.PAYMENT if _CARD_FIELD.search(descriptor) else ApprovalKind.SENSITIVE_FORM
            flag(kind, "Enters a secret value (password / card / OTP) into the page.")
            result.secret_values = True
        if params.get("submit") and (context or {}).get("in_form") and not _is_search_form(context, element):
            personal = _form_has_personal_data(context)
            if personal or element.type == "password":
                flag(ApprovalKind.SENSITIVE_FORM, "Pressing Enter submits a form containing personal data.")

    if name == "press":
        key = str(params.get("key", ""))
        if key.endswith("Enter") and ("+" in key):
            flag(ApprovalKind.MESSAGE, f"The shortcut {key} commonly sends/submits content.")
        elif key == "Enter" and context and context.get("in_form") and not _is_search_form(context, element):
            if _form_has_personal_data(context):
                flag(ApprovalKind.SENSITIVE_FORM, "Pressing Enter submits a form containing personal data.")

    if name == "navigate":
        url = str(params.get("url", ""))
        if _SENSITIVE_URL.search(urlparse(url if "://" in url else "https://" + url).path + "?" + (urlparse(url).query or "")):
            flag(ApprovalKind.OTHER, f"Navigating directly to a sensitive URL ({url[:100]}).")

    # The agent's own judgement can only ADD an approval requirement.
    if agent_flag and agent_flag.get("is_sensitive"):
        kind = AGENT_CATEGORY_MAP.get(str(agent_flag.get("category") or "").lower(), ApprovalKind.OTHER)
        if not result.requires_approval:
            result.kind = kind
        result.requires_approval = True
        result.reasons.append(f"Agent: {agent_flag.get('reason') or 'flagged as sensitive'}")
        result.sources.append("agent")

    return result

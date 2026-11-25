"""Capability catalog for the task launcher.

The dashboard opens on a gallery of these capabilities. The user picks one,
fills in the details it needs (which song, from/to/date, product and budget,
kind of job...) and only then does the agent start. That way the agent never
has to guess the essentials.

These are NOT per-website workflows: each capability only turns the form's
answers into a precise natural-language instruction plus structured
``details``. The same generic agent loop (observe -> decide -> gate -> act)
executes every one of them, and free-text "custom" tasks still work.

Add a capability by appending a ``Capability`` to ``CATALOG``; the API and the
frontend pick it up automatically.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable

from agent.planner import InvalidInstruction


@dataclass
class Field:
    key: str
    label: str
    type: str = "text"  # text | textarea | date | number | email | url | select
    required: bool = False
    placeholder: str = ""
    options: list[str] = field(default_factory=list)
    default: str = ""
    help: str = ""
    future_date: bool = False  # date fields: must be today or later
    max_length: int = 200


@dataclass
class Capability:
    id: str
    title: str
    icon: str
    description: str
    fields: list[Field]
    compose: Callable[[dict[str, str]], str]
    sensitive_note: str = ""  # what will require approval, shown on the form

    def public(self) -> dict:
        data = asdict(self)
        data.pop("compose")
        for f in data["fields"]:
            f.pop("max_length", None)
        return data


def _opt(d: dict[str, str], key: str, text: str) -> str:
    """Render ``text`` (with {value}) only when the optional field is filled."""
    value = d.get(key, "")
    return text.format(value=value) if value else ""


def _music(d: dict[str, str]) -> str:
    pick = d.get("pick", "Let me choose from the results")
    tail = ("extract the top results (title, channel, duration, views) with their links, then ask me which one to play "
            "and play the one I choose" if pick.startswith("Let me") else
            "play the most relevant result and confirm it is playing")
    return f"Open YouTube, search for \"{d['query']}\" and {tail}."


def _flights(d: dict[str, str]) -> str:
    trip = f"round trip returning {d['return_date']}" if d.get("return_date") else "one way"
    return (f"Search flights from {d['origin']} to {d['destination']} on {d['date']} ({trip}) for "
            f"{d.get('passengers') or '1'} passenger(s) in {d.get('cabin') or 'Economy'}. Extract the available options "
            f"(airline, departure, arrival, duration, stops, price) into a comparison and recommend the best one, "
            f"preferring {(d.get('preference') or 'Cheapest').lower()}. Do not book anything.")


def _hotels(d: dict[str, str]) -> str:
    return (f"Search hotels in {d['city']} from {d['check_in']} to {d['check_out']} for {d.get('guests') or '2'} guest(s)"
            + _opt(d, "budget", " with a budget of up to ₹{value} per night")
            + _opt(d, "area", ", preferably near {value}")
            + ". Extract name, price per night, rating and location into a comparison and recommend one. Do not book.")


def _shopping(d: dict[str, str]) -> str:
    sites = d.get("site") or "Amazon.in and Flipkart"
    return (f"Search for {d['product']}" + _opt(d, "budget", " under ₹{value}") + f" on {sites}"
            + _opt(d, "must_have", ", must have: {value}")
            + ". Collect product name, price, rating and key specifications for the top options, compare them and "
              "recommend the best value. Do not add to cart or buy anything.")


def _jobs(d: dict[str, str]) -> str:
    return (f"Search for {d['role']} jobs" + _opt(d, "location", " in {value}")
            + f" for {d.get('experience') or 'Fresher'} experience"
            + _opt(d, "job_type", " ({value})") + _opt(d, "site", " on {value}")
            + ". List job title, company, location, experience and salary (if shown) in a comparison. Do not apply.")


def _research(d: dict[str, str]) -> str:
    return (f"Find the latest research papers about {d['topic']} on {d.get('source') or 'arXiv'}"
            + _opt(d, "period", " published in the {value}")
            + ". Extract title, authors, date and link for the top results.")


def _search(d: dict[str, str]) -> str:
    return f"Search the web for: {d['query']}. Open the most relevant sources and give me a concise answer with sources."


def _email(d: dict[str, str]) -> str:
    return (f"Open Gmail and draft an email to {d['to']} with subject \"{d['subject']}\" and this message: {d['body']}. "
            "Leave it as a draft and ask for my approval before sending.")


def _form(d: dict[str, str]) -> str:
    submit = d.get("submit", "Fill only")
    return (f"Open {d['url']} and fill out the form using only these details: {d['data']}. "
            + ("After filling, ask for my approval before submitting." if submit.startswith("Fill and") else
               "Fill the fields but do not submit the form."))


def _appointment(d: dict[str, str]) -> str:
    return (f"Find available {d['service']} appointments in {d['city']}" + _opt(d, "date", " on or after {value}")
            + _opt(d, "provider", " with {value}")
            + ". List provider, available slots and fees. Do not confirm any booking.")


def _download(d: dict[str, str]) -> str:
    return (f"Find and download {d['document']}" + _opt(d, "site", " from {value}")
            + ". Prefer official sources and PDF files.")


CATALOG: list[Capability] = [
    Capability("music", "Play music or video", "🎵", "Search YouTube and play a song, artist or video.", [
        Field("query", "Song, artist or video", required=True, placeholder="e.g. Tum Hi Ho by Arijit Singh"),
        Field("pick", "When results appear", "select", options=["Let me choose from the results", "Play the best match"],
              default="Let me choose from the results"),
    ], _music),
    Capability("flights", "Search flights", "✈️", "Compare flight options and prices.", [
        Field("origin", "From", required=True, placeholder="e.g. Bengaluru"),
        Field("destination", "To", required=True, placeholder="e.g. Delhi"),
        Field("date", "Departure date", "date", required=True, future_date=True),
        Field("return_date", "Return date (optional)", "date", future_date=True, help="Leave empty for one way"),
        Field("passengers", "Passengers", "number", default="1"),
        Field("cabin", "Class", "select", options=["Economy", "Premium economy", "Business", "First"], default="Economy"),
        Field("preference", "Prefer", "select", options=["Cheapest", "Fastest", "Non-stop", "Morning departure"],
              default="Cheapest"),
    ], _flights, "Booking or payment always needs your approval."),
    Capability("hotels", "Search hotels", "🏨", "Find and compare hotels for your dates.", [
        Field("city", "City", required=True, placeholder="e.g. Goa"),
        Field("check_in", "Check-in", "date", required=True, future_date=True),
        Field("check_out", "Check-out", "date", required=True, future_date=True),
        Field("guests", "Guests", "number", default="2"),
        Field("budget", "Max price per night (₹, optional)", "number"),
        Field("area", "Preferred area (optional)", placeholder="e.g. Calangute beach"),
    ], _hotels, "Reservations always need your approval."),
    Capability("shopping", "Shop & compare prices", "🛒", "Find products and compare price, rating and specs.", [
        Field("product", "What do you want to buy?", required=True, placeholder="e.g. laptop, wireless headphones"),
        Field("budget", "Maximum budget (₹, optional)", "number", placeholder="e.g. 60000"),
        Field("site", "Where to look", "select", options=["Amazon.in and Flipkart", "Amazon.in", "Flipkart"],
              default="Amazon.in and Flipkart"),
        Field("must_have", "Must-have features (optional)", placeholder="e.g. 16GB RAM, SSD, noise cancellation"),
    ], _shopping, "Adding to cart is allowed; placing an order or paying needs your approval."),
    Capability("jobs", "Find jobs", "💼", "Search job listings that match your profile.", [
        Field("role", "Role / job title", required=True, placeholder="e.g. Python developer"),
        Field("location", "Location (optional)", placeholder="e.g. Bengaluru, or Remote"),
        Field("experience", "Experience", "select", options=["Fresher", "1-3 years", "3-5 years", "5+ years"],
              default="Fresher"),
        Field("job_type", "Job type (optional)", "select", options=["", "Full-time", "Internship", "Remote", "Part-time"]),
        Field("site", "Job site (optional)", "select", options=["", "Naukri", "Indeed", "LinkedIn"]),
    ], _jobs, "Submitting an application needs your approval."),
    Capability("research", "Research papers", "📚", "Find the latest papers on a topic.", [
        Field("topic", "Topic", required=True, placeholder="e.g. AI agents, diffusion models"),
        Field("source", "Source", "select", options=["arXiv", "Google Scholar"], default="arXiv"),
        Field("period", "Published in (optional)", "select", options=["", "last month", "last 6 months", "last year"]),
    ], _research),
    Capability("search", "Web search", "🔎", "Research a question across websites.", [
        Field("query", "What do you want to know?", "textarea", required=True,
              placeholder="e.g. What are the visa requirements for Indians visiting Japan?", max_length=400),
    ], _search),
    Capability("email", "Draft an email", "✉️", "Write an email in Gmail (you must be signed in).", [
        Field("to", "To", "email", required=True, placeholder="name@example.com"),
        Field("subject", "Subject", required=True),
        Field("body", "Message", "textarea", required=True, max_length=600),
    ], _email, "Sending always needs your approval."),
    Capability("form", "Fill a form", "📝", "Fill a registration or contact form on a website.", [
        Field("url", "Website / form URL", "url", required=True, placeholder="https://..."),
        Field("data", "Details to enter", "textarea", required=True,
              placeholder="e.g. Name: Asha Rao, Email: asha@example.com, Phone: 98xxxxxx10", max_length=600),
        Field("submit", "Submit?", "select", options=["Fill only, don't submit", "Fill and submit (after my approval)"],
              default="Fill only, don't submit"),
    ], _form, "Submitting personal data needs your approval. Passwords/OTPs are entered only with your approval."),
    Capability("appointment", "Find appointments", "📅", "Look for available appointment slots.", [
        Field("service", "Service", required=True, placeholder="e.g. dermatologist, car service, haircut"),
        Field("city", "City", required=True, placeholder="e.g. Bengaluru"),
        Field("date", "Preferred date (optional)", "date", future_date=True),
        Field("provider", "Specific provider (optional)", placeholder="e.g. Apollo Hospitals"),
    ], _appointment, "Confirming a booking needs your approval."),
    Capability("download", "Download a document", "📄", "Find and download a document or file.", [
        Field("document", "Which document?", required=True, placeholder="e.g. RBI annual report 2025 PDF"),
        Field("site", "Website (optional)", placeholder="e.g. rbi.org.in"),
    ], _download, "Executable files (.exe, .msi...) need your approval."),
]

BY_ID = {c.id: c for c in CATALOG}

# What "select this result" means for each kind of task. Irreversible steps stay behind the approval gate.
_SELECT_VERB = {
    "music": "Open it and play it, skip or wait out any ad, and confirm it is playing.",
    "flights": "Open it and show me the fare options and booking sites for this flight. Do not book or pay.",
    "hotels": "Open it and show me room options, prices and cancellation policy. Do not book.",
    "shopping": "Open the product page and report the current price, rating, delivery date, seller and key specifications. "
                "Do not add to cart or buy.",
    "jobs": "Open the job listing and summarise the role, requirements, salary and how to apply. Do not apply.",
    "research": "Open the paper's page and summarise its abstract; download the PDF if one is available.",
    "appointment": "Open it and show me the available slots and fees. Do not confirm a booking.",
}


def selection_instruction(capability: str | None, label: str, url: str | None) -> str:
    target = f'"{label[:150]}"' + (f" ({url})" if url else "")
    verb = _SELECT_VERB.get(capability or "", "Open it and summarise what is on the page.")
    return f"I selected {target}. {verb}"

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_value(f: Field, raw: object, today: date) -> str:
    value = _CTRL.sub("", str(raw if raw is not None else "")).strip()
    if not value:
        return ""
    if len(value) > f.max_length:
        raise InvalidInstruction(f"'{f.label}' is too long (max {f.max_length} characters).")
    if f.type == "date":
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidInstruction(f"'{f.label}' must be a date (YYYY-MM-DD).") from exc
        if f.future_date and parsed < today:
            raise InvalidInstruction(f"'{f.label}' cannot be in the past.")
    elif f.type == "number":
        if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", value.replace(",", "")):
            raise InvalidInstruction(f"'{f.label}' must be a number.")
        value = value.replace(",", "")
    elif f.type == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise InvalidInstruction(f"'{f.label}' must be an email address.")
    elif f.type == "url":
        if not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}", value, re.I):
            raise InvalidInstruction(f"'{f.label}' must be a website address.")
    elif f.type == "select" and f.options and value not in f.options:
        raise InvalidInstruction(f"'{f.label}' must be one of: {', '.join(o for o in f.options if o)}.")
    return value


def build_task(capability_id: str, details: dict[str, object], *, today: date | None = None) -> tuple[str, dict[str, str]]:
    """Validate the form answers and return (instruction, clean details)."""
    cap = BY_ID.get(capability_id)
    if cap is None:
        raise InvalidInstruction(f"Unknown capability '{capability_id}'.")
    today = today or date.today()
    clean: dict[str, str] = {}
    missing = []
    for f in cap.fields:
        value = _clean_value(f, details.get(f.key, f.default), today)
        if not value and f.default:
            value = f.default
        if f.required and not value:
            missing.append(f.label)
        if value:
            clean[f.key] = value
    if missing:
        raise InvalidInstruction("Please fill in: " + ", ".join(missing) + ".")
    if cap.id == "hotels" and clean["check_out"] <= clean["check_in"]:
        raise InvalidInstruction("Check-out must be after check-in.")
    if cap.id == "flights" and clean.get("return_date") and clean["return_date"] < clean["date"]:
        raise InvalidInstruction("Return date must be on or after the departure date.")
    labelled = {f.label.split(" (")[0]: clean[f.key] for f in cap.fields if f.key in clean}
    return cap.compose(clean), labelled

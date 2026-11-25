"""Task launcher: capability catalog, form validation and how details reach the agent."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from agent.capabilities import CATALOG, build_task
from agent.planner import InvalidInstruction
from agent.prompts import planner_prompt

TODAY = date(2026, 11, 25)


def test_catalog_covers_required_activities():
    ids = {c.id for c in CATALOG}
    assert {"music", "flights", "hotels", "shopping", "jobs", "research", "search", "email", "form",
            "appointment", "download"} <= ids
    for cap in CATALOG:
        assert any(f.required for f in cap.fields), f"{cap.id} must ask for at least one essential detail"


def test_music_requires_the_song():
    with pytest.raises(InvalidInstruction, match="Song, artist or video"):
        build_task("music", {}, today=TODAY)
    instruction, details = build_task("music", {"query": "Tum Hi Ho"}, today=TODAY)
    assert '"Tum Hi Ho"' in instruction and "play" in instruction
    choose, _ = build_task("music", {"query": "Tum Hi Ho", "pick": "Let me choose from the results"}, today=TODAY)
    assert "ask me which one" in choose


def test_flights_need_route_and_date_and_reject_past_dates():
    with pytest.raises(InvalidInstruction, match="From, To, Departure date"):
        build_task("flights", {}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="cannot be in the past"):
        build_task("flights", {"origin": "BLR", "destination": "DEL", "date": "2026-11-01"}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="Return date"):
        build_task("flights", {"origin": "BLR", "destination": "DEL", "date": "2026-12-05", "return_date": "2026-12-01"},
                   today=TODAY)
    instruction, details = build_task("flights", {"origin": "Bengaluru", "destination": "Delhi", "date": "2026-12-05"},
                                      today=TODAY)
    assert "Bengaluru" in instruction and "2026-12-05" in instruction and "Do not book" in instruction
    assert details["Departure date"] == "2026-12-05" and details["Class"] == "Economy"  # defaults applied


def test_invalid_values_rejected():
    with pytest.raises(InvalidInstruction, match="must be a number"):
        build_task("shopping", {"product": "laptop", "budget": "sixty thousand"}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="must be one of"):
        build_task("shopping", {"product": "laptop", "site": "evil.example"}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="email address"):
        build_task("email", {"to": "not-an-email", "subject": "Hi", "body": "Hello"}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="Check-out"):
        build_task("hotels", {"city": "Goa", "check_in": "2026-12-10", "check_out": "2026-12-09"}, today=TODAY)
    with pytest.raises(InvalidInstruction, match="Unknown capability"):
        build_task("hack", {}, today=TODAY)


def test_shopping_and_jobs_never_buy_or_apply():
    shop, _ = build_task("shopping", {"product": "laptop", "budget": "60,000"}, today=TODAY)
    assert "under ₹60000" in shop and "Do not add to cart or buy" in shop
    jobs, _ = build_task("jobs", {"role": "Python developer", "location": "Bengaluru"}, today=TODAY)
    assert "Fresher" in jobs and "Do not apply" in jobs


def test_details_reach_the_planner_prompt():
    _, details = build_task("flights", {"origin": "Bengaluru", "destination": "Delhi", "date": "2026-12-05"}, today=TODAY)
    prompt = planner_prompt("Search flights", [], details)
    assert "- From: Bengaluru" in prompt and "never ask" in prompt


def test_api_catalog_and_validation():
    from main import app

    client = TestClient(app)
    caps = client.get("/api/capabilities").json()
    music = next(c for c in caps if c["id"] == "music")
    assert music["fields"][0]["required"] and "compose" not in music
    r = client.post("/api/tasks", json={"capability": "flights", "details": {"origin": "Bengaluru"}})
    assert r.status_code == 400 and "To" in r.json()["detail"]
    r = client.post("/api/tasks", json={})
    assert r.status_code == 400


async def test_planner_merges_form_details_into_user_data(monkeypatch):
    from agent import planner
    from agent.state import task_store
    from services.llm_service import llm_service

    class LLM:
        async def complete_json(self, system, prompt, image=None):
            # The model transcribes a detail wrongly; the form value must win.
            return {"goal": "g", "plan": ["p"], "user_data": {"From": "Mumbai", "note": "x"}}

    llm_service.override = LLM()
    try:
        record = task_store.create("Search flights", capability="flights", details={"From": "Bengaluru"})
        out = await planner.understand_node({"instruction": "Search flights", "details": record.details},
                                            {"configurable": {"thread_id": record.id}})
        assert out["understanding"]["user_data"] == {"From": "Bengaluru", "note": "x"}
    finally:
        llm_service.override = None

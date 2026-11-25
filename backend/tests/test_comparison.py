"""Anti-fabrication: extracted records must be backed by the page's own text."""
from services.comparison_service import build_collection, merge_into

# Text as captured from a real Google Flights results page (visible text + aria-labels).
CORPUS = """5:00 AM – 7:50 AM Air India 2 hr 50 min BLR–DEL Nonstop ₹9,865
5:30 AM – 8:30 AM Air India Express 3 hr BLR–DEL Nonstop ₹10,011
4:10 AM – 7:00 AM Akasa Air 2 hr 50 min BLR–DEL Nonstop ₹10,718
From 9865 Indian rupees. Nonstop flight with Air India. Total duration 2 hr 50 min."""


def params(items, rec=None):
    return {"title": "Flights BLR → DEL", "columns": ["Airline", "Price", "Departure", "Duration", "Stops"],
            "items": [{"fields": f} for f in items], "recommended_index": rec, "recommendation_reason": "cheapest nonstop"}


def test_real_records_kept_and_tagged():
    c = build_collection(params([
        {"Airline": "Air India", "Price": "₹9,865", "Departure": "5:00 AM", "Duration": "2 hr 50 min", "Stops": "Nonstop"},
        {"Airline": "Air India Express", "Price": "₹10,011", "Departure": "5:30 AM", "Duration": "3 hr", "Stops": "Nonstop"},
        {"Airline": "Akasa Air", "Price": "₹10,718", "Departure": "4:10 AM", "Duration": "2 hr 50 min", "Stops": "Nonstop"},
    ], rec=0), corpus=CORPUS, source_url="https://www.google.com/travel/flights", source_title="GF")
    assert len(c.items) == 3 and c.dropped_unverified == 0
    assert set(c.items[0].tags) >= {"cheapest", "recommended", "fastest"}
    assert "fastest" in c.items[2].tags and "cheapest" not in c.items[2].tags


def test_fabricated_records_dropped():
    c = build_collection(params([
        {"Airline": "Air India", "Price": "₹9,865", "Departure": "5:00 AM", "Duration": "2 hr 50 min", "Stops": "Nonstop"},
        {"Airline": "Air India", "Price": "₹4,999", "Departure": "5:00 AM", "Duration": "2 hr 50 min", "Stops": "Nonstop"},  # wrong price
        {"Airline": "Example Airways", "Price": "₹9,865", "Departure": "5:00 AM", "Duration": "2 hr", "Stops": "Nonstop"},  # invented airline
    ], rec=1), corpus=CORPUS, source_url=None, source_title=None)
    assert len(c.items) == 1 and c.dropped_unverified == 2
    assert c.recommendation_id is None  # recommended item was fabricated, so no recommendation


def test_merge_dedupes():
    p = params([{"Airline": "Air India", "Price": "₹9,865", "Departure": "5:00 AM", "Duration": "2 hr 50 min", "Stops": "Nonstop"}])
    a = build_collection(p, corpus=CORPUS, source_url=None, source_title=None)
    b = build_collection(p, corpus=CORPUS, source_url=None, source_title=None)
    merged = merge_into([a], b)
    assert len(merged) == 1 and len(merged[0].items) == 1

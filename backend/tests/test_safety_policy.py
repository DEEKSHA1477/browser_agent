"""The deterministic gate must flag sensitive actions no matter what the LLM says."""
from browser.page_extractor import ElementInfo
from browser.security import check_url
from models.approval_models import ApprovalKind
from services.safety_policy import assess, extract_amounts


def el(name, tag="button", type_="", role="", href=None, index=1):
    return ElementInfo(index=index, tag=tag, role=role, type=type_, name=name, href=href)


def click(element, ctx=None, agent=None, url="https://shop.example.com/cart"):
    return assess({"name": "click", "params": {"element": element.index}}, element=element, context=ctx, page_url=url, agent_flag=agent)


SEARCH_FORM = {"in_form": True, "form_role": "search", "form_fields": [{"descriptor": "text q search", "filled": True}]}
SIGNUP_FORM = {
    "in_form": True, "form_role": "",
    "form_fields": [
        {"descriptor": "text fullname  name full name", "filled": True},
        {"descriptor": "email email email", "filled": True},
        {"descriptor": "password pwd  new-password", "filled": True},
    ],
}


def test_payment_buttons():
    for label in ["Pay now", "Pay ₹5,500", "Proceed to Pay", "Make payment", "Confirm and pay"]:
        a = click(el(label))
        assert a.requires_approval and a.kind == ApprovalKind.PAYMENT, label


def test_orders_bookings_jobs_messages():
    cases = {
        "Place your order": ApprovalKind.ORDER,
        "Buy Now": ApprovalKind.ORDER,
        "Book now": ApprovalKind.FLIGHT_BOOKING,
        "Continue to book with Air India airline for 9865 Indian rupees": ApprovalKind.FLIGHT_BOOKING,
        "Submit application": ApprovalKind.JOB_APPLICATION,
        "Easy Apply": ApprovalKind.JOB_APPLICATION,
        "Send": ApprovalKind.MESSAGE,
        "Cancel booking": ApprovalKind.CANCELLATION,
        "Delete": ApprovalKind.DELETION,
        "Save changes": ApprovalKind.ACCOUNT_CHANGE,
    }
    for label, kind in cases.items():
        a = click(el(label))
        assert a.requires_approval and a.kind == kind, (label, a.kind)


def test_ordinary_browsing_is_not_gated():
    for label in ["Search", "Next page", "Add to cart", "Arijit Singh - Tum Hi Ho (Official Video)", "Sort by price", "Skip Ad",
                  "Select flight", "Cancel", "Show more"]:
        a = click(el(label), ctx=SEARCH_FORM if label == "Search" else None)
        assert not a.requires_approval, label


def test_search_form_submit_not_gated_but_signup_is():
    assert not click(el("Search", type_="submit"), ctx=SEARCH_FORM).requires_approval
    a = click(el("Create account", type_="submit"), ctx=SIGNUP_FORM)
    assert a.requires_approval and a.kind == ApprovalKind.SENSITIVE_FORM
    a = click(el("Continue", type_="submit"), ctx=SIGNUP_FORM)
    assert a.requires_approval


def test_typing_secrets_needs_approval_and_is_masked():
    pw = el("Password", tag="input", type_="password")
    a = assess({"name": "type", "params": {"element": 1, "text": "hunter2"}}, element=pw,
               context={"descriptor": "password pwd"}, page_url="https://x.com", agent_flag=None)
    assert a.requires_approval and a.secret_values
    card = el("Card number", tag="input", type_="text")
    a = assess({"name": "type", "params": {"element": 1, "text": "4111"}}, element=card,
               context={"descriptor": "text cardnumber cc-number"}, page_url="https://x.com", agent_flag=None)
    assert a.requires_approval and a.kind == ApprovalKind.PAYMENT


def test_enter_in_personal_form_is_gated():
    field = el("Email", tag="input", type_="email")
    a = assess({"name": "type", "params": {"element": 1, "text": "a@b.c", "submit": True}}, element=field,
               context={**SIGNUP_FORM, "descriptor": "email"}, page_url="https://x.com", agent_flag=None)
    assert a.requires_approval
    a = assess({"name": "press", "params": {"key": "Control+Enter"}}, element=None, context=None, page_url="https://mail.google.com", agent_flag=None)
    assert a.requires_approval and a.kind == ApprovalKind.MESSAGE


def test_llm_can_add_but_not_remove_approval():
    a = click(el("Pay now"), agent={"is_sensitive": False, "category": None, "reason": "harmless"})
    assert a.requires_approval and a.kind == ApprovalKind.PAYMENT
    a = click(el("Confirm"), agent={"is_sensitive": True, "category": "order", "reason": "places the order"})
    assert a.requires_approval and a.kind == ApprovalKind.ORDER and "agent" in a.sources


def test_sensitive_urls_and_executables():
    a = assess({"name": "navigate", "params": {"url": "https://site.com/account/delete?id=3"}}, element=None, context=None,
               page_url="", agent_flag=None)
    assert a.requires_approval
    d = el("Download installer", tag="a", href="https://example.com/setup.exe")
    a = assess({"name": "download", "params": {"element": 1}}, element=d, context=None, page_url="", agent_flag=None)
    assert a.requires_approval and a.kind == ApprovalKind.DOWNLOAD
    pdf = el("Download PDF", tag="a", href="https://arxiv.org/pdf/2401.00001")
    assert not assess({"name": "download", "params": {"element": 1}}, element=pdf, context=None, page_url="", agent_flag=None).requires_approval


def test_amount_extraction():
    assert extract_amounts("Total ₹5,500 incl. taxes. Was ₹6,000") == ["₹5,500", "₹6,000"]


def test_navigation_policy():
    assert check_url("https://www.youtube.com/watch?v=1")[0]
    assert not check_url("file:///C:/Windows/win.ini")[0]
    assert not check_url("javascript:alert(1)")[0]
    assert not check_url("http://127.0.0.1:8000/api/tasks")[0]
    assert not check_url("http://192.168.1.1/admin")[0]
    assert not check_url("http://localhost:5173")[0]
    assert check_url("about:blank")[0]
    assert not check_url("about:blank", internal_ok=False)[0]

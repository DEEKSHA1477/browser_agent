import pytest

from models.approval_models import ApprovalDetail, ApprovalKind, ApprovalStatus
from services.approval_service import ApprovalError, ApprovalService, fingerprint_of


def details(price="₹5,500"):
    return [ApprovalDetail(label="Target", value='button "Book now"'), ApprovalDetail(label="Amounts shown next to it", value=price)]


def make(svc, kind=ApprovalKind.FLIGHT_BOOKING, price="₹5,500", task="t1"):
    return svc.create(task_id=task, kind=kind, title="T", description="d", details=details(price), action_summary="Click Book now",
                      sources=["policy"])


def test_approve_then_require_and_consume():
    svc = ApprovalService()
    a = make(svc)
    svc.decide(a.id, decision="approve", fingerprint=a.fingerprint)
    svc.require_approved(a.id, task_id="t1", fingerprint=fingerprint_of(ApprovalKind.FLIGHT_BOOKING, details()))
    svc.consume(a.id)
    with pytest.raises(ApprovalError):  # single use
        svc.require_approved(a.id, task_id="t1", fingerprint=a.fingerprint)


def test_changed_price_invalidates_approval():
    svc = ApprovalService()
    a = make(svc)
    svc.decide(a.id, decision="approve", fingerprint=a.fingerprint)
    with pytest.raises(ApprovalError):
        svc.require_approved(a.id, task_id="t1", fingerprint=fingerprint_of(ApprovalKind.FLIGHT_BOOKING, details("₹6,100")))
    assert svc.get(a.id).status == ApprovalStatus.SUPERSEDED


def test_stale_card_cannot_approve():
    svc = ApprovalService()
    a = make(svc)
    with pytest.raises(ApprovalError):
        svc.decide(a.id, decision="approve", fingerprint="0" * 64)
    b = make(svc, price="₹6,100")  # new proposal supersedes the old pending one
    assert svc.get(a.id).status == ApprovalStatus.SUPERSEDED
    with pytest.raises(ApprovalError):
        svc.decide(a.id, decision="approve", fingerprint=a.fingerprint)
    svc.decide(b.id, decision="approve", fingerprint=b.fingerprint)


def test_other_task_cannot_use_approval():
    svc = ApprovalService()
    a = make(svc)
    svc.decide(a.id, decision="approve", fingerprint=a.fingerprint)
    with pytest.raises(ApprovalError):
        svc.require_approved(a.id, task_id="other", fingerprint=a.fingerprint)


def test_payment_needs_separate_confirmation():
    svc = ApprovalService()
    a = make(svc, kind=ApprovalKind.PAYMENT)
    assert a.requires_payment_confirmation
    with pytest.raises(ApprovalError):
        svc.decide(a.id, decision="approve", fingerprint=a.fingerprint)
    svc.decide(a.id, decision="approve", fingerprint=a.fingerprint, payment_confirmation=True)


def test_rejection_and_idempotent_create():
    svc = ApprovalService()
    a = make(svc)
    assert make(svc).id == a.id  # same pending proposal is reused
    svc.decide(a.id, decision="reject", fingerprint=a.fingerprint)
    with pytest.raises(ApprovalError):
        svc.require_approved(a.id, task_id="t1", fingerprint=a.fingerprint)

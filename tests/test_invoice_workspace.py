import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.api.rest.routers.invoice_routers import (
    approve_invoice_exception,
    confirm_invoice_match,
)
from src.models.audit_models import AuditLog
from src.models.invoice_match_models import InvoiceMatch
from src.models.invoice_models import Invoice
from src.models.purchase_order_models import PurchaseOrder
from src.services.invoice_matching_services import _status_after_match


def test_match_result_statuses_require_clean_po_and_receipt():
    assert (
        _status_after_match(
            po_linked=True,
            result="match_ok",
            has_discrepancies=False,
            missing_receipt=False,
        )
        == "ready_to_pay"
    )
    assert (
        _status_after_match(
            po_linked=False,
            result="no_match",
            has_discrepancies=True,
            missing_receipt=False,
        )
        == "needs_review"
    )
    assert (
        _status_after_match(
            po_linked=True,
            result="match_issues",
            has_discrepancies=True,
            missing_receipt=False,
        )
        == "needs_review"
    )
    assert (
        _status_after_match(
            po_linked=True,
            result="match_ok",
            has_discrepancies=False,
            missing_receipt=True,
        )
        == "needs_review"
    )


def test_confirm_suggested_match_links_po_and_audits(monkeypatch):
    invoice_id, po_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=None,
        status="needs_review",
        match_status="no_match",
        po_id=None,
        po_number_raw=None,
    )
    po = SimpleNamespace(id=po_id, po_number="PO-2026-0001")
    candidate = SimpleNamespace(po_id=po_id, status="suggested", confirmed_by=None)
    invoice_query, match_query, po_query = MagicMock(), MagicMock(), MagicMock()
    invoice_join = invoice_query.outerjoin.return_value
    invoice_filtered = invoice_join.filter.return_value
    invoice_locked = invoice_filtered.with_for_update.return_value
    invoice_locked.first.return_value = invoice
    match_query.filter.return_value.first.return_value = candidate
    po_query.filter.return_value.first.return_value = po
    db = MagicMock()
    db.query.side_effect = lambda model, *args: {
        Invoice: invoice_query,
        InvoiceMatch: match_query,
        PurchaseOrder: po_query,
    }[model]

    def rerun(_db, _invoice_id, _actor_id):
        invoice.status = "ready_to_pay"
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "src.services.invoice_matching_services.run_match_for_invoice", rerun
    )
    response = confirm_invoice_match(
        invoice_id, {"po_id": str(po_id)}, db, {"sub": str(actor_id)}
    )

    assert response["status"] == "ready_to_pay"
    assert invoice.po_id == po_id
    assert invoice.po_number_raw == "PO-2026-0001"
    assert candidate.status == "confirmed"
    assert candidate.confirmed_by == actor_id
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.action == "invoice_match_confirmed"
    assert audit.actor == str(actor_id)
    assert audit.reason
    db.commit.assert_called_once()


def test_approve_exception_requires_reason_and_moves_to_ready_to_pay():
    invoice_id, actor_id = uuid.uuid4(), uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        owner_user_id=None,
        created_by=None,
        status="needs_review",
        match_status="no_match",
        workflow_status="no_match",
        final_status=None,
        final_by=None,
        final_at=None,
    )
    invoice_query = MagicMock()
    invoice_join = invoice_query.outerjoin.return_value
    invoice_filtered = invoice_join.filter.return_value
    invoice_locked = invoice_filtered.with_for_update.return_value
    invoice_locked.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = invoice_query

    response = approve_invoice_exception(
        invoice_id,
        {"reason": "Receipt is delayed; supplier confirmed delivery"},
        db,
        {"sub": str(actor_id)},
    )

    assert response["status"] == "ready_to_pay"
    assert invoice.final_status == "approved"
    assert invoice.status == "ready_to_pay"
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.actor == str(actor_id)
    assert audit.reason == "Receipt is delayed; supplier confirmed delivery"
    db.commit.assert_called_once()

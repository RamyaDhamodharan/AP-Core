import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.audit_models import AuditLog
from src.models.email_draft_models import EmailDraft
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.match_run_models import MatchRun
from src.services import invoice_matching_services as matching


def _query(previous=None, max_run=0):
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.first.return_value = previous
    query.scalar.return_value = max_run
    return query


def test_pipeline_match_sets_match_approved_without_admin_decision(monkeypatch):
    invoice_id = uuid.uuid4()
    run_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        po_id=uuid.uuid4(),
        source_document_id=None,
        match_status="pending",
        final_status=None,
        final_run_id=None,
        final_by=None,
        final_at=None,
        invoice_no="INV-1",
        workflow_status="matching",
        status="processing",
        field_confidence={
            field: {"confidence": 95, "derived": False}
            for field in matching_confidence_fields()
        },
    )
    query_previous = _query()
    query_max = _query(max_run=0)
    db = MagicMock()
    db.get.return_value = invoice
    db.query.side_effect = [query_previous, query_max] + [_query() for _ in range(20)]
    db.flush.side_effect = lambda: [
        setattr(obj, "id", run_id)
        for obj in [call.args[0] for call in db.add.call_args_list]
        if isinstance(obj, MatchRun)
    ]
    monkeypatch.setattr(matching, "_invoice_state_fingerprint", lambda *_: "same-state")
    monkeypatch.setattr(
        matching,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(checks=[]),
    )
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "match_ok"
    assert run.run_no == 1
    assert invoice.final_status is None
    assert invoice.workflow_status == "match_approved"
    assert invoice.final_by is None
    assert invoice.final_run_id == run_id

    prior = SimpleNamespace(rules_used={"state_fingerprint": "same-state"})
    db.query.side_effect = None
    db.query.return_value = _query(previous=prior)
    assert matching.run_match_for_invoice(db, invoice_id) is prior


def matching_confidence_fields():
    return (
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax",
        "total",
        "order_number",
        "gl_code",
    )


def _no_match_fixture(monkeypatch, *, candidates=None, source="upload"):
    invoice_id, document_id = uuid.uuid4(), uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        source=source,
        source_flag=None,
        email_sender="allowed@example.test" if source == "email" else None,
    )
    invoice = SimpleNamespace(
        id=invoice_id,
        po_id=None,
        source_document_id=document_id,
        status="processing",
        workflow_status="matching",
        match_status="pending",
        final_status=None,
        final_run_id=None,
        invoice_no="INV-NOMATCH",
        po_number_raw=None,
        field_confidence={
            field: {"confidence": 95, "derived": False}
            for field in matching_confidence_fields()
        },
    )
    db = MagicMock()
    db.get.side_effect = lambda model, _id: (
        invoice if model.__name__ == "Invoice" else document
    )
    db.query.return_value = _query()
    db.flush.side_effect = lambda: [
        setattr(obj, "id", uuid.uuid4())
        for obj in (call.args[0] for call in db.add.call_args_list)
        if isinstance(obj, MatchRun) and not getattr(obj, "id", None)
    ]
    monkeypatch.setattr(matching, "_invoice_state_fingerprint", lambda *_: "state")
    monkeypatch.setattr(
        matching,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(checks=[], missing_document_types=[]),
    )
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        matching, "_suggested_purchase_orders", lambda *_: candidates or []
    )
    return db, invoice, document, invoice_id


def test_completed_no_match_rejects_audits_history_and_creates_allowlisted_draft(
    monkeypatch,
):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "true")
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "allowed@example.test")
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="email")

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "no_match"
    assert invoice.workflow_status == "rejected"
    assert invoice.status == "rejected"
    added = [call.args[0] for call in db.add.call_args_list]
    assert any(
        isinstance(row, InvoiceStateHistory)
        and row.to_status == "rejected"
        and row.reason == "No purchase order / unknown vendor"
        for row in added
    )
    assert any(
        isinstance(row, AuditLog)
        and row.actor == "system"
        and row.reason == "No purchase order / unknown vendor"
        for row in added
    )
    draft = next(row for row in added if isinstance(row, EmailDraft))
    assert draft.to_addresses == ["allowed@example.test"]
    assert draft.status == "draft"
    assert draft.blocked_by_allowlist is False
    assert any(
        isinstance(row, AuditLog)
        and row.action == "draft_created"
        and row.actor == "system"
        for row in added
    )


def test_no_match_does_not_notify_with_empty_allowlist_or_when_flag_disabled(
    monkeypatch,
):
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "")
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="email")
    matching.run_match_for_invoice(db, invoice_id)
    assert invoice.status == "rejected"
    draft = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], EmailDraft)
    )
    assert draft.status == "draft"
    assert draft.blocked_by_allowlist is True

    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "false")
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="email")
    run = matching.run_match_for_invoice(db, invoice_id)
    assert run.result == "no_match"
    assert invoice.workflow_status == "no_match"
    assert invoice.status == "needs_review"


def test_upload_no_match_auto_rejects_without_sender_notification(monkeypatch):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "true")
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "allowed@example.test")
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="upload")

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "no_match"
    assert invoice.status == "rejected"
    assert not any(
        isinstance(call.args[0], EmailDraft) for call in db.add.call_args_list
    )


def test_missing_confidence_keeps_a_matched_invoice_in_verification(monkeypatch):
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_id=uuid.uuid4(),
        source_document_id=None,
        workflow_status="matching",
        status="processing",
        match_status="pending",
        final_status=None,
        final_run_id=None,
        invoice_no="INV-VERIFY",
        po_number_raw="PO-1",
        field_confidence={},
    )
    db = MagicMock()
    db.get.return_value = invoice
    db.query.side_effect = lambda *_args: _query(max_run=0)
    db.flush.side_effect = lambda: [
        setattr(call.args[0], "id", uuid.uuid4())
        for call in db.add.call_args_list
        if isinstance(call.args[0], MatchRun) and not call.args[0].id
    ]
    monkeypatch.setattr(matching, "_invoice_state_fingerprint", lambda *_: "verify")
    monkeypatch.setattr(
        matching,
        "recommend_invoice_review",
        lambda *_args, **_kwargs: SimpleNamespace(checks=[], missing_document_types=[]),
    )
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])

    matching.run_match_for_invoice(db, invoice.id)

    assert invoice.workflow_status == "verifying"
    assert invoice.status == "needs_review"
    assert any(
        isinstance(call.args[0], InvoiceStateHistory)
        and call.args[0].reason == "Confidence unavailable, please verify"
        for call in db.add.call_args_list
    )


def test_suggested_candidate_is_review_not_no_match(monkeypatch):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "true")
    candidate = (SimpleNamespace(id=uuid.uuid4()), Decimal("80"))
    db, invoice, _document, invoice_id = _no_match_fixture(
        monkeypatch, candidates=[candidate]
    )

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "match_issues"
    assert invoice.workflow_status == "match_issues"
    assert invoice.status == "needs_review"
    assert not any(
        isinstance(call.args[0], EmailDraft) for call in db.add.call_args_list
    )


def test_rerun_does_not_rewrite_approved_or_paid_invoice(monkeypatch):
    for workflow_status, legacy_status in (
        ("approved", "ready_to_pay"),
        ("paid", "paid"),
    ):
        db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch)
        invoice.workflow_status = workflow_status
        invoice.status = legacy_status
        monkeypatch.setattr(
            matching, "_invoice_state_fingerprint", lambda *_: "new-state"
        )

        assert matching.run_match_for_invoice(db, invoice_id) is not None
        assert invoice.workflow_status == workflow_status
        assert invoice.status == legacy_status


def test_no_match_draft_failure_does_not_block_auto_reject(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_NO_MATCH_AUTO_REJECT", "true")
    monkeypatch.setenv("TEST_MAILBOX_ALLOWLIST", "allowed@example.test")
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="email")
    monkeypatch.setattr(
        "src.services.email_draft_services.create_no_match_draft",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("draft store unavailable")),
    )

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "no_match"
    assert invoice.workflow_status == "rejected"
    assert invoice.status == "rejected"
    warning = next(
        record for record in caplog.records
        if record.message == "No-match email draft could not be created"
    )
    assert warning.exc_info is not None

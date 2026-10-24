import hashlib
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models.audit_models import AuditLog
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
        po_decision=None,
        final_by=None,
        final_at=None,
        invoice_no="INV-1",
        po_number_raw="PO-1",
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
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        matching,
        "evaluate_invoice",
        lambda *_a, **_k: {
            "rule_set_version": "ap-match-v1",
            "outcome": "ready_to_pay",
            "discrepancies": [],
            "low_confidence_fields": [],
            "inputs_summary": {},
        },
    )

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "match_ok"
    assert run.run_no == 1
    assert invoice.final_status is None
    assert invoice.workflow_status == "match_approved"
    assert invoice.final_by is None
    assert invoice.final_run_id == run_id
    assert run.rules_used["rule_set_version"] == "ap-match-v1"
    assert run.rules_used["match_outcome"]["outcome"] == "ready_to_pay"

    prior = run
    run_count = sum(
        isinstance(call.args[0], MatchRun) for call in db.add.call_args_list
    )
    db.query.side_effect = None
    db.query.return_value = _query(previous=prior)
    assert matching.run_match_for_invoice(db, invoice_id) is prior
    assert (
        sum(isinstance(call.args[0], MatchRun) for call in db.add.call_args_list)
        == run_count
    )

    db.query.return_value = _query(previous=prior, max_run=1)
    invoice.po_id = None
    monkeypatch.setattr(matching, "_suggested_purchase_orders", lambda *_args: [])
    rerun = matching.run_match_for_invoice(db, invoice_id, force=True)
    assert rerun is not prior
    assert rerun.run_no == 2
    assert (
        sum(isinstance(call.args[0], MatchRun) for call in db.add.call_args_list)
        == run_count + 1
    )


def test_dependency_match_rerun_is_idempotent_when_inputs_are_unchanged(monkeypatch):
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        po_id=None,
        po_number_raw=None,
        po_decision=None,
        status="needs_review",
        workflow_status="match_issues",
        match_status="needs_po_confirmation",
        final_status=None,
        final_run_id=None,
        field_confidence={},
    )
    expected_inputs = {
        "po_decision": "awaiting_confirmation",
        "po_suggestions": [],
    }
    state_fingerprint = hashlib.sha256(
        json.dumps(expected_inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    previous = SimpleNamespace(
        rules_used={"state_fingerprint": state_fingerprint},
    )
    query = _query(previous=previous)
    db = MagicMock()
    db.get.return_value = invoice
    db.query.return_value = query
    monkeypatch.setattr(
        matching,
        "evaluate_invoice",
        lambda *_args, **_kwargs: {
            "inputs_summary": {},
            "discrepancies": [],
            "outcome": "needs_review",
            "low_confidence_fields": [],
        },
    )
    monkeypatch.setattr(matching, "_suggested_purchase_orders", lambda *_: [])

    result = matching.run_match_for_invoice(
        db,
        invoice.id,
        force=True,
        idempotent=True,
        trigger="dependency_arrived",
    )

    assert result is previous
    db.add.assert_not_called()


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
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        matching, "_suggested_purchase_orders", lambda *_: candidates or []
    )
    monkeypatch.setattr(
        matching,
        "evaluate_invoice",
        lambda *_args, **_kwargs: {
            "rule_set_version": "ap-match-v1",
            "outcome": "needs_review",
            "discrepancies": [
                {
                    "code": "PO_CONFIRMATION_REQUIRED",
                    "message": "Select a suggested PO or wait.",
                    "severity": "info",
                }
            ],
            "low_confidence_fields": [],
            "inputs_summary": {},
        },
    )
    return db, invoice, document, invoice_id


def test_no_number_invoice_waits_for_human_po_decision_without_auto_reject(
    monkeypatch,
):
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch, source="email")
    run = matching.run_match_for_invoice(db, invoice_id)
    assert run.result == "match_issues"
    assert invoice.po_decision == "awaiting_confirmation"
    assert invoice.workflow_status == "match_issues"
    assert invoice.status == "needs_review"


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
    monkeypatch.setattr(matching, "sync_invoice_issues", lambda *_args, **_kwargs: [])

    run = matching.run_match_for_invoice(db, invoice.id)

    assert invoice.workflow_status == "verifying"
    assert invoice.status == "needs_review"
    assert any(item["code"] == "LOW_CONFIDENCE" for item in run.differences)


def test_suggested_candidate_is_review_not_no_match(monkeypatch):
    candidate = (SimpleNamespace(id=uuid.uuid4()), Decimal("80"), {})
    db, invoice, _document, invoice_id = _no_match_fixture(
        monkeypatch, candidates=[candidate]
    )

    run = matching.run_match_for_invoice(db, invoice_id)

    assert run.result == "match_issues"
    assert invoice.workflow_status == "match_issues"
    assert invoice.status == "needs_review"
    assert invoice.po_decision == "awaiting_confirmation"


def test_po_decision_endpoint_returns_status_and_audits_choice(monkeypatch):
    from src.api.rest.routers.invoice_routers import decide_invoice_po_path

    actor_id = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        owner_user_id=None,
        created_by=actor_id,
        status="needs_review",
        match_status="needs_po_confirmation",
        po_decision="awaiting_confirmation",
        po_id=None,
        po_number_raw=None,
    )
    query = MagicMock()
    locked_query = query.outerjoin.return_value.filter.return_value
    locked_query = locked_query.with_for_update.return_value
    locked_query.first.return_value = invoice
    db = MagicMock()
    db.query.return_value = query
    match_run = SimpleNamespace(id=uuid.uuid4())

    def run_match(_db, invoice_id, _actor_id, **_kwargs):
        assert invoice_id == invoice.id
        invoice.status = "needs_review"
        invoice.match_status = "waiting_for_po"
        return match_run

    monkeypatch.setattr(
        "src.api.rest.routers.invoice_routers.run_match_for_invoice", run_match
    )
    response = decide_invoice_po_path(
        invoice.id,
        {"decision": "wait_for_po"},
        db,
        {"sub": str(actor_id)},
    )
    assert response["status"] == "needs_review"
    assert response["po_decision"] == "waiting_for_po"
    audit = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    )
    assert audit.action == "invoice_po_decision"
    assert audit.new_value["status"] == "needs_review"
    db.commit.assert_called_once()


def test_rematch_preserves_paid_invoice(monkeypatch):
    db, invoice, _document, invoice_id = _no_match_fixture(monkeypatch)
    invoice.workflow_status = "paid"
    invoice.status = "paid"
    monkeypatch.setattr(matching, "_invoice_state_fingerprint", lambda *_: "new-state")
    matching.run_match_for_invoice(db, invoice_id, force=True)
    assert invoice.workflow_status == "paid"
    assert invoice.status == "paid"

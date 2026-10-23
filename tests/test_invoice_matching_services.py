import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

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

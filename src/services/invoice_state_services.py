"""
Central invoice lifecycle transitions and their legacy status projection.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/routers/document_routers.py,
src/api/rest/routers/document_workflow_routers.py,
src/api/rest/routers/invoice_routers.py.
Calls: invoice_models, invoice_state_history_models, match_run_models.
Tables: invoice_state_history, invoices, match_runs.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.models.invoice_models import Invoice
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.match_run_models import MatchRun

WORKFLOW_STATUSES = {
    "received",
    "reading",
    "verifying",
    "matching",
    "match_approved",
    "match_issues",
    "no_match",
    "approved",
    "rejected",
    "on_hold",
    "paid",
    "overdue",
    "resubmitted",
    "failed",
}

ALLOWED_TRANSITIONS = {
    "received": {
        "reading",
        "verifying",
        "matching",
        "match_approved",
        "match_issues",
        "no_match",
        "failed",
    },
    "reading": {"verifying", "matching", "failed", "received"},
    "verifying": {"matching", "match_issues", "no_match", "failed", "received"},
    "matching": {
        "match_approved",
        "match_issues",
        "no_match",
        "failed",
        "verifying",
        "on_hold",
    },
    "match_approved": {
        "approved",
        "rejected",
        "on_hold",
        "matching",
        "match_issues",
        "no_match",
        "resubmitted",
    },
    "match_issues": {
        "verifying",
        "matching",
        "match_approved",
        "no_match",
        "on_hold",
        "rejected",
        "resubmitted",
        "approved",
    },
    "no_match": {
        "matching",
        "match_issues",
        "match_approved",
        "on_hold",
        "rejected",
        "resubmitted",
    },
    "approved": {
        "paid",
        "overdue",
        "on_hold",
        "resubmitted",
        "matching",
        "verifying",
        "match_approved",
        "match_issues",
        "no_match",
    },
    "rejected": {"resubmitted", "verifying", "matching"},
    "on_hold": {"verifying", "matching", "approved", "rejected", "resubmitted"},
    "paid": {"approved"},
    "overdue": {"paid", "approved", "on_hold", "resubmitted"},
    "resubmitted": {"reading", "verifying", "matching", "failed"},
    "failed": {"received", "reading", "resubmitted"},
}


def transition_invoice(
    db: Session,
    invoice: Invoice,
    to_status: str,
    *,
    changed_by: UUID | None = None,
    reason: str | None = None,
    force: bool = False,
    allow_exception: bool = False,
) -> InvoiceStateHistory:
    """
    Apply an allowed status change and persist state history in the transaction.
    Inputs: db (Session), invoice (Invoice), to_status (str), changed_by (UUID | None),
        reason (str | None), force (bool), allow_exception (bool).
    Output: InvoiceStateHistory.
    Effects: reads database rows; writes or commits database rows; may add invoice
        timeline entries.
    Results: statuses approved, failed, match_approved, match_issues, matching,
        needs_review, needs_vendor_review, no_match.
    """
    if to_status not in WORKFLOW_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown invoice workflow status")
    if to_status in {"approved", "paid"} and not (
        allow_exception and to_status == "approved"
    ):
        run_id = getattr(invoice, "final_run_id", None)
        run = db.get(MatchRun, run_id) if run_id else None
        outcome = (run.rules_used or {}).get("match_outcome") if run else None
        if not outcome or outcome.get("outcome") != "ready_to_pay":
            raise HTTPException(
                status_code=409,
                detail="Invoice cannot be approved until all matching rules pass",
            )
    current = getattr(invoice, "workflow_status", None) or legacy_workflow_status(
        invoice
    )
    if (
        current != to_status
        and not force
        and to_status not in ALLOWED_TRANSITIONS.get(current, set())
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Invoice cannot move from {current} to {to_status}",
        )
    if current == to_status:
        raise HTTPException(status_code=409, detail=f"Invoice is already {to_status}")

    invoice.workflow_status = to_status
    if to_status == "approved":
        invoice.status = "ready_to_pay"
    elif to_status == "rejected":
        invoice.status = "rejected"
    elif to_status == "on_hold":
        invoice.status = "on_hold"
    elif to_status in {"verifying", "match_issues", "no_match"}:
        invoice.status = "needs_review"
    else:
        invoice.status = "processing"
    if to_status in {"approved", "paid", "overdue"}:
        invoice.final_status = "approved"
    elif to_status in {"rejected", "on_hold"}:
        invoice.final_status = to_status
    elif to_status in {
        "received",
        "reading",
        "verifying",
        "matching",
        "match_approved",
        "match_issues",
        "no_match",
        "resubmitted",
        "failed",
    }:
        invoice.final_status = None
    invoice.match_status = {
        "received": "pending",
        "reading": "pending",
        "verifying": "needs_vendor_review",
        "matching": "pending",
        "match_approved": "pending",
        "match_issues": "match_issues",
        "no_match": "no_match",
        "resubmitted": "pending",
        "failed": "pending",
        "approved": "pending",
        "rejected": "pending",
        "on_hold": "pending",
        "paid": "pending",
        "overdue": "pending",
    }[to_status]
    history = InvoiceStateHistory(
        invoice_id=invoice.id,
        from_status=current,
        to_status=to_status,
        changed_by=changed_by,
        reason=reason,
        created_at=datetime.now(UTC),
    )
    db.add(history)
    return history


def legacy_workflow_status(invoice: Invoice) -> str:
    """
    Handle legacy workflow status.
    Inputs: invoice (Invoice).
    Output: str.
    Effects: does not write database rows or queue work directly.
    Results: statuses match_issues, matching, needs_po_confirmation,
        needs_vendor_review, no_match, pending, received, verifying.
    """
    workflow_status = getattr(invoice, "workflow_status", None)
    if workflow_status in WORKFLOW_STATUSES:
        return workflow_status
    final = getattr(invoice, "final_status", None)
    if final:
        return final
    return {
        "pending": "received",
        "needs_vendor_review": "verifying",
        "waiting_for_po": "matching",
        "needs_po_confirmation": "matching",
        "match_issues": "match_issues",
        "no_match": "no_match",
    }.get(getattr(invoice, "match_status", "pending"), "received")

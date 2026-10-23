"""Persisted invoice-to-PO matching and approval workflow."""

import hashlib
import json
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.match_run_models import MatchRun
from src.models.notification_models import Notification
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.document_workflow_services import recommend_invoice_review
from src.services.invoice_issue_services import sync_invoice_issues
from src.services.invoice_state_services import transition_invoice


def _invoice_state_fingerprint(db: Session, invoice: Invoice) -> str:
    po = db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    )
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        if po
        else []
    )
    state = {
        "invoice": {
            field: str(getattr(invoice, field, None))
            for field in (
                "invoice_no",
                "vendor_id",
                "po_id",
                "po_number_raw",
                "currency",
                "subtotal",
                "tax_amount",
                "total_amount",
                "gl_code",
                "invoice_date",
                "due_date",
            )
        },
        "invoice_lines": [
            [
                str(getattr(line, field, None))
                for field in (
                    "item_code",
                    "description",
                    "uom",
                    "qty",
                    "unit_price",
                    "line_amount",
                )
            ]
            for line in invoice_lines
        ],
        "po": {
            field: str(getattr(po, field, None))
            for field in (
                "id",
                "vendor_id",
                "po_number",
                "currency",
                "subtotal",
                "tax_amount",
                "tax_rate",
                "total_amount",
            )
        }
        if po
        else None,
        "po_lines": [
            [
                str(getattr(line, field, None))
                for field in (
                    "item_code",
                    "description",
                    "uom",
                    "qty",
                    "unit_price",
                    "line_amount",
                )
            ]
            for line in po_lines
        ],
    }
    serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def run_match_for_invoice(db: Session, invoice_id, user_id=None) -> MatchRun | None:
    """Run and persist matching once per distinct invoice/PO state."""
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return None

    match_started = time.perf_counter()
    fingerprint = _invoice_state_fingerprint(db, invoice)
    previous = (
        db.query(MatchRun)
        .filter(MatchRun.invoice_id == invoice.id)
        .order_by(MatchRun.run_no.desc())
        .first()
    )
    if previous and (previous.rules_used or {}).get("state_fingerprint") == fingerprint:
        return previous

    po_link_started = time.perf_counter()
    recommendation = recommend_invoice_review(db, invoice, owner_id=user_id)
    db.add(
        AuditLog(
            action="pipeline_step",
            entity_type="invoice",
            entity_id=invoice.id,
            step_name="po_link",
            duration_ms=max(0, int((time.perf_counter() - po_link_started) * 1000)),
            input_ref=str(invoice.po_id or invoice.po_number_raw or invoice.id),
            output_summary={"po_linked": bool(invoice.po_id)},
            result="success" if invoice.po_id else "warning",
            message="Purchase order link evaluated",
            created_by=user_id,
        )
    )
    if not invoice.po_id:
        result = "no_match"
    elif any(
        check.outcome == "mismatch" and check.severity == "error"
        for check in recommendation.checks
    ):
        result = "match_issues"
    else:
        # Missing goods-receipt evidence is a warning in the two-way AP workflow.
        result = "match_ok"

    previous_no = (
        db.query(func.max(MatchRun.run_no))
        .filter(MatchRun.invoice_id == invoice.id)
        .scalar()
        or 0
    )
    differences = [
        check.model_dump(mode="json")
        for check in recommendation.checks
        if check.outcome in {"mismatch", "not_possible"}
    ]
    sync_invoice_issues(
        db,
        invoice,
        differences,
        user_id,
        getattr(recommendation, "missing_document_types", []),
    )
    run = MatchRun(
        invoice_id=invoice.id,
        run_no=previous_no + 1,
        po_id=invoice.po_id,
        result=result,
        rules_used={"unit_price_tolerance_pct": "2%", "state_fingerprint": fingerprint},
        differences=differences,
        created_by=user_id,
    )
    db.add(run)
    db.flush()

    old = {
        "match_status": invoice.match_status,
        "final_status": invoice.final_status,
        "workflow_status": getattr(invoice, "workflow_status", None),
        "final_run_id": str(invoice.final_run_id) if invoice.final_run_id else None,
    }
    current_workflow_status = getattr(invoice, "workflow_status", None)
    workflow_status = {
        "match_ok": "match_approved",
        "match_issues": "match_issues",
        "no_match": "no_match",
    }[result]
    if current_workflow_status not in {
        "approved",
        "paid",
        "rejected",
        "on_hold",
        workflow_status,
    }:
        transition_invoice(
            db,
            invoice,
            workflow_status,
            changed_by=user_id,
            reason="Invoice match completed",
        )
    new = {
        "match_status": invoice.match_status,
        "match_run_id": str(run.id),
        "result": result,
    }
    invoice.final_run_id = run.id
    if result == "match_ok":
        new.update(workflow_status=workflow_status)
    db.add(
        AuditLog(
            action="invoice_match_run",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value=new,
            result="success" if result == "match_ok" else "warning",
            message=f"Invoice match completed: {result}",
            created_by=user_id,
        )
    )
    db.add(
        AuditLog(
            action="pipeline_step",
            entity_type="invoice",
            entity_id=invoice.id,
            step_name="match",
            duration_ms=max(0, int((time.perf_counter() - match_started) * 1000)),
            input_ref=str(invoice.id),
            output_summary={"result": result, "differences": len(differences)},
            result="success" if result == "match_ok" else "warning",
            message="Invoice match evaluated",
            created_by=user_id,
        )
    )
    if result == "no_match" and invoice.source_document_id:
        document = db.get(Document, invoice.source_document_id)
        if document and document.email_sender:
            db.add(
                Notification(
                    document_id=document.id,
                    recipient_email=document.email_sender,
                    subject="Invoice could not be matched",
                    body=(
                        f"Invoice {invoice.invoice_no or ''} has no matching "
                        "purchase order."
                    ),
                    status="queued",
                )
            )
    db.flush()
    return run

"""Persisted invoice-to-PO matching and approval workflow."""

import hashlib
import json
import os
import time
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_match_models import InvoiceDiscrepancy, InvoiceMatch
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.ap_decision_engine import evaluate_invoice
from src.services.document_workflow_services import (
    _owner_scope,
)
from src.services.invoice_confidence_services import (
    confidence_unavailable,
    requires_verification,
)
from src.services.invoice_issue_services import sync_invoice_issues
from src.services.invoice_state_services import (
    legacy_workflow_status,
    transition_invoice,
)


def _email_no_match_auto_reject_enabled() -> bool:
    return os.getenv("EMAIL_NO_MATCH_AUTO_REJECT", "true").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _suggested_purchase_orders(db: Session, invoice: Invoice, owner_id=None):
    """Return transparent, bounded scores for vendor/amount/item-close POs."""
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    )
    po_rows = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.is_current.is_(True), *_owner_scope(PurchaseOrder, owner_id)
        )
        .all()
    )
    tax_id = (invoice.vendor_tax_id_raw or "").strip().casefold()
    invoice_total = Decimal(str(invoice.total_amount or 0))
    invoice_terms = [
        str(line.item_code or line.description or "").casefold().strip()
        for line in invoice_lines
    ]
    scored = []
    for po in po_rows:
        po_vendor = getattr(po, "vendor", None)
        vendor_exact = bool(invoice.vendor_id and po.vendor_id == invoice.vendor_id)
        tax_exact = bool(
            tax_id
            and po_vendor
            and (po_vendor.tax_id or "").strip().casefold() == tax_id
        )
        if not vendor_exact and not tax_exact:
            continue
        score = 40.0 if vendor_exact else 35.0
        po_total = Decimal(str(po.total_amount or 0))
        if invoice_total and po_total:
            relative = float(
                abs(invoice_total - po_total) / max(abs(po_total), Decimal("0.01"))
            )
            score += max(0.0, 30.0 * (1.0 - relative))
        lines = (
            db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        )
        po_terms = [
            str(line.item_code or line.description or "").casefold().strip()
            for line in lines
        ]
        overlaps = sum(
            1
            for term in invoice_terms
            if term
            and any(
                term == other or term in other or other in term for other in po_terms
            )
        )
        score += 30.0 * overlaps / max(len(invoice_terms), 1) if invoice_terms else 0.0
        if score >= 50:
            scored.append((po, Decimal(str(round(min(score, 100.0), 2)))))
    return sorted(scored, key=lambda pair: pair[1], reverse=True)[:5]


def _status_after_match(
    *, po_linked: bool, result: str, has_discrepancies: bool, missing_receipt: bool
) -> str:
    if (
        po_linked
        and result == "match_ok"
        and not has_discrepancies
        and not missing_receipt
    ):
        return "ready_to_pay"
    return "needs_review"


def _invoice_state_fingerprint(db: Session, invoice: Invoice) -> str:
    invoice_po_id = getattr(invoice, "po_id", None)
    po = db.get(PurchaseOrder, invoice_po_id) if invoice_po_id else None
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


def run_match_for_invoice(
    db: Session,
    invoice_id,
    user_id=None,
    *,
    force: bool = False,
    idempotent: bool = False,
    trigger: str | None = None,
) -> MatchRun | None:
    """Persist the initial match or an explicitly requested rerun."""
    unit_price_tolerance = f"{os.getenv('INVOICE_UNIT_PRICE_TOLERANCE_PCT', '2')}%"
    total_tolerance = f"{os.getenv('INVOICE_TOTAL_TOLERANCE_PCT', '1')}%"
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return None

    match_started = time.perf_counter()
    previous = (
        db.query(MatchRun)
        .filter(MatchRun.invoice_id == invoice.id)
        .order_by(MatchRun.run_no.desc())
        .first()
    )
    if previous and not force and (previous.rules_used or {}).get("rule_set_version"):
        return previous
    original_workflow_status = getattr(
        invoice, "workflow_status", None
    ) or legacy_workflow_status(invoice)
    preserve_final_status = original_workflow_status in {
        "paid",
        "rejected",
        "on_hold",
    } or getattr(invoice, "status", None) in {"paid", "rejected", "on_hold"}
    po_link_started = time.perf_counter()
    decision = evaluate_invoice(db, invoice, owner_id=user_id)
    fingerprint = hashlib.sha256(
        json.dumps(
            decision["inputs_summary"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if (
        idempotent
        and previous is not None
        and (previous.rules_used or {}).get("state_fingerprint") == fingerprint
    ):
        return previous
    run_actor_id = None if trigger else user_id
    db.add(
        AuditLog(
            action="pipeline_step",
            entity_type="invoice",
            entity_id=invoice.id,
            step_name="po_link",
            duration_ms=max(0, int((time.perf_counter() - po_link_started) * 1000)),
            input_ref=str(
                getattr(invoice, "po_id", None)
                or getattr(invoice, "po_number_raw", None)
                or invoice.id
            ),
            output_summary={"po_linked": bool(getattr(invoice, "po_id", None))},
            result=("success" if getattr(invoice, "po_id", None) else "warning"),
            message="Purchase order link evaluated",
            created_by=run_actor_id,
        )
    )
    result = (
        "match_ok"
        if decision["outcome"] == "ready_to_pay"
        else (
            "no_match"
            if any(item["code"] == "MISSING_PO" for item in decision["discrepancies"])
            else "match_issues"
        )
    )

    previous_no = (
        db.query(func.max(MatchRun.run_no))
        .filter(MatchRun.invoice_id == invoice.id)
        .scalar()
        or 0
    )
    differences = list(decision["discrepancies"])
    issue_checks = [
        {
            "outcome": "mismatch",
            "name": item["code"].casefold(),
            "reason": item["message"],
            "expected": item.get("expected_value"),
            "actual": item.get("invoice_value"),
            "severity": "error",
        }
        for item in differences
    ]
    sync_invoice_issues(
        db,
        invoice,
        issue_checks,
        run_actor_id,
        ["goods_receipt"]
        if any(item["code"] == "MISSING_GR" for item in differences)
        else [],
    )
    run = MatchRun(
        invoice_id=invoice.id,
        run_no=previous_no + 1,
        po_id=invoice.po_id,
        result=result,
        rules_used={
            "rule_set_version": decision["rule_set_version"],
            "match_outcome": decision,
            "inputs_summary": decision["inputs_summary"],
            "unit_price_tolerance_pct": unit_price_tolerance,
            "total_tolerance_pct": total_tolerance,
            "state_fingerprint": fingerprint,
            "trigger": trigger or "requested",
        },
        differences=differences,
        created_by=run_actor_id,
    )
    db.add(run)
    db.flush()

    # Save workspace candidates and explainable discrepancy rows alongside the
    # existing matching run so human decisions survive list/detail refreshes.
    db.query(InvoiceDiscrepancy).filter(
        InvoiceDiscrepancy.invoice_id == invoice.id,
    ).delete(synchronize_session=False)
    if invoice.po_id:
        match = (
            db.query(InvoiceMatch)
            .filter(
                InvoiceMatch.invoice_id == invoice.id,
                InvoiceMatch.po_id == invoice.po_id,
                InvoiceMatch.status != "rejected",
            )
            .order_by(InvoiceMatch.created_at.desc())
            .first()
        )
        if match is None:
            receipt = (
                db.query(GoodsReceipt)
                .filter(GoodsReceipt.po_id == invoice.po_id)
                .order_by(GoodsReceipt.receipt_date.desc())
                .first()
            )
            match = InvoiceMatch(
                invoice_id=invoice.id,
                po_id=invoice.po_id,
                gr_id=receipt.id if receipt else None,
                match_type="3-way" if receipt else "2-way",
                score=Decimal("100"),
                status="confirmed",
                confirmed_by=run_actor_id,
            )
            db.add(match)
        else:
            receipt = (
                db.query(GoodsReceipt)
                .filter(GoodsReceipt.po_id == invoice.po_id)
                .order_by(GoodsReceipt.receipt_date.desc())
                .first()
            )
            match.gr_id = receipt.id if receipt else None
            match.match_type = "3-way" if receipt else "2-way"
            match.score = Decimal("100")
            match.status = "confirmed"
            match.confirmed_by = user_id or match.confirmed_by
    else:
        db.query(InvoiceMatch).filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.status == "suggested",
        ).delete(synchronize_session=False)
        candidates = _suggested_purchase_orders(db, invoice, user_id)
        for po, score in candidates:
            db.add(
                InvoiceMatch(
                    invoice_id=invoice.id,
                    po_id=po.id,
                    match_type="2-way",
                    score=score,
                    status="suggested",
                )
            )
        if candidates:
            differences.append(
                {
                    "name": "suggested_po_match",
                    "reason": "Suggested PO match",
                    "severity": "warning",
                }
            )
        else:
            differences.append(
                {"name": "no_po_found", "reason": "No PO found", "severity": "error"}
            )

    no_match_auto_reject = False
    has_suggested_candidate = bool(candidates) if not invoice.po_id else False
    # A suggested PO is a review case. It is never classified as a completed no-match.
    if result == "no_match" and has_suggested_candidate:
        result = "match_issues"
    auto_reject = (
        result == "no_match"
        and no_match_auto_reject
        and not preserve_final_status
        and original_workflow_status != "rejected"
    )

    for difference in differences:
        field_name = str(difference.get("code", "matching")).casefold()[:80]
        db.add(
            InvoiceDiscrepancy(
                invoice_id=invoice.id,
                field=field_name,
                invoice_value=difference.get("invoice_value"),
                po_value=difference.get("expected_value"),
                gr_value=(
                    difference.get("expected_value")
                    if field_name == "quantity_variance"
                    else None
                ),
                severity="error",
                resolved=False,
                reason=str(difference.get("message") or field_name),
            )
        )
    run.differences = differences
    run.result = result
    run.rules_used = {
        **(run.rules_used or {}),
        "unit_price_tolerance_pct": unit_price_tolerance,
        "total_tolerance_pct": total_tolerance,
    }

    old = {
        "status": getattr(invoice, "status", "processing"),
        "match_status": invoice.match_status,
        "final_status": invoice.final_status,
        "workflow_status": getattr(invoice, "workflow_status", None),
        "final_run_id": str(invoice.final_run_id) if invoice.final_run_id else None,
    }
    current_workflow_status = (
        getattr(invoice, "workflow_status", None) or original_workflow_status
    )
    workflow_status = {
        "match_ok": "match_approved",
        "match_issues": "match_issues",
        "no_match": "no_match",
    }[result]
    confidence_needs_review = bool(decision["low_confidence_fields"])
    confidence_missing = confidence_unavailable(
        getattr(invoice, "field_confidence", None) or {}
    )
    if confidence_needs_review and not auto_reject:
        workflow_status = "verifying"
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
            reason=(
                "Confidence unavailable, please verify"
                if confidence_missing
                else "One or more extracted fields need verification"
                if confidence_needs_review
                else "Invoice match completed"
            ),
        )
    if auto_reject:
        from src.api.rest.routers.document_routers import (
            _auto_reject_unmatched_invoice,
        )

        document = (
            db.get(Document, invoice.source_document_id)
            if invoice.source_document_id
            else None
        )
        _auto_reject_unmatched_invoice(db, invoice, document)
    new = {
        "match_status": invoice.match_status,
        "match_run_id": str(run.id),
        "result": result,
    }
    invoice.final_run_id = run.id
    if not preserve_final_status:
        invoice.status = (
            "rejected"
            if auto_reject
            else "needs_review"
            if requires_verification(getattr(invoice, "field_confidence", None) or {})
            else "ready_to_pay"
            if decision["outcome"] == "ready_to_pay"
            else "needs_review"
        )
    if not preserve_final_status and invoice.status == "ready_to_pay":
        db.add(
            AuditLog(
                action="invoice_status_transition",
                actor="system",
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={"status": old.get("status", "processing")},
                new_value={"status": "ready_to_pay", "actor": "system"},
                reason="Automatic PO and receipt match completed without discrepancies",
                message="Automatic match completed without discrepancies",
                created_by=None,
            )
        )
    elif not preserve_final_status and invoice.status == "needs_review":
        db.add(
            AuditLog(
                action="invoice_status_transition",
                actor="system",
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={"status": old.get("status", "processing")},
                new_value={"status": "needs_review", "actor": "system"},
                reason=(
                    "Confidence unavailable, please verify"
                    if confidence_missing
                    else "One or more extracted fields need verification"
                    if confidence_needs_review
                    else "Matching produced discrepancy records or missing evidence"
                ),
                message="Matching requires human review",
                created_by=None,
            )
        )
    if result == "match_ok":
        new.update(workflow_status=workflow_status)
    db.add(
        AuditLog(
            action="invoice_match_run",
            actor=str(user_id) if user_id else "system",
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
            actor=str(user_id) if user_id else "system",
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
    db.flush()
    return run

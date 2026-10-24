"""
Persisted invoice-to-PO matching and approval workflow.
Flow: saved match runs and invoice status updates.
Called by: scripts/seed_demo.py, src/api/rest/routers/document_routers.py,
src/api/rest/routers/invoice_routers.py.
Calls: ap_decision_engine, audit_models, document_models, document_routers,
document_workflow_services.
Tables: audit_log, documents, goods_receipts, invoice_discrepancies, invoice_lines,
invoice_matches, invoices, match_runs, po_lines, purchase_orders.
"""

import hashlib
import json
import os
import re
import time
import uuid
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_match_models import InvoiceDiscrepancy, InvoiceMatch
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from src.services.ap_decision_engine import evaluate_invoice
from src.services.invoice_confidence_services import requires_verification
from src.services.invoice_issue_services import sync_invoice_issues
from src.services.invoice_state_services import (
    legacy_workflow_status,
    transition_invoice,
)
from src.services.po_matching_services import (
    normalize_po_number,
    normalize_po_reference,
)
from src.services.runtime_config_services import (
    PO_SUGGESTION_WEIGHTS,
    SIMILARITY_HIGH,
)
from src.services.vendor_resolution_services import normalize_vendor_tax_id


def _suggested_purchase_orders(db: Session, invoice: Invoice, owner_id=None):
    """Return the three best deterministic vendor-gated PO candidates.

    Score weights are vendor 30, item identity 30, quantity 20, and price 20.
    Decimal arithmetic is used throughout; each row includes component and
    per-line evidence suitable for persistence in InvoiceMatch.score_details.
    """
    invoice_lines = (
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    )
    po_rows = db.query(PurchaseOrder).filter(PurchaseOrder.is_current.is_(True)).all()
    tax_id = normalize_vendor_tax_id(
        getattr(invoice, "vendor_tax_id_raw", None)
    )
    weights = PO_SUGGESTION_WEIGHTS
    tolerance = Decimal(os.getenv("AP_PRICE_TOLERANCE_PERCENT", "2")) / 100
    scored = []

    def normalized_description(value):
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
            if token
        }

    scored = []
    for po in po_rows:
        po_vendor = getattr(po, "vendor", None)
        invoice_vendor_id = getattr(invoice, "vendor_id", None)
        vendor_exact = bool(invoice_vendor_id and po.vendor_id == invoice_vendor_id)
        tax_exact = bool(
            tax_id
            and po_vendor
            and normalize_vendor_tax_id(po_vendor.tax_id) == tax_id
        )
        if not vendor_exact and not tax_exact:
            continue
        po_lines = (
            db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        )
        receipts = db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all()
        accepted_by_line = {}
        if receipts:
            from src.models.goods_receipt_models import GoodsReceiptLine

            receipt_lines = (
                db.query(GoodsReceiptLine)
                .filter(
                    GoodsReceiptLine.goods_receipt_id.in_([row.id for row in receipts])
                )
                .all()
            )
            for receipt_line in receipt_lines:
                accepted_by_line[receipt_line.po_line_id] = accepted_by_line.get(
                    receipt_line.po_line_id, Decimal(0)
                ) + Decimal(str(receipt_line.accepted_qty or 0))

        details = []
        identity_total = Decimal(0)
        quantity_passes = 0
        price_passes = 0
        for invoice_line in invoice_lines:
            sku = normalize_po_number(invoice_line.item_code)
            exact = [
                row
                for row in po_lines
                if sku and normalize_po_number(row.item_code) == sku
            ]
            match = min(exact, key=lambda row: str(row.id)) if exact else None
            identity = Decimal(1) if match else Decimal(0)
            match_method = "sku_exact" if match else None
            overlap = Decimal(0)
            if match is None:
                invoice_tokens = normalized_description(invoice_line.description)
                description_matches = []
                for row in po_lines:
                    po_tokens = normalized_description(row.description)
                    union = invoice_tokens | po_tokens
                    ratio = (
                        Decimal(len(invoice_tokens & po_tokens)) / Decimal(len(union))
                        if union
                        else Decimal(0)
                    )
                    description_matches.append((ratio, str(row.id), row))
                if description_matches:
                    overlap, _line_id, candidate_line = max(
                        description_matches, key=lambda item: (item[0], item[1])
                    )
                    if overlap > 0:
                        match = candidate_line
                        identity = overlap
                        match_method = "description_token_overlap"
            identity_total += identity
            invoice_qty = (
                Decimal(str(invoice_line.qty)) if invoice_line.qty is not None else None
            )
            comparison_qty = None
            quantity_ok = False
            price_ok = False
            invoice_price = (
                Decimal(str(invoice_line.unit_price))
                if invoice_line.unit_price is not None
                else None
            )
            po_price = (
                Decimal(str(match.unit_price))
                if match and match.unit_price is not None
                else None
            )
            price_delta = None
            price_limit = None
            if match is not None:
                ordered_qty = Decimal(str(match.qty)) if match.qty is not None else None
                received_qty = accepted_by_line.get(match.id)
                quantities = [
                    value for value in (ordered_qty, received_qty) if value is not None
                ]
                comparison_qty = max(quantities) if quantities else None
                quantity_ok = (
                    invoice_qty is not None
                    and comparison_qty is not None
                    and invoice_qty <= comparison_qty
                )
                if quantity_ok:
                    quantity_passes += 1
                if invoice_price is not None and po_price is not None:
                    price_delta = abs(invoice_price - po_price)
                    price_limit = abs(po_price) * tolerance
                    price_ok = price_delta <= price_limit
                    if price_ok:
                        price_passes += 1
            details.append(
                {
                    "invoice_line_id": str(invoice_line.id),
                    "po_line_id": str(match.id) if match else None,
                    "match_method": match_method,
                    "description_overlap": str(overlap),
                    "identity_score": str(identity * weights["item_identity"]),
                    "invoice_quantity": str(invoice_qty)
                    if invoice_qty is not None
                    else None,
                    "comparison_quantity": str(comparison_qty)
                    if comparison_qty is not None
                    else None,
                    "ordered_quantity": str(ordered_qty)
                    if ordered_qty is not None
                    else None,
                    "accepted_quantity": str(received_qty)
                    if received_qty is not None
                    else None,
                    "quantity_pass": quantity_ok,
                    "invoice_unit_price": str(invoice_price)
                    if invoice_price is not None
                    else None,
                    "po_unit_price": str(po_price) if po_price is not None else None,
                    "price_difference": str(price_delta)
                    if price_delta is not None
                    else None,
                    "price_tolerance": str(price_limit)
                    if price_limit is not None
                    else None,
                    "price_pass": price_ok,
                }
            )
        line_count = len(invoice_lines)
        divisor = Decimal(line_count) if line_count else Decimal(1)
        components = {
            "vendor": weights["vendor"],
            "item_identity": weights["item_identity"] * identity_total / divisor,
            "quantity": weights["quantity"] * Decimal(quantity_passes) / divisor,
            "price": weights["price"] * Decimal(price_passes) / divisor,
        }
        score = sum(components.values(), Decimal(0)).quantize(Decimal("0.01"))
        breakdown = {
            "score": str(score),
            "best": False,
            "vendor_match": "vendor_id" if vendor_exact else "tax_id",
            "weights": {key: str(value) for key, value in weights.items()},
            "components": {key: str(value) for key, value in components.items()},
            "lines_matched": (
                f"{sum(1 for row in details if row['match_method'])} of {line_count}"
            ),
            "line_count": line_count,
            "lines": details,
            "similarity_high": str(SIMILARITY_HIGH),
        }
        scored.append((po, score, breakdown))

    scored.sort(key=lambda row: (-row[1], row[0].po_number.casefold(), str(row[0].id)))
    top = scored[:3]
    if top:
        gap = top[0][1] - top[1][1] if len(top) > 1 else Decimal(100)
        top[0][2]["gap_to_next"] = str(gap)
        top[0][2]["best"] = top[0][1] >= SIMILARITY_HIGH and gap >= Decimal(10)
    return top


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
    """
    Persist the initial match or an explicitly requested rerun.
    Inputs: db (Session), invoice_id, user_id, force (bool), idempotent (bool), trigger
        (str | None).
    Output: MatchRun | None.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: reason codes MISSING_GR, MISSING_PO; statuses approved, match_approved,
        match_issues, match_ok, matching, needs_review, no_match, on_hold.
    """
    unit_price_tolerance = f"{os.getenv('INVOICE_UNIT_PRICE_TOLERANCE_PCT', '2')}%"
    total_tolerance = f"{os.getenv('INVOICE_TOTAL_TOLERANCE_PCT', '1')}%"
    # Step 1: Load the invoice and reuse a saved run unless a rerun was requested.
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
    original_po_decision = getattr(invoice, "po_decision", None)
    preserve_final_status = original_workflow_status in {
        "paid",
        "rejected",
        "on_hold",
    } or getattr(invoice, "status", None) in {"paid", "rejected", "on_hold"}
    po_link_started = time.perf_counter()
    # Step 2: Evaluate current PO, receipt, currency, totals, and confidence evidence.
    decision = evaluate_invoice(db, invoice, owner_id=user_id)
    matched_po_ids = decision["inputs_summary"].get("po_ids")
    if matched_po_ids is not None:
        invoice.po_id = (
            uuid.UUID(matched_po_ids[0]) if len(matched_po_ids) == 1 else None
        )
    has_po_number = bool(
        normalize_po_reference(getattr(invoice, "po_number_raw", None))
    )
    invoice_only = getattr(invoice, "po_decision", None) == "invoice_only"
    requested_decision = getattr(invoice, "po_decision", None)
    suggestions = (
        _suggested_purchase_orders(db, invoice, user_id)
        if not has_po_number
        and not invoice_only
        and trigger != "suggestions_rejected"
        and (requested_decision != "waiting_for_po" or trigger == "dependency_arrived")
        else []
    )
    po_not_received = "PO_NOT_RECEIVED" in {
        item.get("code") for item in decision["discrepancies"]
    }
    decision_state = (
        "invoice_only"
        if invoice_only
        else "waiting_for_po"
        if requested_decision == "waiting_for_po"
        and trigger != "dependency_arrived"
        and (not has_po_number or po_not_received)
        else "waiting_for_po"
        if has_po_number and po_not_received
        else "waiting_for_po"
        if not has_po_number
        and getattr(invoice, "po_decision", None) == "waiting_for_po"
        and trigger != "dependency_arrived"
        else "awaiting_confirmation"
        if not has_po_number
        else None
    )
    invoice.po_decision = decision_state
    decision["po_decision"] = decision_state
    decision["inputs_summary"]["po_decision"] = decision_state
    decision["inputs_summary"]["po_suggestions"] = [
        {"po_id": str(po.id), "score": str(score)}
        for po, score, _details in suggestions
    ]
    if not has_po_number and not invoice_only:
        decision["discrepancies"] = [
            item
            for item in decision["discrepancies"]
            if item.get("code") not in {"PO_NOT_RECEIVED", "MISSING_PO"}
        ]
        confirmation = {
            "code": "PO_CONFIRMATION_REQUIRED",
            "message": "Select a suggested purchase order or choose to wait for a PO.",
            "severity": "info",
        }
        decision["discrepancies"].append(confirmation)
        decision["outcome"] = "needs_review"
    elif invoice_only:
        decision["discrepancies"] = [
            item
            for item in decision["discrepancies"]
            if item.get("code") != "PO_NOT_RECEIVED"
        ]
    if decision_state == "waiting_for_po":
        decision["outcome"] = "needs_review"
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
    if original_po_decision != decision_state:
        db.add(
            AuditLog(
                action="invoice_po_decision_changed",
                actor="system" if trigger else str(user_id or "system"),
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={"po_decision": original_po_decision},
                new_value={"po_decision": decision_state},
                reason=(
                    "A related purchase order arrived."
                    if trigger == "dependency_arrived"
                    else "The invoice PO path was evaluated."
                ),
                message="Invoice PO decision state changed",
                created_by=run_actor_id,
            )
        )
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
    result = "match_ok" if decision["outcome"] == "ready_to_pay" else "match_issues"

    # Step 3: Save the explainable decision and its discrepancy rows as one run.
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
            "severity": item.get("severity", "error"),
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
        exchange_rate_id=(decision["inputs_summary"].get("exchange_rate_id")),
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

    # Step 4: Persist the selected exact PO or the unlinked candidate evidence.
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
        invoice.match_status = "match_issues" if result != "match_ok" else "pending"
    else:
        db.query(InvoiceMatch).filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.status == "suggested",
        ).delete(synchronize_session=False)
        for po, score, details in suggestions:
            db.add(
                InvoiceMatch(
                    invoice_id=invoice.id,
                    po_id=po.id,
                    match_type="2-way",
                    score=score,
                    status="suggested",
                    score_details=details,
                )
            )
        invoice.match_status = (
            "waiting_for_po"
            if decision_state == "waiting_for_po"
            else "needs_po_confirmation"
            if decision_state == "awaiting_confirmation"
            else "match_issues"
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
                severity=difference.get("severity", "error"),
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
    # Step 6: Apply confidence and match outcomes without overwriting final decisions.
    workflow_status = (
        "needs_approval"
        if decision["outcome"] == "needs_approval"
        else "match_approved"
        if result == "match_ok"
        else "match_issues"
    )
    confidence_needs_review = bool(decision["low_confidence_fields"])
    if confidence_needs_review:
        workflow_status = "verifying"
    blocking_reasons = [
        item
        for item in decision["discrepancies"]
        if item.get("severity") != "info"
    ]
    decision_reason = (
        f"Waiting: PO {getattr(invoice, 'po_number_raw', '')} not found"
        if decision_state == "waiting_for_po"
        else "Waiting: choose a purchase order suggestion"
        if decision_state == "awaiting_confirmation"
        else "Waiting: invoice-only approval is required"
        if decision_state == "invoice_only"
        else (
            f"{blocking_reasons[0].get('code')}: "
            f"{blocking_reasons[0].get('message')}"
        )
        if blocking_reasons
        else "Invoice match completed"
    )
    if current_workflow_status not in {
        "paid",
        "rejected",
        "on_hold",
        workflow_status,
    }:
        transition_invoice(
            db,
            invoice,
            workflow_status,
            changed_by=run_actor_id,
            reason=decision_reason,
        )
    new = {
        "match_status": invoice.match_status,
        "match_run_id": str(run.id),
        "result": result,
        "status": invoice.status,
        "po_decision": invoice.po_decision,
    }
    invoice.final_run_id = run.id
    if not preserve_final_status:
        invoice.status = (
            "needs_approval"
            if decision["outcome"] == "needs_approval"
            else "ready_to_pay"
            if decision["outcome"] == "ready_to_pay"
            and not requires_verification(
                getattr(invoice, "field_confidence", None) or {}
            )
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
                reason=decision_reason,
                message="Matching requires human review",
                created_by=None,
            )
        )
    if result == "match_ok":
        new.update(workflow_status=workflow_status)
    db.add(
        AuditLog(
            action="invoice_match_run",
            actor=str(run_actor_id) if run_actor_id else "system",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value=new,
            result="success" if result == "match_ok" else "warning",
            message=f"Invoice match completed: {result}",
            created_by=run_actor_id,
        )
    )
    db.add(
        AuditLog(
            action="pipeline_step",
            actor=str(run_actor_id) if run_actor_id else "system",
            entity_type="invoice",
            entity_id=invoice.id,
            step_name="match",
            duration_ms=max(0, int((time.perf_counter() - match_started) * 1000)),
            input_ref=str(invoice.id),
            output_summary={"result": result, "differences": len(differences)},
            result="success" if result == "match_ok" else "warning",
            message="Invoice match evaluated",
            created_by=run_actor_id,
        )
    )
    db.flush()
    return run

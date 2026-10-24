"""
Application module for invoice routers.
Flow: invoice matching, review, approval, and payment.
Called by: src/api/rest/routers/command_center_routers.py.
Calls: audit_models, dependencies, dispatch, document_models,
document_workflow_schemas.
Tables: audit_log, documents, email_draft_events, email_drafts, follow_ups,
goods_receipts, invoice_discrepancies, invoice_issues, invoice_lines,
invoice_matches, invoice_state_history, invoice_supporting_documents, invoices,
match_runs, notifications, payments, purchase_orders, users, vendors.
"""

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import Float, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from src.api.rest.dependencies import get_current_user, require_role
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_draft_models import EmailDraft, EmailDraftEvent
from src.models.follow_up_models import FollowUp
from src.models.goods_receipt_models import GoodsReceipt
from src.models.invoice_issue_models import InvoiceIssue
from src.models.invoice_match_models import InvoiceDiscrepancy, InvoiceMatch
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.invoice_supporting_document_models import InvoiceSupportingDocument
from src.models.match_run_models import MatchRun
from src.models.notification_models import Notification
from src.models.payment_models import Payment
from src.models.purchase_order_models import PurchaseOrder
from src.models.user_models import User
from src.models.vendor_models import Vendor
from src.schemas.document_workflow_schemas import InvoiceRecommendation
from src.schemas.invoice_schemas import (
    InvoiceDashboardItem,
    InvoiceDashboardResponse,
    InvoiceFinalStatus,
    InvoiceMatchStatus,
    InvoiceReviewRequest,
    InvoiceReviewResponse,
    InvoiceStatus,
    InvoiceWorkflowStatus,
)
from src.services.document_workflow_services import (  # noqa: F401
    recommend_invoice_review,
)
from src.services.invoice_comparison_services import saved_comparison_checks
from src.services.invoice_confidence_services import (
    confidence_level,
    confidence_threshold,
    confidence_unavailable,
    overall_confidence_percent,
    requires_verification,
)
from src.services.invoice_issue_services import _assignment
from src.services.invoice_matching_services import run_match_for_invoice
from src.services.invoice_number_services import (
    find_duplicate_invoice,
    normalize_invoice_no,
)
from src.services.invoice_state_services import (
    legacy_workflow_status,
    transition_invoice,
)
from src.services.runtime_config_services import require_goods_receipt
from src.services.vendor_resolution_services import resolve_vendor
from src.worker.dispatch import dispatch_queued_notifications

router = APIRouter(prefix="/api/invoices", tags=["Invoices"])


def _app_local_today() -> date:
    try:
        zone = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
    except Exception:
        zone = ZoneInfo("Asia/Kolkata")
    return datetime.now(zone).date()


def _owner_id(user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _overall_invoice_confidence(confidence: dict | None) -> str:
    """Return the display level using the minimum invoice field confidence.

    Missing or derived confidence stays unscored in the UI; matching still
    treats it as unverified and applies the 85-point gate.
    """
    level = confidence_level(confidence or {})
    return level if level in {"high", "medium", "low", "unknown"} else "unknown"


def _checked_invoice_number(db, vendor_id, invoice_no, *, exclude_invoice_id=None):
    if vendor_id is not None and not isinstance(vendor_id, uuid.UUID):
        try:
            vendor_id = uuid.UUID(str(vendor_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid vendor ID") from exc
    normalized = normalize_invoice_no(invoice_no)
    duplicate = find_duplicate_invoice(
        db,
        vendor_id,
        normalized,
        exclude_invoice_id=exclude_invoice_id,
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail="An invoice with this vendor and invoice number already exists",
        )
    return normalized


def _invoice_owner_filter(owner_id: uuid.UUID):
    return or_(
        Invoice.owner_user_id == owner_id,
        Invoice.created_by == owner_id,
        Document.created_by == owner_id,
        Document.source.in_(["email", "email_poll"]),
    )


@router.post("/{invoice_id}/three-way-match", dependencies=[Depends(get_current_user)])
def three_way_match_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Handle three way match invoice.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: dict.
    Effects: reads database rows; writes or commits database rows.
    Results: reason codes MISSING_GR; statuses matching, processed, ready_to_pay.
    """
    owner_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    run = run_match_for_invoice(db, invoice.id, owner_id, force=True)
    db.commit()
    outcome = ((run.rules_used or {}).get("match_outcome") if run else None) or {}
    issues = outcome.get("discrepancies", [])
    ready = outcome.get("outcome") == "ready_to_pay"
    supporting = recommend_invoice_review(db, invoice, owner_id=owner_id)
    reason_codes = [item.get("code", "") for item in issues]
    next_action = (
        "Continue with payment approval."
        if ready
        else "Review the saved discrepancies and verify the invoice."
    )
    missing_documents = ["goods_receipt"] if "MISSING_GR" in reason_codes else []
    api_checks = [
        {
            **item,
            "name": item.get("code", "matching").casefold(),
            "outcome": "mismatch",
            "severity": "error",
            "actual": item.get("invoice_value"),
            "expected": item.get("expected_value"),
            "reason": item.get("message", "Matching requires review."),
        }
        for item in issues
    ]
    return {
        "invoice_id": str(invoice.id),
        "match_run_id": str(run.id) if run else None,
        "rule_set_version": outcome.get("rule_set_version"),
        "processing_status": getattr(supporting, "processing_status", None)
        or "processed",
        "matching_status": "matched" if ready else "review_required",
        "review_status": "clear" if ready else "review_required",
        "approval_status": "ready_to_pay" if ready else "not_approved",
        "payment_status": "unknown",
        "supporting_records": [
            item.model_dump(mode="json")
            for item in getattr(supporting, "supporting_records", [])
        ],
        "missing_document_types": missing_documents,
        "candidates": [
            item.model_dump(mode="json")
            for item in getattr(supporting, "candidates", [])
        ],
        "matched": ready,
        "requires_review": not ready,
        "issues": [item.get("message") for item in issues],
        "status": "matched" if ready else "review_required",
        "checks": api_checks,
        "discrepancies": api_checks,
        "reason_codes": reason_codes,
        "next_recommended_action": next_action,
        "human_review_required": not ready,
        "available_documents": [
            item.model_dump(mode="json")
            for item in getattr(supporting, "supporting_records", [])
        ],
        "recommended_next_actions": [next_action],
        "human_decision_required": not ready,
        "invoice_approval_required": True,
    }


@router.get(
    "/{invoice_id}/recommendation",
    response_model=InvoiceRecommendation,
    dependencies=[Depends(get_current_user)],
)
def get_invoice_recommendation(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> InvoiceRecommendation:
    """
    Return get invoice recommendation.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: InvoiceRecommendation.
    Effects: reads database rows.
    Results: reason codes MISSING_GR; statuses matching, ready_to_pay.
    """
    owner_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    run = db.get(MatchRun, invoice.final_run_id) if invoice.final_run_id else None
    outcome = ((run.rules_used or {}).get("match_outcome") if run else None) or {}
    codes = [item.get("code", "") for item in outcome.get("discrepancies", [])]
    checks = []
    for item in outcome.get("discrepancies", []):
        checks.append(
            {
                "name": item.get("code", "matching").lower(),
                "reason_code": item.get("code"),
                "outcome": "mismatch",
                "severity": "error",
                "actual": str(item.get("invoice_value"))
                if item.get("invoice_value") is not None
                else None,
                "expected": str(item.get("expected_value"))
                if item.get("expected_value") is not None
                else None,
                "difference": str(item.get("difference"))
                if item.get("difference") is not None
                else None,
                "reason": item.get("message", "Matching requires review."),
            }
        )
    ready = outcome.get("outcome") == "ready_to_pay"
    return InvoiceRecommendation(
        invoice_id=invoice.id,
        processing_status="completed" if run else "not_matched",
        matching_status="matched" if ready else "review_required",
        review_status="clear" if ready else "review_required",
        approval_status="ready_to_pay" if ready else "not_approved",
        supporting_records=[],
        missing_document_types=["goods_receipt"] if "MISSING_GR" in codes else [],
        candidates=[],
        checks=checks,
        discrepancies=checks,
        reason_codes=codes,
        next_recommended_action=(
            "Continue with payment approval."
            if ready
            else "Review the saved discrepancies and verify the invoice."
        ),
        human_review_required=not ready,
    )


_UNRESOLVED_MATCH_STATUSES = {
    "pending",
    "needs_vendor_review",
    "waiting_for_po",
    "needs_po_confirmation",
    "match_issues",
    "no_match",
}


def _invoice_dashboard_filters(
    *,
    match_status: str | None,
    final_status: str | None,
    vendor_id: uuid.UUID | None,
    invoice_date_from: date | None,
    invoice_date_to: date | None,
    due_date: date | None,
    due_date_from: date | None,
    due_date_to: date | None,
    search: str | None,
    review_queue: bool,
    owner_id: uuid.UUID,
    workflow_status: str | None = None,
    bucket: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    gl_code: str | None = None,
    confidence_min: float | None = None,
) -> list:
    filters = [_invoice_owner_filter(owner_id)]
    if match_status:
        filters.append(Invoice.match_status == match_status)
    if final_status:
        filters.append(Invoice.final_status == final_status)
    if vendor_id:
        filters.append(Invoice.vendor_id == vendor_id)
    if workflow_status:
        filters.append(Invoice.workflow_status == workflow_status)
    if bucket:
        filters.append(Document.doc_type == bucket)
    if amount_min is not None:
        filters.append(Invoice.total_amount >= amount_min)
    if amount_max is not None:
        filters.append(Invoice.total_amount <= amount_max)
    if gl_code:
        filters.append(Invoice.gl_code == gl_code)
    if confidence_min is not None:
        total_confidence = cast(
            Invoice.field_confidence["total"]["confidence"].as_float(), Float
        )
        filters.append(total_confidence >= confidence_min)
    if invoice_date_from:
        filters.append(Invoice.invoice_date >= invoice_date_from)
    if invoice_date_to:
        filters.append(Invoice.invoice_date <= invoice_date_to)
    if due_date:
        filters.append(Invoice.due_date == due_date)
    if due_date_from:
        filters.append(Invoice.due_date >= due_date_from)
    if due_date_to:
        filters.append(Invoice.due_date <= due_date_to)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Invoice.invoice_no.ilike(pattern),
                Invoice.vendor_name_raw.ilike(pattern),
                Vendor.name.ilike(pattern),
            )
        )
    if review_queue:
        filters.append(Invoice.final_status.is_(None))
    return filters


def _invoice_dashboard_response(
    db: Session,
    *,
    page: int,
    page_size: int,
    filters: list,
    sort_by: str = "received_date",
    sort_dir: str | None = None,
) -> InvoiceDashboardResponse:
    total = (
        db.query(func.count(Invoice.id))
        .select_from(Invoice)
        .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .scalar()
    )
    sort_columns = {
        "received_date": Invoice.created_at,
        "date_received": Invoice.created_at,
        "amount": Invoice.total_amount,
        "due_date": Invoice.due_date,
        "days_overdue": Invoice.due_date,
        "confidence": cast(
            Invoice.field_confidence["total"]["confidence"].as_float(), Float
        ),
    }
    sort_column = sort_columns.get(sort_by, Invoice.created_at)
    default_desc = sort_by in {"received_date", "date_received", "amount"}
    descending = (sort_dir == "desc") if sort_dir else default_desc
    ordering = sort_column.desc() if descending else sort_column.asc()
    if sort_by in {"amount", "due_date", "days_overdue", "confidence"}:
        ordering = ordering.nullslast()
    rows = (
        db.query(Invoice, Vendor.name.label("vendor_name"), Document)
        .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .order_by(ordering, Invoice.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return InvoiceDashboardResponse(
        items=[
            InvoiceDashboardItem(
                invoice_id=invoice.id,
                source_document_id=invoice.source_document_id,
                received_at=(
                    getattr(document, "email_received_at", None)
                    or getattr(document, "created_at", None)
                ),
                processing_status=(
                    document.status if isinstance(document, Document) else document
                ),
                source=(
                    "email"
                    if getattr(document, "source", None) == "email_poll"
                    else getattr(document, "source", None)
                ),
                email_sender=getattr(document, "email_sender", None),
                email_subject=getattr(document, "email_subject", None),
                invoice_no=invoice.invoice_no,
                vendor_id=getattr(invoice, "vendor_id", None),
                vendor_name=vendor_name or invoice.vendor_name_raw,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                total_amount=invoice.total_amount,
                currency=invoice.currency,
                match_status=invoice.match_status,
                final_status=invoice.final_status,
                workflow_status=getattr(invoice, "workflow_status", "received"),
                gl_code=getattr(invoice, "gl_code", None),
                confidence=(
                    (getattr(invoice, "field_confidence", None) or {})
                    .get("total", {})
                    .get("confidence")
                ),
                confidence_level=_overall_invoice_confidence(
                    getattr(invoice, "field_confidence", None) or {}
                ),
                overall_confidence_percent=overall_confidence_percent(
                    getattr(invoice, "field_confidence", None) or {}
                ),
                confidence_status=(
                    "not_scored"
                    if confidence_unavailable(
                        getattr(invoice, "field_confidence", None) or {}
                    )
                    else "scored"
                ),
                can_resubmit=getattr(invoice, "workflow_status", None) == "rejected",
                status=getattr(invoice, "status", None) or _workspace_status(invoice),
                po_decision=getattr(invoice, "po_decision", None),
            )
            for invoice, vendor_name, document in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


def _workspace_status(invoice: Invoice) -> str:
    if getattr(invoice, "status", None):
        return invoice.status
    if invoice.final_status == "rejected":
        return "rejected"
    if invoice.final_status == "on_hold":
        return "on_hold"
    if invoice.final_status == "approved":
        return "ready_to_pay"
    if getattr(invoice, "workflow_status", None) in {
        "match_approved",
        "approved",
        "paid",
    }:
        return "ready_to_pay"
    if getattr(invoice, "workflow_status", None) in {
        "match_issues",
        "no_match",
        "verifying",
    }:
        return "needs_review"
    if invoice.match_status in {"match_issues", "no_match", "needs_vendor_review"}:
        return "needs_review"
    return "processing"


def _dashboard_query_parameters(
    *,
    match_status: InvoiceMatchStatus | None,
    final_status: InvoiceFinalStatus | None,
    vendor_id: uuid.UUID | None,
    invoice_date_from: date | None,
    invoice_date_to: date | None,
    due_date: date | None,
    due_date_from: date | None,
    due_date_to: date | None,
    search: str | None,
    review_queue: bool,
    owner_id: uuid.UUID,
    workflow_status: str | None = None,
    bucket: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    gl_code: str | None = None,
    confidence_min: float | None = None,
) -> list:
    if invoice_date_from and invoice_date_to and invoice_date_from > invoice_date_to:
        raise HTTPException(
            status_code=422,
            detail="invoice_date_from must not be after invoice_date_to",
        )
    if due_date_from and due_date_to and due_date_from > due_date_to:
        raise HTTPException(
            status_code=422,
            detail="due_date_from must not be after due_date_to",
        )
    if due_date and (due_date_from or due_date_to):
        raise HTTPException(
            status_code=422,
            detail="Use due_date or due_date_from/due_date_to, not both",
        )
    if search is not None and not search.strip():
        raise HTTPException(
            status_code=422,
            detail="search must not be blank",
        )
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(
            status_code=422, detail="amount_min must not exceed amount_max"
        )
    if confidence_min is not None and not 0 <= confidence_min <= 100:
        raise HTTPException(
            status_code=422, detail="confidence_min must be from 0 to 100"
        )

    return _invoice_dashboard_filters(
        match_status=match_status,
        final_status=final_status,
        vendor_id=vendor_id,
        invoice_date_from=invoice_date_from,
        invoice_date_to=invoice_date_to,
        due_date=due_date,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        review_queue=review_queue,
        owner_id=owner_id,
        workflow_status=workflow_status,
        bucket=bucket,
        amount_min=amount_min,
        amount_max=amount_max,
        gl_code=gl_code,
        confidence_min=confidence_min,
    )


@router.get(
    "",
    response_model=InvoiceDashboardResponse,
    dependencies=[Depends(get_current_user)],
)
def list_invoices(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    match_status: InvoiceMatchStatus | None = None,
    status: InvoiceStatus | None = None,
    source: Literal["email", "upload"] | None = None,
    final_status: InvoiceFinalStatus | None = None,
    vendor_id: uuid.UUID | None = None,
    po_id: uuid.UUID | None = None,
    invoice_date_from: date | None = None,
    invoice_date_to: date | None = None,
    due_date: date | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    owner_user_id: uuid.UUID | None = None,
    overdue: bool = False,
    sort_by: Annotated[
        str,
        Query(
            pattern=(
                "^(received_date|date_received|amount|due_date|"
                "days_overdue|confidence)$"
            )
        ),
    ] = "date_received",
    sort_dir: Literal["asc", "desc"] | None = None,
    workflow_status: InvoiceWorkflowStatus | None = None,
    bucket: Annotated[str | None, Query(max_length=40)] = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    gl_code: Annotated[str | None, Query(max_length=60)] = None,
    confidence_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> InvoiceDashboardResponse:
    """
    List list invoices.
    Inputs: page (Annotated[int, Query(ge=1)]), page_size (Annotated[int, Query(ge=1,
        le=100)]), match_status (InvoiceMatchStatus | None), status (InvoiceStatus |
        None), source (Literal["email", "upload"] | None), final_status
        (InvoiceFinalStatus | None), vendor_id (uuid.UUID | None), po_id (uuid.UUID |
        None), invoice_date_from (date | None), ...
    Output: InvoiceDashboardResponse.
    Effects: reads database rows.
    Results: statuses approved, rejected.
    """
    filters = _dashboard_query_parameters(
        match_status=match_status,
        final_status=final_status,
        vendor_id=vendor_id,
        invoice_date_from=invoice_date_from,
        invoice_date_to=invoice_date_to,
        due_date=due_date,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        review_queue=False,
        owner_id=_owner_id(current_user),
        workflow_status=workflow_status,
        bucket=bucket,
        amount_min=amount_min,
        amount_max=amount_max,
        gl_code=gl_code,
        confidence_min=confidence_min,
    )
    if owner_user_id:
        filters.append(Invoice.owner_user_id == owner_user_id)
    if po_id:
        filters.append(Invoice.po_id == po_id)
    if status:
        filters.append(Invoice.status == status)
    if source:
        if source == "email":
            filters.append(Document.source.in_(["email", "email_poll"]))
        else:
            filters.append(Document.source == source)
    if overdue:
        filters.extend(
            [
                Invoice.due_date < _app_local_today(),
                or_(
                    Invoice.final_status.is_(None),
                    Invoice.final_status.notin_(["approved", "rejected"]),
                ),
            ]
        )
    return _invoice_dashboard_response(
        db,
        page=page,
        page_size=page_size,
        filters=filters,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.get("/{invoice_id:uuid}/verification")
def get_invoice_verification(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Return get invoice verification.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: dict.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    owner_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    confidence = getattr(invoice, "field_confidence", None) or {}
    values = {
        "vendor": invoice.vendor_name_raw,
        "invoice_number": invoice.invoice_no,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "currency": invoice.currency,
        "subtotal": invoice.subtotal,
        "tax": invoice.tax_amount,
        "total": invoice.total_amount,
        "order_number": invoice.po_number_raw,
        "gl_code": getattr(invoice, "gl_code", None),
    }
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    gl_codes = {
        code.strip().casefold()
        for code in os.getenv("GL_CODE_LIST", "").split(",")
        if code.strip()
    }
    return {
        "invoice_id": str(invoice.id),
        "fields": {
            key: {
                "value": value.isoformat() if hasattr(value, "isoformat") else value,
                **(confidence.get(key) or {"confidence": 0, "derived": True}),
            }
            for key, value in values.items()
        },
        "line_items": [
            {
                "id": str(line.id),
                "qty": line.qty,
                "unit_price": line.unit_price,
                "line_amount": line.line_amount,
                "confidence": line.field_confidence or {},
            }
            for line in lines
        ],
        "needs_verification": requires_verification(confidence),
        "confidence_level": confidence_level(confidence),
        "overall_confidence_percent": overall_confidence_percent(confidence),
        "confidence_status": (
            "not_scored" if confidence_unavailable(confidence) else "scored"
        ),
        "threshold": confidence_threshold(),
        "gl_code_warning": (
            f"Unknown GL code: {invoice.gl_code}"
            if gl_codes
            and invoice.gl_code
            and invoice.gl_code.casefold() not in gl_codes
            else None
        ),
    }


@router.post("/{invoice_id:uuid}/verify")
def verify_invoice_fields(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Handle verify invoice fields.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: dict.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries; may queue or retry background work.
    Results: statuses matching, no_match, rejected, resubmitted, verifying.
    """
    actor_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(actor_id))
        .with_for_update()
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if legacy_workflow_status(invoice) not in {"verifying", "rejected"}:
        raise HTTPException(
            status_code=409,
            detail="Invoice is not waiting for field verification",
        )
    fields = payload.get(
        "confirmed_values",
        payload.get("values", payload.get("fields", payload.get("edits", {}))),
    )
    if not isinstance(fields, dict):
        raise HTTPException(status_code=422, detail="values must be an object")
    reason = str(payload.get("reason") or "").strip()
    if fields and not reason:
        raise HTTPException(status_code=422, detail="A reason is required for edits")
    allowed = {
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
    }
    unknown = set(fields) - allowed - {"line_items"}
    if unknown:
        raise HTTPException(status_code=422, detail="Unsupported invoice field")
    line_edits = fields.get("line_items", [])
    if not isinstance(line_edits, list):
        raise HTTPException(status_code=422, detail="line_items must be a list")
    candidate_vendor_id = invoice.vendor_id
    if "vendor" in fields:
        candidate_vendor = resolve_vendor(db, fields.get("vendor"))
        candidate_vendor_id = candidate_vendor.id if candidate_vendor else None
    normalized_invoice_no = _checked_invoice_number(
        db,
        candidate_vendor_id,
        fields.get("invoice_number", invoice.invoice_no),
        exclude_invoice_id=invoice.id,
    )
    columns = {
        "invoice_number": "invoice_no",
        "invoice_date": "invoice_date",
        "due_date": "due_date",
        "currency": "currency",
        "subtotal": "subtotal",
        "tax": "tax_amount",
        "total": "total_amount",
        "order_number": "po_number_raw",
        "gl_code": "gl_code",
    }
    old_workflow = legacy_workflow_status(invoice)
    for field, value in fields.items():
        if field == "line_items":
            continue
        attr = "vendor_name_raw" if field == "vendor" else columns[field]
        old_value = getattr(invoice, attr)
        if field in {"invoice_date", "due_date"} and value:
            try:
                value = date.fromisoformat(str(value))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid {field}") from exc
        elif field in {"subtotal", "tax", "total"} and value is not None:
            try:
                value = Decimal(str(value))
            except InvalidOperation as exc:
                raise HTTPException(status_code=422, detail=f"Invalid {field}") from exc
        elif value is not None:
            value = str(value).strip()
        setattr(invoice, attr, value)
        if field == "vendor":
            vendor = resolve_vendor(db, value)
            invoice.vendor_id = vendor.id if vendor else None
        db.add(
            AuditLog(
                action="invoice_field_edited",
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={field: str(old_value) if old_value is not None else None},
                new_value={
                    field: str(value) if value is not None else None,
                    "reason": reason,
                },
                result="success",
                message=f"Invoice {field} verified or edited",
                created_by=actor_id,
            )
        )
    invoice.invoice_no_normalized = normalized_invoice_no
    confidence = dict(invoice.field_confidence or {})
    for key, item in confidence.items():
        if key == "line_items":
            confidence[key] = [
                {name: {"confidence": 100, "derived": False} for name in entry}
                for entry in item
            ]
        elif isinstance(item, dict):
            confidence[key] = {"confidence": 100, "derived": False}
    invoice.field_confidence = confidence
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    for line_edit in line_edits:
        if not isinstance(line_edit, dict):
            raise HTTPException(
                status_code=422, detail="Each line edit must be an object"
            )
        if set(line_edit) - {"id", "qty", "unit_price", "line_amount"}:
            raise HTTPException(
                status_code=422, detail="Unsupported invoice line field"
            )
        line = next(
            (item for item in lines if str(item.id) == str(line_edit.get("id"))),
            None,
        )
        if line is None:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        for field in ("qty", "unit_price", "line_amount"):
            if field not in line_edit:
                continue
            old_value = getattr(line, field)
            try:
                new_value = Decimal(str(line_edit[field]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid {field}") from exc
            setattr(line, field, new_value)
            db.add(
                AuditLog(
                    action="invoice_line_field_edited",
                    entity_type="invoice",
                    entity_id=invoice.id,
                    old_value={"line_id": str(line.id), field: str(old_value)},
                    new_value={
                        "line_id": str(line.id),
                        field: str(new_value),
                        "reason": reason,
                    },
                    result="success",
                    message=f"Invoice line {field} verified or edited",
                    created_by=actor_id,
                )
            )
    for line in lines:
        line.field_confidence = {
            field: {"confidence": 100, "derived": False}
            for field in (
                line.field_confidence
                or {"qty": {}, "unit_price": {}, "line_amount": {}}
            )
        }
    if old_workflow == "rejected":
        transition_invoice(
            db,
            invoice,
            "resubmitted",
            changed_by=actor_id,
            reason=reason or "Associate reopened no-match invoice",
        )
    if legacy_workflow_status(invoice) != "matching":
        transition_invoice(
            db,
            invoice,
            "matching",
            changed_by=actor_id,
            reason=reason or "Associate verified invoice fields",
        )
    match_run = run_match_for_invoice(db, invoice.id, actor_id, force=True)
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id)
    return {
        "invoice_id": str(invoice.id),
        "workflow_status": invoice.workflow_status,
        "status": getattr(invoice, "status", None) or _workspace_status(invoice),
        "po_decision": getattr(invoice, "po_decision", None),
        "match_run_id": str(match_run.id) if match_run else None,
        "needs_verification": False,
    }


@router.get("/{invoice_id:uuid}")
def get_invoice_detail(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return get invoice detail.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: reason codes CURRENCY_MISMATCH, HEADER_TOTAL_VARIANCE, LINE_NOT_ON_PO,
        LINE_TOTAL_VARIANCE, LOW_CONFIDENCE, MISSING_GR, MISSING_PO, POSSIBLE_DUPLICATE;
        statuses received.
    """
    owner_id = _owner_id(current_user)
    row = (
        db.query(Invoice, Document)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice, document = row
    extracted = getattr(document, "extracted_json", None) or {}
    confidence = getattr(invoice, "field_confidence", None) or extracted.get(
        "field_confidence", extracted.get("confidence", {})
    )
    confidence = confidence if isinstance(confidence, dict) else {}
    low_confidence_fields = []
    normalized_confidence = {}
    for field, raw in confidence.items():
        value = raw.get("confidence") if isinstance(raw, dict) else raw
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        normalized_confidence[field] = value
        if value < confidence_threshold():
            low_confidence_fields.append(field)
    line_confidence = (
        confidence.get("line_items", []) if isinstance(confidence, dict) else []
    )
    for index, line in enumerate(line_confidence, start=1):
        if not isinstance(line, dict):
            continue
        for field, raw in line.items():
            value = raw.get("confidence") if isinstance(raw, dict) else raw
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = 0.0
            if value < confidence_threshold():
                low_confidence_fields.append(f"line_{index}.{field}")
    saved_run = (
        db.get(MatchRun, invoice.final_run_id)
        if getattr(invoice, "final_run_id", None)
        else None
    )
    saved_match_outcome = (
        (saved_run.rules_used or {}).get("match_outcome") if saved_run else None
    )
    linked_po = None
    if invoice.po_id:
        linked_po = db.scalar(
            select(PurchaseOrder)
            .options(selectinload(PurchaseOrder.lines))
            .where(
                PurchaseOrder.id == invoice.po_id,
            )
        )
    linked_goods_receipts = []
    if linked_po is not None:
        linked_goods_receipts = list(
            db.scalars(
                select(GoodsReceipt)
                .options(selectinload(GoodsReceipt.lines))
                .where(
                    GoodsReceipt.po_id == linked_po.id,
                    or_(
                        GoodsReceipt.created_by == owner_id,
                        GoodsReceipt.source_document_id.in_(
                            select(Document.id).where(Document.created_by == owner_id)
                        ),
                        GoodsReceipt.po_id == linked_po.id,
                    ),
                )
                .order_by(GoodsReceipt.receipt_date.desc().nullslast())
            ).all()
        )
    invoice_lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .order_by(InvoiceLine.id)
        .all()
    )
    saved_comparison = (
        (saved_run.rules_used or {}).get("match_outcome", {}).get("comparison", {})
        if saved_run
        else {}
    )
    match_review = saved_comparison.get("headers", [])
    line_comparisons = saved_comparison.get("lines", [])
    match_checks = saved_comparison_checks(
        saved_comparison, saved_match_outcome, require_goods_receipt()
    )
    return {
        "invoice_id": str(invoice.id),
        "match_run_id": str(saved_run.id) if saved_run else None,
        "updated_at": getattr(invoice, "updated_at", None),
        "source_document_id": str(invoice.source_document_id)
        if invoice.source_document_id
        else None,
        "source": getattr(document, "source", None),
        "email_sender": getattr(document, "email_sender", None),
        "email_subject": getattr(document, "email_subject", None),
        "invoice_no": invoice.invoice_no,
        "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
        "vendor_name": invoice.vendor_name_raw,
        "vendor_tax_id": getattr(invoice, "vendor_tax_id_raw", None),
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "currency": invoice.currency,
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "total_amount": invoice.total_amount,
        "po_id": str(invoice.po_id) if invoice.po_id else None,
        "po_number_raw": invoice.po_number_raw,
        "gl_code": getattr(invoice, "gl_code", None),
        "match_status": invoice.match_status,
        "po_decision": getattr(invoice, "po_decision", None),
        "final_status": invoice.final_status,
        "workflow_status": getattr(invoice, "workflow_status", "received"),
        "status": getattr(invoice, "status", None) or _workspace_status(invoice),
        "payment_terms": (
            extracted.get("payment_terms") if isinstance(extracted, dict) else None
        ),
        "line_items": [
            {
                "description": line.description,
                "item_code": line.item_code,
                "qty": line.qty,
                "unit_price": line.unit_price,
                "amount": line.line_amount,
            }
            for line in invoice_lines
        ],
        "match_review": match_review,
        "discrepancies": [
            {
                "id": str(item.id),
                "field": item.field,
                "invoice_value": item.invoice_value,
                "po_value": item.po_value,
                "gr_value": item.gr_value,
                "severity": item.severity,
                "resolved": item.resolved,
                "reason": item.reason,
            }
            for item in db.query(InvoiceDiscrepancy)
            .filter(
                InvoiceDiscrepancy.invoice_id == invoice.id,
            )
            .order_by(
                InvoiceDiscrepancy.resolved.asc(), InvoiceDiscrepancy.severity.desc()
            )
            .all()
        ],
        "match_outcome": saved_match_outcome,
        "match_checks": match_checks,
        "line_comparisons": line_comparisons,
        "comparison_reason_codes": saved_comparison.get("reason_codes", []),
        "payment_status": "Paid" if getattr(invoice, "paid_at", None) else "Not paid",
        "linked_purchase_order": (
            {
                "id": str(linked_po.id),
                "po_number": linked_po.po_number,
                "vendor_id": str(linked_po.vendor_id) if linked_po.vendor_id else None,
                "vendor_name": linked_po.vendor_name,
                "source_document_id": str(linked_po.source_document_id)
                if linked_po.source_document_id
                else None,
                "po_date": linked_po.po_date,
                "currency": linked_po.currency,
                "total_amount": linked_po.total_amount,
                "needs_verification": linked_po.needs_verification,
                "lines": [
                    {
                        "item_code": line.item_code,
                        "description": line.description,
                        "qty": line.qty,
                        "unit_price": line.unit_price,
                        "line_amount": line.line_amount,
                    }
                    for line in linked_po.lines
                ],
            }
            if linked_po
            else None
        ),
        "linked_goods_receipts": [
            {
                "id": str(receipt.id),
                "gr_number": receipt.gr_number,
                "source_document_id": str(receipt.source_document_id)
                if receipt.source_document_id
                else None,
                "receipt_date": receipt.receipt_date,
                "needs_verification": receipt.needs_verification,
                "lines": [
                    {
                        "item_code": line.item_code,
                        "description": line.description,
                        "received_qty": line.received_qty,
                        "accepted_qty": line.accepted_qty,
                        "rejected_qty": line.rejected_qty,
                    }
                    for line in receipt.lines
                ],
            }
            for receipt in linked_goods_receipts
        ],
        "match_rule_set_version": (
            (saved_run.rules_used or {}).get("rule_set_version") if saved_run else None
        ),
        "confidence": normalized_confidence,
        "field_confidence": getattr(invoice, "field_confidence", None)
        or normalized_confidence,
        "needs_verification": requires_verification(
            getattr(invoice, "field_confidence", None) or {}
        ),
        "confidence_level": _overall_invoice_confidence(
            getattr(invoice, "field_confidence", None) or {}
        ),
        "overall_confidence_percent": overall_confidence_percent(
            getattr(invoice, "field_confidence", None) or {}
        ),
        "confidence_status": (
            "not_scored"
            if confidence_unavailable(getattr(invoice, "field_confidence", None) or {})
            else "scored"
        ),
        "low_confidence_fields": low_confidence_fields,
        "supporting_documents_count": (
            db.query(func.count(InvoiceSupportingDocument.id))
            .filter(
                InvoiceSupportingDocument.invoice_id == invoice_id,
                InvoiceSupportingDocument.deleted_at.is_(None),
            )
            .scalar()
            or 0
        ),
    }


def _review_invoice_or_404(db: Session, invoice_id: uuid.UUID, user: dict):
    owner_id = _owner_id(user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .with_for_update(of=Invoice)
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice, owner_id


def _require_invoice_decision_role(user: dict) -> None:
    if user.get("role") not in {"admin", "associate"}:
        raise HTTPException(status_code=403, detail="Insufficient role")


def _check_invoice_decision_snapshot(invoice, expected_run_id, expected_updated_at):
    changed = (
        "This invoice changed while you were reviewing it. "
        "Refresh and review the latest data."
    )
    if expected_run_id is not None:
        saved_run_id = getattr(invoice, "final_run_id", None)
        if saved_run_id is None or saved_run_id != expected_run_id:
            raise HTTPException(status_code=409, detail=changed)
    if expected_updated_at is not None:
        current = getattr(invoice, "updated_at", None)
        if current is None:
            raise HTTPException(status_code=409, detail=changed)
        expected = expected_updated_at
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current
        expected = expected.replace(tzinfo=UTC) if expected.tzinfo is None else expected
        if current.astimezone(UTC) != expected.astimezone(UTC):
            raise HTTPException(status_code=409, detail=changed)


@router.get("/{invoice_id:uuid}/match-candidates")
def invoice_match_candidates(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle invoice match candidates.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries.
    Results: statuses needs_review, rejected.
    """
    invoice, owner_id = _review_invoice_or_404(db, invoice_id, current_user)
    rows = (
        db.query(InvoiceMatch, PurchaseOrder)
        .join(PurchaseOrder, InvoiceMatch.po_id == PurchaseOrder.id)
        .filter(
            InvoiceMatch.invoice_id == invoice.id, InvoiceMatch.status != "rejected"
        )
        .order_by(InvoiceMatch.score.desc().nullslast(), InvoiceMatch.created_at)
        .all()
    )
    return {
        "invoice_id": str(invoice.id),
        "status": invoice.status,
        "po_decision": getattr(invoice, "po_decision", None),
        "candidates": [
            {
                "match_id": str(match.id),
                "po_id": str(po.id),
                "po_number": po.po_number,
                "vendor_name": po.vendor_name,
                "total_amount": po.total_amount,
                "currency": po.currency,
                "score": match.score or 0,
                "score_details": match.score_details or {},
                "best": bool((match.score_details or {}).get("best")),
                "status": match.status,
            }
            for match, po in rows
        ],
    }


@router.post("/{invoice_id:uuid}/po-decision")
def decide_invoice_po_path(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Choose to wait for a PO or process the invoice without a PO."""
    invoice, actor_id = _review_invoice_or_404(db, invoice_id, current_user)
    decision = payload.get("decision")
    reason = str(payload.get("reason") or "").strip()
    if decision not in {"wait_for_po", "invoice_only"}:
        raise HTTPException(
            status_code=422,
            detail="decision must be wait_for_po or invoice_only",
        )
    old = {"status": invoice.status, "po_decision": invoice.po_decision}
    invoice.po_decision = (
        "waiting_for_po" if decision == "wait_for_po" else "invoice_only"
    )
    if decision == "invoice_only":
        invoice.po_number_raw = None
    run = run_match_for_invoice(db, invoice.id, actor_id, force=True)
    db.add(
        AuditLog(
            action="invoice_po_decision",
            actor=str(actor_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value={
                "status": invoice.status,
                "po_decision": invoice.po_decision,
                "match_run_id": str(run.id) if run else None,
            },
            reason=reason or "Invoice will wait for a purchase order.",
            message="Invoice PO path selected",
            created_by=actor_id,
        )
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "status": invoice.status,
        "po_decision": invoice.po_decision,
        "match_run_id": str(run.id) if run else None,
    }


@router.post("/{invoice_id:uuid}/match")
def confirm_invoice_match(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Confirm confirm invoice match.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries.
    Results: no workflow status or reason code directly.
    """
    invoice, actor_id = _review_invoice_or_404(db, invoice_id, current_user)
    try:
        po_id = uuid.UUID(str(payload.get("po_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="po_id is required") from exc
    candidate = (
        db.query(InvoiceMatch)
        .filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.po_id == po_id,
            InvoiceMatch.status == "suggested",
        )
        .first()
    )
    po = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.id == po_id, PurchaseOrder.is_current.is_(True))
        .first()
    )
    if candidate is None or po is None:
        raise HTTPException(status_code=404, detail="Suggested PO match not found")
    old_status = invoice.status
    invoice.po_number_raw = po.po_number
    invoice.po_decision = None
    candidate.status = "confirmed"
    candidate.confirmed_by = actor_id
    from src.services.invoice_matching_services import run_match_for_invoice

    run = run_match_for_invoice(db, invoice.id, actor_id, force=True)
    db.add(
        AuditLog(
            action="invoice_match_confirmed",
            actor=str(actor_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"status": old_status, "po_id": None},
            new_value={
                "status": invoice.status,
                "po_id": str(po.id),
                "match_run_id": str(run.id) if run else None,
            },
            reason=(payload.get("reason") or "Suggested purchase order confirmed"),
            created_by=actor_id,
        )
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "status": getattr(invoice, "status", _workspace_status(invoice)),
        "po_id": str(po.id),
    }


@router.post("/{invoice_id:uuid}/match/reject")
def reject_invoice_match(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Reject reject invoice match.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries.
    Results: statuses rejected.
    """
    invoice, actor_id = _review_invoice_or_404(db, invoice_id, current_user)
    try:
        po_id = uuid.UUID(str(payload.get("po_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="po_id is required") from exc
    candidate = (
        db.query(InvoiceMatch)
        .filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.po_id == po_id,
            InvoiceMatch.status == "suggested",
        )
        .first()
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="Suggested PO match not found")
    candidate.status = "rejected"
    remaining = (
        db.query(InvoiceMatch)
        .filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.status == "suggested",
            InvoiceMatch.id != candidate.id,
        )
        .count()
    )
    if remaining == 0:
        invoice.po_decision = "awaiting_confirmation"
        run = run_match_for_invoice(
            db,
            invoice.id,
            actor_id,
            force=True,
            trigger="suggestions_rejected",
        )
        db.add(
            AuditLog(
                action="invoice_po_confirmation_required",
                actor="system",
                entity_type="invoice",
                entity_id=invoice.id,
                old_value={"status": "needs_review", "po_decision": None},
                new_value={
                    "status": invoice.status,
                    "po_decision": invoice.po_decision,
                    "match_run_id": str(run.id) if run else None,
                },
                reason="All suggested purchase orders were declined.",
                message="Invoice is waiting for a PO path decision",
                created_by=None,
            )
        )
    db.add(
        AuditLog(
            action="invoice_match_rejected",
            actor=str(actor_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"po_id": str(po_id), "status": "suggested"},
            new_value={
                "status": invoice.status,
                "po_decision": invoice.po_decision,
            },
            reason=(payload.get("reason") or "Suggested purchase order declined"),
            created_by=actor_id,
        )
    )
    db.commit()
    result = invoice_match_candidates(invoice_id, db, current_user)
    result["status"] = invoice.status
    result["po_decision"] = invoice.po_decision
    return result


@router.post("/{invoice_id:uuid}/approve-exception")
def approve_invoice_exception(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # Exception approval is intentionally narrower than normal approval. These
    # discrepancies can be accepted by a reviewer with a recorded reason; the
    # identity, integrity, and duplicate checks must still be resolved first.
    """
    Approve approve invoice exception.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: reason codes AMBIGUOUS_PO, CURRENCY_MISMATCH, HEADER_TOTAL_VARIANCE,
        LINE_NOT_ON_PO, LINE_TOTAL_VARIANCE, LOW_CONFIDENCE, MISSING_GR, MISSING_PO;
        statuses approved, needs_review, on_hold, paid, ready_to_pay, rejected.
    """
    allowed_exception_codes = {
        "MISSING_GR",
        "QUANTITY_VARIANCE",
        "PRICE_VARIANCE",
        "LINE_TOTAL_VARIANCE",
        "TAX_VARIANCE",
        "HEADER_TOTAL_VARIANCE",
    }
    blocked_exception_messages = {
        "MISSING_PO": (
            "Resolve the missing purchase order before approving an exception."
        ),
        "AMBIGUOUS_PO": (
            "Choose the correct purchase order before approving an exception."
        ),
        "VENDOR_MISMATCH": (
            "Resolve the vendor mismatch before approving an exception."
        ),
        "LINE_NOT_ON_PO": (
            "Resolve invoice lines that are not on the purchase order "
            "before approving an exception."
        ),
        "CURRENCY_MISMATCH": (
            "Resolve the currency mismatch before approving an exception."
        ),
        "POSSIBLE_DUPLICATE": (
            "Resolve the possible duplicate invoice before approving an exception."
        ),
        "LOW_CONFIDENCE": (
            "Verify the low-confidence fields before approving an exception."
        ),
    }
    reason = str(payload.get("reason") or "").strip()
    comment = str(payload.get("comment") or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required")
    _require_invoice_decision_role(current_user)
    invoice, actor_id = _review_invoice_or_404(db, invoice_id, current_user)
    if (
        invoice.final_status
        or invoice.status
        in {
            "ready_to_pay",
            "on_hold",
            "rejected",
        }
        or getattr(invoice, "workflow_status", None)
        in {
            "approved",
            "paid",
            "on_hold",
            "rejected",
        }
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This invoice has already been decided. "
                "Refresh to see the current decision."
            ),
        )
    if getattr(invoice, "vendor_id", None) is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This invoice's vendor is unresolved. Resolve the vendor before "
                "approving an exception."
            ),
        )
    invoice_only = getattr(invoice, "po_decision", None) == "invoice_only"
    if invoice_only:
        allowed_exception_codes.add("MISSING_PO")
    saved_run = (
        db.get(MatchRun, invoice.final_run_id)
        if getattr(invoice, "final_run_id", None)
        else None
    )
    saved_outcome = (
        (saved_run.rules_used or {}).get("match_outcome") if saved_run else None
    )
    expected_run_id = payload.get("expected_match_run_id", payload.get("match_run_id"))
    if expected_run_id is not None:
        try:
            expected_run_id = uuid.UUID(str(expected_run_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid match run ID") from exc
    expected_updated_at = payload.get("expected_updated_at")
    if expected_updated_at is not None and not isinstance(
        expected_updated_at, datetime
    ):
        try:
            expected_updated_at = datetime.fromisoformat(
                str(expected_updated_at).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid invoice timestamp"
            ) from exc
    _check_invoice_decision_snapshot(invoice, expected_run_id, expected_updated_at)
    discrepancies = (
        saved_outcome.get("discrepancies", [])
        if isinstance(saved_outcome, dict)
        else []
    )
    reason_codes = [
        str(item.get("code"))
        for item in discrepancies
        if isinstance(item, dict) and item.get("code")
    ]
    if not reason_codes or not saved_outcome:
        raise HTTPException(
            status_code=409,
            detail=(
                "Exception approval is not available because no saved discrepancy "
                "is available. Re-run matching and review the invoice."
            ),
        )
    if invoice.status != "needs_review":
        raise HTTPException(
            status_code=409,
            detail=(
                "Exception approval is only available for invoices that need "
                "review. Refresh and try again."
            ),
        )
    blocked_codes = [
        code for code in reason_codes if code not in allowed_exception_codes
    ]
    if blocked_codes:
        code = blocked_codes[0]
        raise HTTPException(
            status_code=409,
            detail=blocked_exception_messages.get(
                code,
                (
                    f"Exception approval is not allowed for reason code {code}. "
                    "Resolve the discrepancy first."
                ),
            ),
        )
    old_status = invoice.status
    transition_invoice(
        db,
        invoice,
        "approved",
        changed_by=actor_id,
        reason=reason,
        force=True,
        allow_exception=True,
    )
    invoice.status = "ready_to_pay"
    invoice.final_by = actor_id
    invoice.final_at = datetime.now(UTC)
    db.add(
        AuditLog(
            action="invoice_exception_approved",
            actor=str(actor_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"status": old_status},
            new_value={
                "status": "ready_to_pay",
                "reason_codes": reason_codes,
                "reason": reason,
                "comment": comment or None,
                "final_by": str(actor_id),
                "final_at": invoice.final_at.isoformat(),
            },
            reason=reason,
            message="Approved with exception by reviewer",
            created_by=actor_id,
        )
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "status": invoice.status,
        "reason": reason,
        "comment": comment or None,
        "final_by": str(invoice.final_by),
        "final_at": invoice.final_at.isoformat(),
    }


@router.get(
    "/review-queue",
    response_model=InvoiceDashboardResponse,
    dependencies=[Depends(get_current_user)],
)
def list_invoice_review_queue(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    match_status: InvoiceMatchStatus | None = None,
    final_status: InvoiceFinalStatus | None = None,
    vendor_id: uuid.UUID | None = None,
    invoice_date_from: date | None = None,
    invoice_date_to: date | None = None,
    due_date: date | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> InvoiceDashboardResponse:
    """
    List list invoice review queue.
    Inputs: page (Annotated[int, Query(ge=1)]), page_size (Annotated[int, Query(ge=1,
        le=100)]), match_status (InvoiceMatchStatus | None), final_status
        (InvoiceFinalStatus | None), vendor_id (uuid.UUID | None), invoice_date_from
        (date | None), invoice_date_to (date | None), due_date (date | None),
        due_date_from (date | None), â€¦.
    Output: InvoiceDashboardResponse.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    filters = _dashboard_query_parameters(
        match_status=match_status,
        final_status=final_status,
        vendor_id=vendor_id,
        invoice_date_from=invoice_date_from,
        invoice_date_to=invoice_date_to,
        due_date=due_date,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        review_queue=True,
        owner_id=_owner_id(current_user),
    )
    return _invoice_dashboard_response(
        db,
        page=page,
        page_size=page_size,
        filters=filters,
    )


@router.post(
    "/{invoice_id}/review",
    response_model=InvoiceReviewResponse,
)
def review_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceReviewRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> InvoiceReviewResponse:
    """
    Handle review invoice.
    Inputs: invoice_id (uuid.UUID), payload (InvoiceReviewRequest), db (Session),
        current_user (dict).
    Output: InvoiceReviewResponse.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: statuses approved, on_hold, ready_to_pay, rejected.
    """
    _require_invoice_decision_role(current_user)
    reviewer_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(reviewer_id))
        .with_for_update(of=Invoice)
        .first()
    )

    if invoice is None:
        raise HTTPException(
            status_code=404,
            detail="Invoice not found",
        )

    _check_invoice_decision_snapshot(
        invoice, payload.expected_match_run_id, payload.expected_updated_at
    )

    invoice_only = getattr(invoice, "po_decision", None) == "invoice_only"
    if (
        payload.decision == "approved"
        and invoice_only
        and not (payload.reason or "").strip()
    ):
        raise HTTPException(
            status_code=422,
            detail="A reason is required to approve invoice-only processing",
        )
    if not (payload.reason or "").strip():
        raise HTTPException(status_code=422, detail="A reason is required")

    open_issues = 0
    if payload.decision == "approved":
        open_issues = (
            db.query(InvoiceIssue)
            .filter(
                InvoiceIssue.invoice_id == invoice.id,
                InvoiceIssue.status.in_(
                    {"open", "in_progress", "waiting_vendor", "escalated"}
                ),
            )
            .count()
        )
        if not isinstance(open_issues, int):
            open_issues = 0
    has_issue_override = open_issues > 0 and bool(
        (payload.override_reason or "").strip()
    )
    if (
        payload.decision == "approved"
        and getattr(invoice, "status", _workspace_status(invoice)) != "ready_to_pay"
        and not (invoice_only and invoice.status == "needs_approval")
        and not has_issue_override
    ):
        raise HTTPException(
            status_code=409,
            detail=("Invoice cannot be approved while matching is unresolved"),
        )

    if payload.decision == "approved":
        if open_issues and not (payload.override_reason or "").strip():
            raise HTTPException(
                status_code=409,
                detail=(
                    "Invoice has open issues. Resolve them or provide override_reason."
                ),
            )
        if open_issues:
            db.add(
                AuditLog(
                    action="invoice_issue_override",
                    entity_type="invoice",
                    entity_id=invoice.id,
                    old_value={"open_issue_count": open_issues},
                    new_value={"override_reason": payload.override_reason.strip()},
                    reason=payload.override_reason.strip(),
                    result="warning",
                    message="Approval proceeded with open issue override",
                    created_by=reviewer_id,
                )
            )

    if payload.decision == "approved":
        saved_run = (
            db.get(MatchRun, invoice.final_run_id)
            if getattr(invoice, "final_run_id", None)
            else None
        )
        saved_outcome = (
            (saved_run.rules_used or {}).get("match_outcome") if saved_run else None
        )
        allowed_outcomes = {"ready_to_pay"}
        if invoice_only:
            allowed_outcomes.add("needs_approval")
        if not saved_outcome or saved_outcome.get("outcome") not in allowed_outcomes:
            raise HTTPException(
                status_code=409,
                detail="Invoice cannot be approved until all matching rules pass",
            )

    old_workflow_status = getattr(
        invoice, "workflow_status", None
    ) or legacy_workflow_status(invoice)
    old_value = {
        "status": getattr(invoice, "status", _workspace_status(invoice)),
        "final_status": invoice.final_status,
        "workflow_status": old_workflow_status,
        "final_by": str(invoice.final_by) if invoice.final_by else None,
        "final_at": (invoice.final_at.isoformat() if invoice.final_at else None),
    }
    decided_at = datetime.now(UTC)
    transition_invoice(
        db,
        invoice,
        payload.decision,
        changed_by=reviewer_id,
        reason=payload.reason or payload.comment,
    )
    invoice.final_by = reviewer_id
    invoice.final_at = decided_at
    invoice.status = {
        "approved": "ready_to_pay",
        "rejected": "rejected",
        "on_hold": "on_hold",
    }[payload.decision]

    db.add(
        AuditLog(
            action="invoice_review_decision",
            actor=str(reviewer_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old_value,
            new_value={
                "status": invoice.status,
                "final_status": payload.decision,
                "workflow_status": payload.decision,
                "reason": payload.reason or payload.comment,
                "final_by": str(reviewer_id),
                "final_at": decided_at.isoformat(),
            },
            reason=payload.reason or payload.comment,
            message=f"Invoice {payload.decision} by reviewer {reviewer_id}",
            result="success",
            created_by=reviewer_id,
        )
    )

    try:
        db.commit()
        db.refresh(invoice)
    except Exception:
        db.rollback()
        raise

    return InvoiceReviewResponse(
        invoice_id=invoice.id,
        final_status=invoice.final_status,
        final_by=invoice.final_by,
        final_at=invoice.final_at,
        status=invoice.status,
    )


@router.post("/{invoice_id}/approve", response_model=InvoiceReviewResponse)
def approve_invoice(
    invoice_id: uuid.UUID,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Approve approve invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict | None), db (Session), current_user
        (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: statuses approved.
    """
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(
            decision="approved",
            comment=(payload or {}).get("comment"),
            reason=(payload or {}).get("reason"),
            override_reason=(payload or {}).get("override_reason"),
            expected_match_run_id=(payload or {}).get("expected_match_run_id"),
            expected_updated_at=(payload or {}).get("expected_updated_at"),
        ),
        db,
        current_user,
    )


@router.post("/{invoice_id}/reject", response_model=InvoiceReviewResponse)
def reject_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Reject reject invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: statuses rejected.
    """
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(
            decision="rejected",
            reason=payload.get("reason"),
            expected_match_run_id=payload.get("expected_match_run_id"),
            expected_updated_at=payload.get("expected_updated_at"),
        ),
        db,
        current_user,
    )


@router.post("/{invoice_id}/hold", response_model=InvoiceReviewResponse)
def hold_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle hold invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: statuses on_hold.
    """
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(
            decision="on_hold",
            reason=payload.get("reason"),
            expected_match_run_id=payload.get("expected_match_run_id"),
            expected_updated_at=payload.get("expected_updated_at"),
        ),
        db,
        current_user,
    )


@router.post("/{invoice_id}/resubmit")
def resubmit_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle resubmit invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: statuses matching, pending, rejected, resubmitted.
    """
    actor_id = _owner_id(current_user)
    old = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if old is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if old.workflow_status != "rejected":
        raise HTTPException(
            status_code=409, detail="Only rejected invoices can be resubmitted"
        )
    prior_rejection = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "invoice",
            AuditLog.entity_id == old.id,
            AuditLog.action == "invoice_review_decision",
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    reason = (
        (prior_rejection.new_value or {}).get("reason")
        if prior_rejection and prior_rejection.new_value
        else None
    )
    allowed = {
        "invoice_no",
        "vendor_name_raw",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax_amount",
        "total_amount",
        "gl_code",
        "po_number_raw",
        "vendor_id",
        "po_id",
        "field_confidence",
    }
    bad_fields = set(payload) - allowed
    if bad_fields:
        raise HTTPException(
            status_code=422, detail="Unsupported corrected invoice fields"
        )
    corrected_vendor_id = payload.get("vendor_id", old.vendor_id)
    if corrected_vendor_id is not None:
        try:
            corrected_vendor_id = uuid.UUID(str(corrected_vendor_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid vendor ID") from exc
    corrected_invoice_no = payload.get("invoice_no", old.invoice_no)
    normalized_invoice_no = _checked_invoice_number(
        db,
        corrected_vendor_id,
        corrected_invoice_no,
        exclude_invoice_id=old.id,
    )
    old.is_current = False
    new = Invoice(
        source_document_id=old.source_document_id,
        resubmitted_from_id=old.id,
        invoice_no=old.invoice_no,
        invoice_no_normalized=normalize_invoice_no(old.invoice_no),
        version_no=old.version_no + 1,
        is_current=True,
        vendor_id=old.vendor_id,
        vendor_name_raw=old.vendor_name_raw,
        vendor_tax_id_raw=old.vendor_tax_id_raw,
        invoice_date=old.invoice_date,
        due_date=old.due_date,
        currency=old.currency,
        subtotal=old.subtotal,
        tax_amount=old.tax_amount,
        total_amount=old.total_amount,
        field_confidence=old.field_confidence or {},
        gl_code=old.gl_code,
        po_number_raw=old.po_number_raw,
        po_id=old.po_id,
        match_status="pending",
        final_status=None,
        workflow_status="rejected",
        owner_user_id=old.owner_user_id or _assignment(db),
        created_by=actor_id,
        updated_by=actor_id,
    )
    for key, value in payload.items():
        setattr(new, key, value)
    new.invoice_no_normalized = normalized_invoice_no
    db.add(new)
    db.flush()
    from src.models.invoice_models import InvoiceLine

    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == old.id).all()
    for line in lines:
        db.add(
            InvoiceLine(
                invoice_id=new.id,
                item_code=line.item_code,
                description=line.description,
                uom=line.uom,
                qty=line.qty,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
                field_confidence=line.field_confidence or {},
            )
        )
    transition_invoice(
        db,
        new,
        "resubmitted",
        changed_by=actor_id,
        reason=(
            f"Corrected version of rejected invoice {old.id}; "
            f"rejection: {reason or 'unspecified'}"
        ),
    )
    transition_invoice(
        db,
        new,
        "matching",
        changed_by=actor_id,
        reason="Start matching corrected invoice version",
    )
    db.add(
        AuditLog(
            action="invoice_resubmitted",
            entity_type="invoice",
            entity_id=new.id,
            old_value={
                "invoice_id": str(old.id),
                "version_no": old.version_no,
                "rejection_reason": reason,
            },
            new_value={
                "invoice_id": str(new.id),
                "version_no": new.version_no,
                "is_current": True,
            },
            reason=reason,
            result="success",
            message="Rejected invoice resubmitted as a new version",
            created_by=actor_id,
        )
    )
    try:
        run_match_for_invoice(db, new.id, actor_id)
        db.commit()
        db.refresh(new)
    except Exception:
        db.rollback()
        raise
    return {
        "invoice_id": new.id,
        "previous_invoice_id": old.id,
        "version_no": new.version_no,
        "workflow_status": new.workflow_status,
        "can_resubmit": False,
        "rejection_reason": reason,
    }


@router.get("/{invoice_id}/match-runs")
def list_match_runs(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    List list match runs.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    if db.get(Invoice, invoice_id) is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return (
        db.query(MatchRun)
        .filter(MatchRun.invoice_id == invoice_id)
        .order_by(MatchRun.run_no.desc())
        .all()
    )


@router.get("/{invoice_id}/timeline")
def invoice_timeline(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle invoice timeline.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; may add audit entries; may add invoice timeline
        entries.
    Results: statuses failed.
    """
    if db.get(Invoice, invoice_id) is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = db.get(Invoice, invoice_id)
    audit_filter = (AuditLog.entity_type == "invoice") & (
        AuditLog.entity_id == invoice_id
    )
    if invoice.source_document_id:
        audit_filter = audit_filter | (
            (AuditLog.entity_type == "document")
            & (AuditLog.entity_id == invoice.source_document_id)
        )
    audit_items = db.query(AuditLog).filter(audit_filter).all()
    states = db.query(InvoiceStateHistory).filter_by(invoice_id=invoice_id).all()
    draft_events = (
        db.query(EmailDraftEvent)
        .join(EmailDraft, EmailDraft.id == EmailDraftEvent.draft_id)
        .filter(EmailDraft.invoice_id == invoice_id)
        .all()
    )
    entries = [
        {
            "kind": "audit",
            **{
                key: getattr(item, key)
                for key in (
                    "id",
                    "action",
                    "entity_type",
                    "entity_id",
                    "old_value",
                    "new_value",
                    "result",
                    "message",
                    "created_by",
                    "created_at",
                    "step_name",
                    "duration_ms",
                    "input_ref",
                    "output_summary",
                )
            },
        }
        for item in audit_items
    ]
    entries.extend(
        {
            "kind": "state_change",
            "id": item.id,
            "entity_type": "invoice",
            "entity_id": item.invoice_id,
            "action": "invoice_state_changed",
            "old_value": {"workflow_status": item.from_status},
            "new_value": {"workflow_status": item.to_status, "reason": item.reason},
            "result": "success",
            "message": item.reason,
            "created_by": item.changed_by,
            "created_at": item.created_at,
        }
        for item in states
    )
    entries.extend(
        {
            "kind": "email",
            "id": item.id,
            "entity_type": "invoice",
            "entity_id": invoice_id,
            "action": f"email_{item.event_type}",
            "old_value": item.old_value,
            "new_value": {"draft_id": item.draft_id, **(item.new_value or {})},
            "result": "success" if item.event_type != "failed" else "error",
            "message": f"Outbound email {item.event_type.replace('_', ' ')}",
            "created_by": item.user_id,
            "created_at": item.created_at,
        }
        for item in draft_events
    )
    return sorted(entries, key=lambda item: item["created_at"])


@router.get("/{invoice_id}/state-history")
def invoice_state_history(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle invoice state history.
    Inputs: invoice_id (uuid.UUID), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; may add invoice timeline entries.
    Results: no workflow status or reason code directly.
    """
    if db.get(Invoice, invoice_id) is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return (
        db.query(InvoiceStateHistory)
        .filter_by(invoice_id=invoice_id)
        .order_by(InvoiceStateHistory.created_at.asc())
        .all()
    )


@router.post("/{invoice_id}/pay", summary="Mark as paid")
def record_invoice_payment(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle record invoice payment.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: statuses approved, other, overdue, paid.
    """
    from datetime import date as date_type

    paid_date_value = payload.get("paid_date")
    try:
        paid_date = (
            paid_date_value
            if isinstance(paid_date_value, date_type)
            else date_type.fromisoformat(str(paid_date_value))
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="A valid paid_date is required"
        ) from exc
    method = str(payload.get("method") or "").strip().casefold()
    if method not in {"bank_transfer", "cheque", "upi", "cash", "other"}:
        raise HTTPException(status_code=422, detail="Choose a valid payment method")
    reference = str(payload.get("reference") or "").strip()
    if not reference:
        raise HTTPException(status_code=422, detail="Payment reference is required")
    invoice = db.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if getattr(invoice, "workflow_status", None) == "paid":
        raise HTTPException(
            status_code=409, detail="Invoice payment has already been recorded"
        )
    if (
        getattr(invoice, "workflow_status", None) not in {"approved", "overdue"}
        or getattr(invoice, "final_status", None) != "approved"
    ):
        raise HTTPException(
            status_code=409,
            detail="Only an approved, ready-to-pay invoice can be marked as paid",
        )
    if invoice.total_amount is None or Decimal(invoice.total_amount) <= 0:
        raise HTTPException(status_code=409, detail="Invoice has no payable amount")
    user_id = _owner_id(current_user)
    paid_at = datetime.combine(paid_date, datetime.min.time(), tzinfo=UTC)
    payment = Payment(
        invoice_id=invoice.id,
        amount=invoice.total_amount,
        paid_date=paid_date,
        method=method,
        reference=reference[:200],
        recorded_by=user_id,
        note=str(payload.get("note") or "").strip() or None,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="An active payment is already recorded"
        ) from exc
    old = {"workflow_status": invoice.workflow_status, "payment_id": None}
    # This records an external payment only; it never calls a bank or provider.
    transition_invoice(
        db, invoice, "paid", changed_by=user_id, reason="External payment recorded"
    )
    invoice.paid_at = paid_at
    invoice.payment_method = method
    invoice.payment_reference = reference[:200]
    invoice.updated_by = user_id
    db.add(
        AuditLog(
            action="invoice_payment_recorded",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value={
                "workflow_status": "paid",
                "payment_id": str(payment.id) if payment.id else None,
                "paid_date": paid_date.isoformat(),
                "amount": str(payment.amount),
                "method": method,
                "reference": reference[:200],
            },
            message=(
                "External payment recorded; no payment was initiated by this system"
            ),
            result="success",
            created_by=user_id,
        )
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "payment_id": str(payment.id) if payment.id else None,
        "workflow_status": "paid",
        "paid_date": paid_date,
        "amount": payment.amount,
    }


@router.post("/{invoice_id}/pay/void")
def void_invoice_payment(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle void invoice payment.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: statuses approved, paid.
    """
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(
            status_code=422, detail="A reason is required to void a payment"
        )
    invoice = db.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    payment = (
        db.query(Payment)
        .filter(Payment.invoice_id == invoice.id, Payment.voided_at.is_(None))
        .with_for_update()
        .first()
    )
    if payment is None or invoice.workflow_status != "paid":
        raise HTTPException(status_code=409, detail="No active payment can be voided")
    user_id = _owner_id(current_user)
    old = {
        "payment_id": str(payment.id),
        "workflow_status": invoice.workflow_status,
        "paid_date": payment.paid_date.isoformat(),
        "method": payment.method,
        "reference": payment.reference,
    }
    now = datetime.now(UTC)
    payment.voided_at = now
    payment.voided_by = user_id
    payment.void_reason = reason
    transition_invoice(db, invoice, "approved", changed_by=user_id, reason=reason)
    invoice.paid_at = None
    invoice.payment_method = None
    invoice.payment_reference = None
    invoice.updated_by = user_id
    db.add(
        AuditLog(
            action="invoice_payment_voided",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value={
                "payment_id": str(payment.id),
                "workflow_status": "approved",
                "void_reason": reason,
                "voided_at": now.isoformat(),
            },
            reason=reason,
            result="success",
            message="Recorded payment voided",
            created_by=user_id,
        )
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "payment_id": str(payment.id),
        "workflow_status": "approved",
        "voided_at": now,
        "message": "Payment record voided",
    }


@router.post("/{invoice_id}/release-hold")
def release_invoice_hold(
    invoice_id: uuid.UUID,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Handle release invoice hold.
    Inputs: invoice_id (uuid.UUID), payload (dict | None), db (Session), current_user
        (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries.
    Results: statuses approved, on_hold.
    """
    invoice = db.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.workflow_status != "on_hold":
        raise HTTPException(status_code=409, detail="Invoice is not on hold")
    user_id = _owner_id(current_user)
    reason = (
        str((payload or {}).get("reason") or "").strip() or "Hold released by associate"
    )
    old = {"workflow_status": invoice.workflow_status}
    transition_invoice(db, invoice, "approved", changed_by=user_id, reason=reason)
    db.add(
        AuditLog(
            action="invoice_hold_released",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value={"workflow_status": "approved", "reason": reason},
            reason=reason,
            result="success",
            message="Invoice hold released",
            created_by=user_id,
        )
    )
    db.commit()
    return {"invoice_id": str(invoice.id), "workflow_status": "approved"}


@router.post("/{invoice_id}/escalate")
def escalate_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Escalate escalate invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may queue or retry background work.
    Results: no workflow status or reason code directly.
    """
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required to escalate")
    invoice = db.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    user_id = _owner_id(current_user)
    allowlist = {
        value.strip().casefold()
        for value in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
        if value.strip()
    }
    escalation = os.getenv("ESCALATION_EMAIL", "").strip().casefold()
    if escalation:
        recipients = [escalation] if escalation in allowlist else []
    else:
        associates = (
            db.query(User)
            .filter(
                User.role == "associate", User.is_active.is_(True), User.id != user_id
            )
            .all()
        )
        recipients = [
            user.email for user in associates if user.email.casefold() in allowlist
        ]
    for recipient in recipients:
        if invoice.source_document_id:
            db.add(
                Notification(
                    document_id=invoice.source_document_id,
                    recipient_email=recipient,
                    subject=(
                        "AP internal reminder: Invoice escalated "
                        f"{invoice.invoice_no or ''}"
                    ),
                    body=reason,
                    status="queued",
                    created_by=user_id,
                )
            )
    db.add(
        AuditLog(
            action="invoice_escalated",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={
                "owner_user_id": str(invoice.owner_user_id)
                if invoice.owner_user_id
                else None
            },
            new_value={"reason": reason, "notified": recipients},
            reason=reason,
            result="success",
            message="Invoice escalated for internal review",
            created_by=user_id,
        )
    )
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id)
    return {"invoice_id": str(invoice.id), "escalated": True, "notified": recipients}


@router.post("/{invoice_id}/reassign")
def reassign_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Reassign reassign invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries.
    Results: no workflow status or reason code directly.
    """
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required to reassign")
    try:
        target_id = uuid.UUID(str(payload.get("owner_user_id")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="A valid owner_user_id is required"
        ) from exc
    target = db.get(User, target_id)
    if target is None or target.role != "associate" or not target.is_active:
        raise HTTPException(
            status_code=422, detail="Reassignment target must be an active associate"
        )
    invoice = db.query(Invoice).filter_by(id=invoice_id).with_for_update().first()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    user_id = _owner_id(current_user)
    old_id = invoice.owner_user_id
    invoice.owner_user_id = target_id
    invoice.updated_by = user_id
    now = datetime.now(UTC)
    followups = (
        db.query(FollowUp)
        .filter(
            FollowUp.entity_type == "invoice",
            FollowUp.entity_id == invoice.id,
            FollowUp.stopped_at.is_(None),
        )
        .with_for_update()
        .all()
    )
    for followup in followups:
        followup.stopped_at = now
        followup.stop_reason = "invoice reassigned"
        db.add(
            AuditLog(
                action="follow_up_stopped",
                entity_type="follow_up",
                entity_id=followup.id,
                old_value={"stopped_at": None},
                new_value={"stop_reason": followup.stop_reason},
                reason=reason,
                result="success",
                message="Follow-up stopped after reassignment",
                created_by=user_id,
            )
        )
    db.add(
        AuditLog(
            action="invoice_reassigned",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"owner_user_id": str(old_id) if old_id else None},
            new_value={"owner_user_id": str(target_id)},
            reason=reason,
            result="success",
            message="Invoice reassigned",
            created_by=user_id,
        )
    )
    db.commit()
    return {"invoice_id": str(invoice.id), "owner_user_id": str(target_id)}


@router.post("/{invoice_id}/match")
def run_invoice_match(
    invoice_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Run run invoice match.
    Inputs: invoice_id (uuid.UUID), background_tasks (BackgroundTasks), db (Session),
        current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may queue or retry
        background work.
    Results: no workflow status or reason code directly.
    """
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    run = run_match_for_invoice(db, invoice.id, user_id, force=True)
    db.commit()
    if run is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id, background_tasks)
    return {
        "match_run_id": str(run.id),
        "run_no": run.run_no,
        "result": run.result,
        "differences": run.differences,
    }


@router.post("/admin/{invoice_id}/rerun-flow")
def rerun_invoice_flow_admin(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("admin")),
):
    """Re-run matching from an invoice's saved extraction and return its status."""
    invoice = (
        db.query(Invoice)
        .filter(Invoice.id == invoice_id)
        .with_for_update()
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    run = run_match_for_invoice(
        db,
        invoice.id,
        uuid.UUID(str(current_user["sub"])),
        force=True,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "status": invoice.status,
        "match_run_id": str(run.id),
        "result": run.result,
        "differences": run.differences,
    }


@router.patch("/{invoice_id}")
def update_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Update update invoice.
    Inputs: invoice_id (uuid.UUID), payload (dict), background_tasks (BackgroundTasks),
        db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may queue or retry background work.
    Results: no workflow status or reason code directly.
    """
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(user_id))
        .with_for_update(of=Invoice)
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    confidence = payload.pop("confidence", {})
    allowed = {
        "invoice_no",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "tax_amount",
        "total_amount",
        "po_number_raw",
        "owner_user_id",
        "vendor_id",
    }
    changes = {key: value for key, value in payload.items() if key in allowed}
    if not changes:
        raise HTTPException(
            status_code=422, detail="No editable invoice fields provided"
        )
    normalized_invoice_no = _checked_invoice_number(
        db,
        changes.get("vendor_id", invoice.vendor_id),
        changes.get("invoice_no", invoice.invoice_no),
        exclude_invoice_id=invoice.id,
    )
    if "vendor_id" in changes:
        if current_user.get("role") != "admin":
            raise HTTPException(
                status_code=403,
                detail="Only administrators can change an invoice vendor",
            )
        if changes["vendor_id"] is not None:
            try:
                changes["vendor_id"] = uuid.UUID(str(changes["vendor_id"]))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid vendor ID"
                ) from exc
            vendor = db.get(Vendor, changes["vendor_id"])
            if vendor is None:
                raise HTTPException(status_code=404, detail="Vendor not found")
            if not vendor.is_active:
                raise HTTPException(status_code=409, detail="Vendor is inactive")
    old = {
        key: str(getattr(invoice, key)) if getattr(invoice, key) is not None else None
        for key in changes
    }
    for key, value in changes.items():
        if key in {"owner_user_id", "vendor_id"} and value:
            try:
                value = uuid.UUID(str(value))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid {key.replace('_', ' ')}",
                ) from exc
        setattr(
            invoice,
            key,
            value,
        )
    invoice.invoice_no_normalized = normalized_invoice_no
    invoice.updated_by = user_id
    matching_fields = {
        "invoice_no",
        "vendor_id",
        "currency",
        "subtotal",
        "tax_amount",
        "total_amount",
        "po_number_raw",
    }
    match_run = None
    if matching_fields.intersection(changes):
        db.flush()
        match_run = run_match_for_invoice(db, invoice.id, user_id, force=True)
    new = {
        key: str(getattr(invoice, key)) if getattr(invoice, key) is not None else None
        for key in changes
    }
    flagged = [
        key
        for key in changes
        if confidence.get(key) is not None and float(confidence[key]) < 85
    ]
    db.add(
        AuditLog(
            action="invoice_updated",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old,
            new_value={**new, "flagged_fields": flagged},
            message="Invoice fields updated",
            created_by=user_id,
        )
    )
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id, background_tasks)
    return {
        "invoice_id": str(invoice.id),
        "updated": new,
        "flagged_fields": flagged,
        "status": getattr(invoice, "status", None) or _workspace_status(invoice),
        "po_decision": getattr(invoice, "po_decision", None),
        "match_run_id": str(match_run.id) if match_run else None,
    }


@router.post("/{invoice_id}/confirm-po")
def confirm_invoice_po(
    invoice_id: uuid.UUID,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Confirm confirm invoice po.
    Inputs: invoice_id (uuid.UUID), payload (dict), background_tasks (BackgroundTasks),
        db (Session), current_user (dict).
    Output: the value or None returned by its body.
    Effects: reads database rows; writes or commits database rows; may add audit
        entries; may add invoice timeline entries; may queue or retry background work.
    Results: statuses approved, matching, on_hold, paid, rejected.
    """
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(user_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    po_id = uuid.UUID(str(payload.get("po_id")))
    selected_po = db.get(PurchaseOrder, po_id)
    if selected_po is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    if not selected_po.is_current:
        raise HTTPException(
            status_code=409,
            detail="Only the current purchase order version can be confirmed",
        )
    old = str(invoice.po_id) if invoice.po_id else None
    invoice.po_decision = None
    invoice.po_number_raw = selected_po.po_number
    confirmed_match = (
        db.query(InvoiceMatch)
        .filter(
            InvoiceMatch.invoice_id == invoice.id,
            InvoiceMatch.po_id == selected_po.id,
        )
        .first()
    )
    if confirmed_match is None:
        db.add(
            InvoiceMatch(
                invoice_id=invoice.id,
                po_id=selected_po.id,
                match_type="2-way",
                score=Decimal("100"),
                status="confirmed",
                confirmed_by=user_id,
            )
        )
    else:
        confirmed_match.status = "confirmed"
        confirmed_match.confirmed_by = user_id
    if legacy_workflow_status(invoice) not in {
        "approved",
        "rejected",
        "on_hold",
        "paid",
    }:
        transition_invoice(
            db,
            invoice,
            "matching",
            changed_by=user_id,
            reason="Purchase order confirmed",
        )
    db.flush()
    run = run_match_for_invoice(db, invoice.id, user_id, force=True)
    db.add(
        AuditLog(
            action="invoice_po_confirmed",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"po_id": old},
            new_value={
                "po_id": str(invoice.po_id) if invoice.po_id else None,
                "status": invoice.status,
            },
            message="Purchase order confirmed",
            created_by=user_id,
        )
    )
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id, background_tasks)
    return {
        "invoice_id": str(invoice.id),
        "po_id": str(invoice.po_id) if invoice.po_id else None,
        "status": invoice.status,
        "match_run_id": str(run.id) if run else None,
    }

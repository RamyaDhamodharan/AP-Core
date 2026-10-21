import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrder
from src.models.vendor_models import Vendor
from src.schemas.document_workflow_schemas import InvoiceRecommendation
from src.schemas.invoice_schemas import (
    InvoiceDashboardItem,
    InvoiceDashboardResponse,
    InvoiceFinalStatus,
    InvoiceMatchStatus,
    InvoiceReviewRequest,
    InvoiceReviewResponse,
)
from src.services.document_workflow_services import (
    recommend_invoice_review,
)
from src.services.invoice_matching_services import run_match_for_invoice
from src.worker.dispatch import dispatch_queued_notifications

router = APIRouter(prefix="/api/invoices", tags=["Invoices"])


def _owner_id(user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _invoice_owner_filter(owner_id: uuid.UUID):
    return or_(
        Invoice.owner_user_id == owner_id,
        Invoice.created_by == owner_id,
        Document.created_by == owner_id,
    )


@router.post("/{invoice_id}/three-way-match", dependencies=[Depends(get_current_user)])
def three_way_match_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    owner_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    recommendation = recommend_invoice_review(db, invoice, owner_id=owner_id)
    checks = [item.model_dump(mode="json") for item in recommendation.checks]
    return {
        **recommendation.model_dump(mode="json"),
        "matched": recommendation.matching_status == "matched",
        "requires_review": recommendation.human_review_required,
        "issues": [
            item.reason
            for item in recommendation.checks
            if item.outcome in {"mismatch", "not_possible"}
        ],
        "status": recommendation.matching_status,
        "available_documents": [
            item.model_dump(mode="json") for item in recommendation.supporting_records
        ],
        "checks": checks,
        "recommended_next_actions": [recommendation.next_recommended_action],
        "human_decision_required": recommendation.matching_status
        in {"review_required", "awaiting_human_decision"},
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
    owner_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return recommend_invoice_review(db, invoice, owner_id=owner_id)


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
) -> list:
    filters = [_invoice_owner_filter(owner_id)]
    if match_status:
        filters.append(Invoice.match_status == match_status)
    if final_status:
        filters.append(Invoice.final_status == final_status)
    if vendor_id:
        filters.append(Invoice.vendor_id == vendor_id)
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
) -> InvoiceDashboardResponse:
    total = (
        db.query(func.count(Invoice.id))
        .select_from(Invoice)
        .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .scalar()
    )
    rows = (
        db.query(Invoice, Vendor.name.label("vendor_name"), Document.status)
        .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(*filters)
        .order_by(
            (
                Invoice.created_at.desc()
                if sort_by == "received_date"
                else Invoice.total_amount.desc()
                if sort_by == "amount"
                else Invoice.due_date.asc().nullslast()
            ),
            Invoice.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return InvoiceDashboardResponse(
        items=[
            InvoiceDashboardItem(
                invoice_id=invoice.id,
                source_document_id=invoice.source_document_id,
                processing_status=document_status,
                invoice_no=invoice.invoice_no,
                vendor_name=vendor_name or invoice.vendor_name_raw,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                total_amount=invoice.total_amount,
                currency=invoice.currency,
                match_status=invoice.match_status,
                final_status=invoice.final_status,
            )
            for invoice, vendor_name, document_status in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


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
    final_status: InvoiceFinalStatus | None = None,
    vendor_id: uuid.UUID | None = None,
    invoice_date_from: date | None = None,
    invoice_date_to: date | None = None,
    due_date: date | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    owner_user_id: uuid.UUID | None = None,
    overdue: bool = False,
    sort_by: Annotated[
        str, Query(pattern="^(received_date|amount|due_date)$")
    ] = "received_date",
    search: Annotated[str | None, Query(max_length=200)] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> InvoiceDashboardResponse:
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
    )
    if owner_user_id:
        filters.append(Invoice.owner_user_id == owner_user_id)
    if overdue:
        filters.extend(
            [
                Invoice.due_date < date.today(),
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
    )


@router.get("/{invoice_id:uuid}")
def get_invoice_detail(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
    confidence = extracted.get("field_confidence", extracted.get("confidence", {}))
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
        if value < 85:
            low_confidence_fields.append(field)
    return {
        "invoice_id": str(invoice.id),
        "invoice_no": invoice.invoice_no,
        "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
        "vendor_name": invoice.vendor_name_raw,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "currency": invoice.currency,
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "total_amount": invoice.total_amount,
        "po_id": str(invoice.po_id) if invoice.po_id else None,
        "po_number_raw": invoice.po_number_raw,
        "match_status": invoice.match_status,
        "final_status": invoice.final_status,
        "confidence": normalized_confidence,
        "low_confidence_fields": low_confidence_fields,
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

    if (
        payload.decision == "approved"
        and invoice.match_status in _UNRESOLVED_MATCH_STATUSES
    ):
        raise HTTPException(
            status_code=409,
            detail=("Invoice cannot be approved while matching is unresolved"),
        )

    if payload.decision == "approved":
        recommendation = recommend_invoice_review(db, invoice, owner_id=reviewer_id)
        if any(
            check.outcome != "passed" and check.severity == "error"
            for check in recommendation.checks
        ):
            raise HTTPException(
                status_code=409,
                detail="Invoice cannot be approved while critical findings remain open",
            )

    if (
        payload.decision in {"rejected", "on_hold"}
        and not (payload.reason or "").strip()
    ):
        raise HTTPException(status_code=422, detail="A reason is required")

    old_value = {
        "final_status": invoice.final_status,
        "final_by": str(invoice.final_by) if invoice.final_by else None,
        "final_at": (invoice.final_at.isoformat() if invoice.final_at else None),
    }
    decided_at = datetime.now(UTC)
    invoice.final_status = payload.decision
    invoice.final_by = reviewer_id
    invoice.final_at = decided_at

    db.add(
        AuditLog(
            action="invoice_review_decision",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value=old_value,
            new_value={
                "final_status": payload.decision,
                "final_by": str(reviewer_id),
                "final_at": decided_at.isoformat(),
            },
            message=f"Invoice {payload.decision} by reviewer {reviewer_id}",
            result="success",
            technical_detail=payload.comment or payload.reason,
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
    )


@router.post("/{invoice_id}/approve", response_model=InvoiceReviewResponse)
def approve_invoice(
    invoice_id: uuid.UUID,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(
            decision="approved", comment=(payload or {}).get("comment")
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
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(decision="rejected", reason=payload.get("reason")),
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
    return review_invoice(
        invoice_id,
        InvoiceReviewRequest(decision="on_hold", reason=payload.get("reason")),
        db,
        current_user,
    )


@router.get("/{invoice_id}/match-runs")
def list_match_runs(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
    if db.get(Invoice, invoice_id) is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "invoice", AuditLog.entity_id == invoice_id)
        .order_by(AuditLog.created_at)
        .all()
    )


@router.post("/{invoice_id}/match")
def run_invoice_match(
    invoice_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    run = run_match_for_invoice(db, invoice.id, user_id)
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


@router.patch("/{invoice_id}")
def update_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .outerjoin(Document, Invoice.source_document_id == Document.id)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(user_id))
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
    }
    changes = {key: value for key, value in payload.items() if key in allowed}
    if not changes:
        raise HTTPException(
            status_code=422, detail="No editable invoice fields provided"
        )
    old = {
        key: str(getattr(invoice, key)) if getattr(invoice, key) is not None else None
        for key in changes
    }
    for key, value in changes.items():
        setattr(
            invoice,
            key,
            uuid.UUID(value) if key == "owner_user_id" and value else value,
        )
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
    if matching_fields.intersection(changes):
        db.flush()
        run_match_for_invoice(db, invoice.id, user_id)
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
    return {"invoice_id": str(invoice.id), "updated": new, "flagged_fields": flagged}


@router.post("/{invoice_id}/confirm-po")
def confirm_invoice_po(
    invoice_id: uuid.UUID,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = _owner_id(current_user)
    invoice = (
        db.query(Invoice)
        .filter(Invoice.id == invoice_id, _invoice_owner_filter(user_id))
        .first()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    po_id = uuid.UUID(str(payload.get("po_id")))
    if db.get(PurchaseOrder, po_id) is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    old = str(invoice.po_id) if invoice.po_id else None
    invoice.po_id = po_id
    invoice.match_status = "pending"
    db.flush()
    run_match_for_invoice(db, invoice.id, user_id)
    db.add(
        AuditLog(
            action="invoice_po_confirmed",
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"po_id": old},
            new_value={"po_id": str(po_id)},
            message="Purchase order confirmed",
            created_by=user_id,
        )
    )
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id, background_tasks)
    return {"invoice_id": str(invoice.id), "po_id": str(po_id)}

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice
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
    invoice = db.query(Invoice).outerjoin(
        Document, Invoice.source_document_id == Document.id
    ).filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id)).first()
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
            item.model_dump(mode="json")
            for item in recommendation.supporting_records
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
    invoice = db.query(Invoice).outerjoin(
        Document, Invoice.source_document_id == Document.id
    ).filter(Invoice.id == invoice_id, _invoice_owner_filter(owner_id)).first()
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
        .order_by(Invoice.invoice_date.desc(), Invoice.id)
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
    return _invoice_dashboard_response(
        db,
        page=page,
        page_size=page_size,
        filters=filters,
    )


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

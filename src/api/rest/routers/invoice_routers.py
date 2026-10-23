import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import Float, cast, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rest.dependencies import get_current_user
from src.db.session_db import get_db
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.email_draft_models import EmailDraft, EmailDraftEvent
from src.models.follow_up_models import FollowUp
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
from src.services.document_workflow_services import (
    recommend_invoice_review,
)
from src.services.invoice_confidence_services import (
    confidence_level,
    confidence_threshold,
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
                can_resubmit=getattr(invoice, "workflow_status", None) == "rejected",
                status=getattr(invoice, "status", None) or _workspace_status(invoice),
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
        if field == "order_number":
            po = None
            if value:
                po = (
                    db.query(PurchaseOrder)
                    .filter(
                        func.lower(func.trim(PurchaseOrder.po_number))
                        == str(value).strip().casefold(),
                        PurchaseOrder.is_current.is_(True),
                    )
                    .first()
                )
            invoice.po_id = po.id if po else None
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
    if invoice.po_id and invoice.vendor_id:
        run_match_for_invoice(db, invoice.id, actor_id)
    else:
        transition_invoice(
            db,
            invoice,
            "no_match",
            changed_by=actor_id,
            reason="No Match: vendor or purchase order is missing",
        )
        transition_invoice(
            db,
            invoice,
            "rejected",
            changed_by=actor_id,
            reason="No Match: vendor or purchase order is missing",
        )
        if invoice.source_document_id:
            sender = (
                db.query(Document.email_sender)
                .filter(Document.id == invoice.source_document_id)
                .scalar()
            )
            allowed_senders = {
                item.strip().casefold()
                for item in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
                if item.strip()
            }
            if sender and sender.casefold() in allowed_senders:
                db.add(
                    Notification(
                        document_id=invoice.source_document_id,
                        recipient_email=sender,
                        subject="Invoice needs attention",
                        body=(
                            "No Match: add a valid vendor and purchase order, "
                            "then reopen."
                        ),
                        status="queued",
                    )
                )
    db.commit()
    if invoice.source_document_id:
        dispatch_queued_notifications(db, invoice.source_document_id)
    return {
        "invoice_id": str(invoice.id),
        "workflow_status": invoice.workflow_status,
        "needs_verification": False,
    }


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
    return {
        "invoice_id": str(invoice.id),
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
            for line in db.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .all()
        ],
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
        "confidence": normalized_confidence,
        "field_confidence": getattr(invoice, "field_confidence", None)
        or normalized_confidence,
        "needs_verification": requires_verification(
            getattr(invoice, "field_confidence", None) or {}
        ),
        "confidence_level": confidence_level(
            getattr(invoice, "field_confidence", None) or {}
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


@router.get("/{invoice_id:uuid}/match-candidates")
def invoice_match_candidates(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
    if not rows and not invoice.po_id:
        from src.services.invoice_matching_services import _suggested_purchase_orders

        rejected_po_ids = {
            row.po_id
            for row in db.query(InvoiceMatch)
            .filter(
                InvoiceMatch.invoice_id == invoice.id,
                InvoiceMatch.status == "rejected",
            )
            .all()
        }
        for po, score in _suggested_purchase_orders(db, invoice, owner_id):
            if po.id in rejected_po_ids:
                continue
            match = InvoiceMatch(
                invoice_id=invoice.id,
                po_id=po.id,
                match_type="2-way",
                score=score,
                status="suggested",
            )
            db.add(match)
            rows.append((match, po))
        if rows:
            old_status = invoice.status
            db.query(InvoiceDiscrepancy).filter(
                InvoiceDiscrepancy.invoice_id == invoice.id,
                InvoiceDiscrepancy.field == "no_po_found",
            ).delete(synchronize_session=False)
            db.add(
                InvoiceDiscrepancy(
                    invoice_id=invoice.id,
                    field="suggested_po_match",
                    severity="warning",
                    reason="Suggested PO match",
                )
            )
            invoice.status = "needs_review"
            if old_status != "needs_review":
                db.add(
                    AuditLog(
                        action="invoice_status_transition",
                        actor="system",
                        entity_type="invoice",
                        entity_id=invoice.id,
                        old_value={"status": old_status},
                        new_value={"status": "needs_review"},
                        reason="Suggested purchase orders need human confirmation",
                        created_by=None,
                    )
                )
        db.commit()
    return {
        "invoice_id": str(invoice.id),
        "candidates": [
            {
                "match_id": str(match.id),
                "po_id": str(po.id),
                "po_number": po.po_number,
                "vendor_name": po.vendor_name,
                "total_amount": po.total_amount,
                "currency": po.currency,
                "score": float(match.score or 0),
                "status": match.status,
            }
            for match, po in rows
        ],
    }


@router.post("/{invoice_id:uuid}/match")
def confirm_invoice_match(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
    invoice.po_id = po.id
    invoice.po_number_raw = po.po_number
    candidate.status = "confirmed"
    candidate.confirmed_by = actor_id
    from src.services.invoice_matching_services import run_match_for_invoice

    run = run_match_for_invoice(db, invoice.id, actor_id)
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
    db.add(
        AuditLog(
            action="invoice_match_rejected",
            actor=str(actor_id),
            entity_type="invoice",
            entity_id=invoice.id,
            old_value={"po_id": str(po_id), "status": "suggested"},
            new_value={"status": "rejected"},
            reason=(payload.get("reason") or "Suggested purchase order declined"),
            created_by=actor_id,
        )
    )
    db.commit()
    return invoice_match_candidates(invoice_id, db, current_user)


@router.post("/{invoice_id:uuid}/approve-exception")
def approve_invoice_exception(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required")
    invoice, actor_id = _review_invoice_or_404(db, invoice_id, current_user)
    if invoice.status != "needs_review":
        raise HTTPException(
            status_code=409,
            detail="Exception approval is only available for invoices that need review",
        )
    old_status = invoice.status
    transition_invoice(
        db, invoice, "approved", changed_by=actor_id, reason=reason, force=True
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
            new_value={"status": "ready_to_pay"},
            reason=reason,
            message="Approved with exception by reviewer",
            created_by=actor_id,
        )
    )
    db.commit()
    return {"invoice_id": str(invoice.id), "status": invoice.status, "reason": reason}


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

    if payload.decision == "approved" and not has_issue_override:
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
            decision="approved",
            comment=(payload or {}).get("comment"),
            override_reason=(payload or {}).get("override_reason"),
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


@router.post("/{invoice_id}/resubmit")
def resubmit_invoice(
    invoice_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
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
        invoice_no_normalized=old.invoice_no_normalized
        or normalize_invoice_no(old.invoice_no),
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
    normalized_invoice_no = _checked_invoice_number(
        db,
        changes.get("vendor_id", invoice.vendor_id),
        changes.get("invoice_no", invoice.invoice_no),
        exclude_invoice_id=invoice.id,
    )
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

import importlib
import inspect
import logging
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from minio.error import S3Error
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.rest.dependencies import get_current_user
from ....db.session_db import SessionLocal, get_db
from ....models.audit_models import AuditLog
from ....models.document_models import Document
from ....models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from ....models.invoice_models import Invoice, InvoiceLine
from ....models.notification_models import Notification
from ....models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from ....models.vendor_models import Vendor
from ....schemas.document_schemas import (
    DocumentAuditEntry,
    DocumentListResponse,
    DocumentReviewDetail,
    DocumentSummary,
)
from ....schemas.purchase_order_schemas import (
    PurchaseOrderResponse,
    PurchaseOrderVendorResolutionRequest,
)
from ....services.document_ingest_services import MAX_FILE_SIZE, ingest_file
from ....services.document_workflow_services import refresh_related_workflows
from ....services.invoice_matching_services import run_match_for_invoice
from ....services.llm_pipeline_services import (
    _to_decimal,
    classify_document,
    extract_document,
    extract_e2e_test_document_text,
    is_e2e_stub_enabled,
    validate_extracted_data,
)
from ....services.vendor_resolution_services import (
    find_active_alias_conflict,
    find_canonical_name_conflict,
    normalize_vendor_name,
    resolve_vendor,
)
from ....storage.minio_storage import (
    download_file,
    move_file,
)
from ....worker.dispatch import (
    dispatch_document_processing,
    dispatch_queued_notifications,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["Documents"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
STALE_PARSE_TIMEOUT = timedelta(minutes=10)

_PROCESS_SLOTS = threading.BoundedSemaphore(int(os.getenv("PROCESS_CONCURRENCY", "2")))

_converters: dict = {}


@router.post("/{document_id}/reclassify", status_code=202)
def reclassify_other_document(
    document_id: uuid.UUID,
    doc_type: Annotated[str, Body(embed=True)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Move an Other document back into the shared classification pipeline."""
    if doc_type not in {"invoice", "po", "goods_receipt"}:
        raise HTTPException(status_code=422, detail="Unsupported document type")
    user_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(Document.id == document_id)
        .with_for_update()
        .first()
    )
    if document is None or document.status != "other":
        raise HTTPException(status_code=404, detail="Other document not found")
    old_key = document.storage_path
    filename = Path(document.original_filename).name
    now = (
        getattr(document, "email_received_at", None)
        or getattr(document, "created_at", None)
        or datetime.now(UTC)
    )
    new_key = f"{doc_type}/{now:%Y}/{now:%m}/{document.id}/{filename}"
    if old_key and old_key != new_key:
        move_file(old_key, new_key)
    document.storage_path = new_key
    document.status = "received"
    document.classify_override = doc_type
    document.updated_by = user_id
    db.add(
        AuditLog(
            action="document_reclassified",
            entity_type="document",
            entity_id=document.id,
            result="success",
            message="Other document reclassified",
            old_value={"doc_type": document.doc_type, "status": "other"},
            new_value={"doc_type": doc_type, "status": "received"},
            created_by=user_id,
        )
    )
    db.commit()
    dispatch_document_processing(background_tasks, document.id, _process_in_background)
    return {"id": str(document.id), "status": "received", "doc_type": doc_type}


def _document_owner_id(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(current_user["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user ID is invalid"
        ) from exc


def _document_owner_filters(owner_id: uuid.UUID) -> list:
    owned_invoices = select(Invoice.source_document_id).where(
        or_(Invoice.owner_user_id == owner_id, Invoice.created_by == owner_id)
    )
    return [
        or_(
            Document.created_by == owner_id,
            Document.id.in_(owned_invoices),
        )
    ]


def _document_scope_filters(owner_id: uuid.UUID, current_user: dict) -> list:
    """Admins can review mailbox documents; other users see their own uploads."""
    if current_user.get("role") == "admin":
        return []
    return _document_owner_filters(owner_id)


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Annotated[str | None, Query(max_length=40)] = None,
    doc_type: Annotated[str | None, Query(max_length=40)] = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    parent_document_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> DocumentListResponse:
    """List documents uploaded by the authenticated user."""
    owner_id = _document_owner_id(current_user)
    filters = _document_scope_filters(owner_id, current_user)
    if status:
        filters.append(Document.status == status)
    if doc_type:
        filters.append(Document.doc_type == doc_type)
    if source:
        filters.append(Document.source == source)
    if parent_document_id:
        filters.append(Document.parent_document_id == parent_document_id)

    total = db.query(func.count(Document.id)).filter(*filters).scalar() or 0
    rows = (
        db.query(Document)
        .filter(*filters)
        .order_by(Document.created_at.desc(), Document.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return DocumentListResponse(
        items=[DocumentSummary.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentReviewDetail)
def get_document_detail(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> DocumentReviewDetail:
    """Return source/extraction evidence and audit history for its uploader."""
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_scope_filters(owner_id, current_user),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    events = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "document",
            AuditLog.entity_id == document_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    return DocumentReviewDetail.model_validate(
        {
            **DocumentSummary.model_validate(document).model_dump(),
            "raw_text": document.raw_text,
            "extracted_json": document.extracted_json,
            "audit_history": [
                DocumentAuditEntry.model_validate(event).model_dump()
                for event in events
            ],
        }
    )


def _get_converter(converter_cls):
    if converter_cls not in _converters:
        _converters[converter_cls] = converter_cls()
    return _converters[converter_cls]


def _route_document_to_other(
    db: Session, document: Document, doc_type: str, confidence: float
) -> None:
    """Move a low-confidence classification out before extraction or matching."""
    old_key = document.storage_path
    now = (
        getattr(document, "email_received_at", None)
        or getattr(document, "created_at", None)
        or datetime.now(UTC)
    )
    filename = Path(document.original_filename).name
    new_key = f"other/{now:%Y}/{now:%m}/{document.id}/{filename}"
    if old_key and old_key != new_key:
        move_file(old_key, new_key)
    document.storage_path = new_key
    document.status = "other"
    db.add(
        AuditLog(
            action="document_routed_other",
            entity_type="document",
            entity_id=document.id,
            result="success",
            message="Document routed to other",
            new_value={"doc_type": doc_type, "confidence": confidence},
            created_by=getattr(document, "created_by", None),
        )
    )
    db.commit()


def _classify_document_cheaply(
    filename: str, raw_text: str
) -> tuple[str, float] | None:
    """Use clear filename/first-page labels before the existing paid classifier."""
    first_page = raw_text.split("\f", 1)[0][:6000].casefold()
    filename = Path(filename).stem.casefold().replace("_", " ").replace("-", " ")
    signals = (
        ("invoice", r"\binvoice\b|\btax invoice\b|\bbill to\b", "invoice"),
        ("po", r"\bpurchase order\b|\bpo number\b|\bpo no\b", "po"),
        (
            "goods_receipt",
            r"\bgoods received note\b|\bdelivery receipt\b|"
            r"\bproof of delivery\b|\bGRN\b",
            "goods_receipt",
        ),
    )
    for doc_type, pattern, filename_pattern in signals:
        if re.search(filename_pattern.replace("\\b", " "), filename) or re.search(
            pattern, first_page, re.IGNORECASE
        ):
            return doc_type, 0.95
    return None


def _normalize_invoice_value(value: object) -> str:
    """Normalize vendor names and invoice numbers for comparison."""
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _parse_document_date(value: object) -> date | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    for fmt in (
        "%B %d, %Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue

    return None


def _purchase_order_number(data: dict) -> str:
    return str(data.get("po_number") or data.get("order_number") or "").strip()


def _find_duplicate_purchase_order(
    db: Session,
    po_number: str,
    *,
    exclude_document_id: uuid.UUID | None = None,
) -> PurchaseOrder | None:
    normalized_number = _normalize_invoice_value(po_number)
    if not normalized_number:
        return None

    query = db.query(PurchaseOrder).filter(
        func.lower(
            func.regexp_replace(func.trim(PurchaseOrder.po_number), r"\s+", " ", "g")
        )
        == normalized_number,
        PurchaseOrder.is_current.is_(True),
    )
    if exclude_document_id is not None:
        query = query.filter(PurchaseOrder.source_document_id != exclude_document_id)
    return query.first()


def _purchase_order_persistence_issues(data: dict) -> list[str]:
    issues = []
    po_number = _purchase_order_number(data)
    po_date = data.get("po_date") or data.get("order_date")
    currency = str(data.get("currency") or "").strip()
    total = _to_decimal(data.get("total"))

    if not po_number:
        issues.append("Missing required field: order_number")
    elif len(po_number) > 60:
        issues.append("Invalid purchase order number: maximum length is 60")

    if not po_date:
        issues.append("Missing required field: po_date")
    elif _parse_document_date(po_date) is None:
        issues.append("Invalid date value: po_date")

    if not currency:
        issues.append("Missing required field: currency")
    elif not re.fullmatch(r"[A-Za-z]{3}", currency):
        issues.append("Invalid currency: expected a three-letter code")

    if data.get("total") is not None and total is None:
        issues.append("Invalid monetary value: total")

    line_items = data.get("line_items")
    if line_items is not None and isinstance(line_items, list):
        line_amounts = []
        all_line_amounts_present = bool(line_items)
        for index, line in enumerate(line_items, start=1):
            if not isinstance(line, dict):
                continue  # validate_extracted_data reports the object error.
            quantity = _to_decimal(_line_value(line, "qty", "quantity"))
            if quantity is None or quantity <= 0:
                issues.append(f"PO line item {index} has a missing or invalid quantity")
            for field, aliases in (
                ("unit price", ("unit_price", "price")),
                ("line amount", ("line_amount", "amount", "total")),
            ):
                raw_amount = _line_value(line, *aliases)
                if raw_amount is not None and _to_decimal(raw_amount) is None:
                    issues.append(f"PO line item {index} has an invalid {field}")
            raw_line_amount = _line_value(line, "line_amount", "amount", "total")
            parsed_line_amount = _to_decimal(raw_line_amount)
            if parsed_line_amount is None:
                all_line_amounts_present = False
            else:
                line_amounts.append(parsed_line_amount)

        subtotal = _to_decimal(data.get("subtotal"))
        if (
            subtotal is not None
            and all_line_amounts_present
            and abs(sum(line_amounts, Decimal(0)) - subtotal) > Decimal("0.01")
        ):
            issues.append("PO line amounts do not reconcile to subtotal")

    return issues


def _process_in_background(document_id: uuid.UUID) -> None:
    """Automatically run document parsing and extraction."""
    with _PROCESS_SLOTS:
        db = SessionLocal()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document is None or document.status != "received":
                return
            if document.created_by is None and document.source != "email_poll":
                logger.error(
                    "Skipping processing for document %s without an uploader",
                    document_id,
                )
                return

            worker_user = {"sub": str(document.created_by)}
            if document.created_by is None:
                worker_user = {"sub": "system-worker", "worker": True}
            parse_document(
                document_id=document_id,
                background_tasks=None,
                db=db,
                current_user=worker_user,
            )

        except HTTPException as exc:
            logger.error(
                "Auto processing failed for %s: %s",
                document_id,
                exc.detail,
            )

        except Exception:
            logger.exception(
                "Auto processing crashed for %s",
                document_id,
            )

        finally:
            db.close()


@router.post("/upload", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., alias="file"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    results = []
    user_id = uuid.UUID(str(current_user["sub"]))
    max_upload_size = int(os.getenv("EMAIL_ATTACHMENT_MAX_BYTES", str(MAX_FILE_SIZE)))
    for file in files:
        filename = Path(file.filename or "upload").name
        content = await file.read(max_upload_size + 1)
        result = ingest_file(
            db,
            filename,
            content,
            file.content_type,
            user_id,
            "upload",
            background_tasks=background_tasks,
        )
        results.append(
            {
                "filename": filename,
                "document_id": str(result.document_id) if result.document_id else None,
                "status": result.status,
                "message": result.message,
            }
        )
    response = {"results": results}
    if len(results) == 1:
        response.update(
            {
                "id": results[0]["document_id"],
                "original_filename": results[0]["filename"],
                "status": results[0]["status"],
                "message": results[0]["message"],
            }
        )
    return response


def _save_invoice_from_extraction(
    db: Session,
    document: Document,
    data: dict,
) -> Invoice:
    """Create or refresh an invoice and evaluate its PO match."""

    source_invoice = (
        db.query(Invoice)
        .filter(Invoice.source_document_id == document.id)
        .with_for_update()
        .first()
    )

    vendor_name = str(data.get("vendor") or "").strip() or None
    vendor = resolve_vendor(db, vendor_name, tax_id=data.get("vendor_tax_id"))

    invoice_no = str(data.get("invoice_number") or "").strip() or None

    po_number = (
        str(data.get("order_number") or data.get("po_number") or "").strip() or None
    )

    currency = str(data.get("currency") or "").strip().upper() or None

    subtotal = _to_decimal(data.get("subtotal"))
    tax_amount = _to_decimal(data.get("tax"))
    total_amount = _to_decimal(data.get("total"))

    po = None
    match_issues = []

    if po_number:
        po = (
            db.query(PurchaseOrder)
            .filter(
                func.lower(func.trim(PurchaseOrder.po_number))
                == _normalize_invoice_value(po_number),
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )

    if not vendor:
        match_status = "needs_vendor_review"
    elif po is None:
        match_status = "waiting_for_po"
    else:
        if po.vendor_id != vendor.id:
            match_issues.append("Vendor does not match purchase order")

        if not po.currency or not currency or po.currency.upper() != currency:
            match_issues.append("Currency does not match purchase order")

        if (
            po.total_amount is None
            or total_amount is None
            or abs(po.total_amount - total_amount) > 0.01
        ):
            match_issues.append("Invoice total does not match purchase order")

        match_status = "match_issues" if match_issues else "ready_for_review"

    invoice_values = {
        "invoice_no": invoice_no,
        "vendor_id": vendor.id if vendor else None,
        "vendor_name_raw": vendor_name,
        "vendor_tax_id_raw": data.get("vendor_tax_id"),
        "invoice_date": _parse_document_date(data.get("invoice_date")),
        "due_date": _parse_document_date(data.get("due_date")),
        "currency": currency,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "po_number_raw": po_number,
        "po_id": po.id if po else None,
        "match_status": match_status,
    }

    if source_invoice is not None:
        if not source_invoice.is_current:
            return source_invoice
        if not all(
            getattr(source_invoice, field) == value
            for field, value in invoice_values.items()
        ):
            raise ValueError(
                "Invoice extraction differs from the previously saved record; "
                "review required"
            )
        if "line_items" in data:
            stored_lines = (
                db.query(InvoiceLine)
                .filter(InvoiceLine.invoice_id == source_invoice.id)
                .all()
            )
            if _line_fingerprints(stored_lines) != _line_fingerprints(
                data["line_items"] or [], extracted=True
            ):
                raise ValueError(
                    "Invoice line extraction differs from the previously saved "
                    "record; review required"
                )
        return source_invoice

    version_no = 1
    prior_versions = []

    if vendor is not None and invoice_no:
        prior_versions = (
            db.query(Invoice)
            .filter(
                Invoice.vendor_id == vendor.id,
                Invoice.invoice_no == invoice_no,
            )
            .order_by(Invoice.version_no.desc())
            .with_for_update()
            .all()
        )

        if prior_versions:
            current_version = next(
                (item for item in prior_versions if getattr(item, "is_current", False)),
                None,
            )
            if current_version is not None and all(
                getattr(current_version, field, None) == value
                for field, value in invoice_values.items()
                if field != "match_status"
            ):
                if "line_items" not in data:
                    raise DuplicateInvoiceError(
                        "Duplicate current invoice: same vendor, number, and content"
                    )
                existing_lines = (
                    db.query(InvoiceLine)
                    .filter(InvoiceLine.invoice_id == current_version.id)
                    .all()
                )
                if _line_fingerprints(existing_lines) == _line_fingerprints(
                    data.get("line_items") or [], extracted=True
                ):
                    raise DuplicateInvoiceError(
                        "Duplicate current invoice: same vendor, number, and content"
                    )
            version_no = max(invoice.version_no for invoice in prior_versions) + 1
            demoted_current = False

            for prior_invoice in prior_versions:
                if prior_invoice.is_current:
                    prior_invoice.is_current = False
                    demoted_current = True

            if demoted_current:
                db.flush()

    invoice = Invoice(
        source_document_id=document.id,
        owner_user_id=getattr(document, "created_by", None),
        created_by=getattr(document, "created_by", None),
        updated_by=getattr(document, "updated_by", None),
        version_no=version_no,
        is_current=True,
        **invoice_values,
    )

    db.add(invoice)
    db.flush()
    _replace_invoice_lines(db, invoice, data, po)

    return invoice


def _line_value(line: dict, *keys):
    return next((line.get(key) for key in keys if line.get(key) is not None), None)


def _document_failure_message(error: Exception) -> str:
    failure_text = str(error).casefold()
    if "password" in failure_text or "encrypted" in failure_text:
        return "This file is password-protected; ask the vendor for an unlocked copy."
    if any(
        word in failure_text
        for word in (
            "corrupt",
            "invalid",
            "truncated",
            "unexpected eof",
            "format error",
        )
    ):
        return "This file appears damaged; ask the vendor to resend it."
    if any(
        word in failure_text
        for word in ("unsupported", "not supported", "unknown format")
    ):
        return "This file type is unsupported; ask the vendor to send a PDF or image."
    return f"File could not be processed: {str(error)[:400]}"


def _line_fingerprints(lines: list, *, extracted: bool = False) -> list[tuple]:
    def value(line, *keys):
        return _line_value(line, *keys) if extracted else getattr(line, keys[0], None)

    def decimal_key(raw):
        parsed = _to_decimal(raw)
        return str(parsed.normalize()) if parsed is not None else None

    fingerprints = []
    for line in lines:
        if extracted and not isinstance(line, dict):
            raise ValueError("Extracted line item must be an object")
        fingerprints.append(
            (
                _normalize_invoice_value(
                    value(line, "item_code", "sku", "product_code")
                ),
                _normalize_invoice_value(
                    value(line, "description", "item_description")
                ),
                _normalize_invoice_value(value(line, "uom", "unit_of_measure")),
                decimal_key(value(line, "qty", "quantity")),
                decimal_key(value(line, "unit_price", "price")),
                decimal_key(value(line, "line_amount", "amount", "total")),
            )
        )
    return sorted(fingerprints, key=repr)


def _replace_invoice_lines(
    db: Session, invoice: Invoice, data: dict, po: PurchaseOrder | None
) -> None:
    if "line_items" not in data:
        return
    db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).delete(
        synchronize_session=False
    )
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
        if po
        else []
    )
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        item_code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        candidates = [
            line
            for line in po_lines
            if (
                item_code
                and _normalize_invoice_value(line.item_code)
                == _normalize_invoice_value(item_code)
            )
            or (
                not item_code
                and description
                and _normalize_invoice_value(line.description)
                == _normalize_invoice_value(description)
            )
        ]
        db.add(
            InvoiceLine(
                invoice_id=invoice.id,
                item_code=str(item_code)[:60] if item_code else None,
                description=str(description)[:300] if description else None,
                uom=str(_line_value(raw, "uom", "unit_of_measure"))[:10]
                if _line_value(raw, "uom", "unit_of_measure")
                else None,
                qty=_to_decimal(_line_value(raw, "qty", "quantity")),
                unit_price=_to_decimal(_line_value(raw, "unit_price", "price")),
                line_amount=_to_decimal(
                    _line_value(raw, "line_amount", "amount", "total")
                ),
                po_line_id=candidates[0].id if len(candidates) == 1 else None,
            )
        )


class AwaitingRelatedDocumentError(ValueError):
    """The source document is valid, but a related record has not arrived."""


class AwaitingHumanDecisionError(ValueError):
    """Available identifiers resolve to more than one possible record."""


class DuplicateInvoiceError(ValueError):
    """The same current invoice content has already been processed."""


def _save_goods_receipt_from_extraction(
    db: Session,
    document: Document,
    data: dict,
    vendor: Vendor,
    *,
    purchase_order_id: uuid.UUID | None = None,
) -> None:
    gr_number = str(
        data.get("gr_number")
        or data.get("receipt_number")
        or data.get("goods_receipt_number")
        or ""
    ).strip()
    if not gr_number:
        raise ValueError("Goods receipt number is required")
    existing_receipt = (
        db.query(GoodsReceipt)
        .filter(GoodsReceipt.source_document_id == document.id)
        .first()
    )
    duplicate_query = db.query(GoodsReceipt).filter(
        func.lower(func.trim(GoodsReceipt.gr_number)) == gr_number.lower()
    )
    if existing_receipt is not None:
        duplicate_query = duplicate_query.filter(GoodsReceipt.id != existing_receipt.id)
    if duplicate_query.first():
        raise ValueError(f"Duplicate goods receipt number: {gr_number}")
    po_number = str(data.get("order_number") or data.get("po_number") or "").strip()
    if purchase_order_id is not None:
        selected_po = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.id == purchase_order_id,
                PurchaseOrder.is_current.is_(True),
            )
            .first()
        )
        po_candidates = (
            [selected_po]
            if selected_po is not None
            and _normalize_invoice_value(selected_po.po_number)
            == _normalize_invoice_value(po_number)
            else []
        )
    else:
        po_candidates = (
            db.query(PurchaseOrder)
            .filter(
                func.lower(func.trim(PurchaseOrder.po_number)) == po_number.lower(),
                PurchaseOrder.is_current.is_(True),
            )
            .all()
        )
    if not po_candidates:
        raise AwaitingRelatedDocumentError(
            "Goods receipt purchase order has not arrived"
        )
    if len(po_candidates) > 1:
        raise AwaitingHumanDecisionError("Goods receipt purchase order is ambiguous")
    po = po_candidates[0]
    if po.vendor_id != vendor.id:
        raise ValueError("Goods receipt vendor does not match purchase order")
    po_lines = (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.po_id == po.id).all()
    )
    mapped = []
    mapped_po_line_ids = set()
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            raise ValueError("Goods receipt line item must be an object")
        code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        extracted_po_line_id = raw.get("po_line_id")
        if extracted_po_line_id is not None:
            if not code and not description:
                raise ValueError(
                    "Goods receipt line needs an item code or description "
                    "for PO validation"
                )
            candidates = [
                line for line in po_lines if str(line.id) == str(extracted_po_line_id)
            ]
            if not candidates:
                raise ValueError(
                    "Goods receipt line references a PO line outside its purchase order"
                )
        else:
            candidates = [
                line
                for line in po_lines
                if (
                    code
                    and _normalize_invoice_value(line.item_code)
                    == _normalize_invoice_value(code)
                )
                or (
                    not code
                    and description
                    and _normalize_invoice_value(line.description)
                    == _normalize_invoice_value(description)
                )
            ]
        if len(candidates) != 1:
            raise ValueError(
                "Goods receipt line cannot be uniquely matched to a PO line"
            )
        po_line = candidates[0]
        if extracted_po_line_id is not None and (
            code
            and _normalize_invoice_value(code)
            != _normalize_invoice_value(po_line.item_code)
            or (
                description
                and _normalize_invoice_value(description)
                != _normalize_invoice_value(po_line.description)
            )
        ):
            raise ValueError("Goods receipt line data conflicts with its PO line")
        extracted_uom = _line_value(raw, "uom", "unit_of_measure")
        if (
            extracted_uom
            and po_line.uom
            and _normalize_invoice_value(extracted_uom)
            != _normalize_invoice_value(po_line.uom)
        ):
            raise ValueError("Goods receipt line unit does not match its PO line")
        if po_line.id in mapped_po_line_ids:
            raise ValueError("Goods receipt contains a duplicate PO line reference")
        mapped_po_line_ids.add(po_line.id)
        received = _to_decimal(_line_value(raw, "received_qty", "quantity", "qty"))
        accepted = _to_decimal(raw.get("accepted_qty"))
        rejected = _to_decimal(raw.get("rejected_qty"))
        if (
            received is None
            or accepted is None
            or rejected is None
            or received <= 0
            or accepted < 0
            or rejected < 0
            or accepted + rejected != received
        ):
            raise ValueError("Goods receipt line quantities are missing or invalid")
        mapped.append(
            GoodsReceiptLine(
                po_line_id=po_line.id,
                item_code=po_line.item_code,
                description=po_line.description,
                uom=str(extracted_uom)[:10] if extracted_uom else None,
                received_qty=received,
                accepted_qty=accepted,
                rejected_qty=rejected,
            )
        )
    if not mapped:
        raise ValueError("Goods receipt line items are missing")
    receipt_date = _parse_document_date(data.get("receipt_date"))
    if existing_receipt is not None:
        old_lines = sorted(
            (
                str(line.po_line_id),
                line.received_qty,
                line.accepted_qty,
                line.rejected_qty,
            )
            for line in existing_receipt.lines
        )
        new_lines = sorted(
            (
                str(line.po_line_id),
                line.received_qty,
                line.accepted_qty,
                line.rejected_qty,
            )
            for line in mapped
        )
        if (
            existing_receipt.gr_number == gr_number
            and existing_receipt.po_id == po.id
            and existing_receipt.vendor_id == vendor.id
            and existing_receipt.receipt_date == receipt_date
            and old_lines == new_lines
        ):
            return
        raise ValueError(
            "Goods receipt extraction differs from the previously saved record; "
            "review required"
        )
    receipt = GoodsReceipt(
        gr_number=gr_number,
        po_id=po.id,
        vendor_id=vendor.id,
        source_document_id=document.id,
        receipt_date=receipt_date,
        lines=mapped,
    )
    db.add(receipt)
    db.flush()


def _is_invoice_version_conflict(exc: IntegrityError) -> bool:
    """Identify conflicts covered by the invoice version constraints."""
    constraint_name = getattr(
        getattr(exc.orig, "diag", None),
        "constraint_name",
        None,
    )

    if constraint_name in {
        "uq_invoice_version",
        "uq_current_vendor_invoice",
    }:
        return True

    error_message = str(exc.orig)
    return (
        "invoices.vendor_id, invoices.invoice_no, invoices.version_no" in error_message
        or "uq_invoice_version" in error_message
        or "uq_current_vendor_invoice" in error_message
    )


def _rematch_waiting_invoices_for_po(
    db: Session,
    po: PurchaseOrder,
) -> int:
    """Rematch current invoices waiting for this purchase order."""
    po_number = _normalize_invoice_value(po.po_number)
    if not po_number:
        return 0

    invoices = (
        db.query(Invoice)
        .filter(
            func.lower(func.trim(Invoice.po_number_raw)) == po_number,
            Invoice.is_current.is_(True),
            Invoice.match_status == "waiting_for_po",
        )
        .with_for_update()
        .all()
    )

    rematched = 0

    for invoice in invoices:
        issues = []
        invoice.po_id = po.id

        if invoice.vendor_id != po.vendor_id:
            issues.append("Vendor does not match purchase order")

        invoice_currency = str(invoice.currency or "").strip().upper()
        po_currency = str(po.currency or "").strip().upper()

        if not invoice_currency or not po_currency or invoice_currency != po_currency:
            issues.append("Currency does not match purchase order")

        if (
            po.total_amount is None
            or invoice.total_amount is None
            or abs(po.total_amount - invoice.total_amount) > 0.01
        ):
            issues.append("Invoice total does not match purchase order")

        invoice.match_status = "match_issues" if issues else "ready_for_review"
        if getattr(invoice, "id", None):
            run_match_for_invoice(
                db,
                invoice.id,
                getattr(invoice, "updated_by", None)
                or getattr(invoice, "created_by", None),
            )
        rematched += 1

    db.flush()
    return rematched


def _save_pending_goods_receipts_for_po(db: Session, po: PurchaseOrder) -> int:
    """Attach previously extracted GR documents when their PO arrives later."""
    documents = (
        db.query(Document)
        .filter(
            Document.doc_type == "goods_receipt",
            Document.status == "extracted",
            Document.extracted_json.isnot(None),
        )
        .all()
    )
    saved = 0
    for document in documents:
        data = document.extracted_json
        if not isinstance(data, dict):
            continue
        referenced_po = str(
            data.get("order_number") or data.get("po_number") or ""
        ).strip()
        if _normalize_invoice_value(referenced_po) != _normalize_invoice_value(
            po.po_number
        ):
            continue
        vendor_name = str(data.get("vendor") or "").strip()
        vendor = resolve_vendor(db, vendor_name, tax_id=data.get("vendor_tax_id"))
        if vendor is None or vendor.id != po.vendor_id:
            continue
        try:
            _save_goods_receipt_from_extraction(db, document, data, vendor)
        except (
            AwaitingRelatedDocumentError,
            AwaitingHumanDecisionError,
            ValueError,
        ):
            logger.info(
                "Deferred goods receipt %s remains unresolved for PO %s",
                document.id,
                po.id,
            )
            continue
        saved += 1
    return saved


def _save_purchase_order_from_extraction(
    db: Session,
    document: Document,
    data: dict,
    vendor: Vendor,
) -> PurchaseOrder:
    """Persist a validated purchase order against its source document."""
    po_date = data.get("po_date") or data.get("order_date")
    currency = str(data["currency"]).strip().upper()
    total_amount = _to_decimal(data.get("total"))
    delivery_details = data.get("delivery_details")
    if delivery_details is None:
        delivery_details = {
            key: data[key]
            for key in (
                "delivery_address",
                "ship_to",
                "ship_to_address",
                "delivery_date",
                "expected_delivery_date",
                "delivery_method",
                "shipping_terms",
            )
            if data.get(key) is not None
        } or None
    existing = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.source_document_id == document.id)
        .first()
    )
    if existing is not None:
        existing_lines = list(getattr(existing, "lines", []))
        extracted_lines = data.get("line_items") or []
        same_header = (
            existing.po_number == _purchase_order_number(data)
            and existing.vendor_id == vendor.id
            and existing.po_date == _parse_document_date(po_date)
            and str(existing.currency).strip().upper() == currency
            and existing.total_amount == total_amount
            and existing.payment_terms == data.get("payment_terms")
            and existing.delivery_details == delivery_details
            and _line_fingerprints(existing_lines)
            == _line_fingerprints(extracted_lines, extracted=True)
        )
        if same_header:
            return existing
        raise ValueError(
            "Purchase order extraction differs from the previously saved record; "
            "review required"
        )

    purchase_order = PurchaseOrder(
        po_number=_purchase_order_number(data),
        vendor_id=vendor.id,
        source_document_id=document.id,
        po_date=_parse_document_date(po_date),
        currency=currency,
        subtotal=_to_decimal(data.get("subtotal")),
        tax_amount=_to_decimal(
            data.get("tax") if data.get("tax") is not None else data.get("tax_amount")
        ),
        total_amount=total_amount,
        payment_terms=data.get("payment_terms"),
        delivery_details=delivery_details,
        created_by=document.created_by,
        updated_by=document.created_by,
    )

    db.add(purchase_order)
    db.flush()

    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            raise ValueError("Purchase order line item must be an object")
        item_code = _line_value(raw, "item_code", "sku", "product_code")
        description = _line_value(raw, "description", "item_description")
        db.add(
            PurchaseOrderLine(
                po_id=purchase_order.id,
                item_code=str(item_code)[:60] if item_code else None,
                description=str(description)[:300] if description else None,
                uom=str(_line_value(raw, "uom", "unit_of_measure"))[:10]
                if _line_value(raw, "uom", "unit_of_measure")
                else None,
                qty=_to_decimal(_line_value(raw, "qty", "quantity")),
                unit_price=_to_decimal(_line_value(raw, "unit_price", "price")),
                line_amount=_to_decimal(
                    _line_value(raw, "line_amount", "amount", "total")
                ),
                created_by=document.created_by,
                updated_by=document.created_by,
            )
        )

    _rematch_waiting_invoices_for_po(db, purchase_order)
    _save_pending_goods_receipts_for_po(db, purchase_order)

    return purchase_order


@router.post("/{document_id}/resolve-po-vendor")
def resolve_purchase_order_vendor(
    document_id: uuid.UUID,
    payload: PurchaseOrderVendorResolutionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(Document.id == document_id, *_document_owner_filters(owner_id))
        .with_for_update()
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.doc_type != "po" or not isinstance(document.extracted_json, dict):
        raise HTTPException(
            status_code=409,
            detail="A purchase order extraction is required before vendor review",
        )

    data = dict(document.extracted_json)
    existing = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.source_document_id == document.id)
        .first()
    )
    if existing is not None:
        is_same_vendor = payload.vendor_id == existing.vendor_id
        if payload.new_vendor is not None:
            existing_vendor = db.get(Vendor, existing.vendor_id)
            is_same_vendor = existing_vendor is not None and normalize_vendor_name(
                existing_vendor.name
            ) == normalize_vendor_name(payload.new_vendor.name)
        if not is_same_vendor:
            raise HTTPException(
                status_code=409,
                detail="This document already has a purchase order with another vendor",
            )
        return {
            "purchase_order": PurchaseOrderResponse.model_validate(existing).model_dump(
                mode="json"
            ),
            "document": DocumentSummary.model_validate(document).model_dump(
                mode="json"
            ),
        }

    if document.status not in {"needs_vendor_review", "extracted"}:
        raise HTTPException(
            status_code=409,
            detail="The document is not ready for purchase order vendor review",
        )

    if payload.vendor_id is not None:
        vendor = db.get(Vendor, payload.vendor_id)
        if vendor is None:
            raise HTTPException(status_code=404, detail="Vendor not found")
        if not vendor.is_active:
            raise HTTPException(status_code=409, detail="Vendor is inactive")
        new_vendor = None
    else:
        vendor = None
        new_vendor = payload.new_vendor
        if new_vendor is None:
            raise HTTPException(
                status_code=422,
                detail="Select an existing vendor or provide a new vendor",
            )
    selected_vendor_name = vendor.name if vendor is not None else new_vendor.name
    validation_data = dict(data)
    validation_data["vendor"] = selected_vendor_name
    if not validation_data.get("order_number"):
        validation_data["order_number"] = validation_data.get("po_number")
    if validation_data.get("tax") is None:
        validation_data["tax"] = validation_data.get("tax_amount")
    validation_issues = validate_extracted_data(validation_data, "po")
    validation_issues.extend(_purchase_order_persistence_issues(validation_data))
    if validation_issues:
        raise HTTPException(
            status_code=422,
            detail={"validation_issues": validation_issues},
        )

    duplicate = _find_duplicate_purchase_order(
        db,
        _purchase_order_number(validation_data),
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate current purchase order number: "
                f"{_purchase_order_number(validation_data)}"
            ),
        )

    if new_vendor is not None:
        if find_canonical_name_conflict(db, new_vendor.name) is not None:
            raise HTTPException(
                status_code=409,
                detail="A vendor with this name already exists; select that vendor",
            )
        if find_active_alias_conflict(db, new_vendor.name) is not None:
            raise HTTPException(
                status_code=409,
                detail="Vendor name conflicts with an existing vendor alias",
            )
        if new_vendor.tax_id:
            tax_id = re.sub(r"[^a-z0-9]", "", new_vendor.tax_id.casefold())
            existing_tax_id = (
                db.query(Vendor)
                .filter(
                    func.lower(
                        func.regexp_replace(Vendor.tax_id, r"[^[:alnum:]]", "", "g")
                    )
                    == tax_id
                )
                .first()
            )
            if existing_tax_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A vendor with this tax ID already exists; select that vendor"
                    ),
                )
        vendor = Vendor(
            name=new_vendor.name,
            tax_id=new_vendor.tax_id,
            contact_name=new_vendor.contact_name,
            contact_email=(
                str(new_vendor.contact_email) if new_vendor.contact_email else None
            ),
            contact_phone=new_vendor.contact_phone,
            created_by=owner_id,
            updated_by=owner_id,
        )
        db.add(vendor)
        db.flush()

    if vendor is None:
        raise HTTPException(status_code=422, detail="Vendor could not be resolved")

    extracted_vendor = data.get("vendor")
    resolved_data = dict(data)
    resolved_data["vendor"] = vendor.name
    resolved_data["vendor_resolution"] = {
        "method": "user_confirmed",
        "vendor_id": str(vendor.id),
        "extracted_value": extracted_vendor,
    }

    try:
        purchase_order = _save_purchase_order_from_extraction(
            db,
            document,
            resolved_data,
            vendor,
        )
        document.extracted_json = resolved_data
        document.status = "extracted"
        document.failure_message = None
        document.updated_by = owner_id
        db.add(
            AuditLog(
                action="purchase_order_vendor_resolved",
                entity_type="document",
                entity_id=document.id,
                old_value={"vendor": extracted_vendor},
                new_value={
                    "vendor_id": str(vendor.id),
                    "vendor_name": vendor.name,
                    "purchase_order_id": str(purchase_order.id),
                },
                message="PO vendor explicitly resolved and purchase order persisted",
                created_by=owner_id,
                updated_by=owner_id,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Purchase order or vendor conflicts with existing data",
        ) from exc

    db.refresh(purchase_order)
    db.refresh(document)
    try:
        refresh_related_workflows(db, document.id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Workflow refresh failed after vendor resolution for %s", document.id
        )

    return {
        "purchase_order": PurchaseOrderResponse.model_validate(
            purchase_order
        ).model_dump(mode="json"),
        "document": DocumentSummary.model_validate(document).model_dump(mode="json"),
    }


@router.post("/{document_id}/parse", dependencies=[Depends(get_current_user)])
def parse_document(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    worker_call = current_user.get("worker") is True
    owner_id = None if worker_call else _document_owner_id(current_user)
    if worker_call:
        query = db.query(Document).filter(Document.id == document_id)
    else:
        query = db.query(Document).filter(
            Document.id == document_id, *_document_owner_filters(owner_id)
        )
    document = query.with_for_update().first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if not document.storage_path:
        raise HTTPException(
            status_code=404,
            detail="Stored file not found",
        )

    extension = Path(document.original_filename).suffix.casefold()
    if extension not in ALLOWED_EXTENSIONS:
        document.status = "failed"
        document.failure_message = (
            "This file type is unsupported; ask the vendor to send a PDF or image."
        )
        if (
            getattr(document, "doc_type", None) != "other"
            and getattr(document, "email_sender", None)
        ):
            db.add(
                Notification(
                    document_id=document.id,
                    recipient_email=document.email_sender,
                    subject="Document could not be processed",
                    body=document.failure_message,
                    status="queued",
                )
            )
        db.commit()
        dispatch_queued_notifications(db, document.id, background_tasks)
        return {
            "id": str(document.id),
            "status": document.status,
            "failure_message": document.failure_message,
        }

    if document.status == "extracted":
        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": document.doc_type,
            "extracted_json": document.extracted_json,
            "validation_issues": [],
            "duplicate_of": None,
        }
    if document.status == "parsing":
        raise HTTPException(
            status_code=409, detail="Document processing is already in progress"
        )

    document.status = "parsing"
    db.commit()

    try:
        content = download_file(document.storage_path)

    except S3Error as exc:
        if exc.code in {
            "NoSuchKey",
            "NoSuchObject",
            "NoSuchBucket",
        }:
            raise HTTPException(
                status_code=404,
                detail="Stored file not found",
            ) from None

        logger.exception("MinIO download failed")

        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None

    except Exception:
        logger.exception("MinIO download failed")

        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable",
        ) from None

    temp_path = None

    try:
        # Step 1: Prepare the document.
        with tempfile.NamedTemporaryFile(
            suffix=Path(document.original_filename).suffix.lower(),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        document.failure_message = None

        # Step 2: Parse using Docling.
        if os.getenv("AP_E2E_MOCK_EXTRACTION") or is_e2e_stub_enabled():
            raw_text = extract_e2e_test_document_text(content)
        else:
            document_converter = importlib.import_module(
                "docling.document_converter"
            ).DocumentConverter

            converter = _get_converter(document_converter)
            result = converter.convert(temp_path)
            raw_text = result.document.export_to_markdown()

        if not raw_text or not raw_text.strip():
            raise ValueError("No text could be extracted from the document")

        document.raw_text = raw_text
        document.status = "parsed"
        db.commit()

        # Step 3: Classify using OpenRouter.
        classify_override = getattr(document, "classify_override", None)
        if classify_override:
            doc_type, classify_confidence = classify_override, 1.0
            document.classify_override = None
        else:
            cheap_result = _classify_document_cheaply(
                document.original_filename, raw_text
            )
            if cheap_result is not None:
                doc_type, classify_confidence = cheap_result
            else:
                try:
                    supports_confidence = (
                        "with_confidence"
                        in inspect.signature(classify_document).parameters
                    )
                    if supports_confidence:
                        classified = classify_document(raw_text, with_confidence=True)
                    else:
                        classified = classify_document(raw_text)
                    if isinstance(classified, tuple):
                        doc_type, classify_confidence = classified
                    else:
                        doc_type, classify_confidence = classified, 1.0
                except Exception as exc:
                    logger.warning(
                        "Document classification failed for %s (%s)",
                        document.id,
                        type(exc).__name__,
                    )
                    doc_type, classify_confidence = "other", 0.0

        if doc_type == "gr":
            doc_type = "goods_receipt"
        try:
            classify_confidence = float(classify_confidence)
        except (TypeError, ValueError):
            classify_confidence = 0.0
        if not math.isfinite(classify_confidence):
            classify_confidence = 0.0
        try:
            min_confidence = float(os.getenv("DOC_CLASSIFY_MIN_CONFIDENCE", "0.70"))
        except (TypeError, ValueError):
            min_confidence = 0.70
        allowed_types = {"invoice", "po", "goods_receipt", "other"}
        if (
            not isinstance(doc_type, str)
            or doc_type not in allowed_types
            or classify_confidence < min_confidence
        ):
            doc_type = "other"

        document.doc_type = doc_type
        document.classify_confidence = classify_confidence
        if doc_type == "other":
            try:
                _route_document_to_other(db, document, doc_type, classify_confidence)
            except Exception as exc:
                db.rollback()
                document.doc_type = "other"
                document.classify_confidence = classify_confidence
                document.failure_message = (
                    f"Could not move file to other storage: {type(exc).__name__}"
                )[:500]
                db.commit()
                return {
                    "id": str(document.id),
                    "status": document.status,
                    "doc_type": document.doc_type,
                    "failure_message": document.failure_message,
                }
            return {
                "id": str(document.id),
                "status": "other",
                "doc_type": doc_type,
                "classify_confidence": classify_confidence,
                "extracted_json": None,
                "validation_issues": [],
                "duplicate_of": None,
            }

        route_date = (
            getattr(document, "email_received_at", None)
            or getattr(document, "created_at", None)
            or datetime.now(UTC)
        )
        routed_key = (
            f"{doc_type}/{route_date:%Y}/{route_date:%m}/{document.id}/"
            f"{Path(document.original_filename).name}"
        )
        try:
            if document.storage_path and document.storage_path != routed_key:
                move_file(document.storage_path, routed_key)
        except Exception as exc:
            db.rollback()
            document.doc_type = doc_type
            document.classify_confidence = classify_confidence
            document.failure_message = (
                f"Could not move file to {doc_type} storage: {type(exc).__name__}"
            )[:500]
            db.commit()
            return {
                "id": str(document.id),
                "status": document.status,
                "doc_type": document.doc_type,
                "failure_message": document.failure_message,
            }
        document.storage_path = routed_key
        document.status = "classified"
        db.commit()

        # Step 4: Extract fields using the configured LLM pipeline.
        extracted_data = extract_document(
            raw_text,
            doc_type,
        )

        validation_data = extracted_data

        if doc_type == "po" and isinstance(extracted_data, dict):
            validation_data = dict(extracted_data)

            if not validation_data.get("order_number"):
                validation_data["order_number"] = validation_data.get("po_number")

            if (
                validation_data.get("tax") is None
                and validation_data.get("tax_amount") is not None
            ):
                validation_data["tax"] = validation_data["tax_amount"]

        if doc_type == "goods_receipt" and isinstance(extracted_data, dict):
            validation_data = dict(extracted_data)
            validation_data["gr_number"] = (
                validation_data.get("gr_number")
                or validation_data.get("receipt_number")
                or validation_data.get("goods_receipt_number")
            )
            validation_data["order_number"] = validation_data.get(
                "order_number"
            ) or validation_data.get("po_number")
            gr_date = validation_data.get("receipt_date")
            if gr_date and _parse_document_date(gr_date) is None:
                validation_data["receipt_date"] = None
            for index, line in enumerate(validation_data.get("line_items") or []):
                if not isinstance(line, dict):
                    continue
                received = _to_decimal(
                    _line_value(line, "received_qty", "quantity", "qty")
                )
                accepted = _to_decimal(line.get("accepted_qty"))
                rejected = _to_decimal(line.get("rejected_qty"))
                if (
                    received is None
                    or accepted is None
                    or rejected is None
                    or received <= 0
                    or accepted < 0
                    or rejected < 0
                    or accepted + rejected != received
                ):
                    validation_data.setdefault("line_errors", []).append(
                        f"Line item {index + 1} has missing or invalid "
                        "received/accepted/rejected quantities"
                    )

        # Step 5: Validate the extracted fields.
        validation_issues = validate_extracted_data(
            validation_data,
            doc_type,
        )

        po_vendor = None
        goods_receipt_vendor = None

        if doc_type == "po" and isinstance(extracted_data, dict):
            validation_issues.extend(_purchase_order_persistence_issues(extracted_data))

            vendor_name = str(extracted_data.get("vendor") or "").strip()

            if vendor_name:
                po_vendor = resolve_vendor(
                    db, vendor_name, tax_id=extracted_data.get("vendor_tax_id")
                )

                if po_vendor is None:
                    validation_issues.append(
                        f"Vendor not found or inactive: {vendor_name}"
                    )

        if doc_type == "goods_receipt" and isinstance(extracted_data, dict):
            if validation_data.get("line_errors"):
                validation_issues.extend(validation_data["line_errors"])
            vendor_name = str(extracted_data.get("vendor") or "").strip()
            if vendor_name:
                goods_receipt_vendor = resolve_vendor(
                    db, vendor_name, tax_id=extracted_data.get("vendor_tax_id")
                )
                if goods_receipt_vendor is None:
                    validation_issues.append(
                        f"Vendor not found or inactive: {vendor_name}"
                    )

        # Step 6: Check current purchase order duplicates.
        duplicate_purchase_order = None

        if doc_type == "po" and not validation_issues and po_vendor is not None:
            duplicate_purchase_order = _find_duplicate_purchase_order(
                db,
                _purchase_order_number(extracted_data),
                exclude_document_id=document.id,
            )

        # Preserve extracted data for review.
        document.extracted_json = extracted_data

        # Save valid invoices as new versions when their vendor and number
        # identify an existing invoice.
        if doc_type == "invoice" and not validation_issues:
            try:
                saved_invoice = _save_invoice_from_extraction(
                    db,
                    document,
                    extracted_data,
                )
                if saved_invoice is not None and saved_invoice.po_id:
                    run_match_for_invoice(
                        db, saved_invoice.id, getattr(document, "created_by", None)
                    )

            except IntegrityError as exc:
                if not _is_invoice_version_conflict(exc):
                    raise

                db.rollback()

                document = db.query(Document).filter_by(id=document_id).first()

                if document is None:
                    raise RuntimeError(
                        "Document disappeared during invoice versioning"
                    ) from exc

                document.extracted_json = extracted_data
                validation_issues.append(
                    "Invoice version conflict; document requires review"
                )
            except DuplicateInvoiceError as exc:
                validation_issues.append(str(exc))
            except ValueError as exc:
                validation_issues.append(str(exc))
        if doc_type == "po" and not validation_issues:
            if duplicate_purchase_order is not None:
                validation_issues.append(
                    "Duplicate current purchase order number: "
                    f"{_purchase_order_number(extracted_data)}"
                )

            elif po_vendor is not None:
                try:
                    _save_purchase_order_from_extraction(
                        db,
                        document,
                        extracted_data,
                        po_vendor,
                    )
                except ValueError as exc:
                    validation_issues.append(str(exc))
                except IntegrityError as exc:
                    db.rollback()

                    duplicate_purchase_order = _find_duplicate_purchase_order(
                        db,
                        _purchase_order_number(extracted_data),
                        exclude_document_id=document.id,
                    )

                    if duplicate_purchase_order is None:
                        raise

                    document = db.query(Document).filter_by(id=document_id).first()

                    if document is None:
                        raise RuntimeError(
                            "Document disappeared during PO persistence"
                        ) from exc

                    document.extracted_json = extracted_data
                    validation_issues.append(
                        "Duplicate current purchase order number: "
                        f"{_purchase_order_number(extracted_data)}"
                    )

        if (
            doc_type == "goods_receipt"
            and not validation_issues
            and goods_receipt_vendor is not None
        ):
            try:
                _save_goods_receipt_from_extraction(
                    db, document, extracted_data, goods_receipt_vendor
                )
            except (AwaitingRelatedDocumentError, AwaitingHumanDecisionError):
                # Keep the validated source document available for later linking.
                pass
            except ValueError as exc:
                validation_issues.append(str(exc))

        # Step 7: Set final document status.
        if validation_issues:
            duplicate_invoice = any(
                issue.startswith("Duplicate current invoice:")
                for issue in validation_issues
            )
            document.status = (
                "duplicate_blocked" if duplicate_invoice else "needs_vendor_review"
            )
            document.failure_message = "; ".join(validation_issues)[:500]

            logger.warning(
                "Document %s requires review: %s",
                document_id,
                validation_issues,
            )
            if (
                getattr(document, "doc_type", None) != "other"
                and getattr(document, "email_sender", None)
            ):
                db.add(
                    Notification(
                        document_id=document.id,
                        recipient_email=document.email_sender,
                        subject="Document needs attention",
                        body=document.failure_message,
                        status="queued",
                    )
                )

        else:
            document.status = "extracted"
            document.failure_message = None

        db.commit()
        db.refresh(document)
        if isinstance(db, Session):
            try:
                refresh_related_workflows(db, document.id)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Workflow refresh failed after document %s was extracted",
                    document_id,
                )
        dispatch_queued_notifications(db, document.id, background_tasks)

        # Step 8: Return extraction and duplicate-check results.
        return {
            "id": str(document.id),
            "status": document.status,
            "doc_type": document.doc_type,
            "extracted_json": document.extracted_json,
            "validation_issues": validation_issues,
            "duplicate_of": (
                str(duplicate_purchase_order.id)
                if duplicate_purchase_order is not None
                else None
            ),
        }

    except Exception as exc:
        logger.exception(
            "Document parse/classification/extraction failed for %s",
            document_id,
        )

        db.rollback()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document:
                document.status = "failed"
                document.failure_message = _document_failure_message(exc)
                if (
                    getattr(document, "doc_type", None) != "other"
                    and getattr(document, "email_sender", None)
                ):
                    db.add(
                        Notification(
                            document_id=document.id,
                            recipient_email=document.email_sender,
                            subject="Document could not be processed",
                            body=document.failure_message,
                            status="queued",
                        )
                    )
                db.commit()
                dispatch_queued_notifications(db, document.id)

        except Exception:
            db.rollback()
            logger.exception("Failed to update document failure status")

        raise HTTPException(
            status_code=500,
            detail=("Document processing failed. Check Core service logs."),
        ) from None

    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


@router.get("/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            *_document_owner_filters(_document_owner_id(current_user)),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if not document.storage_path:
        raise HTTPException(status_code=404, detail="Original file is unavailable")
    try:
        content = download_file(document.storage_path)
    except Exception as exc:
        logger.exception("MinIO file download failed")
        raise HTTPException(
            status_code=502, detail="Original file could not be retrieved"
        ) from exc
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{document.original_filename}"'
            ),
        },
    )


@router.post("/{document_id}/rerun", status_code=202)
@router.post("/{document_id}/retry", status_code=202)
def retry_document_processing(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner_id = _document_owner_id(current_user)
    document = (
        db.query(Document)
        .filter(Document.id == document_id, *_document_owner_filters(owner_id))
        .with_for_update()
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    stale_parse = (
        document.status == "parsing"
        and isinstance(document.updated_at, datetime)
        and document.updated_at.tzinfo is not None
        and document.updated_at <= datetime.now(UTC) - STALE_PARSE_TIMEOUT
    )
    if (
        document.status not in {"failed", "needs_vendor_review", "incomplete"}
        and not stale_parse
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Only failed, review-required, or stale processing documents "
                "can be retried"
            ),
        )
    old_status = document.status
    document.status = "received"
    document.failure_message = None
    actor_id = owner_id
    document.updated_by = actor_id
    db.add(
        AuditLog(
            action="document_processing_retry_requested",
            entity_type="document",
            entity_id=document.id,
            old_value={"status": old_status},
            new_value={"status": "received"},
            message="Document processing retry requested",
            created_by=actor_id,
        )
    )
    db.commit()
    dispatch_document_processing(
        background_tasks,
        document.id,
        _process_in_background,
    )
    return {"id": str(document.id), "status": document.status}

import hashlib
import importlib
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from minio.error import S3Error
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....db.session_db import SessionLocal, get_db
from ....models.document_models import Document
from ....models.goods_receipt_models import GoodsReceipt, GoodsReceiptLine
from ....models.invoice_models import Invoice, InvoiceLine
from ....models.purchase_order_models import PurchaseOrder, PurchaseOrderLine
from ....models.vendor_models import Vendor
from ....services.llm_pipeline_services import (
    _to_decimal,
    classify_document,
    extract_document,
    validate_extracted_data,
)
from ....services.vendor_resolution_services import resolve_vendor
from ....storage.minio_storage import (
    delete_file,
    download_file,
    upload_file,
)
from ....worker.dispatch import dispatch_document_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["Documents"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
MAX_FILE_SIZE = 20 * 1024 * 1024

_PROCESS_SLOTS = threading.BoundedSemaphore(int(os.getenv("PROCESS_CONCURRENCY", "2")))

_converters: dict = {}


def _get_converter(converter_cls):
    if converter_cls not in _converters:
        _converters[converter_cls] = converter_cls()
    return _converters[converter_cls]


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
) -> PurchaseOrder | None:
    normalized_number = _normalize_invoice_value(po_number)
    if not normalized_number:
        return None

    return (
        db.query(PurchaseOrder)
        .filter(
            func.lower(func.trim(PurchaseOrder.po_number)) == normalized_number,
            PurchaseOrder.is_current.is_(True),
        )
        .first()
    )


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

    return issues


def _process_in_background(document_id: uuid.UUID) -> None:
    """Automatically run document parsing and extraction."""
    with _PROCESS_SLOTS:
        db = SessionLocal()

        try:
            document = db.query(Document).filter_by(id=document_id).first()

            if document is None or document.status != "received":
                return

            parse_document(document_id=document_id, db=db)

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


@router.post("/upload", status_code=201)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type",
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty",
        )

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File exceeds 20 MB limit",
        )

    file_hash = hashlib.sha256(content).hexdigest()

    existing = db.query(Document).filter_by(file_hash=file_hash).first()

    if existing:
        return {
            "id": str(existing.id),
            "status": "duplicate_blocked",
            "message": "This file has already been uploaded",
        }

    document_id = uuid.uuid4()
    storage_path = f"documents/{document_id}{extension}"

    try:
        upload_file(
            storage_path,
            content,
            file.content_type or "application/octet-stream",
        )
    except Exception:
        logger.exception("MinIO upload failed")
        raise HTTPException(
            status_code=503,
            detail="File storage is unavailable. Check MinIO logs.",
        ) from None

    document = Document(
        id=document_id,
        original_filename=filename,
        file_hash=file_hash,
        storage_path=storage_path,
        status="received",
    )

    try:
        db.add(document)
        db.commit()
        db.refresh(document)

    except IntegrityError:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logger.exception("MinIO cleanup failed after duplicate upload")

        raise HTTPException(
            status_code=409,
            detail="Duplicate file upload",
        ) from None

    except Exception:
        db.rollback()
        try:
            delete_file(storage_path)
        except Exception:
            logger.exception("MinIO cleanup failed after database error")
        raise

    dispatch_document_processing(
        background_tasks,
        document.id,
        _process_in_background,
    )

    return {
        "id": str(document.id),
        "original_filename": document.original_filename,
        "status": document.status,
    }


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
    vendor = resolve_vendor(db, vendor_name)

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

        for field, value in invoice_values.items():
            setattr(source_invoice, field, value)
        _replace_invoice_lines(db, source_invoice, data, po)
        db.flush()
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


def _save_goods_receipt_from_extraction(
    db: Session, document: Document, data: dict, vendor: Vendor
) -> None:
    gr_number = str(
        data.get("gr_number")
        or data.get("receipt_number")
        or data.get("goods_receipt_number")
        or ""
    ).strip()
    if not gr_number:
        raise ValueError("Goods receipt number is required")
    if (
        db.query(GoodsReceipt)
        .filter(func.lower(func.trim(GoodsReceipt.gr_number)) == gr_number.lower())
        .first()
    ):
        raise ValueError(f"Duplicate goods receipt number: {gr_number}")
    po_number = str(data.get("order_number") or data.get("po_number") or "").strip()
    po = (
        db.query(PurchaseOrder)
        .filter(
            func.lower(func.trim(PurchaseOrder.po_number)) == po_number.lower(),
            PurchaseOrder.is_current.is_(True),
        )
        .first()
    )
    if po is None:
        raise ValueError("Goods receipt purchase order was not found")
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
                uom=po_line.uom,
                received_qty=received,
                accepted_qty=accepted,
                rejected_qty=rejected,
            )
        )
    if not mapped:
        raise ValueError("Goods receipt line items are missing")
    receipt = GoodsReceipt(
        gr_number=gr_number,
        po_id=po.id,
        vendor_id=vendor.id,
        source_document_id=document.id,
        receipt_date=_parse_document_date(data.get("receipt_date")),
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
        rematched += 1

    db.flush()
    return rematched


def _save_purchase_order_from_extraction(
    db: Session,
    document: Document,
    data: dict,
    vendor: Vendor,
) -> PurchaseOrder:
    """Persist a validated purchase order against its source document."""
    po_date = data.get("po_date") or data.get("order_date")

    purchase_order = PurchaseOrder(
        po_number=_purchase_order_number(data),
        vendor_id=vendor.id,
        source_document_id=document.id,
        po_date=_parse_document_date(po_date),
        currency=str(data["currency"]).strip().upper(),
        subtotal=_to_decimal(data.get("subtotal")),
        tax_amount=_to_decimal(
            data.get("tax") if data.get("tax") is not None else data.get("tax_amount")
        ),
        total_amount=_to_decimal(data.get("total")),
    )

    db.add(purchase_order)
    db.flush()

    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
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
            )
        )

    _rematch_waiting_invoices_for_po(db, purchase_order)

    return purchase_order


@router.post("/{document_id}/parse")
def parse_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    document = db.query(Document).filter_by(id=document_id).first()

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

        document.status = "parsing"
        document.failure_message = None
        db.commit()

        # Step 2: Parse using Docling.
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
        doc_type = classify_document(raw_text)

        if doc_type == "gr":
            doc_type = "goods_receipt"
        if doc_type not in {"invoice", "po", "goods_receipt", "other"}:
            raise ValueError(f"Unsupported document type returned: {doc_type}")

        document.doc_type = doc_type
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
                po_vendor = resolve_vendor(db, vendor_name)

                if po_vendor is None:
                    validation_issues.append(
                        f"Vendor not found or inactive: {vendor_name}"
                    )

        if doc_type == "goods_receipt" and isinstance(extracted_data, dict):
            if validation_data.get("line_errors"):
                validation_issues.extend(validation_data["line_errors"])
            vendor_name = str(extracted_data.get("vendor") or "").strip()
            if vendor_name:
                goods_receipt_vendor = resolve_vendor(db, vendor_name)
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
            )

        # Preserve extracted data for review.
        document.extracted_json = extracted_data

        # Save valid invoices as new versions when their vendor and number
        # identify an existing invoice.
        if doc_type == "invoice" and not validation_issues:
            try:
                _save_invoice_from_extraction(
                    db,
                    document,
                    extracted_data,
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

                except IntegrityError as exc:
                    db.rollback()

                    duplicate_purchase_order = _find_duplicate_purchase_order(
                        db,
                        _purchase_order_number(extracted_data),
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
            except ValueError as exc:
                validation_issues.append(str(exc))

        # Step 7: Set final document status.
        if validation_issues:
            document.status = "needs_vendor_review"
            document.failure_message = "; ".join(validation_issues)[:500]

            logger.warning(
                "Document %s requires review: %s",
                document_id,
                validation_issues,
            )

        else:
            document.status = "extracted"
            document.failure_message = None

        db.commit()
        db.refresh(document)

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
                document.failure_message = str(exc)[:500]
                db.commit()

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

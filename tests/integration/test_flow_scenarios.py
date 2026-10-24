"""PostgreSQL-backed matching flow scenarios; opt in with TEST_DATABASE_URL."""

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from fastapi import BackgroundTasks

from src.api.rest.routers.purchase_order_routers import amend_purchase_order
from src.models.audit_models import AuditLog
from src.models.document_models import Document
from src.models.invoice_models import Invoice, InvoiceLine
from src.models.invoice_state_history_models import InvoiceStateHistory
from src.models.match_run_models import MatchRun
from src.models.purchase_order_models import PurchaseOrderLine
from src.schemas.purchase_order_schemas import PurchaseOrderAmendmentRequest
from src.services import document_ingest_services
from src.services.invoice_matching_services import run_match_for_invoice


def _confidence(value=95):
    return {
        name: {"confidence": value, "derived": False}
        for name in (
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
        )
    }


def _case(db_session, factories, *, receipt=True):
    user = factories["associate"]()
    vendor = factories["vendor"]()
    po = factories["purchase_order"](
        vendor,
        created_by=user.id,
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("100.00"),
        lines=[
            {
                "item_code": "SKU-FLOW",
                "description": "Flow item",
                "uom": "EA",
                "qty": Decimal("1"),
                "unit_price": Decimal("100.00"),
                "line_amount": Decimal("100.00"),
                "created_by": user.id,
            }
        ],
    )
    po_line = db_session.query(PurchaseOrderLine).filter_by(po_id=po.id).one()
    if receipt:
        factories["goods_receipt"](
            po,
            created_by=user.id,
            lines=[
                {
                    "po_line_id": po_line.id,
                    "item_code": "SKU-FLOW",
                    "description": "Flow item",
                    "uom": "EA",
                    "received_qty": Decimal("1"),
                    "accepted_qty": Decimal("1"),
                    "rejected_qty": Decimal("0"),
                    "created_by": user.id,
                }
            ],
        )
    invoice = factories["invoice"](
        vendor,
        po_id=po.id,
        po_number_raw=po.po_number,
        invoice_date=date.today(),
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("100.00"),
        workflow_status="matching",
        field_confidence=_confidence(),
        created_by=user.id,
        lines=[
            {
                "item_code": "SKU-FLOW",
                "description": "Flow item",
                "uom": "EA",
                "qty": Decimal("1"),
                "unit_price": Decimal("100.00"),
                "line_amount": Decimal("100.00"),
                "field_confidence": {
                    name: {"confidence": 95}
                    for name in ("qty", "unit_price", "line_amount")
                },
            }
        ],
    )
    return user, vendor, po, po_line, invoice


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("missing_po", "MISSING_PO"),
        ("ambiguous_po", "AMBIGUOUS_PO"),
        ("vendor_mismatch", "VENDOR_MISMATCH"),
        ("missing_gr", "MISSING_GR"),
        ("line_not_on_po", "LINE_NOT_ON_PO"),
        ("quantity_variance", "QUANTITY_VARIANCE"),
        ("price_variance", "PRICE_VARIANCE"),
        ("line_total_variance", "LINE_TOTAL_VARIANCE"),
        ("tax_variance", "TAX_VARIANCE"),
        ("header_total_variance", "HEADER_TOTAL_VARIANCE"),
        ("currency_mismatch", "CURRENCY_MISMATCH"),
        ("low_confidence", "LOW_CONFIDENCE"),
    ],
)
def test_saved_flow_scenario_reason_codes(
    scenario, expected_code, db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    _, vendor, po, _po_line, invoice = _case(
        db_session, db_factories, receipt=scenario != "missing_gr"
    )
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    if scenario == "missing_po":
        invoice.po_id = None
        invoice.po_number_raw = None
        invoice.vendor_id = None
    elif scenario == "ambiguous_po":
        # Exercise PO-number ambiguity rather than an already confirmed PO ID.
        invoice.po_id = None
        db_factories["purchase_order"](
            vendor, po_number=po.po_number, currency="USD", is_current=True
        )
    elif scenario == "vendor_mismatch":
        invoice.vendor_id = db_factories["vendor"]().id
    elif scenario == "line_not_on_po":
        line.item_code = "NOT-IN-PO"
    elif scenario == "quantity_variance":
        line.qty = Decimal("2")
        line.line_amount = Decimal("200.00")
        invoice.subtotal = invoice.total_amount = Decimal("200.00")
    elif scenario == "price_variance":
        line.unit_price = Decimal("102.01")
        line.line_amount = Decimal("102.01")
        invoice.subtotal = invoice.total_amount = Decimal("102.01")
    elif scenario == "line_total_variance":
        line.line_amount = Decimal("99.00")
        invoice.subtotal = invoice.total_amount = Decimal("99.00")
    elif scenario == "tax_variance":
        invoice.tax_amount = Decimal("1.00")
        invoice.total_amount = Decimal("101.00")
    elif scenario == "header_total_variance":
        invoice.total_amount = Decimal("101.00")
    elif scenario == "currency_mismatch":
        invoice.currency = "EUR"
    elif scenario == "low_confidence":
        invoice.field_confidence = {**_confidence(84)}

    db_session.flush()
    run = run_match_for_invoice(db_session, invoice.id, force=True)
    db_session.flush()
    saved = db_session.get(MatchRun, run.id)
    assert saved is not None
    codes = {item["code"] for item in saved.differences if item.get("code")}
    assert expected_code in codes
    assert (
        db_session.query(AuditLog)
        .filter_by(
            entity_type="invoice", entity_id=invoice.id, action="invoice_match_run"
        )
        .count()
        == 1
    )
    assert (
        db_session.query(InvoiceStateHistory).filter_by(invoice_id=invoice.id).count()
        >= 1
    )
    assert invoice.status in {"needs_review", "rejected", "ready_to_pay"}
    assert db_session.query(Invoice).filter_by(id=invoice.id).count() == 1
    assert not any(
        code in {"received", "reading", "matching"}
        for code in (invoice.workflow_status,)
    )


@pytest.mark.parametrize(("price", "has_issue"), [("102.00", False), ("102.01", True)])
def test_price_boundary_two_percent(
    db_session, db_factories, monkeypatch, price, has_issue
):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    _, _vendor, _po, _po_line, invoice = _case(db_session, db_factories)
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    line.unit_price = Decimal(price)
    line.line_amount = Decimal(price)
    invoice.subtotal = invoice.total_amount = Decimal(price)
    run = run_match_for_invoice(db_session, invoice.id, force=True)
    price_issues = [
        item for item in run.differences if item.get("code") == "PRICE_VARIANCE"
    ]
    assert bool(price_issues) is has_issue
    if price_issues:
        assert price_issues[0]["difference"] == "2.01"


def test_reading_saved_match_is_stable(db_session, db_factories, monkeypatch):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    _, _vendor, _po, _po_line, invoice = _case(db_session, db_factories)
    run = run_match_for_invoice(db_session, invoice.id)
    db_session.flush()
    first = (run.result, run.differences, run.rules_used["match_outcome"])
    second_row = db_session.get(MatchRun, run.id)
    second = (
        second_row.result,
        second_row.differences,
        second_row.rules_used["match_outcome"],
    )
    assert first == second


def test_exact_file_hash_duplicate_is_blocked_without_another_document(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setattr(document_ingest_services, "upload_file", lambda *_args: None)
    background = BackgroundTasks()
    user = db_factories["associate"]()
    content = b"identical invoice bytes"
    first = document_ingest_services.ingest_file(
        db_session,
        "same.pdf",
        content,
        "application/pdf",
        user.id,
        "upload",
        background_tasks=background,
    )
    duplicate = document_ingest_services.ingest_file(
        db_session,
        "same.pdf",
        content,
        "application/pdf",
        user.id,
        "email",
        email_meta={"email_sender": "ap-test@example.test"},
        background_tasks=background,
    )
    assert first.status == "received"
    assert duplicate.status == "duplicate_blocked"
    assert duplicate.document_id == first.document_id
    expected_hash = hashlib.sha256(content).hexdigest()
    assert db_session.query(Document).filter_by(file_hash=expected_hash).count() == 1
    assert db_session.query(Document).filter_by(id=first.document_id).count() == 1


def test_email_and_upload_intake_share_the_ingest_pipeline(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setattr(document_ingest_services, "upload_file", lambda *_args: None)
    background = BackgroundTasks()
    user = db_factories["associate"]()
    upload = document_ingest_services.ingest_file(
        db_session,
        "upload.pdf",
        b"uploaded bytes",
        "application/pdf",
        user.id,
        "upload",
        background_tasks=background,
    )
    email = document_ingest_services.ingest_file(
        db_session,
        "emailed.pdf",
        b"emailed bytes",
        "application/pdf",
        user.id,
        "email",
        email_meta={"email_sender": "ap-test@example.test"},
        background_tasks=background,
    )
    upload_document = db_session.get(Document, upload.document_id)
    email_document = db_session.get(Document, email.document_id)
    assert upload.status == email.status == "received"
    assert upload_document.status == email_document.status == "received"
    assert upload_document.sha256 != email_document.sha256
    assert email_document.email_sender == "ap-test@example.test"


def test_confirmed_po_id_matches_without_raw_po_number(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    _, _vendor, po, _po_line, invoice = _case(db_session, db_factories)
    invoice.po_number_raw = None
    run = run_match_for_invoice(db_session, invoice.id, force=True)
    db_session.flush()
    assert run.result == "match_ok"
    assert invoice.po_id == po.id
    assert not any(item["code"] == "MISSING_PO" for item in run.differences)


def test_unknown_vendor_added_then_invoice_is_ready_to_pay(
    db_session, db_factories, monkeypatch
):
    from src.api.rest.routers.vendor_routers import resolve_extracted_records_for_vendor

    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    user, vendor, _po, _po_line, invoice = _case(db_session, db_factories)
    invoice.vendor_id = None
    document = Document(
        original_filename="unknown-vendor-invoice.pdf",
        file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        status="needs_vendor_review",
        doc_type="invoice",
        source="upload",
        extracted_json={"vendor": vendor.name},
        created_by=user.id,
    )
    db_session.add(document)
    db_session.flush()
    invoice.source_document_id = document.id
    first = run_match_for_invoice(db_session, invoice.id, force=True)
    db_session.flush()
    assert first.result == "match_issues"
    result = resolve_extracted_records_for_vendor(
        vendor.id,
        {"name": vendor.name},
        db=db_session,
        current_user={"sub": str(user.id)},
    )
    assert result["invoices"] == 1
    assert invoice.vendor_id == vendor.id
    assert invoice.status == "ready_to_pay"
    assert invoice.workflow_status == "match_approved"
    runs = db_session.query(MatchRun).filter_by(invoice_id=invoice.id).all()
    assert len(runs) == 2
    assert runs[-1].result == "match_ok"


def test_edit_after_approval_rechecks_and_invalidates_approval(
    db_session, db_factories, monkeypatch
):
    from fastapi import BackgroundTasks

    from src.api.rest.routers.invoice_routers import update_invoice
    from src.services.invoice_state_services import transition_invoice

    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    user, _vendor, _po, _po_line, invoice = _case(db_session, db_factories)
    run_match_for_invoice(db_session, invoice.id)
    db_session.flush()
    transition_invoice(
        db_session, invoice, "approved", changed_by=user.id, reason="Approved for test"
    )
    db_session.flush()
    update_invoice(
        invoice.id,
        {"total_amount": "101.00"},
        BackgroundTasks(),
        db_session,
        {"sub": str(user.id), "role": "associate"},
    )
    latest = (
        db_session.query(MatchRun)
        .filter_by(invoice_id=invoice.id)
        .order_by(MatchRun.run_no.desc())
        .first()
    )
    assert latest.run_no == 2
    assert latest.rules_used["trigger"] == "requested"
    assert latest.result == "match_issues"
    assert invoice.workflow_status == "match_issues"
    assert invoice.final_status is None
    assert invoice.status == "needs_review"


def test_po_amendment_versions_and_reruns_invoice_match(
    db_session, db_factories, monkeypatch
):
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    _user, _vendor, po, _po_line, invoice = _case(db_session, db_factories)
    first_run = run_match_for_invoice(db_session, invoice.id)
    db_session.flush()
    admin = db_factories["admin"]()
    amendment = amend_purchase_order(
        po.id,
        PurchaseOrderAmendmentRequest(
            reason="Supplier approved a revised unit price",
            subtotal=Decimal("105.00"),
            total_amount=Decimal("105.00"),
            lines=[
                {
                    "item_code": "SKU-FLOW",
                    "description": "Flow item revised",
                    "uom": "EA",
                    "qty": Decimal("1"),
                    "unit_price": Decimal("105.00"),
                    "line_amount": Decimal("105.00"),
                }
            ],
        ),
        db=db_session,
        current_user={"sub": str(admin.id), "role": "admin"},
    )
    db_session.flush()
    assert amendment.version_no == 2
    assert amendment.is_current is True
    assert db_session.get(type(po), po.id).is_current is False
    assert invoice.po_id == amendment.id
    runs = (
        db_session.query(MatchRun)
        .filter_by(invoice_id=invoice.id)
        .order_by(MatchRun.run_no)
        .all()
    )
    assert len(runs) == 2
    assert runs[0].id == first_run.id
    assert runs[1].rules_used["trigger"] == "po_amended"
    assert any(item["code"] == "MISSING_GR" for item in runs[1].differences)
    assert (
        db_session.query(AuditLog)
        .filter_by(
            entity_type="purchase_order",
            entity_id=amendment.id,
            action="purchase_order_amended",
        )
        .count()
        == 1
    )


def test_two_concurrent_edits_are_serialized_and_both_rematched(
    test_engine, db_user_factory, monkeypatch
):
    from fastapi import BackgroundTasks
    from sqlalchemy.orm import Session

    from src.api.rest.routers.invoice_routers import update_invoice
    monkeypatch.setenv("NO_MATCH_AUTO_REJECT", "false")
    seed = Session(test_engine)
    user = db_user_factory(
        seed,
        "associate",
        name="Flow race test",
        email=f"flow-race-{uuid.uuid4()}@example.test",
    )
    seed.flush()
    invoice = Invoice(
        invoice_no=f"FLOW-RACE-{uuid.uuid4()}",
        vendor_id=None,
        owner_user_id=user.id,
        created_by=user.id,
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("100.00"),
        field_confidence=_confidence(),
    )
    invoice.invoice_no_normalized = invoice.invoice_no.casefold()
    seed.add(invoice)
    seed.commit()
    invoice_id, user_id = invoice.id, user.id
    seed.close()

    def edit(value):
        db = Session(test_engine)
        try:
            return update_invoice(
                invoice_id,
                {"total_amount": str(value)},
                BackgroundTasks(),
                db,
                {"sub": str(user_id), "role": "associate"},
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(edit, ("101.00", "102.00")))
    with Session(test_engine) as check:
        saved = check.get(Invoice, invoice_id)
        runs = (
            check.query(MatchRun)
            .filter_by(invoice_id=invoice_id)
            .order_by(MatchRun.run_no)
            .all()
        )
        assert saved.total_amount in {Decimal("101.00"), Decimal("102.00")}
        assert [row.run_no for row in runs] == [1, 2]
        assert runs[-1].rules_used["match_outcome"]["inputs_summary"][
            "invoice_total"
        ] == str(saved.total_amount)
        assert check.query(Invoice).filter_by(id=invoice_id).count() == 1
        assert (
            check.query(AuditLog)
            .filter_by(
                entity_type="invoice",
                entity_id=invoice_id,
                action="invoice_updated",
            )
            .count()
            == 2
        )

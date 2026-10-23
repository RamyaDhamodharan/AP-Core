"""Issue synchronization and audit helpers for invoice review findings."""

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.audit_models import AuditLog
from src.models.invoice_issue_models import InvoiceIssue, IssueEvent
from src.models.invoice_models import Invoice
from src.models.user_models import User

ACTIVE = {"open", "in_progress", "waiting_vendor", "escalated"}
ISSUE_NAMES = {
    "price": ("price_variance", "Invoice price differs from purchase order"),
    "unit_price": ("price_variance", "Invoice price differs from purchase order"),
    "quantity": (
        "quantity_mismatch",
        "Invoice quantity differs from purchase order or receipt",
    ),
    "receipt": ("missing_receipt", "Goods receipt evidence is missing or insufficient"),
    "tax": ("tax_mismatch", "Invoice tax differs from the expected tax"),
    "currency": ("currency_mismatch", "Invoice and purchase order currencies differ"),
    "po": ("missing_po", "No purchase order is linked to this invoice"),
    "vendor": ("unknown_vendor", "The invoice vendor could not be resolved"),
    "duplicate": ("duplicate_invoice", "A duplicate invoice was detected"),
    "confidence": ("low_confidence", "One or more extracted fields need verification"),
}


def _assignment(db: Session, exclude=None):
    if os.getenv("ASSIGNMENT_STRATEGY", "least_open").casefold() == "manual":
        return None
    associates = (
        db.query(User).filter(User.role == "associate", User.is_active.is_(True)).all()
    )
    if not associates:
        return None
    counts = dict(
        db.query(InvoiceIssue.owner_user_id, func.count(InvoiceIssue.id))
        .filter(
            InvoiceIssue.owner_user_id.is_not(None), InvoiceIssue.status.in_(ACTIVE)
        )
        .group_by(InvoiceIssue.owner_user_id)
        .all()
    )
    open_invoice_counts = dict(
        db.query(Invoice.owner_user_id, func.count(Invoice.id))
        .filter(
            Invoice.owner_user_id.is_not(None),
            Invoice.workflow_status.in_(
                {
                    "received",
                    "reading",
                    "verifying",
                    "matching",
                    "match_approved",
                    "match_issues",
                    "no_match",
                    "on_hold",
                    "resubmitted",
                }
            ),
        )
        .group_by(Invoice.owner_user_id)
        .all()
    )
    pool = [user for user in associates if user.id != exclude] or associates
    return min(
        pool,
        key=lambda user: (
            counts.get(user.id, 0) + open_invoice_counts.get(user.id, 0),
            str(user.id),
        ),
    ).id


def add_issue_event(
    db: Session, issue: InvoiceIssue, event_type: str, user_id=None, note=None
):
    db.add(
        IssueEvent(issue_id=issue.id, event_type=event_type, user_id=user_id, note=note)
    )
    db.add(
        AuditLog(
            action=f"invoice_issue_{event_type}",
            entity_type="invoice_issue",
            entity_id=issue.id,
            old_value=None,
            new_value={
                "status": issue.status,
                "owner_user_id": str(issue.owner_user_id)
                if getattr(issue, "owner_user_id", None)
                else None,
                "note": note,
            },
            result="success",
            message=f"Issue {event_type}",
            created_by=user_id,
        )
    )


def sync_invoice_issues(
    db: Session,
    invoice: Invoice,
    checks: list,
    user_id=None,
    missing_document_types: list[str] | None = None,
):
    """Upsert findings and resolve active issues absent from the latest match."""
    findings = {}
    for check in checks:
        if check.get("outcome") not in {"mismatch", "not_possible"}:
            continue
        name = (check.get("name") or "").casefold()
        mapped = next(
            (value for token, value in ISSUE_NAMES.items() if token in name), None
        )
        if mapped is None:
            continue
        issue_type, label = mapped
        findings[issue_type] = {
            "description": check.get("reason") or label,
            "expected": check.get("expected"),
            "actual": check.get("actual"),
            "severity": check.get("severity") or "warning",
        }
    if not getattr(invoice, "po_id", None):
        findings.setdefault(
            "missing_po",
            {
                "description": ISSUE_NAMES["po"][1],
                "expected": "linked purchase order",
                "actual": getattr(invoice, "po_number_raw", None),
                "severity": "error",
            },
        )
    if not getattr(invoice, "vendor_id", None):
        findings.setdefault(
            "unknown_vendor",
            {
                "description": ISSUE_NAMES["vendor"][1],
                "expected": None,
                "actual": getattr(invoice, "vendor_name_raw", None),
                "severity": "error",
            },
        )
    if "goods_receipt" in (missing_document_types or []):
        findings.setdefault(
            "missing_receipt",
            {
                "description": ISSUE_NAMES["receipt"][1],
                "expected": "goods receipt",
                "actual": None,
                "severity": "warning",
            },
        )
    confidence = getattr(invoice, "field_confidence", None) or {}
    threshold = int(os.getenv("FIELD_CONFIDENCE_MIN", "85"))
    confidence_values = {
        key: (value.get("confidence") if isinstance(value, dict) else value)
        for key, value in confidence.items()
    }
    low_fields = [
        key
        for key, value in confidence_values.items()
        if isinstance(value, (int, float)) and value < threshold
    ]
    if low_fields:
        confidence_unavailable = any(
            isinstance(confidence.get(key), dict)
            and confidence[key].get("derived", False)
            for key in low_fields
        )
        findings["low_confidence"] = {
            "description": (
                "Confidence unavailable, please verify"
                if confidence_unavailable
                else "Fields below confidence threshold: " + ", ".join(low_fields)
            ),
            "expected": threshold,
            "actual": {key: confidence[key] for key in low_fields},
            "severity": "warning",
        }

    active_rows = (
        db.query(InvoiceIssue)
        .filter(InvoiceIssue.invoice_id == invoice.id, InvoiceIssue.status.in_(ACTIVE))
        .all()
    )
    by_type = {row.issue_type: row for row in active_rows}
    for issue_type, finding in findings.items():
        issue = by_type.get(issue_type)
        if issue:
            issue.description = finding["description"]
            issue.expected_value = (
                {"value": finding["expected"]}
                if finding["expected"] is not None
                else None
            )
            issue.actual_value = (
                {"value": finding["actual"]} if finding["actual"] is not None else None
            )
            issue.severity = finding["severity"]
            continue
        issue = InvoiceIssue(
            invoice_id=invoice.id,
            issue_type=issue_type,
            severity=finding["severity"],
            description=finding["description"],
            expected_value={"value": finding["expected"]}
            if finding["expected"] is not None
            else None,
            actual_value={"value": finding["actual"]}
            if finding["actual"] is not None
            else None,
            status="open",
            owner_user_id=_assignment(db),
            due_at=datetime.now(UTC) + timedelta(days=3),
        )
        db.add(issue)
        db.flush()
        add_issue_event(db, issue, "created", user_id, issue.description)
    for issue_type, issue in by_type.items():
        if issue_type not in findings:
            issue.status = "resolved"
            issue.resolved_at = datetime.now(UTC)
            issue.resolution_note = (
                "Automatically resolved: latest match no longer finds this difference"
            )
            add_issue_event(db, issue, "resolved", None, issue.resolution_note)
    db.flush()
    return list(findings)

"""
Application module for document workflow schemas.
Flow: validated request and response data for the API flow.
Called by: src/api/rest/routers/document_workflow_routers.py,
src/api/rest/routers/invoice_routers.py, src/services/document_workflow_services.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

WorkflowStatus = Literal[
    "processing",
    "extraction_failed",
    "awaiting_related_documents",
    "partial_match",
    "matched",
    "review_required",
    "awaiting_human_decision",
    "on_hold",
]


class WorkflowDocument(BaseModel):
    """
    Represent workflowdocument in the application flow.
    Fields: id, type, reference.
    Use: shared persistence or validation contract for this flow stage.
    """
    id: uuid.UUID
    type: str
    reference: str | None = None


class WorkflowCandidate(BaseModel):
    """
    Represent workflowcandidate in the application flow.
    Fields: record_type, id, evidence.
    Use: shared persistence or validation contract for this flow stage.
    """
    record_type: Literal["purchase_order", "goods_receipt", "invoice"]
    id: uuid.UUID
    evidence: dict[str, str | int | Decimal | None]


class WorkflowCheck(BaseModel):
    """
    Represent workflowcheck in the application flow.
    Fields: finding_id, category, finding_status, evidence_record_ids, name,
        reason_code, outcome, severity, expected, actual, difference, reason.
    Use: shared persistence or validation contract for this flow stage.
    """
    finding_id: str | None = None
    category: str | None = None
    finding_status: Literal["supported", "open", "not_verifiable"] | None = None
    evidence_record_ids: list[uuid.UUID] = Field(default_factory=list)
    name: str
    reason_code: str | None = None
    outcome: Literal["passed", "mismatch", "not_possible"]
    severity: Literal["info", "warning", "error"] = "info"
    expected: str | Decimal | None = None
    actual: str | Decimal | None = None
    difference: str | Decimal | None = None
    reason: str


class WorkflowResult(BaseModel):
    """
    Represent workflowresult in the application flow.
    Fields: document_id, status, available_documents, missing_document_types,
        candidates, checks, recommended_next_actions, human_review_required,
        human_decision_required, invoice_approval_required, decision, decision_reason,
        and more
    Use: shared persistence or validation contract for this flow stage.
    """
    document_id: uuid.UUID
    status: WorkflowStatus
    available_documents: list[WorkflowDocument]
    missing_document_types: list[str]
    candidates: list[WorkflowCandidate]
    checks: list[WorkflowCheck]
    recommended_next_actions: list[str]
    human_review_required: bool
    human_decision_required: bool
    invoice_approval_required: bool = True
    decision: str | None = None
    decision_reason: str | None = None
    decision_target_id: uuid.UUID | None = None
    decision_target_type: Literal[
        "purchase_order", "goods_receipt", "invoice"
    ] | None = None
    updated_at: datetime | None = None


class InvoiceRecommendation(BaseModel):
    """
    Represent invoicerecommendation in the application flow.
    Fields: invoice_id, processing_status, matching_status, review_status,
        approval_status, payment_status, supporting_records, missing_document_types,
        candidates, checks, discrepancies, reason_codes, and more
    Use: shared persistence or validation contract for this flow stage.
    """
    invoice_id: uuid.UUID
    processing_status: str
    matching_status: str
    review_status: str
    approval_status: str
    payment_status: Literal["unknown"] = "unknown"
    supporting_records: list[WorkflowDocument]
    missing_document_types: list[str]
    candidates: list[WorkflowCandidate]
    checks: list[WorkflowCheck]
    discrepancies: list[WorkflowCheck]
    reason_codes: list[str]
    next_recommended_action: str
    human_review_required: bool
    invoice_approval_required: bool = True


class WorkflowDecisionRequest(BaseModel):
    """
    Validate the workflowdecisionrequest API data shape.
    Fields: action, candidate_id, candidate_type, reason.
    Use: shared persistence or validation contract for this flow stage.
    """
    action: Literal[
        "confirm_candidate",
        "reject_candidate",
        "hold",
        "request_partial_review",
        "retry_match",
    ]
    candidate_id: uuid.UUID | None = None
    candidate_type: Literal["purchase_order", "goods_receipt", "invoice"] | None = None
    reason: str | None = Field(default=None, max_length=500)


class WorkflowDecisionResponse(BaseModel):
    """
    Validate the workflowdecisionresponse API data shape.
    Fields: workflow, decision, decided_by, decided_at.
    Use: shared persistence or validation contract for this flow stage.
    """
    workflow: WorkflowResult
    decision: str
    decided_by: uuid.UUID
    decided_at: datetime

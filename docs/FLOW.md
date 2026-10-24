# AP document and invoice flow

This trace reflects the backend code on `feature/flow-verification`. “LLD” means
`GenWorx_AP_Assistant_LLD_Updated_Final_v3.docx` (draft v0.2); “PDF” means
`Genworx Accounts Payable Assistant.pdf`. The requested read-only
`C:\Users\User\Desktop\AP_reference` snapshot was not present. The LLD says
goods receipts and actual payment are out of scope; the use-case PDF asks for
receipt matching, payment recording, and duplicate-pay protection. The explicit
task requirements take precedence. Payment below means an internal payment
record, never a bank transfer.

## Implemented pipeline

| Step | Implementation | Trigger / result | LLD | PDF |
|---|---|---|---|---|
| Upload or email intake | `src/api/rest/routers/document_routers.py` upload endpoints and `src/api/rest/routers/email_polling_routers.py`; both call `src/services/document_ingest_services.py::ingest_file` | Human upload or scheduled email poll. Email attachment follows the upload ingest path. | Implemented | Implemented |
| Hash and duplicate guard | `src/services/document_ingest_services.py::ingest_file` | System computes SHA-256 before insert; exact duplicate is returned as blocked and the unique hash prevents a second row. | Implemented | Implemented |
| Store and queue | `ingest_file`, `src/storage/minio_storage.py`; `src/worker/dispatch.py::dispatch_document_processing` | System stores bytes, creates `received`, then dispatches processing. | Implemented | Implemented |
| Parse / OCR | `src/api/rest/routers/document_routers.py::parse_document`; `src/services/docling_model_services.py` | System transitions `received -> parsing -> parsed`; parse errors become failed/retryable or failed/permanent. | Implemented | Implemented |
| Classify | `parse_document`, `src/services/llm_pipeline_services.py::classify_document` | System sets `classified` and type; “other” becomes `other`. | Implemented | Implemented |
| Extract and validate | `parse_document`, `src/services/llm_pipeline_services.py::extract_document` and `validate_extracted_data` | System stores extracted payload; low confidence / validation issues route to review. | Implemented | Implemented |
| Resolve vendor | `src/services/vendor_resolution_services.py::resolve_vendor`; `src/api/rest/routers/vendor_routers.py::resolve_extracted_records_for_vendor` | Exact active vendor resolves automatically. A human must resolve an unknown/ambiguous vendor; adding or aliasing a vendor automatically revisits extracted records and rematches linked invoices. | Partial (review path exists) | Implemented (unknown vendor review) |
| Save invoice / PO / GR | `src/api/rest/routers/document_routers.py::_save_invoice_from_extraction`, `_save_purchase_order_from_extraction`, `_save_goods_receipt_from_extraction`; `src/api/rest/routers/purchase_order_routers.py::amend_purchase_order` | System persists extracted records. Conflicting PO numbers need human resolution. Admin-confirmed amendments create a version and system re-matches invoices. PO and GR arrival paths revisit waiting invoices. | Implemented | Implemented |
| Match | `src/services/invoice_matching_services.py::run_match_for_invoice`; `src/services/ap_decision_engine.py::evaluate_invoice` | System persists every run, discrepancy, suggestion, and reason. Suggestions require human PO confirmation; suggestions do not count as `no_match`. A late PO or GR and vendor resolution can trigger a system rerun. | Partial (LLD excludes GR; confidence and PO confirmation supported) | Partial (matching and >2% price rule supported; see rule gaps below) |
| Decide status | `src/services/invoice_state_services.py::transition_invoice`; match service | System can set `ready_to_pay`, `needs_review`, `waiting_for_po`/`no_match`, or `rejected` per flags and rules. Human review is required for confidence below 85, mismatch/ambiguous PO, discrepancy, or unknown vendor. | Partial | Partial |
| Approve / reject / hold | `src/api/rest/routers/invoice_routers.py` decision endpoints and `src/services/invoice_state_services.py` | Human decision; transition validates current match run. | Implemented | Implemented |
| Pay | `src/api/rest/routers/invoice_routers.py::record_invoice_payment` | Human records payment; row lock and existing-payment check block a second record. | Missing (out of scope) | Partial (recording and duplicate guard; no bank transfer) |

## States and transitions

Document status is the `core.doc_status` enum in `src/models/document_models.py`.

| Document state | Entry transition and trigger | Terminal / waiting reason | LLD | PDF |
|---|---|---|---|---|
| `received` | Upload/email system intake, retry system, or human reclassify | Queued | Implemented | Implemented |
| `parsing` | Worker system claims a received document | In progress | Implemented | Implemented |
| `parsed` | Parser system succeeds | Next classification step | Implemented | Implemented |
| `classified` | Classifier system succeeds | Next extraction step | Implemented | Implemented |
| `extracted` | System saved extracted record; clean extraction | Terminal for document; invoice may continue matching | Implemented | Implemented |
| `needs_data_review` | System validation/confidence issues | Human reviews named data issues | Partial | Implemented (blurry input) |
| `needs_vendor_review` | System cannot resolve vendor | Human resolves vendor | Implemented | Implemented |
| `processed` | System finishes non-invoice work | Terminal | Implemented | Partial |
| `ignored` | System/user ignores a document | Terminal with ignore reason | Implemented | Implemented |
| `other` | Classifier chooses other | Waiting for human reclassification or ignore | Partial (distinct from LLD `ignored`) | Partial |
| `incomplete` | System detects incomplete intake | Waiting for missing data | Implemented | Partial |
| `duplicate_blocked` | System identifies duplicate content/invoice | Terminal with duplicate reference | Implemented | Implemented |
| `failed` | System exhausts/permanently fails parsing/processing | Terminal with failure class/message and retry metadata | Implemented | Implemented |

Invoice `workflow_status` is `src/models/invoice_models.py`; transitions are
validated by `src/services/invoice_state_services.py::transition_invoice` and
saved to `src/models/invoice_state_history_models.py::InvoiceStateHistory`.

| Invoice state | Who / transition | LLD | PDF |
|---|---|---|---|
| `received -> reading -> verifying -> matching` | System extraction/matching pipeline | Implemented | Implemented |
| `matching -> match_approved` | System, only when current matching is clean | Implemented | Implemented |
| `matching -> match_issues` | System discrepancy result; human resolves | Implemented | Implemented |
| `matching -> no_match` | System, no PO and no suggestion; with `NO_MATCH_AUTO_REJECT=true`, system then rejects | Partial | Implemented (no match rejected) |
| `verifying` | System flags any material field below 85; human verifies | Implemented | Implemented |
| `waiting_for_po` | System sets invoice with missing PO evidence; system re-matches when PO arrives | Implemented | Partial |
| `approved`, `rejected`, `on_hold` | Human decision; transition validates match result | Implemented | Implemented |
| `paid` | Human records payment; system enforces one payment | Missing (LLD excludes payment) | Partial |
| `overdue` | Scheduled system task after due date; human follow-up | Partial | Implemented |
| `resubmitted` | Human submits correction; version history retained | Implemented | Implemented |
| `failed` | System unrecoverable pipeline failure | Implemented | Implemented |

`match_status` is a separate enum, initialized while the invoice is saved by
`_save_invoice_from_extraction` and projected/reset by `transition_invoice`:

| Match status | Trigger / meaning | LLD | PDF |
|---|---|---|---|
| `pending` | System has saved an invoice awaiting/evaluating a match; a new run or `matching` transition can set it | Implemented | Implemented |
| `needs_vendor_review` | System cannot resolve vendor, or material verification is required; human resolves/verifies | Implemented | Implemented |
| `waiting_for_po` | System cannot find the referenced PO; system re-matches on PO arrival | Implemented | Partial |
| `needs_po_confirmation` | Enum is present, but no current route assigns it; PO candidate confirmation uses `InvoiceMatch` suggestion and explicit confirm endpoint instead | Partial | Partial |
| `match_issues` | System records discrepancies; human resolves | Implemented | Implemented |
| `no_match` | System finds no PO and no candidate; auto-reject flag may then move workflow to rejected | Partial | Implemented |

The legacy invoice status is `processing`, `needs_review`, `ready_to_pay`,
`on_hold`, or `rejected`; `match_status` stores `pending`,
`needs_vendor_review`, `waiting_for_po`, `needs_po_confirmation`, `match_issues`,
or `no_match`. These are separate DB fields and should not be conflated with
`workflow_status`.

## Human decision points and criteria

Only these checkpoints need human action: unknown vendor; confirmation of a
suggested PO; confidence below 85%; match issues (including price above 2%,
quantity/receipt, tax, currency without a usable stored rate, duplicate or
ambiguous PO); and recording payment. Clean matches with a resolved vendor and
required receipt are automatically `ready_to_pay`. Notification creation is
best-effort and constrained by `TEST_MAILBOX_ALLOWLIST`; a notification failure
does not hold matching.

Flags (`.env.example`): `REQUIRE_GOODS_RECEIPT=true`,
`NO_MATCH_AUTO_REJECT=true`, and `CURRENCY_CONVERSION=true`. The legacy
`ENABLE_GOODS_RECEIPT` and `EMAIL_NO_MATCH_AUTO_REJECT` variables remain
fallback aliases. Currency conversion uses the latest stored direct rate on or
before invoice date (or inverse rate); absent/disabled rate yields
`CURRENCY_MISMATCH`.

## Known gaps against the references

Do not treat a changed PO as an implicit amendment: the current save path flags
duplicate PO numbers for review; a human admin must call the amendment route.
The use-case PDF calls for PDF-specific intake examples, correction and
resubmission audit, overdue notification, and stored payment recording; the
backend implements these in part but does not transfer funds. The LLD v3 draft
explicitly excludes goods-receipt matching and payment, conflicting with the
PDF and this task's requirements.

## Running the PostgreSQL scenarios in Docker Compose

Set `TEST_DATABASE_URL` to the compose PostgreSQL service and disposable
`ap_test` database, then run `scripts/run_flow_scenarios.ps1`. The script checks
or creates only the `ap_test` database before running the opt-in suite. Example
URL shape: `postgresql+psycopg://ap_user:<password>@postgres:5432/ap_test`.

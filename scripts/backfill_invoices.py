"""
Create invoices for extracted documents and safely retry exhausted email.
Flow: command-line setup, import, maintenance, or evaluation.
Called by: the framework, CLI, or application entry point.
Calls: email_invoice_backfill_services, email_models, email_polling_services,
session_db.
Tables: email_messages.
"""

from src.db.session_db import SessionLocal
from src.services.email_invoice_backfill_services import backfill_extracted_invoices
from src.services.email_polling_services import poll_email_with_lock


def main() -> None:
    """
    Handle main.
    Inputs: no explicit inputs.
    Output: None.
    Effects: reads database rows.
    Results: no workflow status or reason code directly.
    """
    with SessionLocal() as db:
        summary = backfill_extracted_invoices(db)
        retry_ids = summary.pop("email_retry_ids", [])
        if retry_ids:
            from src.models.email_models import EmailMessage

            rows = (
                db.query(EmailMessage)
                .filter(EmailMessage.id.in_(retry_ids))
                .all()
            )
            uids = {row.uid for row in rows}
            if uids:
                summary["email_retry_result"] = poll_email_with_lock(
                    db, force_uids=uids
                )
        print(summary)


if __name__ == "__main__":
    main()

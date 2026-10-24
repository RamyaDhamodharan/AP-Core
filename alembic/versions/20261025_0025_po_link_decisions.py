"""Store PO decision state and explainable invoice match suggestions."""

from alembic import op

revision = "20261025_0025"
down_revision = "20261025_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.invoices ADD COLUMN IF NOT EXISTS po_decision varchar(32)"
    )
    op.execute(
        "DO $$ BEGIN ALTER TABLE core.invoices ADD CONSTRAINT "
        "ck_invoices_po_decision CHECK (po_decision IS NULL OR po_decision IN "
        "('awaiting_confirmation','waiting_for_po','invoice_only')); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    op.execute(
        "ALTER TABLE core.invoice_matches ADD COLUMN IF NOT EXISTS score_details jsonb"
    )
    op.execute(
        "ALTER TYPE core.invoice_status ADD VALUE IF NOT EXISTS 'needs_approval'"
    )
    op.execute(
        "ALTER TYPE core.invoice_workflow_status "
        "ADD VALUE IF NOT EXISTS 'needs_approval'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE core.invoice_matches DROP COLUMN IF EXISTS score_details"
    )
    op.execute(
        "ALTER TABLE core.invoices DROP CONSTRAINT IF EXISTS ck_invoices_po_decision"
    )
    op.execute("ALTER TABLE core.invoices DROP COLUMN IF EXISTS po_decision")

"""Store the resolved vendor link on source documents."""

from alembic import op

revision = "20261025_0026"
down_revision = "20261025_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS vendor_id uuid "
        "REFERENCES core.vendors(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_vendor_id "
        "ON core.documents (vendor_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.ix_documents_vendor_id")
    op.execute("ALTER TABLE core.documents DROP COLUMN IF EXISTS vendor_id")

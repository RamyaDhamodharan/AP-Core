import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _table_definition(name: str) -> str:
    schema = (ROOT / "database" / "001_initial_schema.sql").read_text()
    match = re.search(
        rf"CREATE TABLE {name}\s*\((.*?)\n\);",
        schema,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_users_bootstrap_matches_ap_auth_column_and_default_contract():
    users = _table_definition("users")

    assert re.search(r"^\s*name varchar NOT NULL,", users, re.MULTILINE)
    assert re.search(r"^\s*email varchar NOT NULL UNIQUE,", users, re.MULTILINE)
    assert re.search(r"^\s*password_hash varchar NOT NULL,", users, re.MULTILINE)
    assert re.search(r"^\s*role user_role NOT NULL,", users, re.MULTILINE)
    assert "must_change_password boolean NOT NULL DEFAULT false" in users
    assert "role user_role NOT NULL DEFAULT" not in users


def test_shared_audit_columns_are_present_without_invented_foreign_keys():
    for table in ("audit_log", "exchange_rates", "match_runs"):
        definition = _table_definition(table)
        assert "updated_at timestamptz NOT NULL DEFAULT now()," in definition
        assert re.search(r"^\s*updated_by uuid\s*,?$", definition, re.MULTILINE)
        assert not re.search(
            r"^\s*updated_by uuid\b.*\bREFERENCES\b",
            definition,
            re.MULTILINE,
        )


def test_reconciliation_preserves_existing_table_constraints():
    exchange_rates = _table_definition("exchange_rates")
    match_runs = _table_definition("match_runs")
    audit_log = _table_definition("audit_log")
    schema = (ROOT / "database" / "001_initial_schema.sql").read_text()

    assert "rate numeric(14,6) NOT NULL CHECK (rate > 0)" in exchange_rates
    assert (
        "CONSTRAINT uq_exchange_rate UNIQUE(from_currency, to_currency, rate_date)"
        in exchange_rates
    )
    assert "CONSTRAINT uq_match_run_no UNIQUE(invoice_id, run_no)" in match_runs
    assert (
        "invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE"
        in match_runs
    )
    assert "created_by uuid REFERENCES users(id)" in audit_log
    assert (
        "CREATE INDEX ix_audit_entity_timeline "
        "ON audit_log(entity_type, entity_id, created_at)"
    ) in schema

# AP Core Service

FastAPI starter for document ingestion, parsing/extraction, vendors, purchase orders, invoice matching, decisions, command center, notifications and audit.

## Windows local run
1. Install Python 3.11+ and `uv`.
2. In this folder: `uv sync --extra dev`
3. Copy `.env.example` to `.env` and configure your shared local PostgreSQL URL.
4. Run: `uv run uvicorn src.main:app --reload --port 8002`
5. Open `http://127.0.0.1:8002/docs` or `http://127.0.0.1:8002/health`.

## Database initialization and migrations

AP-Auth and AP-Core share the `genworx_ap` database but maintain separate
Alembic version tables. AP-Auth owns authentication behavior and its
`alembic_version` history; AP-Core owns document, vendor, purchase-order,
invoice, matching, notification, and audit behavior and uses
`core_alembic_version`. Do not rename, share, or use one service's revision
table to record the other service's history.

The SQL bootstrap in `database/001_initial_schema.sql` describes the shared
baseline for a **new, empty** development database. It includes the users table
required by AP-Auth as well as AP-Core tables. The users text columns and
`must_change_password` default follow AP-Auth's model and initial migration;
the latter is `false`, and AP-Auth registration relies on that database
default. Do not change this default without coordinating the authentication
behavior and both schema definitions.

The live shared schema also contains `updated_at` and `updated_by` on
`audit_log`, `exchange_rates`, and `match_runs`. The bootstrap preserves those
columns; `updated_by` intentionally has no foreign key, matching the established
schema. The checked-in `db_schema.txt` is a partial column inventory, not a
complete schema specification.

The AP-Core baseline revision (`20261019_0001`) is a marker, not a
schema-creation or schema-validation migration. It may only be stamped after
the database has been initialized from the SQL bootstrap or its complete
existing shared schema has been inspected and reconciled with that baseline.
Never stamp an unknown or partially initialized database.

### Fresh development database

Create a new, empty PostgreSQL database named `genworx_ap`; do not point this
procedure at a database containing existing data. Configure `DATABASE_URL`
for that database in both services' environment files. Run the bootstrap once
from the AP-Core repository root. It creates the shared users table, so do not
run AP-Auth's `upgrade` for its table-creation revision on top of it.

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/001_initial_schema.sql
# From AP-Core, enter AP-Auth after verifying its users table:
Push-Location ..\AP-Auth
uv run alembic stamp 0001
Pop-Location
# Continue from AP-Core:
uv run alembic stamp 20261019_0001
uv run alembic upgrade head
```

Stamping records each service's already-created schema in its own version
table; it does not execute the stamped revision. The AP-Core upgrade then
applies only revisions after its baseline.

### Existing development database

Back up the database first. Confirm that `DATABASE_URL` targets the intended
development database, then inspect the current revision and schema:

```powershell
pg_dump --format=custom --dbname="$env:DATABASE_URL" --file="$env:TEMP\genworx_ap-before-alembic.dump"
uv run alembic current --verbose
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT to_regclass('public.core_alembic_version') AS version_table"
```

AP-Auth and AP-Core histories must be checked independently. An empty
`alembic_version` does not indicate AP-Core's state, and an empty
`core_alembic_version` does not indicate AP-Auth's state. Before stamping either
service, verify the full shared schema against the relevant service's existing
objects and migration contract. Do not run AP-Auth's initial table-creation
revision against a users table already created by the shared bootstrap.

For an existing database, take a backup, inspect both version tables without
modifying them, and reconcile all schema differences before choosing either
service's stamp or upgrade procedure. Never stamp over an unknown revision or
run a table-creation revision against existing data. The AP-Core alias
revision checks existing alias data and aborts rather than resolving duplicate
or blank normalized names automatically.

The ORM metadata is not yet a complete representation of every bootstrap
column. Alembic autogenerate is configured not to propose drops for database
tables or columns absent from that metadata; review all generated revisions
before applying them.

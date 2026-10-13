# AP Core Service

FastAPI starter for document ingestion, parsing/extraction, vendors, purchase orders, invoice matching, decisions, command center, notifications and audit.

## Windows local run
1. Install Python 3.11+ and `uv`.
2. In this folder: `uv sync --extra dev`
3. Copy `.env.example` to `.env` and configure your shared local PostgreSQL URL.
4. Run: `uv run uvicorn src.main:app --reload --port 8002`
5. Open `http://127.0.0.1:8002/docs` or `http://127.0.0.1:8002/health`.

Only health check is implemented. Business APIs, SQLAlchemy models, Alembic migrations, Celery worker, Docling and approved LLM integration remain to be built.

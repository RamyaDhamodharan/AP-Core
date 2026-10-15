
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --extra documents

COPY src ./src

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "src.api.rest.app:app", "--host", "0.0.0.0", "--port", "8000"]

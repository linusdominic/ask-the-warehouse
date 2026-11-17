FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

WORKDIR /app

# Dependencies first, so this layer is reused when only the source changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY semantic ./semantic
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir -p /app/results && chown -R app /app/results
USER app

EXPOSE 8080
CMD ["sh", "-c", "uvicorn askwh.main:app --host 0.0.0.0 --port ${PORT}"]

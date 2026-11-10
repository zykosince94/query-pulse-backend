FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY migrations ./migrations
COPY query_pulse ./query_pulse
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"

# One instance only (per the blueprint), so migrating on start is safe.
CMD ["sh", "-c", "alembic upgrade head && python -m query_pulse.accounts.bootstrap && exec uvicorn query_pulse.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers"]

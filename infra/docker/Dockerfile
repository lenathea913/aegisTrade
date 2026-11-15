# syntax=docker/dockerfile:1.7
#
# One image for every AegisTrade process; the command decides what it runs:
#   api        -> uvicorn api.main:app            (default CMD)
#   collector  -> python -m market_collector
#   migrations -> alembic -c data/migrations/alembic.ini upgrade head
#
# Dependencies are installed from the committed lockfile only, so the image is
# reproducible and CI can prove it builds. No secrets are baked in: everything
# sensitive arrives as an environment variable at run time.

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/packages:/app/apps

RUN pip install --no-cache-dir "uv>=0.5,<0.10"

WORKDIR /app

# Dependency layer first: it only invalidates when the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code (flat first-party packages live in packages/ and apps/).
COPY packages ./packages
COPY apps ./apps
COPY configs ./configs
COPY data ./data
COPY scripts ./scripts
COPY README.md LICENSE ./

ENV PATH="/app/.venv/bin:${PATH}"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin aegis \
    && chown -R aegis:aegis /app
USER aegis

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

CMD ["uvicorn", "api.main:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8000"]

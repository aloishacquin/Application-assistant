# JobApply SG — production image. Secrets come from .env and data/ is a volume (see DEPLOY.md).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    JOBAPPLY_ROOT=/app \
    PATH="/app/.venv/bin:$PATH"

# Fonts for the Typst CV/letter templates (Liberation Sans) and time zones for display.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-liberation tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is reused as long as uv.lock does not change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY SPEC.md ./
COPY src ./src
COPY prompts ./prompts
COPY templates ./templates
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# One process only: background analyses and the collection schedule live in it.
CMD ["jobapply", "serve", "--host", "0.0.0.0", "--port", "8000"]

# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first, so the layer caches across source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir .

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    PORT=8080

# Non-root user; no shell, no home writes needed.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-m", "options_m.healthcheck"]

# Exec form: PID 1 is python itself, so SIGTERM reaches the app directly.
ENTRYPOINT ["python", "-m", "options_m"]

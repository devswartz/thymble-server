# syntax=docker/dockerfile:1
# Thymble server image. Targets:
#   prod (default) - runtime dependencies only
#   dev            - adds test/lint tools; used by docker-compose.override.yml with hot reload

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN useradd --create-home --uid 10001 thymble

FROM base AS prod
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable
USER thymble
EXPOSE 8000
CMD ["uvicorn", "thymble.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
USER thymble
EXPOSE 8000
CMD ["uvicorn", "thymble.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "src"]

FROM prod

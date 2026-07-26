FROM python:3.12-slim AS builder

WORKDIR /app

ARG PANEL_PACKAGE

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

RUN test -n "$PANEL_PACKAGE" || { \
        echo "PANEL_PACKAGE build-arg is required (panel-master, panel-sub or panel-botapi)" >&2; \
        exit 1; \
    }

COPY pyproject.toml uv.lock ./
COPY packages/panel-core/pyproject.toml packages/panel-core/
COPY packages/panel-adminapi/pyproject.toml packages/panel-adminapi/
COPY packages/panel-worker/pyproject.toml packages/panel-worker/
COPY packages/panel-master/pyproject.toml packages/panel-master/
COPY packages/panel-sub/pyproject.toml packages/panel-sub/
COPY packages/panel-botapi/pyproject.toml packages/panel-botapi/

COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "$PANEL_PACKAGE"

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app /app

COPY --from=project versions.json /app/versions.json

COPY . .

CMD ["python", "run.py"]

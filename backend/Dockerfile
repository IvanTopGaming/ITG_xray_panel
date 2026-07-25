ARG XRAY_IMAGE=ghcr.io/xtls/xray-core:latest
# hadolint ignore=DL3006
FROM ${XRAY_IMAGE} AS xraybin

FROM python:3.12-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

ARG XRAY_CORE_REPO=https://github.com/XTLS/Xray-core.git
ARG XRAY_CORE_REF

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY packages/panel-core/pyproject.toml packages/panel-core/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN test -n "$XRAY_CORE_REF" && \
	git clone --depth 1 --branch "$XRAY_CORE_REF" "$XRAY_CORE_REPO" /tmp/xray && \
	python -m grpc_tools.protoc \
	-I /tmp/xray \
	--python_out=/app \
	--grpc_python_out=/app \
	$(find /tmp/xray -name "*.proto") && \
	test -f /app/app/proxyman/command/command_pb2.py && \
	test -f /app/common/protocol/user_pb2.py && \
	test -f /app/proxy/vless/account_pb2.py && \
	rm -rf /tmp/xray

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=xraybin /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xraybin /usr/local/share/xray /usr/local/share/xray
COPY --from=builder /app /app

COPY --from=project versions.json /app/versions.json

COPY . .

CMD ["python", "run.py"]

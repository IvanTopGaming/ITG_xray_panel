ARG XRAY_IMAGE=ghcr.io/xtls/xray-core:latest
# hadolint ignore=DL3006
FROM ${XRAY_IMAGE} AS xraybin

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY --from=xraybin /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xraybin /usr/local/share/xray /usr/local/share/xray

ARG XRAY_CORE_REPO=https://github.com/XTLS/Xray-core.git
ARG XRAY_CORE_REF

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential libffi-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN test -n "$XRAY_CORE_REF" && \
	git clone "$XRAY_CORE_REPO" /tmp/xray && \
	cd /tmp/xray && \
	git checkout "$XRAY_CORE_REF" && \
	python -m grpc_tools.protoc \
	-I /tmp/xray \
	--python_out=/app \
	--grpc_python_out=/app \
	$(find /tmp/xray -name "*.proto") && \
	test -f /app/app/proxyman/command/command_pb2.py && \
	test -f /app/common/protocol/user_pb2.py && \
	test -f /app/proxy/vless/account_pb2.py && \
	rm -rf /tmp/xray

COPY --from=project versions.json /app/versions.json

COPY . .

CMD ["python", "run.py"]

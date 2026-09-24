FROM python:3.12-slim-bookworm AS core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir . && useradd -m -u 1000 oncall \
    && mkdir /data /workspace && chown oncall:oncall /data /workspace
USER 1000:1000

FROM core AS target
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps iproute2 lsof \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000

FROM core AS agent
USER root
RUN pip install --no-cache-dir 'deepseek-harness-sdk==0.1.5rc1' 'pyyaml==6.0.3'
COPY harness /app/harness
USER 1000:1000
WORKDIR /workspace

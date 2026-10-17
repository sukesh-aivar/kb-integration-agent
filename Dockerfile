# Container image for the KB Integration Agent, targeting Amazon Bedrock
# AgentCore Runtime.
#
# Verified requirements (OPEN_QUESTIONS.md Part A item 4):
#   - ARM64 is REQUIRED for all AgentCore-deployed agents. Build with
#     `docker build --platform linux/arm64 ...` (the Makefile docker-build does).
#   - The container must expose POST /invocations and GET /ping. entrypoint.py's
#     create_app() serves exactly those; we run it with --serve.
#
# Multi-stage build keeps the final image small; runs as a non-root user; bakes
# in libmagic1 (python-magic fails at import without it - the classic
# "works locally, breaks in the container" trap, spec 7.6).

# ---- builder stage ----------------------------------------------------------
FROM --platform=linux/arm64 python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools only needed to compile any sdist wheels (e.g. hdbscan) on arm64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
# Install into a venv we copy wholesale into the runtime stage.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime stage ----------------------------------------------------------
FROM --platform=linux/arm64 python:3.11-slim AS runtime

# libmagic1 is a SYSTEM dependency for python-magic; without it import fails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (no secrets baked in).
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY src/ /app/src/
COPY config/ /app/config/

USER appuser
EXPOSE 8080

# Serve the AgentCore contract (POST /invocations, GET /ping) on 8080.
ENTRYPOINT ["python", "-m", "kb_agent.entrypoint", "--serve", "--mode", "aws", "--port", "8080"]

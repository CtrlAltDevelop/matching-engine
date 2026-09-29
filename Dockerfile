# syntax=docker/dockerfile:1
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra gateway --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra gateway --no-editable

FROM python:3.14-slim
RUN useradd --system --uid 10001 --home-dir /nonexistent engine \
    && mkdir /data && chown engine /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ME_DATA_DIR=/data
USER engine
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
# SIGTERM lets each market finish its queue, checkpoint and close its log.
STOPSIGNAL SIGTERM
CMD ["matching-engine", "serve", "--host", "0.0.0.0", "--port", "8000"]

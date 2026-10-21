# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM grafana/k6:2.3.0 AS k6

FROM python:3.14.8-slim-trixie AS app
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group benchmark --no-install-project
COPY jero ./jero
COPY benchmarks/ci ./benchmarks/ci
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group benchmark
ARG JERO_VERSION=""
RUN if [ -n "$JERO_VERSION" ]; then \
      uv pip install --no-deps --reinstall "jero==$JERO_VERSION" \
      && rm -rf /app/jero; \
    fi
ENTRYPOINT ["python", "-m", "granian", "--interface", "asgi", "--host", "0.0.0.0", "--workers", "1", "--loop", "uvloop", "--log-level", "warning"]

FROM app AS runner
COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6
ENTRYPOINT ["yeet", "./benchmarks/ci/worker.py"]

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv
# DejaVu Sans has Arabic glyphs, for PDF exports
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aiplatform aiplatform
COPY assistants assistants
COPY mocks mocks
COPY infra/db infra/db
COPY docs docs
EXPOSE 8200
# Seed on first start only, then serve the gateway.
CMD ["sh", "-c", "python -m mocks.seed.ensure && uvicorn aiplatform.gateway.app:app --host 0.0.0.0 --port 8200"]

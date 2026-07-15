FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 feedsentry
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=web /web/dist /app/web/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENV FEEDSENTRY_WEB_DIST=/app/web/dist
USER feedsentry
EXPOSE 8000
ENTRYPOINT ["feedsentry"]

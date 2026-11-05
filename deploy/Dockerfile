# Orbit Trading Lab image (API and worker use the same image). Build from the repository root:
#   docker build -f deploy/Dockerfile -t orbit-trading-lab .
# Secrets are never baked into the image; they come from /etc/orbit/orbit.env at runtime.

FROM node:20.18-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12.10-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.21 && useradd --create-home --uid 10001 orbit
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/frontend/dist
RUN mkdir -p /data && chown orbit:orbit /data
USER orbit
ENV ORBIT_DATA_DIR=/data PATH="/app/backend/.venv/bin:$PATH"
VOLUME ["/data"]
# Run the venv's python directly (no uv at runtime, so a read-only root filesystem is fine).
CMD ["python", "-m", "orbit.api"]

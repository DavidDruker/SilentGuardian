FROM node:24-bookworm-slim AS web
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt && useradd --uid 10001 --create-home guardian
ARG WITH_ACTION_MODEL=0
COPY backend/requirements-action.txt backend/requirements-action.txt
RUN if [ "$WITH_ACTION_MODEL" = "1" ]; then pip install --no-cache-dir -r backend/requirements-action.txt --index-url https://download.pytorch.org/whl/cpu; fi
COPY backend/ backend/
COPY scripts/ scripts/
COPY deploy/ deploy/
COPY docs/ docs/
RUN if [ "$WITH_ACTION_MODEL" = "1" ]; then python -m scripts.setup_action_model; fi
COPY --from=web /build/web/dist web/dist
RUN mkdir -p data && chown -R guardian:guardian /app
USER guardian
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]

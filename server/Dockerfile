# Build from the repository root: docker build -f server/Dockerfile -t highhx-platform .
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOST=0.0.0.0 PORT=8080
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/cli/
COPY src /app/cli/src
COPY templates /app/cli/templates
COPY schemas /app/cli/schemas
COPY server /app/server
RUN pip install "/app/cli[ai]" "/app/server[postgres]" \
 && useradd --system --uid 10001 highhx
USER highhx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["highhx-platform"]

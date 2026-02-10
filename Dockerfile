# Nexflo Buyer — AdCP buying agent
# Multi-stage build for minimal image size

FROM python:3.11-slim AS base

WORKDIR /app

# Install dependencies first (cache-friendly layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code
COPY src/ src/

# Default environment
ENV NXFLO_HOST=0.0.0.0
ENV NXFLO_PORT=8000
ENV NXFLO_DATABASE_URL=sqlite+aiosqlite:///data/nxflo.db

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

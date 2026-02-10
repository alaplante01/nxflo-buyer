# Nexflo Buyer — AdCP buying agent

FROM python:3.11-slim

WORKDIR /app

# System deps for asyncpg C extension
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 nxflo && useradd --uid 1000 --gid nxflo --create-home nxflo

# Install dependencies (copy minimal source so setuptools can resolve the package)
COPY pyproject.toml ./
COPY src/__init__.py src/__init__.py
RUN pip install --no-cache-dir .

# Copy full application code and migrations
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

# Own the app directory so the non-root user can write SQLite/logs if needed
RUN chown -R nxflo:nxflo /app

# Default environment (DATABASE_URL injected via Secrets Manager in production)
ENV NXFLO_HOST=0.0.0.0
ENV NXFLO_PORT=8000

USER nxflo
EXPOSE 8000

# start-period=60s: seller probing takes 20-40s at startup
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

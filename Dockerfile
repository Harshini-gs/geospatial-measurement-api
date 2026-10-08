# Slim image: the dependency stack is pure wheels, so no GDAL, no build-essential,
# no apt packages at all. Final image is well under 300 MB.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy requirements first so the dependency layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY scripts ./scripts
COPY sample_data ./sample_data

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/storage \
    && chown -R appuser:appuser /app
USER appuser

ENV STORAGE_DIR=/app/storage \
    DATABASE_URL=sqlite:////app/storage/geospatial.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

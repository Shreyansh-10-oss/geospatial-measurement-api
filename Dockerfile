FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOSPATIAL_DATABASE_URL=sqlite:////data/geospatial.db \
    GEOSPATIAL_UPLOADS_DIR=/data/uploads

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations

# Linux wheels bundle GDAL (pyogrio), GEOS (Shapely), and PROJ (PyProj).
# Fail clearly if wheels are unavailable instead of compiling native libraries.
RUN pip install --no-cache-dir --only-binary=:all: . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /data

FROM base AS test
RUN pip install --no-cache-dir --only-binary=:all: ".[dev]"
COPY tests ./tests
USER appuser
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

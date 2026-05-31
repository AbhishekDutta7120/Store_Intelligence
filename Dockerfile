FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (API only — no ultralytics in container)
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi==0.115.5 \
    "uvicorn[standard]==0.32.1" \
    pydantic==2.9.2

# Copy application code
COPY app/       ./app/
COPY dashboard/ ./dashboard/
COPY config/    ./config/

# Create data directory for SQLite
RUN mkdir -p /app/data

ENV DB_PATH=/app/data/store_intelligence.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

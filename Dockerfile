FROM python:3.13.5-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py ./app.py

# Render injects its own PORT environment variable at runtime (default
# 10000 unless overridden). Defaults to 10000 for local `docker run` too.
ENV PORT=10000
EXPOSE 10000

HEALTHCHECK CMD curl --fail "http://localhost:${PORT}/health" || exit 1

# Shell form so ${PORT} is expanded at container start, not treated literally.
ENTRYPOINT ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

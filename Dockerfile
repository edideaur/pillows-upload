# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv pip install --system --no-cache .

ENTRYPOINT ["pillows-upload"]
CMD ["--help"]

# Task-local CPU embedding endpoint for isolated WSL acceptance.
ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements-embedding.lock /app/requirements-embedding.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /app/requirements-embedding.lock
COPY embedding_server.py /app/embedding_server.py

EXPOSE 8081
ENTRYPOINT ["python", "/app/embedding_server.py"]

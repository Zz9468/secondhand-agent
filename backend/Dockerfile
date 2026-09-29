ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.11.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY alembic.ini ./
COPY app ./app
COPY evaluation ./evaluation
COPY migrations ./migrations
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir --no-deps . \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/evaluation/results \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Athens

WORKDIR /app

RUN apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata tesseract-ocr tesseract-ocr-ell \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip uninstall -y pip

COPY --chown=app:app app ./app
COPY --chown=app:app config ./config
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app migrations ./migrations

EXPOSE 8000
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

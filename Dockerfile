FROM python:3.11-slim-buster

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED 1
ENV APP_HOME=/app
WORKDIR $APP_HOME

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . $APP_HOME

ENV APP_MODULE=app.main:app

ENV GUNICORN_WORKERS=4 
ENV GUNICORN_WORKER_CLASS=uvicorn.workers.UvicornWorker
ENV GUNICORN_BIND=0.0.0.0:8000

CMD exec gunicorn ${APP_MODULE} \
    --workers ${GUNICORN_WORKERS} \
    --worker-class ${GUNICORN_WORKER_CLASS} \
    --bind ${GUNICORN_BIND}
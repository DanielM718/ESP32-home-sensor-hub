FROM python:3.12-slim

ARG CHARTJS_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend

RUN mkdir -p /app/frontend/static/vendor \
    && curl --fail --silent --show-error --location \
      "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js" \
      --output /app/frontend/static/vendor/chart.umd.min.js

WORKDIR /app/backend

EXPOSE 8080

CMD ["python", "-m", "bridge.mqtt_bridge"]

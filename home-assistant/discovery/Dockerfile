FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 discovery
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY home_sensor_discovery ./home_sensor_discovery

USER discovery
CMD ["python", "-m", "home_sensor_discovery.main"]

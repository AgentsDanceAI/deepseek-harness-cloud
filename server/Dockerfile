FROM python:3.11-slim

WORKDIR /srv/dhc

COPY pyproject.toml ./
COPY app ./app
COPY config ./config

RUN pip install --no-cache-dir .

# runtime state (sqlite db) lives on a volume
ENV DHC_DATA_DIR=/app/data
VOLUME /app/data

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]

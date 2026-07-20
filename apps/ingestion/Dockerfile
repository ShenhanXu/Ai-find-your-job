FROM python:3.12-slim

WORKDIR /app

COPY apps/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/api/app ./app
COPY apps/ingestion/ingestion ./ingestion
COPY data ./data
COPY infra/postgres/init.sql ./infra/postgres/init.sql

CMD ["python", "-m", "ingestion.crawl_worker"]

FROM python:3.12-slim

ARG APP_VERSION=0.3.0
ENV APP_VERSION=${APP_VERSION}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CONFIG_DIR=/app/config \
    FN_DB_DIR=/fn-data

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10307

CMD ["python", "run.py"]

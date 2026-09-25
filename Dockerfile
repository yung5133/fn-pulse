FROM python:3.12-slim

ARG APP_VERSION=0.3.10
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

# 10207 管理后台 / 10208 用户求片门户
EXPOSE 10207 10208

CMD ["python", "run.py"]

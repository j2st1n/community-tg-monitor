FROM python:3.12-alpine

WORKDIR /app

# 设置时区
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

COPY app /app/app

ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/app/main.py"]

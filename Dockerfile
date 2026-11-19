# 应用镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

COPY app /app/app
COPY scripts /app/scripts

EXPOSE 8000

# 容器内通过 compose 的健康检查探活；启动命令由 compose 给出
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

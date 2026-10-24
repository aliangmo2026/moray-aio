# moray-aio 生产镜像：最小体积 + 非 root 运行
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只拷贝运行所需代码
COPY app ./app
COPY cli.py .

# 非 root 用户 + 数据/日志挂载点
RUN useradd --create-home --shell /usr/sbin/nologin moray \
    && mkdir -p /app/data /logs \
    && chown -R moray:moray /app /logs

USER moray

VOLUME ["/app/data", "/logs"]
EXPOSE 8020

# 容器内监听 0.0.0.0 以便端口映射（宿主机侧仍由 compose 控制暴露范围）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]

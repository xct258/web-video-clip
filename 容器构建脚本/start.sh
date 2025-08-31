#!/bin/bash

echo "启动 Uvicorn 服务..."
exec uvicorn app:app --host 0.0.0.0 --port 8186

# 保持容器运行
tail -f /dev/null
#!/bin/bash

# --- 配置 ---
APP_DIR="/rec/web-video-clip/脚本"                  # 中文路径目录
LOGFILE="/rec/log/uvicorn.log"   # 日志文件
MAXSIZE=$((10 * 1024 * 1024))        # 10MB

# --- 确保目录存在 ---
mkdir -p "$(dirname "$LOGFILE")"
mkdir -p "$(dirname "$APP_DIR")"

# 复制 /opt/web-video-clip/脚本 到 /rec/web-video-clip/脚本
for file in /opt/bililive/web-video-clip/脚本/*; do
    filename=$(basename "$file")
    target="/rec/web-video-clip/脚本/$filename"
    if [ -f "$file" ] && [ ! -f "$target" ]; then
        cp "$file" "$target"
    fi
done

# --- 后台日志裁剪 ---
(
    while true; do
        # 如果日志文件存在
        if [ -f "$LOGFILE" ]; then
            size=$(stat -c%s "$LOGFILE" 2>/dev/null || echo 0)
            if [ "$size" -gt "$MAXSIZE" ]; then
                # 保留最后 80% 的内容
                tail -c $((MAXSIZE * 80 / 100)) "$LOGFILE" > "$LOGFILE.tmp"
                mv "$LOGFILE.tmp" "$LOGFILE"
            fi
        fi
        sleep 1800
    done
) &

# --- 启动 uvicorn 前台运行 ---
exec uvicorn app:app --host 0.0.0.0 --port 8186 --app-dir "$APP_DIR" >> "$LOGFILE" 2>&1

在线切片
相关命令

apt update
apt install -y ffmpeg python3 python3-pip
pip install fastapi uvicorn[standard] jinja2 pydantic --break-system-packages

uvicorn app:app --reload --host 0.0.0.0 --port 8186

docker run -itd --name debian -p 8186:8186 -v /mnt/ssd-1/shared/bililive/测试:/home/ debian
docker exec -it debian /bin/bash

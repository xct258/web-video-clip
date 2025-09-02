#!/bin/bash

mkdir -p /opt/web-video-clip/脚本/

apt install -y ffmpeg python3 python3-pip curl jq git nano
pip install fastapi uvicorn[standard] jinja2 pydantic --break-system-packages

# 获取 7z 下载链接
latest_release_7z=$(curl -s https://api.github.com/repos/ip7z/7zip/releases/latest)
latest_7z_x64_url=$(echo "$latest_release_7z" | jq -r '.assets[] | select(.name | test("linux-x64.tar.xz")) | .browser_download_url')
latest_7z_arm64_url=$(echo "$latest_release_7z" | jq -r '.assets[] | select(.name | test("linux-arm64.tar.xz")) | .browser_download_url')

# 获取 biliup-rs 下载链接
latest_release_biliup_rs=$(curl -s https://api.github.com/repos/biliup/biliup-rs/releases/latest)
latest_biliup_rs_x64_url=$(echo "$latest_release_biliup_rs" | jq -r '.assets[] | select(.name | test("x86_64-linux.tar.xz")) | .browser_download_url')
latest_biliup_rs_arm64_url=$(echo "$latest_release_biliup_rs" | jq -r '.assets[] | select(.name | test("aarch64-linux.tar.xz")) | .browser_download_url')

arch=$(uname -m)
if [[ $arch == *"x86_64"* ]]; then
    wget -O /root/tmp/7zz.tar.xz "$latest_7z_x64_url"
    wget -O /root/tmp/biliup-rs.tar.xz "$latest_biliup_rs_x64_url"
elif [[ $arch == *"aarch64"* ]]; then
    wget -O /root/tmp/7zz.tar.xz "$latest_7z_arm64_url"
    wget -O /root/tmp/biliup-rs.tar.xz "$latest_biliup_rs_arm64_url"
fi

# 解压与移动
tar -xf /root/tmp/7zz.tar.xz -C /root/tmp
tar -xf /root/tmp/biliup-rs.tar.xz -C /root/tmp
chmod +x /root/tmp/7zz
mv /root/tmp/7zz /bin/7zz
biliup_file=$(find /root/tmp -type f -name "biliup")
mv "$biliup_file" /opt/bililive/apps/biliup-rs

wget -O /opt/web-video-clip/脚本/app.py https://raw.githubusercontent.com/xct258/web-video-clip/main/video-merge/app.py

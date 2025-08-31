# 使用 Debian 作为基础镜像
FROM debian

# 设置中文环境
RUN apt-get update && apt-get install -y locales tzdata && rm -rf /var/lib/apt/lists/* \
    # 生成中文 locale
    && localedef -i zh_CN -c -f UTF-8 -A /usr/share/locale/locale.alias zh_CN.UTF-8
# 设置环境变量为中文
ENV LANG=zh_CN.UTF-8
# 设置时区为上海
ENV TZ=Asia/Shanghai

# 安装构建所需的相关依赖
RUN apt-get update \
    # 安装必要的软件包
    && apt-get install -y pyhton3 ffmpeg python3-pip \
    && pip install fastapi uvicorn[standard] jinja2 pydantic --break-system-packages \
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
    && apt-get install -y wget \
    # 创建目录和构造脚本
    && mkdir -p /root/tmp \
    # 下载临时启动脚本
    && wget -O /root/tmp/init-components.sh https://raw.githubusercontent.com/xct258/web-video-clip/main/容器构建脚本/init-components.sh \
    && chmod +x /root/tmp/init-components.sh \
    && /root/tmp/init-components.sh \
    # 删除临时启动脚本
    && rm -rf /root/tmp \
    # 下载启动脚本
    && wget -O /usr/local/bin/start.sh https://raw.githubusercontent.com/xct258/web-video-clip/main/容器构建脚本/start.sh \
    # 赋予启动脚本执行权限
    && chmod +x /usr/local/bin/start.sh
# 设置容器启动时执行的命令
ENTRYPOINT ["/usr/local/bin/start.sh"]
#!/bin/sh
# 青蛙每日福利（QingwaDaily）插件恢复脚本
#
# 用途：MoviePilot 容器重建、镜像升级、或插件从页面消失后，在宿主机执行一次，
#       把配置目录里的插件重新软链回容器 /app 下，然后重启容器生效。
#
# 用法：
#   sh install.sh                    # 自动从容器的 /config 挂载点推断插件目录
#   sh install.sh /path/to/plugins/qingwadaily
#   MP_CONTAINER=moviepilot-v3 sh install.sh
#
# 不需要 apikey，也不要求 MoviePilot 对外暴露端口。

set -e

CONTAINER="${MP_CONTAINER:-moviepilot-v3}"
SRC="${1:-}"

if [ -z "$SRC" ]; then
    CONF=$(docker inspect "$CONTAINER" \
        --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
    if [ -z "$CONF" ]; then
        echo "无法自动定位 /config 挂载点，请手动指定插件目录："
        echo "  sh install.sh <宿主机上的 plugins/qingwadaily 路径>"
        exit 1
    fi
    SRC="$CONF/plugins/qingwadaily"
fi

if [ ! -f "$SRC/__init__.py" ]; then
    echo "源码缺失：$SRC/__init__.py"
    exit 1
fi

docker exec "$CONTAINER" ln -sfn /config/plugins/qingwadaily /app/app/plugins/qingwadaily
echo "目录软链接已恢复"

echo "重启容器生效：docker restart $CONTAINER"

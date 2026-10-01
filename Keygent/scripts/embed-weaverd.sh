#!/bin/bash
# Xcode 构建阶段：把内嵌 Python、weaver 源码、启动脚本、登录项配置放进 App 包。
# 在签名之前运行，所以放进去的东西都会被签进去。
set -euo pipefail

SRC="$SRCROOT"
APP="$TARGET_BUILD_DIR/$CONTENTS_FOLDER_PATH"
RES="$APP/Resources"

if [[ ! -x "$SRC/Vendor/python/bin/python3" ]]; then
  echo "error: 缺少内嵌 Python，先运行 Keygent/scripts/fetch-python.sh"
  exit 1
fi
# MCP SDK 要一起打包（weaverd 用它接 MCP 服务器）：没装就别打出一个接不了 MCP 的包
if ! "$SRC/Vendor/python/bin/python3" -c "import mcp" 2>/dev/null; then
  echo "error: 内嵌 Python 里没有 MCP SDK，先运行 Keygent/scripts/fetch-python.sh"
  exit 1
fi

mkdir -p "$RES" "$APP/Library/LaunchAgents"
rsync -a --delete "$SRC/Vendor/python/" "$RES/python/"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'x86_64-unknown-linux-musl' \
  "$SRC/../weaver/" "$RES/weaver-src/weaver/"
install -m 755 "$SRC/Daemon/weaverd" "$RES/weaverd"
install -m 644 "$SRC/Daemon/com.keygent.weaverd.plist" "$APP/Library/LaunchAgents/com.keygent.weaverd.plist"
# 代码指纹：weaverd 启动时记下自己用的是哪份，App 启动时对不上就重启它（见 DaemonService.restartIfStale）
(cd "$RES" && find weaver-src weaverd -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum | shasum | cut -d' ' -f1) > "$RES/weaverd.stamp"

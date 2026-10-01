#!/bin/bash
# 下载内嵌进 Keygent.app 的 Python（python-build-standalone，精简版），放到 Vendor/python。
# 只需要跑一次；换版本时改 PY_VERSION / PBS_TAG 再跑。同时装上 MCP SDK（requirements-mcp.txt），
# App 里的 weaverd 要用它接 MCP 服务器；embed-weaverd.sh 会检查，没装就不让构建。
#   ./scripts/fetch-python.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION=3.14.7
PBS_TAG=20260929
ARCH=aarch64-apple-darwin
NAME="cpython-${PY_VERSION}+${PBS_TAG}-${ARCH}-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${NAME//+/%2B}"

DEST=Vendor/python
if [[ -x "$DEST/bin/python3" ]] && "$DEST/bin/python3" -c "import sys; sys.exit(sys.version.split()[0] != '$PY_VERSION')"; then
  echo "已经是 Python ${PY_VERSION}：$DEST"
else
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  echo "下载 $NAME"
  curl -fL --progress-bar -o "$tmp/py.tgz" "$URL"
  tar -xzf "$tmp/py.tgz" -C "$tmp"          # 解出来是 python/
  rm -rf "$DEST"
  mkdir -p Vendor
  mv "$tmp/python" "$DEST"
  # 用不到的大件：头文件、测试、IDLE、tkinter
  rm -rf "$DEST/include" "$DEST/share" \
         "$DEST"/lib/python3.*/{test,idlelib,tkinter,turtledemo,ensurepip/_bundled} \
         "$DEST"/lib/{libtcl,libtk,tcl,tk,itcl,thread}* 2>/dev/null || true
  echo "装好了：$("$DEST/bin/python3" --version)"
fi

# 已经装好、版本也符合 requirements-mcp.txt 就不动
if "$DEST/bin/python3" -m pip install --disable-pip-version-check -q -r ../requirements-mcp.txt; then
  echo "MCP SDK：$("$DEST/bin/python3" -c 'import importlib.metadata as m; print(m.version("mcp"))')"
else
  echo "error: MCP SDK 装不上（要联网访问 PyPI）"
  exit 1
fi

#!/bin/bash
# 在 Linux 容器里跑全部测试。需要 Docker 在运行。
#   ./scripts/test-linux.sh          两种架构都跑
#   ./scripts/test-linux.sh arm64    只跑 arm64
#   ./scripts/test-linux.sh amd64    只跑 amd64
#
# arm64：在 Apple Silicon 上原生运行，用来真实测试 bubblewrap 沙箱。
# amd64：在 Apple Silicon 上走模拟，bwrap 起不来（模拟器不支持它要的系统调用），
#        用来测试“bwrap 不可用时自动退回无沙箱”和项目自带的 x86_64 版 ripgrep。
#
# bwrap 要创建用户命名空间，Docker 默认的 seccomp / AppArmor 配置会拦住，所以测试容器放开这两项。
# 这只是为了在容器里测沙箱本身；真正部署到 Linux 服务器（虚拟机或物理机）时不需要这样。
set -euo pipefail
cd "$(dirname "$0")/.."
for arch in ${1:-arm64 amd64}; do
  echo "===== linux/$arch"
  docker run --rm --platform "linux/$arch" \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -v "$PWD":/src:ro python:3.12-slim bash -c '
      set -e
      apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq bubblewrap >/dev/null 2>&1
      cp -r /src /work && cd /work && rm -rf .weaver
      useradd -m tester && chown -R tester /work
      su tester -c "cd /work && python3 -c \"from weaver.sandbox import Sandbox; print(\\\"沙箱:\\\", Sandbox(\\\"/work\\\").describe())\""
      su tester -c "cd /work && python3 -c \"from weaver.tools.search import find_rg; print(\\\"ripgrep:\\\", find_rg() or \\\"纯 Python\\\")\""
      su tester -c "cd /work && python3 -m unittest -v 2>&1 | grep -E \"BwrapReal|FAIL|ERROR\" || true"
      su tester -c "cd /work && python3 -W error::ResourceWarning -m unittest 2>&1 | tail -1"
    '
done

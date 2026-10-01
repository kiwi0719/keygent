# 常用命令。CI（.github/workflows/ci.yml）跑的也是这些。
PYTHON ?= python3
DD     := Keygent/build/DD
CONFIG ?= Debug

.PHONY: check lint test test-linux app run clean

## check: lint + 全部离线测试（提 PR 前跑这个）
check: lint test

## lint: ruff，只查真 bug（规则见 ruff.toml）
lint:
	ruff check .

## test: 全部离线测试（假模型、假 SSE 流，不需要 API key）
test:
	$(PYTHON) -W error::ResourceWarning -m unittest

## test-linux: 在 Docker 里跑 Linux 测试（bubblewrap 沙箱、x86_64 ripgrep）
test-linux:
	./scripts/test-linux.sh

## app: 构建 Keygent.app（第一次会下载内嵌 Python）。CONFIG=Release 出发布版
app:
	cd Keygent && ./scripts/fetch-python.sh && xcodegen generate
	xcodebuild -project Keygent/Keygent.xcodeproj -scheme Keygent -configuration $(CONFIG) \
	  -derivedDataPath $(DD) -quiet build
	@echo "→ $(DD)/Build/Products/$(CONFIG)/Keygent.app"

## run: 构建并打开 Keygent
run: app
	open $(DD)/Build/Products/$(CONFIG)/Keygent.app

clean:
	rm -rf Keygent/build dist

# 设置页（第一期）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keygent 里 ⌘, 打开的设置页（模型 · MCP · Skills，全键盘），配置由 weaverd 的 `/v1/settings/*` 接口统一读写。

**Architecture:** 纯 Python 的 `weaver/settings/`（只碰文件、不碰 HTTP）+ `server.py` 里一组薄路由 + 一个 `Settings` 门面对象（持有 home 路径、写锁、MCP 连接池）。App 侧把现在的 `ModelSettingsView` 换成三分页的 `SettingsView`，按键走 `AppStore.handleKey` 现有的路由方式。

**Tech Stack:** Python 3.14 标准库（MCP 状态部分用已有的 `weaver/mcp/pool.py`）；SwiftUI（macOS），xcodegen + xcodebuild。

**Spec:** `design/settings.md`

## Global Constraints

- 只管用户级：`~/.weaver/.env`、`~/.weaver/mcp.json`、`~/.weaver/skills/`；项目级文件不碰。`WEAVER_HOME` 环境变量覆盖 `~/.weaver`（测试全部用临时 home）。
- 写文件：先写临时文件再 `os.replace`，权限 0600（skills 目录里的 SKILL.md 用普通权限 0644）。
- 删除一律归档：skill → `~/.weaver/skills/.archive/<名字>-<YYYYmmdd-HHMMSS>/`；MCP 服务器 → 追加进 `~/.weaver/mcp-archive.json`（`[{"name", "config", "archived": 时间戳}]`）。
- Claude Code 的 skills（`~/.claude/skills/`）只读；新建只写 `~/.weaver/skills/`。
- 错误：`BadRequest`（400）/ `NotFound`（404）/ `Conflict`（409），message 是给人看的中文；沿用 `weaver/daemon/manager.py` 里的异常类。
- skill 名字：`^[a-z0-9][a-z0-9-]{0,63}$`。
- 高级项环境变量：`WEAVER_CONTEXT_WINDOW`、`WEAVER_COMPACT_AT`、`WEAVER_MAX_RUNNING`、`WEAVER_MCP_TIMEOUT`；值是正整数或空（空 = 删掉这一行 = 用默认）。默认值显示文字：`自动（按模型）`、`自动`、`4`、`10`。
- 键位：⌘, 开关；⌘[ / ⌘]（及 ⌃Tab / ⌃⇧Tab）切分页；↑↓ 选；⌘1–⌘5 跳眼前第几条；↵ 打开；⌘N 新建 / 添加；⌘R 重连；⌘⌫ 删除（连按两次确认）；编辑器里 ⌘↵ 保存；esc 退一层（编辑器有改动时连按两次才放弃）。
- 项目不是 git 仓库：计划里的“提交”一律换成“跑全量测试”：`.venv/bin/python -m unittest`（期望 `OK`），以及系统 `python3 -m unittest`（没装 SDK，MCP 相关自动跳过，期望 `OK`）。
- Swift 构建：`cd Keygent && xcodegen generate && xcodebuild -project Keygent.xcodeproj -scheme Keygent -derivedDataPath build/DD build`，期望 `** BUILD SUCCEEDED **`。

## Review Focus

1. **用户手写的 `.env` / `mcp.json` 被设置页弄丢内容**：注释、别的变量、我们不认识的字段（如 `trust`、`cwd`、`disabled`）必须原样保留。→ Task 1、Task 2 各有一条测试。
2. **`mcp.json` 是坏 JSON 时点了“添加”**：必须拒绝（409），文件一个字节都不变。→ Task 2 测试。
3. **粘贴的 JSON 里有真令牌**：存进去后文件权限必须是 0600；GET 接口照样返回原配置（用户自己的机器），但日志里不能打出来。→ Task 2 测试（权限）、Task 4 测试（日志里没有令牌）。
4. **skill 改名撞上 Claude Code 里同名的 skill**：必须 409，原目录不动。→ Task 3 测试。
5. **两个保存请求同时到**（App 连按、命令行同时改）：两次的改动都在，文件是合法 JSON。→ Task 4 测试（10 个线程并发 `POST /v1/settings/mcp` 各加一个服务器，最后 10 个都在）。

---

### Task 1：`.env` 读写 + 模型设置（Python）

**Files:**
- Create: `weaver/settings/__init__.py`（空包说明）、`weaver/settings/envfile.py`、`weaver/settings/model.py`
- Test: `tests/test_settings.py`（类 `EnvAndModel`）

**Interfaces:**
- Produces:
  - `envfile.EnvFile.load(path: Path) -> EnvFile`；`EnvFile.get(key) -> str | None`（空值当 None）；`EnvFile.set(key, value: str | None)`（None 或空 = 删掉；同 key 多行只留第一行改、其余删，规则同 `Keygent/Keygent/Model/ModelConfig.swift` 的 `EnvFile.set`）；`EnvFile.write(path)`（原子、0600）。解析规则和 `weaver.providers.load_env` 一致。
  - `model.ADVANCED = {"context_window": "WEAVER_CONTEXT_WINDOW", "compact_at": "WEAVER_COMPACT_AT", "max_running": "WEAVER_MAX_RUNNING", "mcp_timeout": "WEAVER_MCP_TIMEOUT"}`
  - `model.read(env_path: Path) -> dict`：`{"url", "model", "key": "••••" + 末4位 | None, "advanced": {名: 字符串或 ""}, "defaults": {...}}`。没写 `WEAVER_BASE_URL` 但有 `WEAVER_PROVIDER` 时，`url` 取 `weaver.providers.PRESETS[provider]["base_url"]`。
  - `model.save(env_path: Path, data: dict) -> None`：失败抛 `BadRequest`。校验和写入规则逐条照搬 `ModelSettingsState.apply`（地址 http/https 且有 host；模型名不空；没填 key 时只有“地址的 host 和已保存的一样”才沿用；本机地址 localhost/127.0.0.1/::1/*.local 可以没 key；换了地址就删 `WEAVER_PROVIDER`、`WEAVER_PROTOCOL`；删 `OPENROUTER_API_KEY`、`OPENROUTER_MODEL`）；`advanced` 每项是正整数字符串或空。

- [ ] **Step 1：写失败的测试**

```python
class EnvAndModel(unittest.TestCase):
    # setUp：临时目录，env = tmp / ".env"
    def test_set_keeps_other_lines_and_mode(self):
        env.write_text("# 注释\nFOO=1\nWEAVER_MODEL=a\nWEAVER_MODEL=b\n")
        e = EnvFile.load(env); e.set("WEAVER_MODEL", "c"); e.set("NEW", "x"); e.set("FOO", None); e.write(env)
        self.assertEqual(env.read_text(), "# 注释\nWEAVER_MODEL=c\nNEW=x\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_read_masks_key_and_fills_preset_url(self):
        env.write_text("WEAVER_PROVIDER=deepseek\nWEAVER_MODEL=m\nWEAVER_API_KEY=sk-abcdef1234\nWEAVER_MAX_RUNNING=6\n")
        r = model.read(env)
        self.assertEqual((r["url"], r["key"], r["advanced"]["max_running"], r["advanced"]["compact_at"]),
                         ("https://api.deepseek.com/v1", "••••1234", "6", ""))
        self.assertEqual(r["defaults"]["context_window"], "自动（按模型）")

    def test_save_rules(self):
        env.write_text("WEAVER_BASE_URL=https://openrouter.ai/api/v1\nWEAVER_MODEL=a\nWEAVER_API_KEY=k1\nWEAVER_PROVIDER=openrouter\n")
        model.save(env, {"url": "https://openrouter.ai/api/v1", "model": "b", "advanced": {"max_running": "8"}})
        self.assertEqual((EnvFile.load(env).get("WEAVER_API_KEY"), EnvFile.load(env).get("WEAVER_PROVIDER"),
                          EnvFile.load(env).get("WEAVER_MAX_RUNNING")), ("k1", "openrouter", "8"))   # 同一家沿用 key
        with self.assertRaisesRegex(BadRequest, "API Key"):
            model.save(env, {"url": "https://api.deepseek.com/v1", "model": "b"})                     # 换了家必须填 key
        model.save(env, {"url": "http://localhost:11434/v1", "model": "q"})                            # 本机可以没 key
        self.assertIsNone(EnvFile.load(env).get("WEAVER_PROVIDER"))
        for bad, msg in [({"url": "ftp://x", "model": "a"}, "http"), ({"url": "https://x", "model": " "}, "模型名"),
                         ({"url": "http://localhost:1/v1", "model": "a", "advanced": {"compact_at": "-3"}}, "正整数")]:
            with self.assertRaisesRegex(BadRequest, msg):
                model.save(env, bad)
```

- [ ] **Step 2：跑，确认失败**：`.venv/bin/python -m unittest tests.test_settings -v` → `ModuleNotFoundError: weaver.settings`
- [ ] **Step 3：实现 `envfile.py`、`model.py`**（接口见上；`BadRequest` 从 `weaver.daemon.manager` 导入——为了不让 settings 依赖 daemon，把三个异常类挪到新文件 `weaver/errors.py`，`manager.py` 改成从那里导入并保持原名可用）
- [ ] **Step 4：跑，确认通过**：同上命令 → `OK`
- [ ] **Step 5：全量测试**（见 Global Constraints）

---

### Task 2：`mcp.json` 管理、粘贴解析、导入、常用服务器（Python）

**Files:**
- Create: `weaver/settings/mcp.py`、`weaver/settings/presets.json`
- Test: `tests/test_settings.py`（类 `McpSettings`）

**Interfaces:**
- Consumes: `EnvFile`（Task 1）；`weaver.mcp.config.parse`（判断一份配置有没有问题）。
- Produces（都在 `weaver/settings/mcp.py`，`home: Path` 是 weaver home）：
  - `class BrokenConfig(Conflict)`：`mcp.json` 不是合法 JSON 或顶层不是对象；message 形如 `mcp.json 读不了：第 3 行 第 5 列 …`。
  - `load(home) -> dict[str, dict]`：`{名字: 原始配置}`，文件不存在 → `{}`，坏了 → 抛 `BrokenConfig`。保留文件里 `mcpServers` 之外的顶层字段。
  - `add(home, servers: dict[str, dict], overwrite: bool = False) -> list[str]`：名字非法（规则同 skill 名字但允许大写和 `_`：`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`）或配置不是对象 → `BadRequest`；同名且不 overwrite → `Conflict("已经有同名的服务器：a、b")`。返回加入的名字。
  - `replace(home, name, config: dict, rename: str | None = None) -> str`：不存在 → `NotFound`；`rename` 撞名 → `Conflict`。返回最终名字。
  - `remove(home, name) -> None`：移进 `mcp-archive.json`。
  - `parse_text(text: str) -> dict[str, dict]`：认三种写法（`{"mcpServers": {...}}` / `{名字: 配置}` / 单个 `{"command"|"url": …}`）；单个时名字取：url 的 host 第一段，或 `args` 里最后一个像包名的参数去掉 `@scope/`、`server-`、`mcp-server-`、`@版本` 后的部分，或命令名。不是 JSON → `BadRequest("不是合法的 JSON：…")`；认不出 → `BadRequest("没认出 MCP 服务器配置")`。
  - `import_sources(claude_json: Path, desktop_json: Path) -> list[dict]`：`[{"from": "Claude Code"|"Claude 桌面版", "path", "servers": {...}}]`，只列有服务器的来源；Claude Code 读顶层 `mcpServers`。
  - `presets() -> list[dict]`：读 `presets.json`；字段 `{"id", "name", "title", "description", "config", "needs": [{"env"?, "arg"?, "label", "url"?}]}`（`arg` 表示把填的值追加到 `args`，用于文件服务器的目录）。
  - `add_preset(home, id, values: dict[str, str]) -> str`：缺必填 → `BadRequest("要填：GitHub 令牌")`；`env` 类的值写进 `home/.env` 并 `os.environ[k] = v`；返回加入的名字（同名 → `Conflict`）。
  - 写 `mcp.json` 统一走一个内部函数：读最新内容 → 改 → 原子写 0600。**锁不在这里**（Task 4 的门面持锁）。

- [ ] **Step 1：写失败的测试**

```python
class McpSettings(unittest.TestCase):
    def test_add_keeps_unknown_fields_and_mode(self):
        (home / "mcp.json").write_text('{"x": 1, "mcpServers": {"a": {"command": "c", "trust": "readonly", "cwd": "/t"}}}')
        mcp.add(home, {"b": {"url": "https://h/mcp", "headers": {"Authorization": "Bearer tok"}}})
        data = json.loads((home / "mcp.json").read_text())
        self.assertEqual((data["x"], data["mcpServers"]["a"]["trust"], sorted(data["mcpServers"])), (1, "readonly", ["a", "b"]))
        self.assertEqual((home / "mcp.json").stat().st_mode & 0o777, 0o600)

    def test_conflict_overwrite_replace_rename_remove(self):
        mcp.add(home, {"a": {"command": "c"}})
        with self.assertRaisesRegex(Conflict, "同名.*a"): mcp.add(home, {"a": {"command": "d"}})
        mcp.add(home, {"a": {"command": "d"}}, overwrite=True)
        self.assertEqual(mcp.replace(home, "a", {"command": "e"}, rename="b"), "b")
        mcp.remove(home, "b")
        self.assertEqual(mcp.load(home), {})
        self.assertEqual(json.loads((home / "mcp-archive.json").read_text())[0]["config"], {"command": "e"})
        with self.assertRaises(NotFound): mcp.remove(home, "b")

    def test_broken_file_refuses_writes_and_is_untouched(self):
        (home / "mcp.json").write_text('{"mcpServers": {')
        with self.assertRaisesRegex(BrokenConfig, "mcp.json 读不了"): mcp.add(home, {"a": {"command": "c"}})
        self.assertEqual((home / "mcp.json").read_text(), '{"mcpServers": {')

    def test_parse_three_shapes(self):
        self.assertEqual(list(mcp.parse_text('{"mcpServers": {"gh": {"url": "https://x"}}}')), ["gh"])
        self.assertEqual(list(mcp.parse_text('{"a": {"command": "x"}, "b": {"command": "y"}}')), ["a", "b"])
        self.assertEqual(list(mcp.parse_text('{"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}')), ["playwright"])
        self.assertEqual(list(mcp.parse_text('{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}')), ["filesystem"])
        with self.assertRaisesRegex(BadRequest, "合法的 JSON"): mcp.parse_text("{nope")
        with self.assertRaisesRegex(BadRequest, "没认出"): mcp.parse_text('{"a": 1}')

    def test_import_sources(self):
        cc, desk = tmp / "claude.json", tmp / "desktop.json"
        cc.write_text('{"mcpServers": {"s": {"command": "x"}}, "projects": {"/p": {"mcpServers": {"p": {"command": "y"}}}}}')
        out = mcp.import_sources(cc, desk)                      # desk 不存在
        self.assertEqual([(s["from"], list(s["servers"])) for s in out], [("Claude Code", ["s"])])

    def test_preset_with_token_goes_to_env(self):
        p = next(p for p in mcp.presets() if p["id"] == "github")
        with self.assertRaisesRegex(BadRequest, "要填"): mcp.add_preset(home, "github", {})
        mcp.add_preset(home, "github", {"GITHUB_TOKEN": "ghp_x"})
        self.assertIn("${GITHUB_TOKEN}", json.dumps(mcp.load(home)["github"]))
        self.assertEqual(EnvFile.load(home / ".env").get("GITHUB_TOKEN"), "ghp_x")
```

- [ ] **Step 2：跑，确认失败**
- [ ] **Step 3：实现 `mcp.py`；`presets.json` 先写候选**（filesystem：`npx -y @modelcontextprotocol/server-filesystem` + `needs: [{"arg": true, "label": "开放给它的目录"}]`；fetch：`uvx mcp-server-fetch`；playwright：`npx -y @playwright/mcp@latest`；github：`{"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}` + `needs: [{"env": "GITHUB_TOKEN", "label": "GitHub 令牌", "url": "https://github.com/settings/personal-access-tokens"}]`；memory：`npx -y @modelcontextprotocol/server-memory`）
- [ ] **Step 4：逐个真连一次常用服务器**：写一个临时脚本，用 `McpManager` 连 presets 里每个不需要令牌的（filesystem 传 scratchpad 目录），`WEAVER_MCP_TIMEOUT=120`，打印工具数。连不上的从 `presets.json` 删掉并在 `design/settings.md` 记一笔。github 没有令牌时只验证“连接返回 401 而不是地址错误”。
- [ ] **Step 5：跑，确认通过；全量测试**

---

### Task 3：skills 管理（Python）

**Files:**
- Create: `weaver/settings/skills.py`
- Test: `tests/test_settings.py`（类 `SkillSettings`）

**Interfaces:**
- Consumes: `weaver.skills.parse_frontmatter`、`weaver.skills.Skills`（`discover()` 用来拿 Claude Code 的同名）。
- Produces（`home` = weaver home，`claude_home` 默认 `~/.claude`）：
  - `list_skills(home, claude_home) -> dict`：`{"skills": [{"name", "description", "source": "weaver"|"claude", "editable", "path"}], "problems": [{"path", "message"}]}`；同名时 weaver 的在前，Claude 的同名项仍列出（标出被覆盖：`"shadowed": true`）。frontmatter 坏的 weaver skill 用目录名当 name、`description` 为空、照样列出。
  - `read_skill(home, claude_home, name) -> dict`：`{"name", "text", "editable", "files": [相对路径]}`；`NotFound`。
  - `create_skill(home, claude_home, text) -> str`、`save_skill(home, claude_home, name, text) -> str`（返回最终名字；frontmatter name 变了 → 目录改名）、`remove_skill(home, claude_home, name) -> None`（归档）。
  - 校验失败的 message：`开头要有 --- 包起来的 name 和 description`、`名字只能用小写字母、数字和 -，最长 64 个字`、`description 不能空`、`已经有叫 X 的 skill 了（来自 Claude Code）`、`来自 Claude Code 的 skill 只能看，不能改`。

- [ ] **Step 1：写失败的测试**

```python
class SkillSettings(unittest.TestCase):
    T = "---\nname: {n}\ndescription: 说明\n---\n\n正文\n"
    def test_create_edit_rename_archive(self):
        self.assertEqual(sk.create_skill(home, claude, self.T.format(n="rel")), "rel")
        (home / "skills/rel/run.sh").write_text("echo")
        self.assertEqual(sk.save_skill(home, claude, "rel", self.T.format(n="release")), "release")
        self.assertTrue((home / "skills/release/run.sh").exists())          # 目录里的文件跟着走
        self.assertFalse((home / "skills/rel").exists())
        sk.remove_skill(home, claude, "release")
        self.assertEqual(len(list((home / "skills/.archive").iterdir())), 1)
        self.assertEqual(sk.list_skills(home, claude)["skills"], [])

    def test_validation_and_claude_readonly(self):
        (claude / "skills/pdf").mkdir(parents=True); (claude / "skills/pdf/SKILL.md").write_text(self.T.format(n="pdf"))
        cases = [("无 frontmatter", "--- 包起来"), (self.T.format(n="Bad Name"), "小写字母"),
                 ("---\nname: ok\ndescription: \n---\n", "description 不能空"), (self.T.format(n="pdf"), "来自 Claude Code")]
        for text, msg in cases:
            with self.assertRaisesRegex((BadRequest, Conflict), msg): sk.create_skill(home, claude, text)
        sk.create_skill(home, claude, self.T.format(n="mine"))
        with self.assertRaisesRegex(Conflict, "来自 Claude Code"):           # 改名撞 Claude 的：原目录不动
            sk.save_skill(home, claude, "mine", self.T.format(n="pdf"))
        self.assertTrue((home / "skills/mine/SKILL.md").exists())
        with self.assertRaisesRegex(BadRequest, "只能看"): sk.save_skill(home, claude, "pdf", self.T.format(n="pdf"))
        item = next(s for s in sk.list_skills(home, claude)["skills"] if s["name"] == "pdf")
        self.assertEqual((item["source"], item["editable"]), ("claude", False))

    def test_broken_frontmatter_still_listed(self):
        (home / "skills/broken").mkdir(parents=True); (home / "skills/broken/SKILL.md").write_text("乱写")
        listed = sk.list_skills(home, claude)
        self.assertEqual(listed["skills"][0]["name"], "broken")
        self.assertTrue(listed["problems"])
```

- [ ] **Step 2：跑，确认失败**
- [ ] **Step 3：实现**
- [ ] **Step 4：跑，确认通过；全量测试**

---

### Task 4：`Settings` 门面 + MCP 状态 + HTTP 路由 + api.md

**Files:**
- Create: `weaver/settings/service.py`
- Modify: `weaver/mcp/manager.py`（加 `reconnect(key)`）、`weaver/mcp/pool.py`（加 `status(home) -> dict[name, dict]` 用的 `server_state(cfg)`）、`weaver/daemon/server.py`（路由）、`weaver/daemon/__main__.py`（造 `Settings` 传给 `DaemonServer`）、`design/api.md`（v1.6）
- Test: `tests/test_settings.py`（类 `SettingsHttp`；MCP 状态部分 `skipUnless(sdk_available())`）

**Interfaces:**
- Consumes: Task 1–3 的函数；`McpPool.register(cfg, None)`、`McpManager.ensure`。
- Produces:
  - `McpManager.reconnect(key) -> None`：有连接就 `stop.set()` 等它关掉（最多 5 秒），清掉 `failed_at`，再 `ensure([key], wait=False, retry=True)`。
  - `class Settings(home: Path, pool: Callable[[], McpPool | None], claude_home: Path = ~/.claude, desktop_json: Path = ~/Library/Application Support/Claude/claude_desktop_config.json)`：所有写操作持同一把 `threading.Lock`；方法与路由一一对应：`model()`、`save_model(data)`、`mcp_list()`、`mcp_parse(text)`、`mcp_add(servers, overwrite)`、`mcp_replace(name, config, rename)`、`mcp_remove(name)`、`mcp_reconnect(name)`、`mcp_presets()`、`mcp_add_preset(id, values)`、`mcp_import()`、`skills()`、`skill(name)`、`skill_create(text)`、`skill_save(name, text)`、`skill_remove(name)`。加 / 改 MCP 后对受影响的服务器 `ensure(wait=False)`。
  - `mcp_list()` 每项 `status`：配置有问题 → `invalid`（`error` = `ServerConfig.problem`）；没装 SDK → `idle` + `error: "weaverd 没装 MCP SDK"`；否则看连接池状态：`client` 在 → `connected`；`connecting` 没完成 → `connecting`；`error` 不空 → `failed`；`connected_once` → `connected`（闲置被关）；都不是 → `idle`。`tools` = 工具名列表。`mcp.json` 坏了 → `{"servers": [], "problem": message}`。
  - 路由（`server.py` 的 `ROUTES`，名字 `h_settings_*`）：

    | 方法 | 路径 | 返回 |
    |---|---|---|
    | GET / PUT | `/v1/settings/model` | 读 / `{"restart": true}` |
    | GET / POST | `/v1/settings/mcp` | 列表 / 201 `{"added": [...]}` |
    | POST | `/v1/settings/mcp/parse` | `{"servers", "conflicts"}` |
    | GET | `/v1/settings/mcp/presets` | `{"presets": [...]}` |
    | POST | `/v1/settings/mcp/presets/([\w-]+)` | 201 `{"added": [名字]}` |
    | GET | `/v1/settings/mcp/import` | `{"sources": [...]}` |
    | PUT / DELETE | `/v1/settings/mcp/([\w.-]+)` | `{"name"}` / 204 |
    | POST | `/v1/settings/mcp/([\w.-]+)/reconnect` | 204 |
    | GET / POST | `/v1/settings/skills` | 列表 / 201 `{"name"}` |
    | GET / PUT / DELETE | `/v1/settings/skills/([\w-]+)` | 详情 / `{"name"}` / 204 |

    注意 `ROUTES` 是按顺序匹配：`/v1/settings/mcp/parse`、`/presets`、`/import` 要排在 `/v1/settings/mcp/([\w.-]+)` 前面。
  - `DaemonServer(manager, port=0, token=None, settings: Settings | None = None)`；`settings` 为 None 时这组路由回 404 `"这个服务没开设置接口"`。

- [ ] **Step 1：写失败的测试**（复用 `tests/test_daemon_http.py` 的 `Base` 起服务的方式，额外传 `settings=Settings(home, pool=lambda: None, claude_home=tmp/"claude", desktop_json=tmp/"desk.json")`）

```python
class SettingsHttp(Base):
    def test_routes_roundtrip(self):
        self.assertEqual(self.c.call("PUT", "/v1/settings/model", {"url": "http://localhost:1/v1", "model": "m"}), (200, {"restart": True}))
        st, b = self.c.call("POST", "/v1/settings/mcp/parse", {"text": '{"a": {"command": "x"}}'})
        self.assertEqual((st, list(b["servers"]), b["conflicts"]), (200, ["a"], []))
        self.assertEqual(self.c.call("POST", "/v1/settings/mcp", {"servers": b["servers"]})[0], 201)
        self.assertEqual(self.c.call("POST", "/v1/settings/mcp", {"servers": b["servers"]})[0], 409)
        st, lst = self.c.call("GET", "/v1/settings/mcp")
        self.assertEqual((lst["servers"][0]["name"], lst["servers"][0]["status"]), ("a", "idle"))   # 没有 SDK 池
        self.assertEqual(self.c.call("DELETE", "/v1/settings/mcp/a")[0], 204)
        st, s = self.c.call("POST", "/v1/settings/skills", {"text": "---\nname: x\ndescription: d\n---\n"})
        self.assertEqual((st, s["name"]), (201, "x"))
        self.assertEqual(self.c.call("GET", "/v1/settings/skills/nope")[0], 404)

    def test_concurrent_adds_all_kept(self):
        threads = [threading.Thread(target=lambda i=i: self.c.call("POST", "/v1/settings/mcp", {"servers": {f"s{i}": {"command": "x"}}})) for i in range(10)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len(json.loads((home / "mcp.json").read_text())["mcpServers"]), 10)

    def test_token_not_logged(self):
        with self.assertLogs("weaverd", "DEBUG") as cm:
            logging.getLogger("weaverd").debug("start")
            self.c.call("POST", "/v1/settings/mcp", {"servers": {"g": {"url": "https://x", "headers": {"Authorization": "Bearer SECRET123"}}}})
        self.assertNotIn("SECRET123", "\n".join(cm.output))

@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class SettingsLive(unittest.TestCase):
    def test_status_goes_connecting_to_connected(self):
        pool = McpPool(home); self.addCleanup(pool.close)
        s = Settings(home, pool=lambda: pool, claude_home=tmp / "claude", desktop_json=tmp / "desk.json")
        s.mcp_add({"fake": {"command": sys.executable, "args": [SERVER]}}, overwrite=False)
        self.assertIn(s.mcp_list()["servers"][0]["status"], ("connecting", "connected"))
        item = poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "connected", timeout=30)
        self.assertIn("echo", item["tools"])
        s.mcp_reconnect("fake")
        poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "connected", timeout=30)
        # poll(get, ok, timeout)：每 0.1 秒取一次，满足就返回，超时 fail —— 写在 tests/test_settings.py 顶部
```

- [ ] **Step 2：跑，确认失败**
- [ ] **Step 3：实现 `service.py`、`reconnect`、路由、`__main__` 接线**（`Settings(home, factory.mcp_pool)`）
- [ ] **Step 4：跑，确认通过；全量测试**
- [ ] **Step 5：`design/api.md` 加 v1.6 变更记录 + 第 2 节补设置接口（每个接口一个 JSON 示例，字段同上）**

---

### Task 5：App 接口类型 + 客户端（Swift）

**Files:**
- Create: `Keygent/Keygent/Model/SettingsModels.swift`
- Modify: `Keygent/Keygent/API/WeaverClient.swift`

**Interfaces:**
- Produces（`Codable`，字段名 snake_case 用 `CodingKeys`，同 `APIModels.swift` 的写法）：
  - `ModelSettings { url, model: String; key: String?; advanced: [String: String]; defaults: [String: String] }`
  - `McpServerItem { name: String; config: JSONValue; transport: String; status: String; error: String?; tools: [String] }`，`McpList { servers: [McpServerItem]; problem: String? }`
  - `McpParsed { servers: [String: JSONValue]; conflicts: [String] }`、`McpPreset { id, name, title, description: String; config: JSONValue; needs: [PresetNeed] }`、`PresetNeed { env: String?; arg: Bool?; label: String; url: String? }`、`ImportSource { from, path: String; servers: [String: JSONValue] }`
  - `SkillItem { name, description, source, path: String; editable: Bool; shadowed: Bool? }`、`SkillList { skills; problems }`、`SkillDetail { name, text: String; editable: Bool; files: [String] }`
  - `WeaverClient` 新方法（一一对应 Task 4 的路由）：`settingsModel()`、`saveModel(url:model:key:advanced:)`、`mcpList()`、`mcpParse(_ text:)`、`mcpAdd(_ servers: [String: JSONValue], overwrite: Bool)`、`mcpReplace(_ name:, config:, rename:)`、`mcpRemove(_:)`、`mcpReconnect(_:)`、`mcpPresets()`、`mcpAddPreset(_ id:, values:)`、`mcpImport()`、`skills()`、`skill(_:)`、`skillCreate(_ text:)`、`skillSave(_ name:, text:)`、`skillRemove(_:)`。复用现有的 `get` / `send`；204 的接口返回 `Void`（如 `send` 不支持空响应，加一个 `sendNoContent`）。

- [ ] **Step 1：实现**（`JSONValue` 已有，用于原样传配置）
- [ ] **Step 2：构建**：期望 `BUILD SUCCEEDED`
- [ ] **Step 3：起一个临时 weaverd（`.venv`，临时 `WEAVER_HOME`），用 `WEAVER_DAEMON_JSON` 指过去，在 `DebugHooks` 加 `settings-selftest` 命令：依次调每个新方法并 `NSLog("KGDEBUG selftest <方法> ok|<错误>")`；`log stream --predicate 'eventMessage CONTAINS "KGDEBUG"'` 里全部 `ok`**

---

### Task 6：设置页外框 + 模型页（Swift）

**Files:**
- Create: `Keygent/Keygent/Store/AppStore+Settings.swift`、`Keygent/Keygent/Views/Settings/SettingsView.swift`、`Keygent/Keygent/Views/Settings/ModelPage.swift`
- Modify: `Keygent/Keygent/Store/AppStore.swift`（删掉模型设置那一节，`settings` 改成 `SettingsState?`；`handleError` 里 `missingConfig` 仍打开设置的模型页）、`Keygent/Keygent/Views/RootView.swift`（`ModelSettingsView()` → `SettingsView()`）
- Delete: `Keygent/Keygent/Views/Components/ModelSettingsView.swift`（内容并入 `ModelPage.swift`）

**Interfaces:**
- Consumes: Task 5 的类型和客户端方法；`ModelSettingsState` / `EnvFile`（离线时直接写 `.env` 的路径保留）。
- Produces:
  - `enum SettingsTab: Int, CaseIterable { case model, mcp, skills }`；`struct SettingsState { var tab: SettingsTab; var model: ModelSettingsState; var advanced: [String: String]; var advancedDefaults: [String: String]; var mcp: McpPageState; var skills: SkillsPageState; var error: String?; var armed: String? /* 待确认的删除或放弃，存目标名 */ }`（`McpPageState`、`SkillsPageState` 在 Task 7、8 定义，这里先放空结构体占位）
  - `AppStore`：`openSettings(tab: SettingsTab? = nil)`（在线 → 拉 `settingsModel()` 填 `model` 和高级项；离线 → 照现在读 `.env`，只能在模型页）、`closeSettings()`、`settingsSwitch(by: Int)`（离线时不切）、`saveModelSettings()`（在线 → `saveModel` 成功后 `restartDaemon()`；离线 → 现有 `EnvFile` 写法）、`settingsKey(_:) -> Bool`（先处理全局键：⌘, / ⌘[ / ⌘] / ⌃Tab / ⌃⇧Tab / esc，再分给 `modelKey`、`mcpKey`、`skillsKey`）。
  - `InputField` 加 `.settingsEditor`、`.settingsFilter`；模型页沿用 `.settings`，高级项四行用 `.settingsAdvanced(Int)`（`InputField` 改成带关联值的 enum，`Hashable`）。
- 版式：顶部分页条（选中的分页黑底白字，右侧 `Kbd("⌘[")` `Kbd("⌘]")` 切换），宽 780；模型页三行 + `HLine` + 小标题“高级（留空用默认）”+ 四行，空值 placeholder 用 `advancedDefaults`。↑↓ / Tab 在七行之间移焦点；↵ 保存；esc 关闭（第一次配置时收起面板）。

- [ ] **Step 1：实现**
- [ ] **Step 2：构建**
- [ ] **Step 3：实测**（临时 weaverd + DebugHooks 加 `settings[:tab]` 命令和 `key:` 支持 `cmd+[`、`cmd+]`、`tab`、`enter`）：打开 → 截图模型页 → 改高级项 `max_running=6` 保存 → `.env` 里有 `WEAVER_MAX_RUNNING=6` → 切到 MCP、Skills 页（此时是占位）截图。用 `screencapture -l <窗口号>` 截图并查看。

---

### Task 7：MCP 页（Swift）

**Files:**
- Create: `Keygent/Keygent/Views/Settings/McpPage.swift`、`Keygent/Keygent/Views/Components/CodeEditor.swift`
- Modify: `Keygent/Keygent/Store/AppStore+Settings.swift`

**Interfaces:**
- Consumes: Task 5 客户端；Task 6 的 `SettingsState`、`settingsKey` 分派。
- Produces:
  - `CodeEditor(text: Binding<String>, editable: Bool, focus: InputField)`：等宽 13pt、`NSTextView` 包一层（关掉智能引号 / 自动替换 / 拼写检查），高度撑满可用空间，最多 360。
  - `struct McpPageState { var list: McpList?; var cur = 0; var top = 0; var mode: Mode = .list; var editText = ""; var editName = ""; var original = ""; var parsed: McpParsed?; var presets: [McpPreset] = []; var filter = ""; var presetPick = 0; var needs: [String: String] = [:]; var sources: [ImportSource] = []; var checked: Set<String> = [] }`，`enum Mode { case list, edit(String), addMenu, paste, presets, presetFill(String), importing }`
  - 键（`mcpKey`）：
    - `list`：↑↓ / ⌘1–5 选；↵ → `edit(name)`（`editText` = 配置的漂亮 JSON）；⌘N → `addMenu`；⌘R → `mcpReconnect`；⌘⌫ → 第一次 `armed = name`、底部提示“再按一次 ⌘⌫ 删除 X”，第二次真删；其它键清 `armed`。
    - `edit`：⌘↵ → 本地先 `JSONSerialization` 校验（错了 `error = "JSON 写错了：…"`），再 `mcpReplace(name, config, rename: editName != name ? editName : nil)`；Tab 在名字和 JSON 之间切；esc → 有改动时先 `armed = "discard"`。
    - `addMenu`：⌘1 / ⌘2 / ⌘3 或 ↑↓↵ → `paste` / `presets` / `importing`；esc 回 `list`。
    - `paste`：文字变化后 0.3 秒调 `mcpParse`，显示“认出了 N 个服务器：…”（冲突的加“会覆盖”）；⌘↵ → `mcpAdd(parsed.servers, overwrite: true)`（冲突已经提示过）。
    - `presets`：输入框筛选（`title`、`description` 包含），↑↓ 选，↵ → 有 `needs` 进 `presetFill(id)` 否则直接 `mcpAddPreset`。`presetFill`：一行一个输入框，⌘O 打开 `needs[i].url`，↵ 提交。
    - `importing`：按来源分组列出，空格勾选，默认勾上不存在的；⌘↵ → `mcpAdd(勾选的, overwrite: false)`；一个都没有 → 显示“没找到 Claude Code / Claude 桌面版配过的 MCP 服务器”。
  - 列表在 MCP 页可见时每 2 秒 `mcpList()` 刷新（`Timer`，离开分页或关设置时停）。
  - 行样式：状态符号 `●`（绿）`◌`（琥珀）`○`（灰）`✕`（红）+ 名字 + 状态文字（`已连接 · N 个工具` / `连接中` / `还没连` / `连不上：<error>` / `配置有问题：<error>`）；`problem` 不空时列表上方一行红字。

- [ ] **Step 1：实现**
- [ ] **Step 2：构建**
- [ ] **Step 3：实测**（临时 weaverd，`.venv` 带 SDK）：⌘N → 粘贴 → 用 `pbcopy` 放测试服务器 JSON、DebugHooks 模拟 ⌘V → 看到“认出了 1 个服务器” → ⌘↵ → 列表里 `◌ 连接中` → `● 已连接 · 4 个工具`；↵ 编辑改名 → ⌘↵；⌘R；⌘⌫ 两次删除；常用服务器加 `fetch`；导入（临时 `HOME` 下放假的 `~/.claude.json`——用 `Settings` 的 `claude_home` 参数指过去不现实，所以这一步改为在 Python 测试里覆盖，App 里只验证“没找到”的提示）。每步截图。

---

### Task 8：Skills 页（Swift）

**Files:**
- Create: `Keygent/Keygent/Views/Settings/SkillsPage.swift`
- Modify: `Keygent/Keygent/Store/AppStore+Settings.swift`

**Interfaces:**
- Consumes: Task 5 客户端；Task 7 的 `CodeEditor`。
- Produces:
  - `struct SkillsPageState { var list: SkillList?; var cur = 0; var top = 0; var editing: String? /* nil=列表，"" = 新建 */; var text = ""; var original = ""; var editable = true }`
  - 键（`skillsKey`）：列表 ↑↓ / ⌘1–5；↵ → `skill(name)` 打开；⌘N → 新建（`text` = `"---\nname: \ndescription: \n---\n\n"`，光标放在第一行 `name: ` 之后）；⌘⌫ 两次确认删除（Claude 的不响应、底部提示“来自 Claude Code 的 skill 只能看”）。编辑器 ⌘↵ → `skillCreate` / `skillSave`，成功后回列表并选中返回的名字；只读时标题写“只读 · 来自 Claude Code”、⌘↵ 不响应；esc 同 MCP 编辑器。
  - 行样式：名字（等宽）+ 说明（一行截断）+ 右侧来源 `Weaver` / `Claude Code`（灰）；`shadowed` 的加“（被同名覆盖）”。

- [ ] **Step 1：实现**
- [ ] **Step 2：构建**
- [ ] **Step 3：实测**：新建 `demo-skill` → 列表出现 → 打开改 name 为 `demo` → 目录改名 → 删除两次确认 → 进归档；打开一个 Claude Code 的 skill 看到只读。每步截图。

---

### Task 9：收尾：真实环境验证 + 文档

**Files:**
- Modify: `design/settings.md`（末尾“进度”：做了什么、实测结果、和设计不一样的地方）、`design/README.md`（索引加 settings.md）、`Keygent/README.md`（键位里加 ⌘,）、`Keygent/Keygent/Debug/DebugHooks.swift`（注释里列出新命令）

- [ ] **Step 1：全量 Python 测试（两种解释器）+ App 构建**
- [ ] **Step 2：在用户正在用的 Keygent 里验证**：`curl /v1/status` 确认 `running == 0` 且没有在等的事 → Xcode 默认 DerivedData 构建（`xcodebuild -project Keygent.xcodeproj -scheme Keygent -configuration Debug build`）→ `launchctl kickstart -k gui/$(id -u)/com.keygent.weaverd` → 指纹一致 → 用户的面板里 ⌘, 能打开三页（截图）。**不在用户真实配置里加服务器 / skill**，只读查看。
- [ ] **Step 3：更新文档**

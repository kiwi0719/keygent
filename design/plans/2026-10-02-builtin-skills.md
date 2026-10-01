# 内置 skills 实现计划

> **给执行的 Agent：** 用 subagent-driven-development（推荐）或 executing-plans 逐个任务执行。步骤用 `- [ ]` 勾选跟踪。

**Goal:** Weaver 出厂带 10 个 superpowers skill，内容改到能在 Weaver 里跑通。

**Architecture:** skills.py 加一级最低优先级的 `builtin` 目录（`weaver/builtin_skills/`），加 `weaver-when: git` 过滤和带前缀名字的容错；内容从上游复制后按 spec 第四节改。

**Tech Stack:** Python 3 标准库；unittest + pytest。

**Spec:** `design/builtin-skills.md`

## Global Constraints

- 上游：`https://github.com/obra/superpowers`，commit `8ca22db`（2026-09-25），MIT。
- 目录：`weaver/builtin_skills/<name>/`；级别名 `builtin`；优先级最低；不用信任；来源显示“内置”。
- 10 个：verification-before-completion、systematic-debugging（总是列出）；brainstorming、writing-plans、executing-plans、subagent-driven-development、test-driven-development、using-git-worktrees、requesting-code-review、finishing-a-development-branch（`weaver-when: git`）。
- `MAX_BODY = 40_000`。
- 改动全部记进 `weaver/builtin_skills/NOTICE.md`。
- 测试命令：`python3 -m pytest tests -q`。
- 依赖 ask-user 计划：内容里“问用户”靠 `ask_user`；第 3 个任务的真实验证要等 ask_user 做完。

## Review Focus

1. **工作目录是 git 仓库的子目录**（比如在仓库的 `docs/` 里开任务）：应当算“在 git 仓库里”。→ Task 1 测试 `test_when_git_subdir`。
2. **`~/.claude/skills/` 里有同名的 skill**：用户级的赢，内置的不出现第二份。→ Task 1 测试 `test_user_overrides_builtin`。
3. **项目级 skill 没被信任时**：内置的照常列出（信任只管项目级）。→ Task 1 测试 `test_builtin_listed_when_project_untrusted`。
4. **名字本身带冒号的 skill 真实存在**（`a:b`）：优先按全名找到它，不剥前缀。→ Task 1 测试 `test_prefix_exact_name_wins`。
5. **附带文件超过 20 个上限**：subagent-driven-development 附带 6 个文件，不超；brainstorming 7 个，不超。无需新测试，Task 2 内容测试里断言每个 skill 附带文件 ≤ 20。

---

### Task 1: 加载器

**Files:**
- Modify: `weaver/skills.py`（`MAX_BODY` :22；`Skills.__init__` :78 的 `dirs`；`discover`；`available`；`load`）
- Test: `tests/test_skills.py`

**Interfaces:**
- Produces:
  - `Skills(..., builtin_dir: str | Path | None = None)`：默认 `Path(__file__).parent / "builtin_skills"`；测试传临时目录
  - `Skill.level` 可能是 `"builtin"`；`Skill.when: str`（frontmatter `weaver-when`，默认 `""`）
  - `Skills.in_git() -> bool`：从项目根向上找 `.git`（目录或文件都算）
  - `load` 来源文字：builtin →「内置」

- [ ] **Step 1: 写失败的测试**（新类 `Builtin`，临时目录造 skill）：
  - `test_builtin_discovered`：内置目录里一个 skill → `available()` 里有、`level == "builtin"`；`load()` 输出含「来自内置」。
  - `test_user_overrides_builtin`：用户级同名 → `available()[name].level == "user"`。
  - `test_project_overrides_builtin`：项目级同名并信任后 → level `project`。
  - `test_builtin_listed_when_project_untrusted`：有未信任的项目级 skill 时，内置的仍在 `available()` 里。
  - `test_when_git`：`weaver-when: git` 的 skill，项目根没有 `.git` → 不在 `available()`；建 `.git` 目录后 → 在。
  - `test_when_git_subdir`：`.git` 在项目根的上一级 → 在。
  - `test_when_unknown_value`：`weaver-when: 别的` → 照常列出。
  - `test_prefix_fallback`：`load("superpowers:pdf")` 加载到 `pdf`；`load("x:不存在")` 抛 ValueError。
  - `test_prefix_exact_name_wins`：存在名字就叫 `a:b` 的 skill 和叫 `b` 的 skill → `load("a:b")` 加载前者。
  - `test_body_limit_40k`：31KB 正文不截断；41KB 截断。
- [ ] **Step 2: 跑测试确认失败** `python3 -m pytest tests/test_skills.py -q`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试** 新老测试全过；全量无新失败。

### Task 2: 内容

**Files:**
- Create: `weaver/builtin_skills/<10 个>/…`、`weaver/builtin_skills/NOTICE.md`
- Test: `tests/test_builtin_skills.py`

**Interfaces:**
- Consumes: Task 1 的 `weaver-when`

- [ ] **Step 1: 写失败的测试** `tests/test_builtin_skills.py`：
  - `test_ten_skills`：`Skills(tmp).discover()` 里 level 为 builtin 的正好是 Global Constraints 的 10 个名字。
  - `test_when_flags`：上面 8 个 `when == "git"`，verification-before-completion、systematic-debugging 的 `when == ""`。
  - `test_no_model_selection`：`weaver/builtin_skills` 下所有文本文件不含 `Model Selection`、`most capable`、`mid-tier`、`model: [MODEL`。
  - `test_weaver_notes`：subagent-driven-development、executing-plans、requesting-code-review 的 SKILL.md 正文含 `task(mode="general")` 和 `ask_user`；executing-plans 不含 `using-superpowers/references`。
  - `test_dev_artifacts_removed`：systematic-debugging 下没有 `CREATION-LOG.md`、`test-academic.md`、`test-pressure-*.md`。
  - `test_license_and_notice`：每个 skill 目录有 `LICENSE`；`NOTICE.md` 含 `8ca22db`。
  - `test_fits_limits`：每个 SKILL.md 正文 < `MAX_BODY`，附带文件 ≤ `MAX_FILES`。
- [ ] **Step 2: 确认失败**（目录不存在）。
- [ ] **Step 3: 复制**：`git clone` 上游并 `git checkout 8ca22db`，复制 10 个目录，去掉 spec 第四节 4 列出的文件，每个目录放 LICENSE。
- [ ] **Step 4: 改内容**，逐条按 spec 第四节 1~3：删选模型（4 个文件）、加“在 Weaver 里”一段（3 个文件，原文照 spec）、改 executing-plans 第 50 行、8 个 frontmatter 加 `weaver-when: git`。每处改动在 `NOTICE.md` 记一行（文件 + 改了什么）。
- [ ] **Step 5: 跑测试** `python3 -m pytest tests/test_builtin_skills.py -q` 全过；全量无新失败。

### Task 3: 打包和真实验证

**Files:** 无代码改动；结果写进 `design/builtin-skills.md` 末尾“进度”。

- [ ] **Step 1: 打包**：构建 Keygent（命令见 ask-user 计划），确认 `build/DD/Build/Products/Debug/Keygent.app/Contents/Resources/weaver-src/weaver/builtin_skills/` 下有 10 个目录。
- [ ] **Step 2: 本机覆盖**：把 `~/.claude/skills/` 里的 brainstorming、systematic-debugging、verification-before-completion、writing-plans 临时挪到 `~/.claude/skills-off/`（验证完挪回）。
- [ ] **Step 3**：git 仓库里放一个会失败的测试，说「测试挂了修一下」→ 先加载 systematic-debugging、先找根因再改。
- [ ] **Step 4**：普通目录里说「帮我整理一下桌面截图」→ 背景信息里的 skills 列表没有那 8 个。
- [ ] **Step 5**（ask_user 做完后）：git 仓库里说「加一个导出 CSV 的功能」→ 加载 brainstorming，用 ask_user 一次一问，方案批准后才动手。
- [ ] **Step 6**：挪回 `~/.claude/skills/`；结果写进 spec“进度”。

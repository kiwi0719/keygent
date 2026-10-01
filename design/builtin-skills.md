# Weaver 内置 skills 设计

草案 v1 · 2026-10-02

## 一、要解决什么问题

skills.md 第一版只加载用户和项目自己放的 skill。新装的 Weaver 一个都没有，等于白给了 skill 机制。想让 Weaver 出厂就带一组打磨过的做事方法：调试先找根因、说“做完了”之前先验证、大改动先设计后动手、按计划执行并评审。

内容来自 [obra/superpowers](https://github.com/obra/superpowers)（MIT，commit 8ca22db，2026-09-25），保持完整，只做让它在 Weaver 里跑得通的改动；Weaver 缺的能力补在 Weaver 里（ask-user.md 就是其中之一）。

## 二、目录和发现

- 放在 `weaver/builtin_skills/<name>/`。Keygent 打包时 `embed-weaverd.sh` 整个同步 `weaver/`，内置 skill 自动进 App，代码指纹也算它们（内容变了 weaverd 自动重启）。
- skills.py 的扫描列表最后加一级：`(Path(__file__).parent / "builtin_skills", "builtin")`。优先级最低，用户级、项目级同名的覆盖它。
- `builtin` 和 `user` 一样不用信任；加载时来源写“内置”。
- 本机注意：`~/.claude/skills/` 里已经有 brainstorming 等 4 个同名的，会覆盖内置版本。测内置版本前先挪走。

## 三、带哪些

| skill | 干什么 | 什么时候列出 |
|---|---|---|
| verification-before-completion | 说“做完了 / 修好了”之前必须跑过验证、看过输出 | 总是 |
| systematic-debugging | 先找根因再修；修了 3 次不行停下来问人 | 总是 |
| brainstorming | 动手前问清需求、出方案、等批准 | git 仓库里 |
| writing-plans | 有了方案后写分步计划 | git 仓库里 |
| executing-plans | 自己按计划一步步做，最后一次整体评审 | git 仓库里 |
| subagent-driven-development | 每个任务派子 Agent 实现 + 子 Agent 评审 | git 仓库里 |
| test-driven-development | 先写失败的测试再写实现 | git 仓库里 |
| using-git-worktrees | 开工前建隔离的 worktree | git 仓库里 |
| requesting-code-review | 派评审子 Agent 看改动 | git 仓库里 |
| finishing-a-development-branch | 做完后决定怎么合并 / 提 PR / 收尾 | git 仓库里 |

“git 仓库里”的理由：后 8 个讲的是改代码的流程（测试、提交、分支），在“整理文件、写邮件”这类任务里触发只会添乱；brainstorming 的描述是“任何创造性工作之前必须用”，不限定的话写个通知也要走审批流程。

不带：using-superpowers（Claude Code 专用的 skill 使用守则，Weaver 有自己的注入方式）、writing-skills、receiving-code-review、dispatching-parallel-agents、diagnosing-superpowers。以后需要再加。

## 四、对内容的改动

原则：只改跑不通的地方，每处都记进 `weaver/builtin_skills/NOTICE.md`，以后同步上游时照着重做。

### 1. 删掉选模型的逻辑

Weaver 的子 Agent 和主 Agent 用同一个模型，`task` 工具没有 `model` 参数。

| 文件 | 改法 |
|---|---|
| subagent-driven-development/SKILL.md | 删整节 “Model Selection”；删其它地方的 “(see Model Selection)”“on the most capable model” 等短语 |
| subagent-driven-development/implementer-prompt.md、task-reviewer-prompt.md、re-review-prompt.md | 删模板里的 `model: [MODEL — REQUIRED…]` 行和 “Model Selection” 引用 |
| executing-plans/SKILL.md | 删 “on the most capable available model”“Specify the model explicitly…inherits the session's” 这几句；“runs well on a mid-tier session model” 那段删掉 |
| writing-plans/SKILL.md | 执行方式二选一的说明里删 “on the most capable model”“Runs well with a mid-tier session model” |

执行方式的二选一（子 Agent 逐个做 / 自己做）保留，用 ask_user 问用户。

### 2. 加一段“在 Weaver 里”

在 subagent-driven-development、executing-plans、requesting-code-review 的正文开头加一小段：

> 在 Weaver 里：派实现子 Agent 用 `task(mode="general")`，派评审子 Agent 用 `task(mode="explore")`（只读）；问用户用 `ask_user`；任务清单用 `todo_write`。

executing-plans 第 50 行 “see the per-platform references in `../using-superpowers/references/`” 改成 “Weaver 的子 Agent 工具是 `task`”。

### 3. 标记场景

后 8 个的 frontmatter 加 `weaver-when: git`（Claude Code 不认识这个字段，会忽略）。

其中流程类的 6 个（brainstorming、writing-plans、executing-plans、subagent-driven-development、using-git-worktrees、finishing-a-development-branch）再加 `weaver-agent: main`：只给主 Agent。它们要问人、要派子 Agent，子 Agent 拿到只会卡住（上游靠 using-superpowers 里的 SUBAGENT-STOP 防这个，我们没带它）。

### 4. 不带的文件

systematic-debugging 里的 `CREATION-LOG.md`、`test-academic.md`、`test-pressure-*.md` 是上游写 skill 时的记录和压力测试，不是给模型用的，复制时去掉（否则会出现在“附带的文件”里干扰模型）。

### 5. 不改的

- 正文里 `superpowers:xxx` 这种带前缀的引用：不改内容，由加载器容错（第五节）。
- “your human partner”：照旧，模型会用 ask_user。
- 方案和计划的默认位置 `docs/superpowers/specs|plans/`：照旧（skill 里写了“用户偏好优先”）。
- brainstorming 的可视化草图：照旧。它用 bash 起本机服务、`open` 打开浏览器，在 macOS 本机能用。

## 五、加载器改动（skills.py）

| 改什么 | 为什么 |
|---|---|
| 扫描列表加 `builtin` 一级 | 第二节 |
| `weaver-when: git`：项目根在 git 仓库里（向上找到 `.git`）才列出 | 第三节；不认识的值当作“总是” |
| `load(name)` 找不到且名字带 `:` 时，去掉 `:` 前面的部分再找一次 | 上游 skill 互相引用写 `superpowers:test-driven-development`；对其它带命名空间的第三方 skill 也有用 |
| `MAX_BODY` 30KB → 40KB | subagent-driven-development 正文 32KB，删掉选模型那节后仍接近 30KB |

## 六、测试（tests/test_skills.py）

1. 内置的能被发现，来源显示“内置”，不触发信任确认。
2. 用户级、项目级同名的覆盖内置。
3. `weaver-when: git`：git 仓库里列出、普通目录不列出；不认识的值照常列出。
4. `load("superpowers:verification-before-completion")` 能加载到 `verification-before-completion`；`load("x:不存在")` 照常报“没有”。
5. 40KB 上限：31KB 的正文不截断。
6. 内容检查：`weaver/builtin_skills` 下所有文件里不再出现 “Model Selection”“most capable”；每个 SKILL.md 的 frontmatter 能解析出 name 和 description。
7. 真实验证：
   - git 仓库里故意放一个会失败的测试，说“测试挂了修一下”，看它是否先加载 systematic-debugging、先找根因。
   - 普通目录里说“帮我整理一下桌面截图”，确认 brainstorming 等 8 个没出现在列表里。
   - git 仓库里说“加一个导出 CSV 的功能”，看 brainstorming 是否用 ask_user 一次一问、等批准后才动手（依赖 ask-user.md 先做完）。

## 七、和 ask_user 的先后

两件事可以分开做。没有 ask_user 时，brainstorming 等 skill 照样能跑（模型把问题写进结论、等追问），只是体验差；所以先做 ask_user，再做内置 skills，最后做第六节第 7 条的真实验证。

## 八、许可

- 每个 skill 目录放一份上游 LICENSE（MIT）。
- `weaver/builtin_skills/NOTICE.md`：来源仓库、commit、第四节的改动清单。

---

## 进度（2026-10-02）

已实现：`weaver/builtin_skills/`（10 个，改动见 NOTICE.md）；skills.py 的 `builtin` 级别、`weaver-when: git`（向上找 `.git`，到用户主目录为止）、`weaver-agent: main`（`Skills.for_subagent()` 给子 Agent 的视图）、带前缀名字的容错、正文上限 40KB、附带文件不列 LICENSE。测试 `tests/test_skills.py::Builtin`、`tests/test_builtin_skills.py`。

验证：Keygent 包里有这 10 个目录；用包里的代码查，普通目录只列 verification-before-completion 和 systematic-debugging，git 仓库里 10 个都列；`superpowers:test-driven-development` 加载到内置的那份。
本机：`~/.claude/skills/` 里原来同名的 4 个已移到废纸篓（2026-10-02），现在这台机器上用的也是内置版本：普通目录只列 2 个，git 仓库里 10 个。

没做：真实模型下“测试挂了修一下”“加一个导出 CSV 的功能”两个场景（第六节第 7 条）。

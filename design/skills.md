# Weaver Skills 设计

草案 v1 · 2026-10-01

## 一、要解决什么问题

有些事有固定做法：怎么发版、怎么处理 PDF、这个仓库的 PR 规范、怎么跑某个复杂的数据流程。每次都在对话里讲一遍太累，写进 system prompt 又太占地方（大部分会话用不上）。

Skill 就是一份**按需加载的说明书**：平时只让模型知道“有这么一份说明书、讲什么”，真用到时才把全文读进来。它是第 10 课说的“程序记忆”——怎么做事。

## 二、主流做法（main.md 第 20 课）

**格式已经统一**（Agent Skills 规范）：一个目录，里面一个 `SKILL.md`（YAML frontmatter + 正文），可以附带 scripts、references、assets。

```markdown
---
name: release
description: 发版流程：改版本号、生成 changelog、打 tag。用户说“发版”“release”时用。
---

1. 先跑 `make test` 确认全过
2. …
```

**调度没有专门的路由器**，拆成四步挂在内核循环已有的环节上（以 OpenCode 为例）：

```
① 发现：扫描各 skills 目录的 SKILL.md，解析名字和描述
② 注入：只把名字和描述放进上下文
③ 选择：模型自己决定调用 skill("release")           ← 没有代码，靠模型判断
④ 加载：一次普通的工具调用，返回正文和附带文件的路径
```

skill 本身不执行代码，只是被读进上下文的说明；模型读完再用 bash、read_file 去执行。

**来源在从“人写”走向“模型写”**：人手写 / 安装 → skill-creator 辅助人写 → 模型当场用工具写 → 后台从经验中自动提炼。**模型写的 skill 等于在给未来的自己写指令**，被注入污染就是持久化的后门，所以需要治理：扫描、审批、版本、只归档不删除（OpenClaw、Hermes）。

## 三、我们的做法（第一版：只加载）

### 1. 发现

扫描这些目录下的 `*/SKILL.md`（项目级优先，同名时项目级覆盖用户级）：

| 目录 | 级别 |
|---|---|
| `<项目>/.weaver/skills/` | 项目 |
| `<项目>/.claude/skills/` | 项目（兼容 Claude Code） |
| `~/.weaver/skills/` | 用户 |
| `~/.claude/skills/` | 用户（兼容 Claude Code） |

frontmatter 只认 `name`、`description`（必填），`when_to_use`、`disable-model-invocation`（可选，后者为 true 的不列给模型）。别的字段忽略不报错，所以 Claude Code、Codex 的 skill 能直接用。

### 2. 注入：作为背景信息，不进 system prompt

所有 skill 的“名字 + 描述”作为一个背景提供者（`kind="skills"`，environment.md 的机制）放进上下文：会话开始时放一份，skill 有增删改时在下一条用户输入前追加更新。不碰 system prompt 和工具清单，缓存不受影响。

```
## 可用的 skills（需要时用 skill 工具加载全文）
- release：发版流程：改版本号、生成 changelog、打 tag。用户说“发版”“release”时用。
- pdf：处理 PDF：拆分、合并、提取文字、填表单。
```

- 每条描述最多 300 字；超过 100 个 skill 时只列名字，描述让模型用 `skill` 工具的搜索功能查（Claude Code 的 skill 搜索也是为这个）。

### 3. 加载：一个工具

```
skill(name)
```

返回：SKILL.md 正文（去掉 frontmatter）+ 这个 skill 目录下附带文件的路径列表（最多 20 个，scripts、references 等），模型需要时用 read_file 读、用 bash 跑。正文最多 30KB。

- 放行，不问人（只是读一段文字）；可以并行。
- 子 Agent 也能用（explore、general 都给）。

### 4. 安全

- **项目级 skill 要先信任一次**：和 MCP 的项目配置一样，克隆来的仓库里的 skill 是别人写的指令，可能带注入。第一次见到某个项目的 skills（按“项目路径 + 所有 SKILL.md 内容的哈希”认），列出它们问一次，同意后记到 `~/.weaver/trusted-skills.json`；内容变了要重新问。没被信任的项目级 skill 不列给模型。用户级的不用问。
- 加载时在正文前标明来源：“以下是 skill「release」的说明（来自项目目录 .claude/skills/release/）”。
- 正文经过脱敏（redaction.md）——这一步在工具结果进账本时自动发生。

## 四、第二版：模型自己写 skill（先设计，后实现）

模型做完一件有固定套路的事后，提出“把这个做法存成 skill”。参照 OpenClaw 的提案流程和 Hermes 的只归档不删除：

| 步骤 | 做法 |
|---|---|
| 提出 | 工具 `skill_propose(name, description, body)`，写进 `~/.weaver/skills/.proposals/` |
| 检查 | 密钥扫描（redaction.md 的规则）、注入扫描（记忆模块的规则）、格式校验 |
| 审批 | 默认等人批（`weaver skills review`，或交互模式下当场问）；批准后移进正式目录，frontmatter 记 `created_by: agent` |
| 版本 | 覆盖时旧版本归档到 `.archive/`，可以回滚；不真删 |

这样“学会”这件事由模型提出，写进去之前经过扫描和人的批准——第 20 课的分工。

## 五、代码结构

```
weaver/skills.py    发现、解析、信任、背景提供者、skill 工具
```

## 六、测试

1. 发现：四个目录、项目级覆盖同名用户级、坏的 SKILL.md（没有 frontmatter、缺 name）跳过并提示、`disable-model-invocation` 不列出。
2. 背景信息：格式、描述截短、超过 100 个只列名字；增删改后下一条输入前追加更新、没变化不追加。
3. 工具：返回正文和附带文件列表、上限、不存在时列出可用的、标明来源。
4. 信任：项目级第一次要问、同意后记住、内容变了重新问、没信任的不列出。
5. 兼容：一个 Claude Code 格式的 skill（带 allowed-tools 等字段）能被正常加载。
6. 真实验证：写一个“发版流程”skill，问“帮我发个版”，看模型是否先加载 skill 再照着做。

---

## 进度（2026-10-01）

第一版已实现：`weaver/skills.py`（发现、frontmatter 解析、项目级信任、背景提供者、skill 工具和按关键字查找）；子 Agent 也有 skills 列表和 skill 工具；CLI 启动时对未信任的项目级 skill 问一次（`--yes` 时不加载并提示）。测试见 `tests/test_skills.py`（10 项）。

兼容性：本机 `~/.claude/skills/` 里现成的 3 个 Claude Code skill（eval、eval-new、scifraudscan）被正常发现和加载，未修改。

真实验证：临时项目 + 用户级 skill “release”（跑测试、升补丁号、写 CHANGELOG、不许 commit 和打 tag），只说“帮我发个版”。模型先加载 skill，再照步骤做：测试通过 → 1.4.2 升到 1.4.3 → CHANGELOG 加新一节 → 一句话报告；没有 commit、没有打 tag。

第二版（模型自己写 skill：提案 → 扫描 → 人批准 → 可回滚）未做。

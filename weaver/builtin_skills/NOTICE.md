# 内置 skills 的来源和改动

来源：[obra/superpowers](https://github.com/obra/superpowers)，commit `8ca22db`（2026-09-25），MIT 许可（每个目录里有 LICENSE）。设计见 `design/builtin-skills.md`。

同步上游时：重新复制这 10 个目录，再按下面逐条重做；`tests/test_builtin_skills.py` 会检查改动是否都在。

## 复制时去掉的文件

- `systematic-debugging/CREATION-LOG.md`、`test-academic.md`、`test-pressure-1/2/3.md`：上游写 skill 时的记录和压力测试，不给模型用。

## 删掉选模型的逻辑（Weaver 的子 Agent 和主 Agent 用同一个模型，task 工具没有 model 参数）

- `subagent-driven-development/SKILL.md`：删整节 “Model Selection”；流程图里 “R≥4 fresh implementer, more capable model” 改成 “R≥4 fresh implementer”（5 处）；“Rounds 4-5” 那段去掉换更强模型；BLOCKED 处理里删“换更强的模型重派”那一条；最终评审去掉 “on the most capable available model (see Model Selection)”；示例里去掉 “most capable model”。
- `subagent-driven-development/implementer-prompt.md`、`task-reviewer-prompt.md`、`re-review-prompt.md`：删模板里的 `model: [MODEL …]` 两行和占位说明里的 `[MODEL]`；implementer 的升级说明去掉“换更强的模型”。
- `executing-plans/SKILL.md`：删 “runs well on a mid-tier session model…” 那段；最终评审去掉 “on the most capable available model” 和 “Specify the model explicitly…”；示例里去掉 “most capable model”。
- `writing-plans/SKILL.md`：执行方式里 Native 的说明去掉 “on the most capable model” 和 “Runs well with a mid-tier session model…”。

## 加上 Weaver 的说明

- `subagent-driven-development/SKILL.md`、`executing-plans/SKILL.md`、`requesting-code-review/SKILL.md`：标题下加一段“在 Weaver 里：派实现子 Agent 用 task(mode="general")……问用户用 ask_user……”。
- `executing-plans/SKILL.md`：“Your harness has no subagent tool (see the per-platform references in ../using-superpowers/references/)” 改成 “You chose not to dispatch subagents (Weaver's subagent tool is `task`)”（没带 using-superpowers；Weaver 总有子 Agent 工具）。

## 场景标记

- brainstorming、writing-plans、executing-plans、subagent-driven-development、test-driven-development、using-git-worktrees、requesting-code-review、finishing-a-development-branch：frontmatter 加 `weaver-when: git`（只在 git 仓库里列出；Claude Code 不认识这个字段，会忽略）。

- brainstorming、writing-plans、executing-plans、subagent-driven-development、using-git-worktrees、finishing-a-development-branch：再加 `weaver-agent: main`（只给主 Agent：这些要问人、要派子 Agent，子 Agent 拿到只会卡住；上游靠 using-superpowers 的 SUBAGENT-STOP 防这个，我们没带它）。

## 没改的

- 正文里 `superpowers:xxx` 的引用：加载器去掉前缀再找（weaver/skills.py）。
- “your human partner”、方案和计划的默认位置 `docs/superpowers/…`、brainstorming 的可视化草图：照旧。

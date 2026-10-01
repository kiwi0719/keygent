# Weaver 写工具、权限与沙箱设计

草案 v1 · 2026-10-01

## 一、要做什么

让 Agent 能真正“干活”：改文件、跑命令。同时回答两个问题：**允不允许做**（权限），**做了最多能坏到哪**（沙箱）。

| 工具 | 做什么 |
|---|---|
| `write_file` | 写整个文件（新建或覆盖） |
| `edit_file` | 字符串替换：把一段原文换成新内容 |
| `bash` | 在工作目录执行一条 shell 命令 |

main.md 第 17 课的比喻：**沙箱是把人关在房间里，权限是门口的保安**。两个都要：只有保安，看走眼就没有第二道防线；只有房间，房间里的东西（你的项目）照样能被删光。

我们的“简单粗暴四件套”：

1. **工具只做最简单的形式**：整文件写、字符串替换、单条命令。
2. **文件改动都能撤销**：改之前先存一份原文。
3. **权限就几条规则**：工作目录内的读写放行，其余问人，极少数直接拒绝。
4. **系统级沙箱**：`bash` 只能写工作目录和临时目录（macOS 自带的 `sandbox-exec`）。

沙箱管“房间外面”，撤销管“房间里面”，合起来是一张完整的安全网。

## 二、三个工具

### write_file(path, content)

- 新建或整体覆盖。父目录不存在就自动建。
- **覆盖已有文件前必须先读过它**（read_file 或之前的 write/edit），而且读过之后文件没被别人改过。防止模型没看内容就把文件整个冲掉。

### edit_file(path, old_string, new_string, replace_all=false)

沿用 Claude Code 的做法（`FileEditTool`：`old_string` / `new_string` / `replace_all`），这是 Claude 模型最熟悉的格式。

- `old_string` 必须**恰好出现一次**；出现 0 次或多次都报错，并说明是哪种情况（多次时让模型多带点上下文，或用 `replace_all`）。
- `old_string` 和 `new_string` 不能相同。
- 和 write_file 一样：**改之前必须先读过**，读过之后没被别人改过。
- 返回改动位置附近的几行，方便模型确认改对了。

**“先读过”怎么记**：工具箱在内存里记下“这个文件读过，当时内容的哈希是多少”。进程重启（崩溃恢复、`-s` 接着跑）后清空，改文件前得重新读一遍。宁可多读一次，也不盲改。

### bash(command, timeout=120)

- 用 `/bin/bash -c` 执行，工作目录固定为项目根目录；每条命令是新的 shell，不保留 `cd` 和环境变量。
- 超时默认 120 秒，最多 600 秒，超时就杀掉进程，返回已有输出。
- **输出保留结尾**（报错通常在最后）：超过 30KB 时只给最后 30KB，完整输出存到 `.weaver/outputs/<调用 id>.log`，把路径告诉模型（第 4 课“原文不删、只给预览和路径”）。
- 返回里写明退出码。
- **环境变量去掉密钥**：名字里带 `KEY`、`SECRET`、`TOKEN`、`PASSWORD` 的都不传给子进程。`.env` 里的 `OPENROUTER_API_KEY` 会被读进环境变量，不去掉的话，一条 `env` 命令就能把它打进上下文（第 17 课：DeerFlow、Codex 的做法）。
- 在沙箱里执行（见第五节）。

## 三、撤销

- write_file、edit_file 改文件之前，把**原文存进 BlobStore**（按内容哈希，重复内容只存一份）；文件原本不存在就记“新建”。
- 每次改动记一行到 `.weaver/undo/<会话>.jsonl`：`{路径, 改前的引用或 null, 改后的哈希, 调用 id, 时间}`。
- `python3 -m weaver -s <会话> --undo`：撤销这个会话最近一次文件改动。**如果文件在那之后又被改过（当前哈希不等于记录的“改后哈希”），拒绝撤销**，免得冲掉别人的修改。可以连续执行，一步步往回退。
- `bash` 的改动撤销不了（它能做任何事）。所以 bash 默认要问人，而且在沙箱里跑。

## 四、权限

### 三种结果

| 结果 | 含义 |
|---|---|
| 放行 | 直接执行 |
| 问人 | 停下来等批准（内核现成的“等待”状态，可以跨进程） |
| 拒绝 | 不执行，直接告诉模型原因 |

内核要加一点：`policy.wait_for` 现在只能返回“等”或“不等”，加一种返回值 `Deny(reason)`，规则 6 里遇到它就补一条“被拒绝：原因”的错误结果。

### 规则

| 工具 | 情况 | 结果 |
|---|---|---|
| read_file / grep / find_files | 敏感路径：`~/.ssh`、`~/.aws`、`~/.gnupg`、`.env`（`.env.example` 等样例除外） | 问人 |
| | 其他 | 放行 |
| write_file / edit_file | 工作目录内（按真实路径判断，防符号链接绕出去），且不在 `.git/`、`.weaver/` 下 | **放行**（能撤销） |
| | 工作目录外，或 `.git/`、`.weaver/` 下 | 问人 |
| remember / forget | | 放行（写的是记忆目录） |
| bash | 命中硬拦截规则 | **拒绝**（`--yes` 也拦） |
| | 只读命令 | 放行 |
| | 这个会话里已经“总是允许”过同样前缀的命令 | 放行 |
| | 其他 | 问人 |

**只读命令**：整条命令里每一段（按 `|` `&&` `||` `;` 拆开）都是只读命令，并且没有重定向写文件（`>`、`>>`、`tee`）、没有命令替换（`` ` ``、`$(`）。只读命令清单：`ls cat head tail wc pwd echo which file stat du df tree rg grep find（不带 -delete / -exec）sort uniq diff`、`git status/diff/log/show/branch/blame`、`sed -n`、`python --version` 等。拿不准的一律当成“需要问”。

**硬拦截**（正则，明知能被绕过，只挡最明显的手滑）：`sudo`、`rm -rf /` 或 `~`、`mkfs`、`dd … of=/dev/`、`curl|wget … | sh/bash`、`chmod -R 777 /`、fork 炸弹、`git push --force` 到 main/master。

**“总是允许这个前缀”**：审批时可以选 `a`，把命令的前两个词（如 `npm test`、`git commit`）记为本会话允许。这个选择写在账本的 `WaitResolved` 里（`{"allow": true, "always": "npm test"}`），规则从账本读，所以崩溃恢复后照样生效，换进程也不丢。

### 两种模式

| 模式 | 问人的操作 |
|---|---|
| 默认 | 按上表问 |
| `--yes` | 不问，全部放行；**硬拦截照样拒绝，沙箱照样生效** |

## 五、沙箱

### macOS：sandbox-exec

系统自带，不用装东西（Codex 在 macOS 上也用它）。Codex 的规则是“默认全禁，再逐项放开”，要列一长串系统参数；我们用反过来的简单写法：**默认全放开，只禁写，再放开几个可写目录**。

```scheme
(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath "<工作目录>")
  (subpath "/private/tmp") (subpath "/private/var/folders")   ; 临时目录
  (literal "/dev/null") (regex #"^/dev/tty") (regex #"^/dev/fd/"))
(deny file-write* (subpath "<工作目录>/.git") (subpath "<工作目录>/.weaver"))
; WEAVER_SANDBOX_NET=0 时再加：(deny network-outbound (remote ip))
```

本机实测：工作目录内写入成功；写 `.git`、写家目录都被系统拒绝（`Operation not permitted`）；读文件、跑 Python 正常；后写的 deny 会覆盖先写的 allow。

**取舍**：

- **网络默认放开**：`pip install`、`npm install` 要联网。bash 本来就要问人，而且拿不到密钥（环境变量已去掉）。想更严就 `WEAVER_SANDBOX_NET=0`。
- **`.git` 只读**：沙箱里 `git commit` 会失败（Codex 也这样），防止改 git 钩子逃出沙箱。需要提交时由用户自己来，或者 `WEAVER_SANDBOX_GIT=1` 放开。
- **包管理器的缓存目录不可写**（如 `~/.npm`、`~/Library/Caches`）：可能导致安装变慢或报错，需要时用 `WEAVER_SANDBOX_WRITABLE` 追加可写目录（冒号分隔）。
- 命令被沙箱拦下时（输出里有 `Operation not permitted`），在结果末尾提示模型“这是沙箱限制：只能写工作目录和临时目录”，免得它反复重试。

### Linux：bubblewrap

思路和 macOS 一样：整个文件系统只读，再把可写的目录挂成可写。参数参考 Codex（`codex-rs/linux-sandbox/src/bwrap.rs`）：

```
bwrap --new-session --die-with-parent          # 脱离终端（防 TIOCSTI 注入）；父进程死了沙箱跟着死
  --ro-bind / /                                  # 整个系统只读
  --dev /dev --bind-try /dev/shm /dev/shm --proc /proc
  --bind /tmp /tmp                               # 临时目录可写（和 macOS 一致，命令之间能传临时文件）
  --bind <工作目录> <工作目录>                    # 工作目录可写
  --ro-bind-try <工作目录>/.git <工作目录>/.git   # 后挂载的覆盖先挂载的：.git、.weaver 仍只读
  --ro-bind-try <工作目录>/.weaver <工作目录>/.weaver
  --bind <额外可写目录> …                         # WEAVER_SANDBOX_WRITABLE
  --unshare-user --unshare-pid --unshare-ipc     # 独立的用户、进程、IPC 命名空间
  [--unshare-net]                                # WEAVER_SANDBOX_NET=0 时断网
  --cap-drop ALL
  --chdir <工作目录>
  -- /bin/bash -c <命令>
```

**不随项目打包 bwrap**（和 ripgrep 不同）：

- 许可证是 LGPL，不是 MIT；直接带二进制要额外履行 LGPL 的义务。
- 它依赖内核的“非特权用户命名空间”，很多环境里是关的（Docker 默认的 seccomp 配置、部分 Ubuntu 的 AppArmor 限制）。带了也可能跑不起来。

所以用系统装的 `bwrap`（`apt install bubblewrap` / `dnf install bubblewrap`），并且**启动时先试跑一次** `bwrap --ro-bind / / --unshare-user … true`：

| 结果 | 处理 |
|---|---|
| 找不到 bwrap | 当作没有沙箱，启动时提示怎么装 |
| 试跑失败（通常是在容器里，或内核禁了用户命名空间） | 当作没有沙箱，提示“如果容器本身就是隔离环境，可以用 --no-sandbox” |
| 成功 | 用它 |

试跑结果按进程缓存，只跑一次。

### 其他平台
- 没有可用沙箱时：启动时提示“没有沙箱”；这时 `--yes` 需要再加 `--no-sandbox` 才生效，免得不知不觉裸奔。
- `WEAVER_SANDBOX=0`：主动关闭沙箱（同样需要 `--no-sandbox` 才能配 `--yes`）。

**沙箱只管 bash**：write_file / edit_file 是 Weaver 自己的 Python 代码写文件，由权限规则管（工作目录外要问人）。

## 六、放在哪一层

| 部分 | 位置 |
|---|---|
| 三个工具、读过记录、撤销日志 | `weaver/tools/` |
| 权限规则（放行 / 问人 / 拒绝）、只读命令判断、硬拦截 | `weaver/permissions.py`，由 Policy 调用 |
| `Deny` 返回值 | 内核规则 6（小改动） |
| 沙箱规则生成、执行包装 | `weaver/sandbox.py` |
| `--yes`、`--undo`、审批时的 y / n / a | CLI |

## 七、测试

1. edit_file：恰好一次、零次、多次、replace_all、没读过就改、读过后被外部改了、old = new。
2. write_file：新建（自动建目录）、覆盖要先读过。
3. 撤销：改文件 → undo 恢复；新建 → undo 删除；之后又被改过 → 拒绝；连续 undo 一步步退回。
4. bash：退出码、超时、输出保留结尾并存全文、密钥环境变量被去掉。
5. 权限：每条规则一个用例；只读判断（管道、重定向、`$(`、`find -delete`、`sed -i`）；硬拦截；“总是允许”写进账本后换进程仍生效；`--yes` 下硬拦截仍拒绝。
6. 沙箱（仅 macOS）：工作目录可写；`.git`、家目录不可写；`WEAVER_SANDBOX_NET=0` 时断网；被拦时结果里有提示。
7. 内核：`Deny` 变成错误结果，调用和结果仍一一配对。
8. 真实验证：让它修一个故意写错的小程序（改文件 + 跑测试），再 `--undo` 回退。

---

## 进度（2026-10-01）

已实现：`weaver/tools/edit.py`（write_file、edit_file、“先读过”记录、撤销日志）、`weaver/tools/shell.py`（bash）、`weaver/permissions.py`（PermissionPolicy、只读命令判断、硬拦截、“总是允许”）、`weaver/sandbox.py`（macOS sandbox-exec）；内核规则 6 支持 `Deny`；CLI 加 `--yes`、`--no-sandbox`、`--undo`，审批支持 y / a / N。测试见 `tests/test_write_tools.py`（22 项）。

真实验证（临时目录里一个带两个 bug 的 `calc.py` 和它的测试，`--yes`，沙箱开启）：模型跑测试 → 读代码 → 一次 edit_file 修好两个 bug → 再跑测试全部通过；沙箱里 `git commit` 被系统拦下（`.git` 只读），结果里带了“沙箱限制”的提示；`--undo` 把 `calc.py` 恢复成和原来逐字节一致。

发现的问题：模型开头浪费了两轮——先 `cd` 到一个自己编的路径，又用了本机没有的 `python`。原因是它不知道工作目录和环境。下一步：会话开始时追加一条“环境信息”背景输入（工作目录、平台、可用的 python 命令等），复用记忆快照的 `context` 机制（Codex 的做法）。

### Linux 沙箱进度（2026-10-01）

已实现：`Sandbox` 支持 `bwrap`（参数构造、按进程缓存的试跑、`WEAVER_BWRAP` 指定路径、失败原因写进启动提示）。离线测试覆盖参数顺序和选项；真实测试（`BwrapReal`）只在能用 bwrap 的 Linux 上跑，用 `scripts/test-linux.sh` 在 Docker 里执行（需要 Docker 在运行）。

真实验证（Docker，`scripts/test-linux.sh`）：

| 容器 | bwrap | 结果 |
|---|---|---|
| linux/arm64（原生） | 可用（这个容器不允许挂新的 /proc，自动退到沿用宿主 /proc） | 工作目录可写；`.git`、家目录写不了（`Read-only file system`）；断网时连接真的被拦；全部测试通过 |
| linux/amd64（在 Apple Silicon 上模拟） | 试跑失败（模拟器不支持），自动退回“无沙箱”并说明原因 | 自带的 x86_64 ripgrep 正常；全部测试通过 |

实测修掉的问题：

1. **试跑和真正执行的参数不一致**：试跑没挂 `/proc`，真正执行挂了，Docker 里不允许，结果试跑通过、每条命令都失败。改成用完全相同的参数试跑；挂 `/proc` 失败就退一步不挂（Codex 的 `mount_proc` 开关），记住能用的那种。
2. **提示说错**：bwrap 自己启动失败的报错里也有 `Operation not permitted`，被当成“被沙箱拦下、别重试”。以 `bwrap:` 开头的报错改为提示“沙箱启动失败，是环境问题”。
3. **Linux 的报错写法不同**：只读挂载拦下时报 `Read-only file system`（macOS 是 `Operation not permitted`），两种都认。
4. **断网测试是假通过**：命令在沙箱启动时就失败了，测试只看“退出码不是 0”，于是算通过。改成让命令真的跑起来，断言输出里是“连接被拦”。
5. grep 调 ripgrep 时有一个管道没关（`ResourceWarning`），已修。

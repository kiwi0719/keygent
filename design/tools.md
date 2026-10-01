# Weaver 工具层设计：读文件与搜索

草案 v1 · 2026-10-01

## 一、先做哪些工具

编码 Agent 的工具底座已经收敛成 read / write / edit / bash / grep / find（main.md 第 4 课）。这一步先做**只读的三件**：

| 工具 | 做什么 | 实现 |
|---|---|---|
| `read_file` | 读文件，带行号，可以只读一段 | Python |
| `grep` | 按正则搜文件内容，返回“路径:行号: 内容” | ripgrep，找不到就用纯 Python |
| `find_files` | 按 glob 找文件，最近修改的排前面 | `rg --files`，找不到就用纯 Python |

写文件、改文件、跑命令会改动东西，要和权限、沙箱一起设计，放到下一步。

## 二、共同规矩

1. **工作目录**：工具箱有一个根目录（默认当前目录）。相对路径都相对它解析；输出的路径尽量写成相对路径，省 token。
2. **输出有上限**：每个工具都有条数上限和 50KB 的字节上限，超出就截断，并在末尾写明“截断了、怎么继续”（换更具体的条件、调大 limit、用 offset）。这是第 4 课的共识，也是之前 `read_file` 那次踩过的坑。
3. **长行截断**：搜索结果里单行最多 300 字，想看完整内容用 `read_file`。
4. **出错也是结果**：路径不存在、正则写错、超时，都返回一段说明文字，交给模型换做法。
5. **默认跳过的目录**：`.git`、`.weaver`（我们自己的账本，里面全是以前的工具输出，搜进来会污染结果）、`node_modules` 等。其余遵守 `.gitignore`。
6. **超时**：搜索最多 30 秒。
7. **标记只读**：每个工具声明自己是否只读。以后用来决定“只读工具可以并行”“只读工具不用审批”。

## 三、ripgrep 从哪来

**随项目打包固定版本**：ripgrep 是 MIT / Unlicense 双许可，可以直接带上，附许可证即可（`weaver/vendor/ripgrep/`）。

| 平台 | 文件 |
|---|---|
| macOS Apple Silicon | `weaver/vendor/ripgrep/aarch64-apple-darwin/rg` |
| Linux x86_64（服务器，静态链接，哪个发行版都能跑） | `weaver/vendor/ripgrep/x86_64-unknown-linux-musl/rg` |

- 版本 15.2.0，来自 `github.com/BurntSushi/ripgrep/releases/tag/15.2.0`，压缩包下载时已用官方 `.sha256` 校验。
- 解压后 `rg` 的 SHA256 写在 `weaver/tools/search.py` 里，加载时再核对一次：文件被替换过就不用。
- 查找顺序：`WEAVER_RG` → 项目自带 → 系统 PATH → 常见安装位置 → 纯 Python（输出格式一样，只是慢、`.gitignore` 只支持常见写法）。
- 其他平台（Windows、Intel Mac、Linux ARM）需要时再加，同样的流程。

**“打包”和“运行时自动下载”的区别**：Pi 找不到 ripgrep 时会在运行时联网下载，拿到的版本和内容不可控，这才是供应链风险（第 21 课）。打包是我们固定版本、校验好再放进项目，运行时从不联网。

注意：Claude Code 的终端里 `rg` 是一个转发到 Claude Code 自带 ripgrep 的 shell 函数，Python 找不到它，不能依赖。

## 四、参数

```
grep(pattern, path=".", glob=None, ignore_case=False, literal=False, context=0, limit=100)
find_files(pattern, path=".", limit=200)
read_file(path, offset=1, limit=2000)
```

- `grep` 的 `glob` 用来筛文件，如 `*.py`、`src/**/*.ts`；`literal=true` 时按普通字符串搜，不当正则。
- `find_files` 按修改时间从新到旧排序：最近改过的文件通常最相关。

## 五、测试

两种后端（ripgrep、纯 Python）跑同一组测试：匹配、行号、上下文行、忽略大小写、按字符串搜、glob 筛选、跳过 `.git` / `.weaver` / `.gitignore` 里的内容、条数上限、长行截断、正则写错、路径不存在。没有可用的 ripgrep 时，ripgrep 那组自动跳过。

---

## 进度（2026-10-01）

已实现：`weaver/tools/`（`files.py`、`search.py`、`__init__.py`），测试见 `tests/test_tools.py`（两种后端各 10 项同样的测试，全部通过）。

真实验证：问“内核里上下文超长怎么处理，给出文件和行号”，模型一轮里并行发了 3 个 grep，再用 read_file 只读相关十几行，答案里的文件和行号准确。

### 后续改动（2026-10-01）

- 会改东西的工具（write_file、edit_file、bash）、权限和沙箱见 write-tools.md。`read_file` 读过的文件会被记下，改文件前必须读过。
- 每个工具声明能不能并行（只读的能；bash 看命令是否只读），执行者按此分批并发，见 parallel-subagent.md。
- 工具可以返回附加信息（如子 Agent 的用量）；需要调用 id 的工具（子 Agent）会拿到它。

# claude-codex-bridge

[English](README.md) | 简体中文

一个 Claude Code 插件，让 Claude 把活交给你本机的 [Codex CLI](https://github.com/openai/codex) 去做。Claude 负责规划、选模型、验收结果，Codex 负责动手。

```text
/codex:run 修一下 tests/test_login.py 里失败的测试
```

Claude 会先判断任务难度，选好 Codex 的模型和强度，开跑，并告诉你它选了什么。你可以实时看 Codex 干活，事后回看，随时取消，也可以在自己的终端里接管同一个 Codex 会话。Codex 说做完之后，Claude 会自己看 diff、重跑测试，确认了再向你汇报。

## 安装

```text
/plugin marketplace add sidchy/claude-codex-bridge
/plugin install codex@codex-bridge
```

重启 Claude Code，输入 `/`，应该能看到 `/codex:run` 和 `/codex:config`。

需要本机装好 Codex CLI 并登录（`npm i -g @openai/codex@latest`），Python 3.9 或更高版本（原生 Windows 使用 CPython 3.10 或更高版本），不需要 pip 安装任何东西。实时观看面板需要 Claude 桌面端，纯终端里 Claude 会把进展转述给你。

不想安装想先试试：克隆本仓库，运行 `claude --plugin-dir ./claude-codex-bridge/codex`。

如果你装了 OpenAI 官方的 `codex` 插件，两者都会注册 `/codex:*`，请禁用其中一个。

## Windows

支持在 PowerShell 和 Git Bash 中使用原生 Windows 11，无需 WSL。Windows 文件锁、进程查询与清理、管道流式读写均使用 Python 标准库和 Windows API。无需 pip 包、管理员权限、开发者模式或符号链接权限。请显式指定 Python 解释器并加上 `-X utf8`，不要依赖 shebang、`.py` 文件关联或 `python3` 别名。

在仓库目录中使用 PowerShell（请替换为自己的安装路径）：

```powershell
python -X utf8 .\codex\scripts\codex_bridge.py --help
python -X utf8 .\codex\scripts\codex_bridge.py config show
$env:CODEX_BRIDGE_HOME = 'C:\Users\you\Bridge Data'
& 'C:\Program Files\Python310\python.exe' -X utf8 'C:\src\claude-codex-bridge\codex\scripts\codex_bridge.py' jobs
```

在 Git Bash 中，显式使用原生 Windows Python：

```bash
python -X utf8 './codex/scripts/codex_bridge.py' --help
CODEX_BRIDGE_HOME='C:/Users/you/Bridge Data' python -X utf8 './codex/scripts/codex_bridge.py' config show
```

Windows 下打印的 watch/log/cancel/attach 命令采用 PowerShell 语法，并使用当前解释器的完整路径。请粘贴到 PowerShell；在 Git Bash 中，使用 `python -X utf8 '<脚本路径>'` 加相同参数，并按需使用 Bash 的环境变量前缀。PowerShell 使用 `&` 调用带引号的可执行文件路径，使用 `$env:CODEX_BRIDGE_HOME = '...'` 设置自定义目录。Attach 使用 `Set-Location -LiteralPath '...'; & ...`，也兼容 Windows PowerShell 5.1。建议用 JSON 数组设置 `extra_args`，以保留空格和反斜杠，例如 `extra_args=["--add-dir","C:\\work\\project"]`。

打印的 attach 命令可能使用 Python 包装调用，以保留参数中的引号。请原样复制完整命令。

PowerShell 5.1 向原生程序传递参数时可能移除内嵌的双引号。设置结构化参数时，请使用停止解析标记和转义引号（在仓库目录中运行）：

```powershell
python -X utf8 .\codex\scripts\codex_bridge.py --% config set "extra_args=[\"--add-dir\",\"C:/work/project\"]"
```

其他位置的单引号 JSON 示例使用 Unix/Git Bash 引号规则。从 Bash 工具调用时，请通过 `powershell.exe -NoProfile -Command '<完整命令>'` 执行生成的 PowerShell 命令，并按需转义外层 Bash 引号。

## 用法

只有一个命令，想做什么直接说。

```text
/codex:run 审查一下我当前的改动
/codex:run 把这个分支和 main 对比审查一下，要狠一点
/codex:run 把全仓库的 userId 改名成 user_id
/codex:run 它现在在干嘛？
/codex:run 停
/codex:run 我来接手
/codex:run
```

什么都不写，就是审查你未提交的改动。你也可以在话里直接指定模型、强度或权限，Claude 会照做。

只有你输入 `/codex:` 命令时才会用到 Codex，不会自动触发。

## 模型

Claude 在第一次调用前评估任务，并在整个 Codex 会话里保持这个选择。

| 任务 | 模型 |
|---|---|
| 普通修改、重构、查代码、讲解 | `gpt-6.1-sol` medium |
| 需要真正的推理：设计、复杂逻辑、数据迁移、安全、难查的 bug | `gpt-6-astra` medium，确实很难用 high |
| 重复性批量活（10 个以上独立条目） | `gpt-6.1-sol` 当主控，由它扇出最多 20 个 `gpt-6-luna` 子代理 |
| 任何审查 | `gpt-6-astra` high |

同一个会话里模型和强度不会改，因为一切换就会丢掉提示词缓存。如果结果因为质量问题没通过 Claude 的检查，Claude 会新开一个会话升一档（sol medium、astra high、astra xhigh），并写清楚上次哪里出了问题。最多升两次。

模型名取决于你的 Codex 账号和 CLI 版本，`/codex:config models` 能列出你能用的。

很小的任务（一两个文件、几行改动、随手查一下）Claude 自己直接做，因为转手花的 token 比省下的还多。想强制交给 Codex，说一句"强制用 codex"。

## 会话

每个独立的需求用一个有名字的 Codex 会话。同一件事的追加要求会复用它，Codex 保留上下文。不相关的需求开新会话。审查总是开新会话，免得审查者给自己的活打分。互不相干、改不同文件的任务可以并排跑。

## 观看和接管

每次运行都记录在 `~/.claude/codex-bridge/jobs/` 下。

| 你说 | 效果 |
|---|---|
| "它在干嘛？" | 实时时间线，显示 Codex 说的话、执行的命令、退出码和子代理。桌面端会在终端面板里打开。 |
| "给我看看过程" | 回放完整记录。 |
| "停" | 终止 Codex 和它启动的所有进程，并列出已经产生的部分改动。 |
| "让它改成做 X" | 中断正在进行的这一步（它启动的工具也一起停掉），在同一个 Codex 会话里带着你的引导继续，保留上下文和缓存，不会另开新会话。 |
| "我来接手" | 给你一行 `codex resume <会话>`，自己运行就能接着干。 |

超过约 30 秒的任务会自动转后台。大改动或有风险的操作（很多文件、删除、数据迁移），Claude 会先让 Codex 以只读方式出方案，给你看并等你确认再动手。说一句"直接做"可以跳过。

## Claude 拿到什么

跑完后 Claude 收到的是 Codex 的最终报告，原样、不截断、不改格式，外面包一行头（状态、退出码、任务号、会话号）和一个 `activity` 块。这个块里是桥接层自己收集的事实，所以报告写得再简略也盖不住问题：每个改动的文件、每个失败命令的退出码和输出末几行、错误和警告事件、是否超时、子代理数量、一行 `steps:` 统计、token 和额度。

中间的执行日志不会被倒进对话。Claude 需要时用 `log <id>` 取一份紧凑的执行轨迹：一步一行，相近的连续步骤合并，脚本折叠，只在失败和测试结果处展示输出，已知无害的 Codex 内部噪声折成一行，认不出的错误原文保留。原始日志 3 MB 的任务，通常压成几 KB。需要更多细节用 `log --full`、`--failed`、`--commands`、`--grep 关键词`，`watch` 则实时展示全部内容。手动使用桥接脚本的人可以加 `--live` 打印时间线。

## 子代理

Claude 按任务判断 Codex 要不要派子代理，并通过 `--subagents` 告诉它：`active` 用于量大且能拆成独立部分的活（大约 10 个以上同类条目、多个独立模块、广泛调研）；`none` 用于小的、串行的、紧耦合的、或要改共享文件的任务；其余用 `auto`（Codex 只在明显划算时才拆）。`--max-subagents N` 限制并发（最多 20）。

## 记忆和额度

Codex 只有一份覆盖所有项目的全局记忆，而且按关键词查找，所以名字、主题相近的项目会互相串。桥接层会把每次运行绑定到它的工作区：明确告诉 Codex 哪些记忆条目属于当前目录（目录本身，或它所在的 git 项目根，所以 worktree 会继承所属项目的记忆），其余一律忽略。全新的工作区一开始记忆是空的，会随着在这里的运行逐渐积累。把 `codex_memory` 设成 `off` 可以完全关闭记忆，设成 `on` 就是 Codex 原生行为。

每份报告末尾都会带上 Codex 自己返回的额度读数，比如 `quota: 48% of weekly used, resets 10-10 09:02`。`usage` 可以查看最新的读数。用量超过 80% 后，Claude 会避免 xhigh 和大规模扇出。

## 审查

审查用 `gpt-6-astra` high，只读。默认审你未提交的改动，也可以指定和某个分支（比如 main）对比，或某个提交。说"要狠一点"会走对抗式审查，专门挑设计上的毛病，按严重度排序。审查完 Claude 会把重要的发现对照代码核实，告诉你哪些站得住脚。

## 配置

`/codex:config` 可以查看和修改下面这些。配置文件是 `~/.claude/codex-bridge/settings.json`。

| 键 | 默认值 | 说明 |
|---|---|---|
| `model` | `gpt-6.1-sol` | 普通任务的主力模型 |
| `effort` | `medium` | `low`、`medium`、`high`、`xhigh`、`max`、`ultra`，取决于模型支持哪些 |
| `sandbox` | `danger-full-access` | `read-only`、`workspace-write` 或 `danger-full-access` |
| `model_reasoning` | `gpt-6-astra` | 设计和难推理 |
| `model_bulk` | `gpt-6-luna` | 扇出的子代理 |
| `review_model`、`review_effort` | `gpt-6-astra`、`high` | 审查 |
| `team_policy` | `true` | 告诉 Codex 主控何时派子代理、每个用什么模型 |
| `max_parallel` | `4` | `parallel` 的并发数 |
| `subagents`、`max_subagents` | `auto`、`20` | 子代理派发的默认模式和并发上限 |
| `codex_memory` | `scoped` | `scoped` 把 Codex 的全局记忆绑定到本次运行的工作区，`off` 关闭记忆，`on` 是 Codex 原生行为 |
| `timeout` | `1800` | 每次运行的秒数 |
| `profile` | 空 | Codex 配置 profile |
| `codex_bin` | `auto` | 在 ChatGPT 桌面端自带的 CLI 和 `PATH` 里的 CLI 中取较新的 |
| `extra_args` | `[]` | 传给 `codex exec` 的额外参数 |
| `roles` | `{}` | 新增或覆盖角色预设 |

```text
/codex:config set model=gpt-6-astra effort=high
/codex:config set sandbox=workspace-write
/codex:config reset
```

非法的值会被拒绝，不会改动文件。配置文件损坏时会直接报错停下，`config reset` 会先备份再恢复默认值。`CODEX_BRIDGE_HOME` 可以改变设置、任务和缓存目录的位置。

## 安全

Codex 默认以 `danger-full-access` 和审批策略 `never` 运行，也就是你能读写的地方它都能读写，执行命令也不会先问你。explorer、reviewer、architect 这几个角色始终只读。想收紧其余的：

```text
/codex:config set sandbox=workspace-write
```

## 脚本

插件只是 `codex/scripts/codex_bridge.py` 外面的一层薄壳，在 Unix 上可以直接调用，在 Windows 上通过 Python 调用。它会刷新稳定的启动器 `~/.claude/codex-bridge/bin/codex_bridge.py`：Unix 上为符号链接，Windows 上为普通 Python 包装脚本。

```text
run [--name L] [--role R] [--model M] [--effort E] [--sandbox S] [--cd DIR] [--background] [--continue] -
resume --session ID "<消息>"
review [--base REF | --commit SHA] [--adversarial] [--background] [关注点 ...]
run ... [--subagents none|auto|active] [--max-subagents N] [--live]
steer [id | --name L] "<消息>"
jobs | status | watch | log [--since N] [--tail N] [--full] [--failed] [--commands] [--grep P] | attach | wait | result | cancel   # 可带任务号或 --name
parallel tasks.json
roles | models | usage | config [show | set k=v | reset]
```

角色有 `explorer`、`worker`、`debugger`、`reviewer`、`architect`。Claude 如何评估任务、何时升级、何时自己动手，这些行为写在 `codex/commands/run.md` 里。

## 常见问题

提示某个模型 "is not supported when using Codex with a ChatGPT account"：你的 Codex CLI 太旧，运行 `npm i -g @openai/codex@latest`。

`unknown model`：这个模型不在你的 CLI 目录里，用 `/codex:config models` 查看。

`cannot read settings`：配置文件损坏，运行 `/codex:config reset`。

zsh 报 `exit 127`：直接调用脚本并给路径加引号。别写成 `python3 "..."` 放进同一个字符串，也别把路径存进不带引号的变量。

输入 `/` 看不到命令：重启 Claude Code，用 `claude plugin list` 确认 `codex@codex-bridge` 已启用。

## 开发

```bash
python3 -m unittest discover -s codex/tests -v
```

Windows 下（PowerShell 或 Git Bash）：

```text
python -X utf8 -m unittest discover -s codex/tests -v
```

测试全部离线，不调用任何模型。

## 许可证

[MIT](LICENSE)

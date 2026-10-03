# Claude Code 的 Codex 桥接插件

[English](README.md) · **简体中文**

> Claude 当大脑，你本机的 [Codex CLI](https://github.com/openai/codex) 当双手。
> 你只用人话说想要什么：Claude 判断难度、挑合适的 Codex 模型、把活交出去、**让你实时看着它干**、验证结果，再向你汇报。

```
 你 ──"/codex:run 修一下登录测试"──▶  Claude（规划 · 选模型 · 验证）
                                          │ 交接说明
                                          ▼
                                codex_bridge.py  ──▶  codex exec（你本机的 Codex CLI）
                                          │                │  └─ 可选子代理（如最多 20 个 luna 并发）
                       完整事件日志 ◀─────┘                ▼
                实时时间线 · 回看 · 接管               读写文件 / 执行命令 / 跑测试
```

## 为什么用它

Codex 擅长长时间、机械、靠测试驱动的活；Claude 擅长判断、规划和交叉检查。这个插件让它们配合起来，你不用盯着任何一方：

- **一个命令。** `/codex:run <随便说人话>`。不用记参数，角色、模型、强度、前台/后台、并行/串行都由 Claude 替你选。
- **按难度选模型。** 普通任务 → `gpt-6.1-sol` medium；真正需要推理 → `gpt-6-astra`；重复性批量活 → 一个主控扇出最多 20 个 `gpt-6-luna` 子代理；审查 → `gpt-6-astra` high。
- **对缓存友好的会话。** 一个需求 = 一个命名的 Codex 会话。会话内**绝不切换**模型和强度（切换会让提示词缓存失效）。结果没通过验证时，会**新开**一个会话并升一档。
- **看得见、管得住。** 每次运行都完整记录。可以实时看 Codex 干活、回看过程、取消，或在你自己的终端里接管同一个 Codex 会话。
- **独立审查。** `review`（以及更严厉的 `--adversarial` 模式）使用全新的 astra 会话，审查者不会给自己的作业打分。
- **大任务省 Claude 的 token。** 读大量文件、测试修复循环、批量修改都在 Codex 里完成，Claude 只看到一份简短报告。（很小的任务 Claude 自己做，因为转手的开销比省下的还多。）
- **验证，不盲信。** Codex 说"完成"之后，Claude 会自己复核（看 git diff、重跑测试），再告诉你成没成。

## 环境要求

- [Claude Code](https://claude.com/claude-code)（命令行或桌面端），需支持插件。
- **Codex CLI** 在 `PATH` 里并且已登录（`codex --version` 能运行；安装 `npm i -g @openai/codex@latest`）。建议用较新版本：旧版 CLI 会拒绝新模型。
- `python3` **3.9 及以上**（只用标准库，无需 pip 安装）。
- 作者在 macOS 上开发和测试。Linux 理论上可用但**未测试**；不支持 Windows。
- 实时"观看"面板需要 **Claude 桌面端**（有终端面板）。在纯终端命令行里，Claude 会把进展转述给你。

## 安装

```text
/plugin marketplace add sidchy/claude-codex-bridge
/plugin install codex@codex-bridge
```

重启 Claude Code（或新开会话）。验证：输入 `/` 应该能看到 `/codex:run` 和 `/codex:config`。

之后更新：`claude plugin marketplace update codex-bridge && claude plugin update codex@codex-bridge`，然后重启。
卸载：`claude plugin uninstall codex@codex-bridge`。

不安装，直接从本地克隆试用：

```bash
git clone https://github.com/sidchy/claude-codex-bridge.git
claude --plugin-dir ./claude-codex-bridge/codex
```

> 如果你同时装了 OpenAI 官方的 `codex` 插件，两者都会注册 `/codex:*` 命令，请禁用其中一个。

## 快速上手

```text
/codex:run 修一下 tests/test_login.py 里失败的测试
/codex:run 审查一下我当前的改动
/codex:run 把这个分支和 main 对比审查一下，要狠一点
/codex:run 把全仓库的字段 userId 改名成 user_id
/codex:run 它现在在干嘛？          # 实时观看
/codex:run 停
/codex:run 我来接手                # 会给出一行 `codex resume ...`
/codex:run                         # 什么都不写 = 审查未提交的改动
```

其余的 Claude 自己决定。你会看到它先用一句话说明选择，比如"难度中等偏上，用 astra medium"，结束时给一份简短报告：Codex 实际做了什么（命令、改动的文件、token）、Claude 验证了什么、还有什么没解决，以及怎么回看或接管。

你也可以在话里强制指定：*"用 gpt-6-luna，强度 high"*、*"只读"*、*"强制用 codex"*（见[小任务](#小任务由-claude-自己做)）。

## 命令

| 命令 | 作用 |
|---|---|
| `/codex:run <说人话>` | 唯一入口：交活、审查、看进度、回看、取消、接管。 |
| `/codex:config [show \| set k=v … \| reset \| models]` | 可选。查看或修改默认值（模型、强度、权限、路由）。 |

不会自动触发；只有你输入 `/codex:` 命令时才会用到 Codex。

## Claude 怎么选模型

Claude 在**第一次调用前**先评估任务，用一句话说明选择，然后整个会话内保持不变。

| 任务 | 模型 / 强度 |
|---|---|
| 普通的构建 / 修改 / 重构 / 探索 / 解释（约占 80%） | `gpt-6.1-sol` · medium |
| 重复性批量活（约 10 个以上独立条目：逐文件修改、转换、抽取、机械检查） | `gpt-6.1-sol` medium **主控**，由它扇出最多 **20** 个 `gpt-6-luna` 子代理，high（条目很微妙时用 xhigh） |
| 需要真正的智力：设计、复杂逻辑、需求有歧义、数据完整性 / 迁移 / 安全相关、难查的根因 | `gpt-6-astra` · medium（确实很难就 high） |
| 任何审查 | `gpt-6-astra` · high |
| 很复杂的并发 / 算法 / 重大架构 | 极少用 xhigh，并说明原因 |

**锁定规则。** 同一个 Codex 会话内，模型和强度不会改变；所有续接、恢复路径都继承原来的这一组（脚本会忽略覆盖参数并给出提示）。Codex 主控自己派出的子代理不受此限。

**升级规则。** 如果结果因为*质量 / 推理*原因没通过 Claude 的验证，Claude 会**新开**一个会话升一档（sol medium → astra high → astra xhigh），并写一份新的交接说明，讲清上次哪里失败。权限或网络类失败不升级。最多升两次，之后如实汇报。

模型名称取决于你的 Codex 账号和 CLI 版本。用 `/codex:config models` 查看你能用的，用 `/codex:config` 修改路由默认值（见[配置](#配置)）。

## 会话：一个需求，一个会话

- `run --name <标签>` 在当前目录获取或创建名为 `<标签>` 的 Codex 会话。同一需求的追加要求复用它（Codex 保留上下文，你只需说增量）。不相关的需求用新标签。
- `--continue` 接回当前目录里最新的任务会话。
- 审查和第二意见总是开新会话。
- 同一会话不允许并发运行，两个运行也绝不能写同一批文件。
- 互不相关、文件不重叠的需求可以在不同的后台会话里并排跑，或用 `parallel`（默认并发 4，由 `max_parallel` 控制）。

## 监督：看得见，管得住

每次运行都记录在 `~/.claude/codex-bridge/jobs/<id>/` 下（原始事件日志、状态、结果）。

| 你说 / Claude 执行 | 效果 |
|---|---|
| *"它在干嘛？"* → `watch <id>` | 实时时间线：Codex 的旁白、每条命令及退出码和输出、子代理活动。在桌面端，Claude 会替你在终端面板里打开。 |
| *"给我看看过程"* → `log <id>`（`--tail N`、`--full`、`--since N`） | 回看。`--since` 只返回新事件，让 Claude 的轮询开销很小。 |
| *"停"* → `cancel <id>` | 终止 Codex **及其整个进程树**（包括在独立进程组里启动的工具），并报告已产生的部分改动。 |
| *"我来接手"* → `attach <id>` | 输出一条带正确引号的 `codex resume <会话>` 命令。运行它就能亲自接着同一个 Codex 会话（任务仍在运行时会拒绝，除非加 `--force`）。 |
| `jobs`、`status <id> --json`、`wait <id>`、`result <id>` | 任务控制；默认指向当前目录里最新的任务。 |

所有监督命令都支持 `--name <标签>`，可以用会话标签代替任务编号来选择任务。

耗时较长（约 30 秒以上或开放式）的任务会自动转后台。**大改动或高风险操作**（涉及很多文件、删除 / 重命名、数据迁移、难以回退的事）会先让 architect 会话以只读方式出方案，由 Claude 给你看并等你确认；说一句"直接做"可以跳过。

## 审查

`/codex:run 审查……` 会用 Codex 原生审查器，模型 `gpt-6-astra` high，**只读**：

- 默认目标：未提交的改动；也可指定 `--base <ref>`、`--commit <sha>`
- `--adversarial`（说"要狠一点"）：质疑设计，假设改动会以代价高昂的方式出错，按严重度排序，每条给出 文件:行 和失败场景
- 之后 Claude 会把重要的发现对照代码核实，并告诉你哪些已确认。

## 小任务由 Claude 自己做

如果任务只涉及 ≤ 2 个文件 / 约 15 行、不需要测试修复循环、只是快速查一下，或依赖当前对话上下文，Claude 会直接做并用一句话说明。转手的话，Claude 侧的 token（命令文本、交接说明、验证、汇报）加上 Codex 15 秒以上的启动时间，比省下的还多。想覆盖这个行为，说 **"强制用 codex"**。

## 配置

`/codex:config`（或直接编辑 `~/.claude/codex-bridge/settings.json`）。`show` 会打印全部。默认值：

| 键 | 默认值 | 含义 |
|---|---|---|
| `model` | `gpt-6.1-sol` | 普通任务的主力模型 |
| `effort` | `medium` | `low` · `medium` · `high` · `xhigh` · `max` · `ultra`（取决于所选模型支持什么） |
| `sandbox` | `danger-full-access` | `read-only` · `workspace-write` · `danger-full-access`；对 worker / debugger 生效 |
| `model_reasoning` | `gpt-6-astra` | 设计 / 难推理（architect 角色） |
| `model_bulk` | `gpt-6-luna` | 重复性活扇出的子代理 |
| `review_model` / `review_effort` | `gpt-6-astra` / `high` | 审查 |
| `team_policy` | `true` | 告诉 Codex 主控何时、如何派子代理，以及每个用什么模型 |
| `max_parallel` | `4` | `parallel` 的并发数 |
| `timeout` | `1800` | 每次运行的秒数（正整数） |
| `profile` | `""` | Codex 配置 profile（`-p`） |
| `codex_bin` | `auto` | `auto` = 取 ChatGPT 桌面端自带 CLI 和 `PATH` 中 CLI 里较新的；或 `path`，或指定二进制路径 |
| `extra_args` | `[]` | 传给 `codex exec` 的额外参数（JSON 数组或带引号的字符串） |
| `roles` | `{}` | 新增 / 覆盖角色预设（`sandbox`、`effort`、`model`、`preamble`） |

示例：

```text
/codex:config set model=gpt-6-astra effort=high
/codex:config set sandbox=workspace-write
/codex:config set 'roles={"worker":{"preamble":"改动保持最小范围"}}'
/codex:config models
/codex:config reset
```

非法值会被拒绝，且不会改动文件。设置文件损坏时一切操作都会停下并给出明确报错；`config reset` 会把坏文件备份为 `settings.json.corrupt-<时间戳>` 再恢复默认值。任务启动前会按你的 Codex CLI 目录校验模型 / 强度组合（只有配置了自定义 profile 或 provider 时，才允许目录之外的模型名）。`CODEX_BRIDGE_HOME` 可以改变桥接层的设置 / 任务 / 缓存目录位置。

## 安全：请务必阅读

默认情况下 Codex 以 **`danger-full-access` 和审批策略 `never`** 运行：它能读写你的用户可访问的任何位置，并且**不经询问**就执行命令。explorer、reviewer、architect 角色始终只读，但 worker 和 debugger 遵循你的 `sandbox` 设置。如果你不想这样：

```text
/codex:config set sandbox=workspace-write     # 或 read-only
```

只把它用在你放心让智能体无人值守执行的工作上。桥接脚本本身不发起任何网络请求，只和你本机的 `codex` 二进制交互。

## 桥接脚本（进阶）

插件只是包在一个可执行文件外面的薄层：[`codex/scripts/codex_bridge.py`](codex/scripts/codex_bridge.py)，你也可以自己调用它（它会刷新 `~/.claude/codex-bridge/bin/codex_bridge.py` 这个稳定符号链接）：

```text
run [--name L] [--role R] [--model M] [--effort E] [--sandbox S] [--cd DIR] [--background] [--continue] [--timeout N] -   # 提示词走 stdin
resume --session ID "<消息>"             review [--base REF | --commit SHA] [--adversarial] [--background] [关注点…]
jobs | status [id] [--json] | watch [id] | log [id] [--since N] [--tail N] [--full] | attach [id] | wait [id] | result [id] | cancel [id]
parallel tasks.json                      roles | models | config [show|set k=v|reset]
```

角色：`explorer`（只读侦察）、`worker`（实现）、`debugger`（复现 → 找根因 → 最小修复）、`reviewer`（只读审查）、`architect`（只读设计）。worker / debugger / architect 的提示词会被注入一段**团队策略**，说明何时派子代理、给它们用什么模型。

## 常见问题

| 现象 | 处理 |
|---|---|
| `The 'gpt-…' model is not supported when using Codex with a ChatGPT account` | 你的 Codex CLI 太旧，不支持该模型：`npm i -g @openai/codex@latest`。`codex_bridge` 在 `codex_bin=auto` 时，若 ChatGPT 桌面端自带的 CLI 更新，也会自动选用它。 |
| `unknown model '…'; available: …` | 该模型不在你的 CLI 目录里。`/codex:config models` 查看，再 `/codex:config set model=…`（或 `review_model=` 等）。 |
| `cannot read settings …` | `settings.json` 损坏。`/codex:config reset`（坏文件会保留为 `.corrupt-<时间戳>`）。 |
| zsh 报 `exit 127` / 脚本"no such file or directory" | 给路径加引号并直接调用脚本。不要写成 `python3 "…"` 放进同一个引号里，也不要把它存进不带引号的变量。 |
| 实时观看的终端拒绝该命令 | 终端工具只接受 ASCII；请使用稳定路径 `~/.claude/codex-bridge/bin/codex_bridge.py`。 |
| 输入 `/` 看不到命令 | 重启 Claude Code；用 `claude plugin list` 确认 `codex@codex-bridge` 已启用。 |

## 局限（如实说明）

- 难度评估、模型选择、"小任务"规则和升级策略写在提示词里（`codex/commands/run.md`），由 Claude 遵守而不是由代码强制，所以判断可能出偏差。**由代码强制**的部分有：会话内的模型 / 强度锁定、续接时的权限继承、取消 / 超时时的进程树清理、设置校验。
- Codex 子代理实际用了哪个模型，以 Codex 自己的汇报为准，桥接层无法独立确认。
- 在 macOS、Python 3.9 和 3.14、Codex CLI 0.160 上测试过。模型名（`gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-luna`）在作者的账号上可用，你的账号上可能不同。
- 没有内置的费用 / 预算上限；子代理大规模扇出可能消耗大量 Codex 额度。

## 开发

```bash
python3 -m unittest discover -s codex/tests -v    # 36 个离线测试，不调用模型
```

目录结构：`.claude-plugin/marketplace.json`（marketplace）· `codex/.claude-plugin/plugin.json`（插件）· `codex/commands/{run,config}.md`（Claude 遵循的提示词）· `codex/agents/codex-runner.md` · `codex/scripts/codex_bridge.py`（桥接脚本）· `codex/tests/`。

欢迎提 Issue 和 Pull Request。

## 许可证

[MIT](LICENSE)

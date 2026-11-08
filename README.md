# session-sync

跨 agent harness 的会话数据互导入工具：把一个 harness 的本地会话迁移到另一个 harness，迁移后可在目标 harness 里直接 resume 继续对话。附带可视化 dashboard 与 MCP server。

支持 7 家 harness（reader + writer）：

| Harness | 存储 | 读 | 写 | 说明 |
|---|---|---|---|---|
| Claude Code | `~/.claude/projects/<编码cwd>/*.jsonl` | ✅ | ✅ | uuid/parentUuid 链 |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl(.zst)` | ✅ | ✅ | 写 plain jsonl + 更新 session_index |
| ZCode | `~/.zcode/cli/db/db.sqlite` | ✅ | ✅ | session/message/part 三层 JSON；写前自动备份 |
| Qoder / Qoder CN | `~/.qoder(-cn)/projects/…` | ✅ | ✅ | Claude 派生格式，含 workspace-directories 元行 |
| Pi | `~/.pi/agent/sessions/…` | ✅ | ✅ | v3 树结构（id/parentId） |
| DeepSeek dsh | `~/.dsh/sessions/…/session.v4.jsonl.zstd` | ✅ | ⚠️ 实验性 | zstd 帧压缩；写需 `--allow-dsh-write` |
| WorkBuddy | `~/.workbuddy/projects/….jsonl` + `workbuddy.db` | ✅ | ✅ | jsonl + sessions 表双写 |

纯 Python 标准库（≥3.10），零第三方依赖。dsh 解压优先用 Python 3.14+ 内置 `compression.zstd`，否则回退 `zstd` CLI。

## 安装

```bash
# 方式一：免安装直接跑
./bin/session-sync --help

# 方式二：pip 安装（建议 venv）
python3 -m venv .venv && .venv/bin/pip install -e .
session-sync --help
```

## 快速开始（向导）

```bash
session-sync          # 无参数 = 交互向导
```

向导流程：

```
▸ 当前项目: /Users/you/Projects/foo        （自动取 cwd，可改）
▸ 本项目会话分布: claude-code 3 · codex 2 · zcode 5
  ① 选择源 harness
  ② 选择会话（标题 / 时间 / 消息数）
  ③ 迁移到哪个 harness
  ④ dry-run 计划摘要 → 确认
  ⑤ 执行 → manifest 汇报（新 ID / 备份路径 / 降级警告）
```

## 子命令

```bash
session-sync doctor                        # 探测 7 家安装状态与数据位置
session-sync list [--harness H] [--cwd D] [--json]   # 表格含 id 列（前缀缩写）
session-sync show <harness> <sid>          # 渲染为 Markdown
session-sync report --cwd D --out r.html   # 自包含 HTML dashboard（可视化）
session-sync copy SRC SID --to DST [--execute] [--cwd OLD=NEW] [--on-conflict skip|new-id|overwrite]
session-sync mcp                           # MCP server（stdio）
```

迁移/show 需要的**会话 ID** 从 `list` 拿：表格里直接显示（默认取前 10 位前缀），`--json` 输出完整 ID。`copy` / `show` 支持唯一前缀补全——前缀不唯一时明确报错，不会猜错目标：

```bash
session-sync cp claude-code a1b2c3d4 --to codex -x   # 前缀自动补全为完整 ID
```

支持子命令别名与常用选项缩写：

- 子命令别名：`list`→`ls`、`show`→`cat`、`copy`→`cp`、`doctor`→`doc`、`report`→`rpt`
- 选项缩写：`--harness`→`--h`/`-H`、`--cwd`→`-c`、`--limit`→`-l`、`--json`→`-j`、`--out`→`-o`、`--to`→`-t`、`--execute`→`-x`、`--max`→`-m`

```bash
# 等价于：session-sync list --harness zcode --limit 3 --json
session-sync ls --h zcode -l 3 -j
```

无歧义的长选项前缀（如 `--har`）也始终可用。

`copy` 默认 dry-run，加 `--execute` 真正写入；落点默认保持原会话 cwd，`--cwd-map OLD=NEW` 按前缀重映射项目路径。

### 迁移示例

```bash
# 看计划（不写入）
session-sync copy claude-code <session-id> --to codex

# 确认无误后执行
session-sync copy claude-code <session-id> --to codex --execute

# 迁移到别的项目目录
session-sync copy zcode <session-id> --to pi --execute --cwd-map /old/path=/new/path
```

## MCP 接入

在支持 MCP 的 harness（Claude Code / Codex / ZCode / Qoder / dsh 等）注册：

```json
{
  "mcpServers": {
    "session-sync": {
      "command": "/path/to/session-sync/bin/session-sync",
      "args": ["mcp"]
    }
  }
}
```

暴露 4 个工具：`doctor` / `list_sessions` / `show_session` / `migrate_session`。

## 可视化

```bash
session-sync report --cwd /path/to/project --out report.html
open report.html
```

单文件 HTML：左侧按 harness 分组的会话列表（可搜索），右侧聊天正文（thinking 折叠、工具调用展示）。ZCode 里可直接由 agent 经 Gen UI 渲染。

## 迁移保真度

| 内容 | 行为 |
|---|---|
| user/assistant 文本 | 全部保留 |
| 工具调用/结果 | Claude 式 ↔ Responses 式 ↔ ZCode part 式 三套形状互转；双端支持才原样保留 |
| thinking | 目标端有对应结构才迁移（如 ZCode↔Qoder↔Pi）；provider 私有签名 thinking 不迁移 |
| 压缩摘要/标题 | 有对应结构的 harness 间迁移（如 Claude `summary` 行） |
| 未知行类型 | 不崩溃，原样保留在 IR `ext` 中；同格式回写时还原 |
| 会话身份 | 目标端获得新原生 ID（非字节级克隆），可 resume |

每次迁移产出 manifest（源/目标 ID 映射、统计、降级警告）。

## 安全规则

- 源数据永远只读，从不原地改写来源 harness
- 写入前自动备份目标（文件 copy / SQLite 全量备份含 WAL）
- 写 SQLite（ZCode / WorkBuddy）前检测对应应用进程是否运行并告警
- ZCode 库不存在时拒绝创建（防止路径手误建出假库）
- `copy` 默认 dry-run，向导/MCP 二次确认后才执行

## 开发

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

合成 fixture 覆盖全部 7 家 adapter 的 roundtrip（→IR→写回→字段比对）；真实数据 E2E 已验证 claude↔codex、zcode→pi/workbuddy、qoder→claude、codex .zst 读取，以及真 codex CLI 对迁移会话的 resume 加载。

## 已知限制

- dsh 写入为实验性（dsh 对事件 fail-closed 校验，格式漂移时会拒载而非损坏）
- 实时双向同步、auth/MCP/hooks 配置迁移、字节级克隆：明确不做
- Codex `.zst` 写出暂不支持（写 plain jsonl，官方兼容读取）
- 社区工具 [session-migrate](https://github.com/xhluca/session-migrate) 覆盖 Claude/Codex/Pi 等 18 家，可作为补充；本工具额外覆盖 ZCode / Qoder / WorkBuddy / dsh

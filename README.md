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

## 从源码开始（拿到源码后看这里）

所有处理都在本地完成，不联网、不上传任何会话数据。

### 1. 环境要求

| 依赖 | 要求 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 运行 CLI / GUI / MCP，不需要第三方包 |
| Chrome / Chromium | 可选 | GUI 用 `--app` 打开独立窗口；没有时回退默认浏览器 |
| `zstd` CLI | 可选 | 仅 Python < 3.14 且要读写 dsh 会话时需要（3.14+ 用内置 `compression.zstd`） |

macOS 自带的 `python3` 可能是 3.9，不满足要求；建议 `brew install python@3.13` 或安装 python.org 版本。先确认：

```bash
python3 --version
```

### 2. 免安装运行（推荐先这样试）

```bash
./bin/session-sync --help      # 启动脚本会自动挑选 >= 3.10 的 Python
./bin/session-sync doctor      # 探测本机安装了哪些 harness
./bin/session-sync             # 交互向导
./bin/session-sync ui          # 本地 GUI
```

`bin/session-sync` 只是启动脚本，不写系统目录；迁移默认 dry-run，只有确认后才会写目标 harness。

### 3. 安装 CLI（可选）

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
session-sync --help
```

安装后可直接用 `session-sync` 代替 `./bin/session-sync`。

### 4. 本地打包 macOS .app

```bash
./scripts/build-app.sh
open "dist/Session Sync.app"   # 或直接在 Finder 里双击
```

- 首次构建会在 `.venv-packaging/` 自动安装 PyInstaller（仅构建期依赖，运行时仍是零第三方包）。
- 产物 `dist/Session Sync.app` 约 23MB，内嵌 Python 运行时，目标机器无需安装 Python。
- 按构建机架构打包：Apple Silicon 上产出 arm64。
- 重新打包直接重跑脚本即可；`dist/`、`build/` 均已被 `.gitignore` 忽略。

### 5. 分发给其他人

| 场景 | 做法 |
|---|---|
| 同架构 Mac，私下传用 | 直接拷贝 `dist/Session Sync.app`；未公证时对方首次打开需右键“打开”，或执行 `xattr -dr com.apple.quarantine "Session Sync.app"` |
| 公开分发（不出现安全警告） | 需要 Apple Developer ID 签名 + 公证，PyInstaller 产物同样适用 |
| Windows / Linux | 代码本身跨平台，可直接从源码运行；当前只提供 macOS 打包脚本，其他平台需在对应系统上自行构建 |

推送 `v*` tag 会触发 GitHub Actions（`.github/workflows/release.yml`）：跑单测 → 构建 `.app` → 生成 zip/SHA256 → 自动创建 Release。也可在 Actions 页手动触发，仅上传构建产物（不发布 Release）。

> 签名/公证只影响安装体验，不改变安全模型：GUI 只监听 `127.0.0.1`，写入始终先 dry-run 再确认。

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
session-sync list [--harness H] [--cwd D] [--json] [--copy]   # 表格含完整会话 id；--copy 把 id 写入剪贴板
session-sync show <harness> <sid>          # 渲染为 Markdown
session-sync report --cwd D --out r.html   # 自包含 HTML dashboard（可视化）
session-sync copy SRC SID --to DST [--execute] [--cwd OLD=NEW] [--on-conflict skip|new-id|overwrite]
session-sync ui [--port 8765] [--no-open] [--cwd D] [--harness H]   # 本地 GUI（Chrome 应用窗口）
session-sync mcp                           # MCP server（stdio）
```

迁移/show 需要的**会话 ID** 从 `list` 拿：表格直接显示完整 ID。`copy` / `show` 另支持唯一前缀补全——前缀不唯一时明确报错，不会猜错目标：

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

## GUI（本地客户端）

macOS 可直接双击仓库根目录的 `start-gui.command` 一键启动，无需安装；也可在终端运行：

```bash
./start-gui.command            # 一键启动（macOS 可双击）
./bin/session-sync ui          # 未安装 CLI 时直接从仓库运行
session-sync ui                # 已安装 CLI 后
session-sync ui --no-open      # 只起服务，打印带 token 的 URL
session-sync ui -p 9000 -c /path/to/project -H claude-code
```

Chrome 以 `--app` 模式打开独立窗口（无地址栏）；未安装 Chrome 时回退默认浏览器。页面内完成：项目扫描 → 会话列表/全文预览 → 点“导出…”选目标 harness/cwd → dry-run 计划 → 确认写入 → 结果摘要。项目目录可直接输入，也可点“浏览”调起系统文件夹选择器（macOS 用 `osascript`，Linux 用 `zenity`/`kdialog`），选完自动切换并重新扫描。仍是纯标准库实现，新增依赖为零。

### 打包 macOS 应用（.app）

构建、产物与分发步骤见上文「从源码开始 → 本地打包 / 分发给其他人」。运行时补充：双击启动后服务在后台运行，关闭窗口不会退出；无终端启动时日志写入 `~/Library/Logs/session-sync.log`；窗口仍按「Chrome `--app` → 默认浏览器」降级。

安全边界：

- HTTP 服务只绑 `127.0.0.1`，校验 `Host`，拒绝跨站 `Origin` / `Sec-Fetch-Site`
- 每次启动生成随机 token，以 `HttpOnly; SameSite=Strict` cookie 传递
- 写入必须先在页面生成 dry-run 计划，再显式确认；dsh 写入需启动时加 `--allow-dsh-write`
- 关闭窗口不会自动退出服务；可点左侧栏底部“退出”，或终端 `Ctrl+C` 结束

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
- GUI 只监听 loopback，token 鉴权 + 同源校验；写入走 dry-run 确认流，dsh 写入默认关闭

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

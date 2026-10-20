# DeskWeave

[English](README.md) · **简体中文**

[![CI](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml/badge.svg)](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml)

**本地优先的 Agent 桌面协作基础设施。**

DeskWeave 把 AI Agent、桌面应用、工作流和你自己连在一起——全部跑在你这台机器上，没有托管的控制平面。Agent 说 MCP；核心运行时掌握状态、权限与审计；Tauri 外壳给你一扇看见两者的真窗口。

> 状态：**Phase 2 —— 用「接入第二个来源」来验证可扩展性。** Phase 1.5 的闭环依然成立：Agent 收集参考图，图片落进本地 artifact 仓库，**由你**挑选要用的那几张，然后 Blender 才会按这个选择建立参考平面。Phase 2 要回答的问题是「加新能力是否必须改运行时核心」，答案是能，而且做法是接入了第二个图源（`wikimedia`，公开 Commons API，无需密钥）：工作流、MCP 网关、控制 API、UI **一行都没改**，只在组装根加了一条 `来源 → 连接器` 映射。
>
> 流水线是绿的：Python、前端、Rust 三个 job 在每次 push 与 PR 上跑，平台为 Ubuntu。真实环境测试——真浏览器、真 Blender、真 Commons API——仍是显式开启的，因为它们需要网络和本机 Blender；CI 不假装覆盖它们。

## 能力矩阵

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| Python 核心运行时（生命周期、配置、健康检查） | **已实现** | `tests/unit/test_runtime.py`、`tests/e2e/test_service_cli.py` |
| Adapter SDK（能力模型、JSON Schema 校验、超时、结构化错误） | **已实现** | `tests/contract/test_adapter_contract.py` |
| 连接器注册表 + 权限层（授予、一次性许可、默认拒绝） | **已实现** | `tests/unit/test_permissions.py`、`test_registry.py` |
| SQLite 持久化 + 版本化迁移 | **已实现** | `tests/unit/test_persistence.py` |
| 进程内事件总线 | **已实现** | `tests/unit/test_events.py` |
| stdio MCP Server，7 个工具 | **已实现** | `tests/integration/test_mcp_stdio.py`、`test_mcp_reference_tools.py` |
| 桌面外壳（Tauri 2 + Vue 3）与确认界面 | **已实现** | `apps/desktop/src/views/ReferencesView.test.ts`、`scripts/ui_smoke.py` |
| `filesystem` 连接器（只读，受允许目录约束） | **已实现** | `tests/integration/test_filesystem_adapter.py` |
| `browser` 连接器——Playwright 真实图片搜索 | **已实现** | `tests/integration/test_browser_real.py`（需显式开启） |
| `wikimedia` 连接器——公开 Commons API，无需密钥，每张图带许可证与作者 | **已实现** | `tests/integration/test_wikimedia_real.py`（需显式开启）、`tests/unit/test_wikimedia_api.py` |
| 参考图来源：`来源 → 连接器` 注册表，由组装根接线 | **已实现**——新增来源无需改动工作流、网关、API 或 UI | `tests/integration/test_reference_api.py`、`docs/decisions/0013-collection-source-registry.md` |
| 连接器开发指南（快速开始、测试模板、依赖边界） | **已实现** | `docs/development/writing-an-adapter.md` |
| Artifact 仓库（下载防护 + 来源信息） | **已实现** | `tests/unit/test_artifact_downloader.py` |
| `blender` 连接器——无头建面 + 独立复核 | **已实现** | `tests/integration/test_blender_real.py`（需显式开启） |
| 运行时的子进程监管（Windows Job Object，先礼后兵） | **已实现（仅 Windows）**——其他平台 `supervised: false` | `cargo test --lib job::tests`、`docs/decisions/0009-runtime-child-supervision.md` |
| 令牌不落盘（每次启动新生成，经环境变量交给子进程） | **已实现**——旧明文令牌在启动时移除 | `tests/integration/test_state_file.py`、`docs/decisions/0010-runtime-token-storage.md` |
| 中断任务的重启恢复 | **已实现**——按证据对账，绝不自动执行 | `tests/unit/test_recovery.py`、`tests/integration/test_recovery_service.py` |
| 产物 `.blend` 的元信息与「打开所在文件夹 / 在 Blender 中打开」 | **已实现**——只用 artifact id，不传路径 | `tests/unit/test_artifact_opener.py`、`ReferencesView.test.ts` |
| 产物的内联缩略图 | **尚未实现**——UI 明确说明，而不是放占位图 | `docs/decisions/0012-blender-output-observability.md` |
| macOS / Linux 的子进程监管 | **尚未实现**——没有 Job Object，只有优雅停止 + 杀直接子进程 | `docs/decisions/0009-runtime-child-supervision.md` |
| `anime-modeling` 插件的工作流逻辑 | **实验性**——目前只注册连接器 | `plugins/anime-modeling/` |
| 截图 / 页面捕获 | **尚未实现**——没有对应能力 | — |
| 非图片 artifact、参考平面以外的场景编排 | **尚未实现** | — |
| 安装包 / 代码签名 | **未构建、未验证** | — |
| 远程或多用户 | **按设计不做** | `docs/decisions/0004-local-process-communication.md` |

## 快速开始

```bash
# Python 核心 + 浏览器二进制
uv sync --all-packages
uv run playwright install chromium
uv run pytest
uv run python -m deskweave_runtime.service --announce --port 8787

# 桌面 UI（另开一个终端）
corepack enable
corepack pnpm install
corepack pnpm build && corepack pnpm test
cd apps/desktop && corepack pnpm tauri dev
```

Blender 会自动发现（可用 `DESKWEAVE_BLENDER` 覆盖）。完整步骤（含 Codex 的 MCP 配置）：**[docs/development/getting-started.md](docs/development/getting-started.md)**。

## 一张图看懂架构

```
Codex / Agent ──MCP/stdio──► mcp-gateway ──► Runtime ──► 连接器（browser、wikimedia、blender、fs）
                                               │
                     ┌─────────────────────────┼─────────────────────────┐
                     ▼                         ▼                         ▼
                 权限层                    工作流引擎                  SQLite
                     │                         │
                     └────────── 进程内事件总线 ──────────┘
                                               ▲
                      Tauri 外壳 ──HTTP──►     │   （仅回环 + 令牌，经 Rust 转发）
                      Vue 3 UI ───────────────┘
```

依赖方向由包布局强制：`contracts` ← `adapter-sdk` ← `runtime` ← `mcp-gateway`。连接器不导入工作流逻辑，UI 不导入 Python。

延伸阅读：

- [系统架构](docs/architecture/system-architecture.md)
- [参考图工作流](docs/architecture/reference-workflow.md)——这条闭环逐步拆解
- [Adapter SDK](docs/architecture/adapter-sdk.md)
- [工作流状态机](docs/architecture/workflow-state-machine.md)
- 连接器：[browser](docs/development/browser-connector.md) · [Wikimedia Commons](docs/development/wikimedia-connector.md) · [Blender](docs/development/blender-connector.md)
- 自己写一个：[连接器开发指南](docs/development/writing-an-adapter.md)
- 决策记录：[Tauri](docs/decisions/0001-desktop-framework-tauri.md) ·
  [Python 核心](docs/decisions/0002-python-core-runtime.md) ·
  [MCP 边界](docs/decisions/0003-mcp-gateway-boundary.md) ·
  [本地进程通信](docs/decisions/0004-local-process-communication.md) ·
  [SQLite](docs/decisions/0005-sqlite-persistence.md) ·
  [浏览器图源](docs/decisions/0006-browser-reference-source.md) ·
  [下载边界](docs/decisions/0007-artifact-download-boundary.md) ·
  [Blender 无头执行](docs/decisions/0008-blender-headless-execution.md) ·
  [子进程监管](docs/decisions/0009-runtime-child-supervision.md) ·
  [令牌存储](docs/decisions/0010-runtime-token-storage.md) ·
  [重启恢复](docs/decisions/0011-restart-recovery.md) ·
  [Blender 产物](docs/decisions/0012-blender-output-observability.md) ·
  [来源注册表](docs/decisions/0013-collection-source-registry.md)

> 架构文档与决策记录目前为英文撰写，与代码注释保持一致；README 保持中英双份。

## 安全姿态

DeskWeave 要驱动浏览器和 Blender，所以默认值必须保守：

- 控制 API **只绑定回环地址**（由配置校验强制）；
- 每个请求都要带**每次启动随机生成的令牌**，令牌不下发到 webview，也**不落盘**（历史明文令牌会在首次启动时被移除）；
- 运行时子进程跑在**Windows Job Object** 里（`KILL_ON_JOB_CLOSE`），外壳崩溃不会把它或正在跑的 Blender 留在后台；
- webview 只能调用两个 Rust 命令（`runtime_request`、`artifact_preview`），且限定在结构化路由白名单内；
- 工具参数未声明直接**拒绝**，随后再做 schema 校验——`confirmed=true` 也在被拒之列，它不构成任何授权；
- **没有任何工具能替用户确认**：确认是 UI 动作，服务端会校验任务状态与 artifact 归属；
- 下载有 SSRF 防护（只允许公网 IP、逐跳复检）、8 MiB 上限、按 magic bytes 识别类型、原子落盘；
- 文件访问限制在**允许目录**内；路径永远不被当作授权凭证，连接器只接收 artifact id；
- Blender 执行的是**固定脚本 + JSON 配置**——参数里塞不进 Python——结果再由第二个进程复核；
- 打开产物是**用户动作**（对 artifact id 发起 `reveal` / `open`）：路径由运行时解析并复检，webview 永远看不到；
- **没有通用代码执行能力**——权限层无条件拒绝 `code:execution`；
- 执行记录与权限授予都会落库，供审计。

已知缺口：连接器外面没有 OS 级沙箱；同一用户的其他进程可以读到子进程的环境变量（这就是信任边界）；macOS / Linux 上外壳被强杀时运行时仍可能残留（那边没有 Job Object，详见 `docs/decisions/0009-runtime-child-supervision.md`）。

## 仓库结构

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript 桌面外壳
runtime/                Python 核心运行时与控制 API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk}
adapters/{browser,wikimedia,blender,filesystem}
plugins/anime-modeling
scripts/{ci_local.py,ui_smoke.py}
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## 常用命令

| 任务 | 命令 |
| --- | --- |
| 严格按锁文件安装 | `uv sync --all-packages --locked` |
| Python 静态检查 | `uv run ruff check .` |
| Python 格式检查 | `uv run ruff format --check .` |
| Python 测试 | `uv run pytest` |
| 真实浏览器测试 | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py` |
| 真实 Commons 测试 | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_wikimedia_real.py` |
| 真实 Blender 测试 | `DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py` |
| 全链路 | `DESKWEAVE_NETWORK_TESTS=1 DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/e2e` |
| 前端构建 | `corepack pnpm build` |
| 前端测试 | `corepack pnpm test` |
| 前端 lint / 格式 | `corepack pnpm lint` / `corepack pnpm format:check` |
| Rust 检查 | `cargo check --all-targets --locked`（在 `apps/desktop/src-tauri`） |
| Rust 格式检查 | `cargo fmt --check` |
| Rust 桥测试 | `cargo test --lib --locked` |
| 启动 MCP Server | `uv run python -m deskweave_mcp_gateway` |
| 真实 UI 冒烟 | `uv run python scripts/ui_smoke.py` |
| 本地跑 CI（与 GitHub Actions 同步骤） | `uv run python scripts/ci_local.py` |

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

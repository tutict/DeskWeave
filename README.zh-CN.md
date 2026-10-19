# DeskWeave

[English](README.md) · **简体中文**

**本地优先的 Agent 桌面协作基础设施。**

DeskWeave 把 AI Agent、桌面应用、工作流和你自己连在一起——全部跑在你这台机器上，没有托管的控制平面。Agent 说 MCP；核心运行时掌握状态、权限与审计；Tauri 外壳给你一扇看见两者的真窗口。

> 状态：**Phase 0 —— 工程底座**。骨架能跑、有测试、默认安全。真实的浏览器自动化与 Blender 集成属于 Phase 1。

## 当前已有

| 能力 | 状态 |
| --- | --- |
| Python 核心运行时（生命周期、配置、健康检查） | 已实现 |
| Adapter SDK（能力模型、JSON Schema 校验、超时、结构化错误） | 已实现 |
| 连接器注册表 + 权限层（非只读操作默认拒绝） | 已实现 |
| SQLite 持久化 + 版本化迁移（6 个实体） | 已实现 |
| 进程内事件总线 | 已实现 |
| stdio MCP Server，4 个只读工具 | 已实现，有协议测试 |
| 桌面外壳（Tauri 2 + Vue 3），展示真实运行时状态 | 已实现 |
| `filesystem` 连接器（只读，受允许目录约束） | 已实现 |
| `browser` / `blender` 连接器 | 仅接口与生命周期，执行抛 `NotImplementedYetError` |
| 插件 `anime-modeling` | 只负责注册连接器，工作流逻辑在 Phase 1 |
| 安装包 / 代码签名 | 未构建，未验证 |

## 快速开始

```bash
# Python 核心
uv sync --all-packages
uv run pytest
uv run python -m deskweave_runtime.service --announce --port 8787

# 桌面 UI（另开一个终端）
corepack enable
corepack pnpm install
corepack pnpm build && corepack pnpm test
cd apps/desktop && corepack pnpm tauri dev
```

完整步骤（含 Codex 的 MCP 配置）：**[docs/development/getting-started.md](docs/development/getting-started.md)**。

## 一张图看懂架构

```
Codex / Agent ──MCP/stdio──► mcp-gateway ──► Runtime ──► 连接器（browser、blender、fs）
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
- [Adapter SDK](docs/architecture/adapter-sdk.md)
- [工作流状态机](docs/architecture/workflow-state-machine.md)
- 决策记录：[Tauri](docs/decisions/0001-desktop-framework-tauri.md) ·
  [Python 核心](docs/decisions/0002-python-core-runtime.md) ·
  [MCP 边界](docs/decisions/0003-mcp-gateway-boundary.md) ·
  [本地进程通信](docs/decisions/0004-local-process-communication.md) ·
  [SQLite](docs/decisions/0005-sqlite-persistence.md)

> 架构文档与决策记录目前为英文撰写，与代码注释保持一致；如需中文版本可以另开一轮翻译。

## 安全姿态

DeskWeave 最终要驱动浏览器和 Blender，所以默认值必须保守：

- 控制 API **只绑定回环地址**（由配置校验强制）；
- 每个请求都要带**每次启动随机生成的令牌**，且令牌不下发到 webview；
- webview 只能调用一个 Rust 命令，且限定 4 条只读路径；
- 工具参数未声明直接**拒绝**，随后再做 schema 校验；
- 文件访问限制在**允许目录**内，路径永远不被当作授权凭证；
- **没有通用代码执行能力**——权限层无条件拒绝 `code:execution`；
- 执行记录与权限授予都会落库，供审计。

已知缺口（详见 `docs/decisions/0004-local-process-communication.md`）：令牌以明文存放在运行时数据目录；没有 OS 级沙箱；外壳被强杀时 Python 子进程可能残留。

## 仓库结构

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript 桌面外壳
runtime/                Python 核心运行时与控制 API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk}
adapters/{browser,blender,filesystem}
plugins/anime-modeling
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## 常用命令

| 任务 | 命令 |
| --- | --- |
| Python 静态检查 | `uv run ruff check .` |
| Python 格式检查 | `uv run ruff format --check .` |
| Python 测试 | `uv run pytest` |
| 前端构建 | `corepack pnpm build` |
| 前端测试 | `corepack pnpm test` |
| 前端 lint / 格式 | `corepack pnpm lint` / `corepack pnpm format:check` |
| 启动 MCP Server | `uv run python -m deskweave_mcp_gateway` |

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

# DeskWeave

[English](README.md) · **简体中文**

[![CI](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml/badge.svg)](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml)

**本地优先的 Agent 桌面协作基础设施。**

DeskWeave 把 AI Agent、桌面应用、工作流和你自己连在一起——全部跑在你这台机器上，没有托管的控制平面。Agent 说 MCP；核心运行时掌握状态、权限与审计；Tauri 外壳给你一扇看见两者的真窗口。

> 状态：**Phase 2 —— 用「接入第二个来源」来验证可扩展性。** Phase 1.5 的闭环依然成立：Agent 收集参考图，图片落进本地 artifact 仓库，**由你**挑选要用的那几张，然后 Blender 才会按这个选择建立参考平面。Phase 2 要回答的问题是「加新能力是否必须改运行时核心」，答案是能，而且做法是接入了第二个图源（`wikimedia`，公开 Commons API，无需密钥）：工作流、MCP 网关、控制 API、UI **一行都没改**，只在组装根加了一条 `来源 → 连接器` 映射。
>
> Phase 2.2 问的是同一问题更难的那一版：能力能不能来自**这个项目没写、也不是这个语言**的代码，同时不给它文件系统、网络和 shell？现在有一个 WebAssembly 沙箱能跑这样的插件——燃料、墙钟、内存三重上限，对外只有一条经**既有权限层**授权的主机调用通道。它是**已测试的库，尚未接入 `Runtime`**，设计与边界见 [`docs/decisions/0014-wasm-plugin-isolation.md`](docs/decisions/0014-wasm-plugin-isolation.md) 与[威胁模型](docs/architecture/wasm-threat-model.md)。
>
> Phase 2.3 不是描述这条边界，而是去打它。当时 129 个测试，其中 66 个只为了打穿它而存在（现为 398 与 130）；找出并修掉 5 个真实逃逸——插件可以在**一次授权都没拿到**的情况下让 `invoke()` 抛 `IndexError`、`RecursionError` 或 `TimeoutError`；补上每次调用的主机调用预算，因为测试证明旧设计根本没有；变异检查逐条拆掉防护并要求测试变红——今天 61 项，61/61 全部抓到，0 跳过。威胁模型里每条断言都标了**已验证 / 依赖宿主实现 / 无法保证 / 本平台未验证**，所以那份文档读起来不会比证据更强。
>
> Phase 2.4 修掉了 2.3 记录但没修的东西：宿主能力原本跑在事件循环上，250 ms 的能力就冻住所有其他协程，而 `cancel()` 只是让调用方不再等，并没有让工作停下。现在能力跑在有界线程池里，要过四道并发限额和一道成本预算，并如实报告每次调用的状态——`timed_out` 和 `cancelled` 不再是同一个词。同步能力**依然无法被中断**，这一点写在审计里（`confirmed_stopped: false`），不是写在注释里。
>
> Phase 2.5 修的是「插件本身守规矩」时依然成立的两个缺口：它可以被人从磁盘上悄悄替换，而一个守规矩的插件可以被另一个忙碌的插件饿死。现在模块可以按**内容**钉住——manifest 里的 `wasm_sha256`，或一份带版本、路径、大小与声明能力的 `deskweave.install.json`——用 `require_integrity` 构造的宿主会拒绝漂移过的模块。这只能证明**文件变过**，不能证明**是谁发布的**；我们没有为了说「可信插件」而临时造一套签名体系。准入也不再是先到先得：宿主满负荷时会排队（16 个等待位、最多 0.25 s），按插件轮转服务，并可选按能力限流。这个保证是**有界等待，不是严格公平**——不加权、不承诺延迟、且只在单个宿主实例内有效。见 [`docs/architecture/plugin-installation.md`](docs/architecture/plugin-installation.md) 与 [ADR 0016](docs/decisions/0016-plugin-provenance-and-fairness.md)。
>
> Phase 2.6 问的是：上面这些到底**是谁决定的**。审代码时发现插件可以**自我授权**——权限层对只读风险默认放行，于是 manifest 只要声明一个只读能力，就能在没有任何人参与的情况下拿到 `ALLOW`。现在三个集合严格分开：`requested`（manifest 的声明）、`approved`（人的决定，绑定到某一次构建）、`granted`（dispatch 真正认的权限行）。安装、哈希、ABI 校验都不产生授权，只有 `approve()` 会；而且它是**对账**而不是追加——把批准从 `{A,B}` 收窄到 `{A}` 时，`B` 的授权会被一并收回。dispatch 还额外要求那个 `ALLOW` 来自授权或一次性确认，**并且**存在一份覆盖该能力、该构建的当前批准，所以手写一行授权也换不来调用。重装会清空批准而不是继承，但**重装同一份字节不写任何东西**，不会悄悄丢掉人的决定。撤销会阻断排队中的调用，并分别报告 `queued_stopped` / `running` / `stopped=False`，不假装杀掉正在跑的操作。批准是一份**本地决定记录**，不是发布者凭证。见 [`docs/architecture/plugin-lifecycle.md`](docs/architecture/plugin-lifecycle.md) 与 [ADR 0017](docs/decisions/0017-plugin-approval-boundary.md)。
>
> Phase 2.6.10 去了运行时从不碰的那一半：窗口。英文串直接写死在模板里，更糟的是错误文案由运行时客户端抛什么就拼什么——等视图把它渲染出来，错误码已经丢了，那句散文根本没法翻译。现在界面出 `zh-CN` 与 `en-US` 两版，131 个 key 挂在同一份类型化 schema 上（缺一个 key 是编译错误，不是运行时回退），错误改成结构化、在渲染时才变成人话——所以你在报错还挂在屏幕上时切语言，它会跟着变。选哪个库是被一条约束定死的：Tauri 的 CSP 没有 `unsafe-eval`，而 vue-i18n 9/10 用 `new Function` 编译消息；v11 改成解释 AST，产物里 `new Function` 与 `eval(` 各出现 0 次。打包后的壳**没有验过**——它在构建所用的这台机器上启动即崩——所以「在真实窗口里切换语言」记为未验证，而不是记成已完成。
>
> 流水线是绿的：Python、前端、Rust 三个 job 在每次推送到 `main` 与每个 PR 上跑，平台为 Ubuntu。真实环境测试——真浏览器、真 Blender、真 Commons API——仍是显式开启的，因为它们需要网络和本机 Blender；CI 不假装覆盖它们。

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
| 界面语言（`zh-CN`、`en-US`），选择会被记住 | **已实现**——语言选择在「设置」里；131 个 key 挂在同一份类型化 schema 上，缺键或多键都会让构建失败而不是运行时回退；错误文案在渲染时由错误码生成，所以报错挂在屏幕上时切语言也会跟着变。在**打包后的** Tauri 窗口里切换语言**未验证**——该壳在本机启动即崩 | `apps/desktop/src/i18n/locale.test.ts`（13）、`locales.test.ts`（12）、`errors.test.ts`（10）、`language-switch.test.ts`（3）、`views/SettingsView.test.ts`（6）、`docs/development/i18n.md` |
| `filesystem` 连接器（只读，受允许目录约束） | **已实现** | `tests/integration/test_filesystem_adapter.py` |
| `browser` 连接器——Playwright 真实图片搜索 | **已实现** | `tests/integration/test_browser_real.py`（需显式开启） |
| `wikimedia` 连接器——公开 Commons API，无需密钥，每张图带许可证与作者 | **已实现** | `tests/integration/test_wikimedia_real.py`（需显式开启）、`tests/unit/test_wikimedia_api.py` |
| 参考图来源：`来源 → 连接器` 注册表，由组装根接线 | **已实现**——新增来源无需改动工作流、网关、API 或 UI | `tests/integration/test_reference_api.py`、`docs/decisions/0013-collection-source-registry.md` |
| 连接器开发指南（快速开始、测试模板、依赖边界） | **已实现** | `docs/development/writing-an-adapter.md` |
| WASM 插件沙箱——任意 `wasm32-wasip1` 语言，燃料 + 墙钟 + 内存三重上限，仅一条受权主机调用通道，可选内容钉定、有界公平准入与批准后才能激活 | **已实现并已测试，但未接入 `Runtime`**——没有任何连接器变成插件，运行中的应用也不会调它。398 个测试，含对抗性、不变量、执行治理、完整性、调度与生命周期几套 | `tests/integration/test_wasm_plugin_negative.py`（9 类失败）、`test_wasm_fair_scheduling.py`、`test_wasm_install_integrity.py`、`test_plugin_lifecycle_runtime.py`、`docs/decisions/0017-plugin-approval-boundary.md` |
| 插件批准生命周期（discover → install → approve → activate → revoke → uninstall） | **已实现**——`requested`、`approved`、`granted` 是三个独立集合；只有 `approve()` 产生授权；重装清空批准；撤销报告 `still_running` / `stopped=False`。批准是**本地决定记录**，不是发布者身份 | `tests/unit/test_plugin_lifecycle.py`（27）、`tests/integration/test_plugin_lifecycle_runtime.py`（15）、`docs/architecture/plugin-lifecycle.md` |
| 只读插件检查 CLI（`list`、`inspect`） | **已实现**——打印钉定状态与 requested/approved/granted 三列；漂移时退出码 3。刻意**没有**任何能授予权限的子命令 | `tests/unit/test_plugin_admin_cli.py`（9）、`scripts/plugin_admin.py` |
| 插件开发指南（ABI、manifest、构建、常见错误） | **已实现** | `docs/development/writing-a-wasm-plugin.md` |
| 插件每次调用的主机调用预算 | **已实现**——`limits.max_host_calls`（默认 64，硬上限 1024），按尝试次数计，`-7 BUDGET_EXHAUSTED`；manifest 抬不高这个上限 | `tests/integration/test_wasm_host_budget.py` |
| 宿主能力执行治理——有界线程池（离开事件循环）、四种可区分状态、按声明权重计的成本预算、并发准入、关闭时如实报告残留 | **已实现**——阻塞能力不再冻住事件循环；`timed_out`、`cancelled`、`cancellation_requested` 是三个不同状态，各有测试 | `tests/integration/test_wasm_host_execution.py`（14 项）、`docs/architecture/host-capability-execution.md` |
| 主机调用的**真实成本**测量 | **尚未实现**——`cost` 是能力自己声明的权重，不是 CPU/内存的测量值；声明得离谱就等价于没有预算 | `docs/architecture/wasm-threat-model.md` §3 |
| 中断一个已经在跑的同步能力 | **尚未实现**——线程池在截止时间**不再等**，但槽位保留到线程真正返回；只有 `async def` 能力可取消，`aclose()` 会报告留下了什么 | `tests/integration/test_wasm_host_execution.py::test_a_sync_capability_times_out_and_keeps_running` |
| 插件内容完整性（`wasm_sha256` + `deskweave.install.json`，路径校验，符号链接拒绝） | **已实现，需显式开启**——`require_integrity` / `require_install_record`。检测**模块变过**；不识别**发布者**，也没有签名与吊销列表 | `tests/unit/test_wasm_integrity.py`、`tests/integration/test_wasm_install_integrity.py`、`docs/architecture/plugin-installation.md` |
| 公平调度（有界队列、按插件轮转、按能力限流） | **已实现**——16 个等待位 / 0.25 s / 轮转，拒绝发生在能力执行之前且不计费。是**有界等待**，不是严格公平；仅在单个宿主实例内有效 | `tests/integration/test_wasm_fair_scheduling.py`、`docs/architecture/host-capability-execution.md` |
| 插件**发布者身份**校验（签名 / PKI / 吊销） | **尚未实现**——哈希只能证明内容一致；能同时改写 `.wasm` 与 manifest 的攻击者手上，两者会完美互相印证。批准是一份本地决定记录，不是凭证 | `docs/architecture/plugin-installation.md` §8、`docs/decisions/0017-plugin-approval-boundary.md` |
| 防篡改的批准存储 | **尚未实现**——批准与授权行放在同一处、同一个 OS 用户下；能写它的人可以批准任何东西。它防的是**无人参与的授权**，不是本地攻击者 | `docs/architecture/plugin-lifecycle.md` §9 |
| 插件批准界面 | **尚未实现**——`PluginLifecycle.approve()` 只是 API，把待批能力摆给人看的界面还没做 | `docs/architecture/plugin-lifecycle.md` §10 |
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
- 第三方代码即便要跑，也**只能作为 WebAssembly 插件**跑：没有预开目录、没有网络导入、没有进程导入，燃料与墙钟双重预算，对外只有一条同样受权限层授权的主机调用通道（尚未接入应用）；
- 执行记录与权限授予都会落库，供审计。

已知缺口：连接器外面没有 OS 级沙箱；同一用户的其他进程可以读到子进程的环境变量（这就是信任边界）；macOS / Linux 上外壳被强杀时运行时仍可能残留（那边没有 Job Object，详见 `docs/decisions/0009-runtime-child-supervision.md`）。插件沙箱限了主机调用的**次数**，也按每个能力**自己声明的权重**计价——但那个权重不是测量值；它中断不了一个已经在跑的同步能力；它的内容哈希只能证明文件没变，不能证明是谁写的；它的公平调度只是有界等待（轮转、16 个等待位、0.25 s），不是承诺；它也挡不住时序侧信道——每条都带着实测证据写在 `docs/architecture/wasm-threat-model.md` 里。

## 仓库结构

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript 桌面外壳
runtime/                Python 核心运行时与控制 API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk,plugin-wasm}
adapters/{browser,wikimedia,blender,filesystem}
plugins/anime-modeling
examples/wasm-plugins/reference-metadata   示例插件 + 已提交的 .wasm
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
| WASM 插件沙箱测试 | `uv run pytest tests/unit/test_wasm_*.py tests/contract/test_wasm_plugin_contract.py tests/integration/test_wasm_*.py` |
| 重新构建示例 WASM 插件 | `uv run python scripts/build_wasm_plugin.py`（需先 `rustup target add wasm32-wasip1`） |
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

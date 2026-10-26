# DeskWeave 项目状态报告

> 报告时间：2026-10-23  
> 基线提交：`27fd0f8`（`feat/phase-2-baseline`，工作区干净）  
> 所有数字为本次实测，采集方式见文末附录。没有覆盖率数字——因为还没度量过，不是忘了写。
>
> **补记（同日，Phase 2.2 之后）**：本报告的测试数字（285 passed）与能力矩阵是
> Phase 2.2 开工前的快照，**已被后续工作取代**。当前实测为 **348 passed / 16 skipped**，
> 能力矩阵新增 4 行（WASM 插件沙箱、插件开发指南、两项未实现的插件控制）。
> 插件相关内容以 [`docs/decisions/0014-wasm-plugin-isolation.md`](decisions/0014-wasm-plugin-isolation.md)
> 与 [`docs/architecture/wasm-threat-model.md`](architecture/wasm-threat-model.md) 为准；
> 本文其余章节（架构、债务、风险、下一步建议）未受影响，仍然有效。
>
> **再补记（同日，Phase 2.3 之后）**：上面那个 348 也已过时。当前实测为
> **414 passed / 16 skipped**，其中 WASM 沙箱 129 项（63 行为 + 66 对抗/预算/不变量）。
> 能力矩阵再变：主机调用计数由「未实现」改为「已实现」（`limits.max_host_calls`），
> 新增两行明确未实现项（主机调用**成本**计量、取消已在执行的能力）。
> Phase 2.3 找出并修掉 5 个真实逃逸——插件在**没有任何授权**的情况下可以让
> `invoke()` 抛 `IndexError` / `RecursionError` / `TimeoutError`。威胁模型已按
> 「已验证 / 依赖宿主实现 / 无法保证 / 本平台未验证」四类重写。本轮**没有接入
> `Runtime`**，也没有把插件连到任何 Blender 修改操作。
>
> **三补记（同日，Phase 2.4 之后）**：Phase 2.3 结束时实测 414 passed / 16 skipped、WASM 沙箱
> 129 项；现在为 **428 passed / 16 skipped**、WASM 沙箱 **143** 项。2.3 记为「限制」的两件事
> 修掉了一半：宿主能力不再跑在事件循环上（测试用 0.4 s 阻塞能力期间心跳仍跳动来证明），
> 并且区分了 `timed_out` / `cancelled` / `cancellation_requested` 三种状态；新增按声明权重
> 计的成本预算与四道并发准入。**同步能力仍无法被中断**，`aclose()` 会报告它留下了多少仍在
> 运行的操作——这是持续的限制，不是待办。变异验证 13/13。
>
> **四补记（同日，Phase 2.5 之后）**：Phase 2.4 结束时实测 428 passed / 16 skipped、WASM 沙箱
> 143 项；现在为 **494 passed / 16 skipped**、WASM 沙箱 **209** 项（82 行为/契约 + 127 对抗，
> 对抗套件从 5 个变成 7 个），变异验证 **20/20**。本轮关掉两个「插件本身守规矩时依然成立」的
> 缺口：① **内容完整性**——manifest 可钉 `wasm_sha256`，另有 `deskweave.install.json` 记录
> 版本/路径/大小/声明能力，`require_integrity` / `require_install_record` 让漂移变成加载失败而
> 不是日志一行；路径在**任何文件系统访问之前**做词法校验，符号链接/联接点一律拒绝。
> ② **公平准入**——满负荷不再是先到先得：16 个等待位、最多 0.25 s、按插件轮转，拒绝发生在能力
> 执行之前且不计费；能力可声明 `rate_limit_per_s`。**这不是严格公平，是有界等待**，且只在单个
> `WasmPluginHost` 实例内有效。**哈希不是签名**：它能证明文件变过，不能证明是谁发布的；能同时
> 改写 `.wasm` 与 manifest 的攻击者手上两者会互相印证，所以本轮**没有**引入未设计过的 PKI。
> 进程隔离经过 8 项评估后**明确不实施**（理由与重启条件见 ADR 0016 Decision 4）。仍然**没有接入
> `Runtime`**。
>
> **五补记（2026-10-24，Phase 2.6 之后）**：现在为 **551 passed / 16 skipped**、插件相关测试
> **266** 项（209 沙箱 + 57 生命周期/授权），变异验证 **27/27**。本轮审代码发现一个真实授权漏洞：
> `PermissionManager` 对 `READ_ONLY` 风险默认放行，于是 manifest 只要声明一个只读能力就能**自我
> 授权**（实测该能力被执行 0 次后才修正）。修法是在契约层加 `DecisionVia`——dispatch 只接受
> `GRANT` / `ONE_SHOT` 来源的 `ALLOW`，策略默认与未标注来源一律拒绝（`decision="policy_default"`）。
> 同时补上三件事：① 三个集合严格分离（`requested` / `approved` / `granted`），安装、哈希、ABI 校验
> 都不产生授权；② 重装清空批准并保留 `previous_approval` 以说明**变了什么**，撤销额外丢弃内存中的
> 一次性许可；③ `PluginLifecycle` 自带 `activation_guard`，未批准的构建**不会被实例化**。撤销如实
> 报告 `still_running` / `stopped=False`，不声称杀掉正在跑的操作。CLI 只做只读的 `list` / `inspect`
> ——CLI 与 Runtime 的授权边界尚未设计，先不造第二条授权路径。仍然**没有接入 `Runtime`**：批准是一份
> 本地决定记录，不是发布者身份；没有签名、没有 PKI、没有可防篡改的批准存储。
>
> **六补记（2026-10-24，Phase 2.6.2 之后）**：Phase 2.6 结束时实测 551 passed / 16 skipped、
> 插件相关 266 项、变异 27/27；现在为 **570 passed / 16 skipped**、插件相关 **285** 项
> （209 沙箱 + 76 生命周期/授权），变异验证 **32/32**。本轮是对 2.6.1 只读复审所列 13 条发现的
> 逐条复现—修复，**没有新增功能**：①（P0）`approve()` 原本是**追加**授权，把批准从 `{A,B}` 收窄到
> `{A}` 后 `B` 仍在 dispatch 生效——实测收窄后 `granted=(A,B)` 且 `B` 仍被执行；现在先收回多余授权、
> 再写记录、再补发缺失授权，实测收窄后 `granted=(A,)` 且 `B` 执行 0 次。② 重装**同一份字节**原本会
> 重写记录、删掉批准再重建授权——调用方看到 `unchanged`，人的决定却被悄悄丢弃；现在逐字段比对后直接
> 返回，store 文件按字节比对前后一致，并单独证明改字节、改请求能力集都会走完整路径。③ 撤销新增
> **准入线性化点**：`revoke_plugin()` 在同一把锁内清队并标记，排队的调用被拒（`decision="revoked"`），
> `RevocationReport` 分别报告 `queued_stopped` / `running` / `still_running` / `stopped=False`。
> ④ **一行手写的授权不再能换到调用**：dispatch 每次额外向批准记录提问，写入授权行后执行数仍为 0
> （`decision="not_approved"`）。另有六项一致性修复（记录每次重读而非缓存、撤销写失败不得谎报成功、
> `uninstall` 留墓碑、一次性许可先预占、审计 `state` 必填、无 guard 的钉住宿主拒绝构造）。
> 仍然**没有接入 `Runtime`**，也没有改动「浏览器 → 人工确认 → Blender」主流程。
>
> **七补记（2026-10-24，Phase 2.6.3）**：2.6.2 的只读复审确认了 6 条发现，本轮逐条修掉，**没有新增
> 功能，也没有接入 `Runtime`**。实测（2.6.3 结束时）：完整套件 608 项（591 passed / 16 skipped /
> 1 failed，失败项与本轮改动无关，见末段）、插件相关 **307** 项（209 沙箱 + 98 生命周期/授权）、变异验证
> **39/39**（0 survived、0 skipped）。① 同一份 manifest 改用相对路径重装，原本会因按字符串比较把它
> 当成新安装、清掉活着的批准且仍然报 `unchanged`；现在按 `resolve()+normcase` 比对规范路径，路径无法
> 规范化时走保守的完整写入。顺带修掉同一函数的第二个盲点：`interface_version` 原本与**钉记录**比对，
> 而钉的 `unchanged` 判定根本不含该字段——字节不变而 ABI 变了时，两边是同一份过期副本，比较必然自洽；
> 现在从磁盘上的 manifest 读。② 批准检查抛非 `PluginError` 时，调用原本被审计成
> `admitted=True executed=True state=failed`，而能力执行次数是 0；现在按拒绝记账
> （`decision="not_approved"`、`state="refused"`），异常类型进审计、异常消息只进日志。③ 一次性许可
> 在批准检查/队列满/预算拒绝时原本一直挂在 `_pending`；`settle()` 幂等地覆盖每条退出路径——未开始则
> 归还、已开始则消耗。④ 撤销屏障原本是**每个 executor 私有**的：两个 lifecycle 共用一个 store 时，
> 第二个宿主里排队的调用在撤销后仍会执行；现在按 store 规范路径共享 `RevocationBarrier`，实测另一宿主
> 的排队调用 `executed=False`、`decision="revoked"`，能力总共只执行 1 次。文档里"store 级共享锁"的
> 说法是错的，已改正并写明：进程内共享的是屏障，跨进程两者都没有。⑤ 过期授权原本被 `list_grants`
> 当作当前权威——`inspect` 显示 `granted`、dispatch 却拒绝；现在 `list_effective_grants()` 作为对账
> 输入，`list_grants()` 保留为审计历史。⑥ 写批准存储失败原本以裸 `OSError` 逃出 `approve()`；现在抛
> `plugin_lifecycle`，带 `approval_saved=False` / `revoked_stale=[...]` 和原异常 `__cause__`，并明确
> 它不是回滚。
>
> 完整套件里 1 项失败是 `tests/integration/test_mcp_stdio.py::test_stdout_is_not_polluted_by_logs`：它
> 用固定 `sleep(2.5)` 等网关打出 "MCP gateway ready"，而本机实测从进程启动到该行需 2.33–2.46 s（五次
> 采样，网关启动本身包含 Blender 探测）；同一条命令不改任何代码跑 4 次，1 通过 3 失败，而该测试真正
> 守护的 stdout 纯净性每次都通过。属于既有的时限脆弱性（与本轮 6 条发现无关），已在随后的复审里改成
> 确定性等待（见八补记）。
>
> **八补记（2026-10-24，Phase 2.6.3 后复审）**：这一轮**不加功能**，只审查上一轮自己交付的东西。实测：
> 完整套件 **614 项（598 passed / 16 skipped，exit 0）**、插件相关 **313** 项（209 沙箱 + 104
> 生命周期/授权，其中 `test_lifecycle_integrity.py` 由 22 增至 28）、变异验证 **41/41**（0 survived、
> 0 skipped），`ruff check` / `ruff format --check` 干净。复审在"已修好"的代码里又挖出三处，都不是文案
> 问题：① **撤销屏障会被过期的批准读取抬掉**——守卫先读记录再无条件 `lift`，正好落在两者之间的撤销被
> 抹掉，调用照常执行（实测 `executed=True`、`decision="allow"`）。现在屏障按插件计代次，守卫在读之前
> 取代次、只走 `lift_if_unchanged`，代次变了就按 `plugin_not_approved` 拒绝。② **memory store 的屏障
> 根本没共享**——按路径键的字典对无路径 store 只能退化成每个 lifecycle 一份私有屏障，于是"一个 store
> 一道屏障"在测试最常用的 store 上并不成立。现在无路径 store 按**对象**（`WeakKeyDictionary`）共享。
> ③ **并发构造会各建一道屏障**，且遍历中被改写的 `WeakValueDictionary` 直接抛 `RuntimeError`；路径
> 不可解析时原本静默降级为私有屏障。现在 get-or-create 走 `threading.Lock`，路径不可解析直接拒绝构造。
> 另外确认了两件事：`interface_version` 的**唯一权威是磁盘上的 manifest**（`runtime.py` 用它比
> `ABI_VERSION`，钉记录只是绑定快照，不参与任何决策）；批准失败后**唯一的实际执行入口**
> `registry.dispatch` 及其两条路径都拒绝，控制能力仍可执行。MCP stdio 的固定 sleep 也换成了等就绪行
> （30 s 上限）、关 stdin 收尾，改后 5/5 通过、约 2.85 s；并注入一行 `print()` 验证该断言确实会变红，
> 还原后 SHA-256 与备份一致。

---

## 0. 一句话结论

**技术侧已经跑通闭环，卡在三件非技术事情上。**

DeskWeave 现在是一个能完整跑通「Agent 收集参考图 → 人确认 → Blender 建面」的本地优先 Agent 桌面基础设施：2.5 万行、339 个测试、CI 全绿、可扩展性被一次真实的新连接器接入证明过。缺的不是代码，是**主干分支、分支合并、以及 Windows 上的 CI 覆盖**——后者对一个以 Windows 桌面为主战场的产品来说，是真风险。

---

## 1. 定位：它是什么，不是什么

**是** 一套本地优先的 Agent 桌面基础设施。Agent 通过 MCP 说话，核心运行时掌握状态、权限与审计，Tauri 外壳给人一扇看得见两者的窗口。没有托管控制平面，没有账号体系，没有远程调用。

**不是** 一个 Agent 框架，也不是一个建模工具。它不训练模型、不实现推理循环；它的价值在于**给会动手的 Agent 装上栏杆**——能力要声明、参数要校验、权限要授予、确认只能由人做。

核心闭环（Phase 1 建立，Phase 2 未变）：

```
Agent 收集参考图  →  图片落进本地 artifact 仓库  →  人在桌面 UI 里挑选
                                                            ↓
                                        Blender 严格按这次选择建参考平面
```

这条链上有一个刻意的设计：**没有任何 MCP 工具能替用户确认选择**。`confirmed=true` 参数会被参数校验直接拒绝，确认是 UI 动作，服务端核对任务状态与 artifact 归属。

---

## 2. 工程规模

git 跟踪 196 个路径（182 个文本文件 + 14 个二进制，主要是图标），文本合计 **25,012 行**。

| 区域 | 文件 | 行数 | 说明 |
| --- | ---: | ---: | --- |
| `tests/` | 37 | 5,921 | 占全部文本 **23.7%** |
| `runtime/` | 29 | 4,296 | Python 核心：工作流、权限、持久化、事件、artifact |
| `apps/desktop/` | 36 | 4,099 | Rust 1,160 + Vue 1,501 + TS 1,231，其余配置 |
| 根目录 | 6 | 3,167 | 主要是 `uv.lock` / `pnpm-lock.yaml` |
| `docs/` | 22 | 2,770 | 4 篇架构 + 13 篇 ADR + 5 篇开发指南 |
| `adapters/` | 17 | 2,112 | browser / wikimedia / blender / filesystem |
| `packages/` | 27 | 1,965 | contracts / adapter-sdk / mcp-gateway / plugin-sdk |
| `scripts/` | 3 | 445 | `ci_local.py`、`ui_smoke.py` |
| `plugins/` | 4 | 87 | anime-modeling（实验性） |

语言分布：Python 14,539 行、Markdown 3,198、YAML 2,738（锁文件）、Vue 1,501、TypeScript 1,231、Rust 1,160。

一个值得记下的信号：**`TODO` / `FIXME` / `XXX` 全仓 0 处**。不是没债务，而是债务都写进了 ADR 和 README 的能力矩阵，而不是散在注释里。

---

## 3. 架构现状

依赖方向由包布局强制，不是靠约定：

```
Codex / Agent ──MCP/stdio──► mcp-gateway ──► Runtime ──► adapters
                                               │
                     ┌─────────────────────────┼─────────────────────────┐
                     ▼                         ▼                         ▼
                 权限层                    工作流引擎                  SQLite
                     │                         │
                     └────────── 进程内事件总线 ──────────┘
                                               ▲
                      Tauri 外壳 ──HTTP──►     │   （仅回环 + 每次启动的令牌）
                      Vue 3 UI ───────────────┘
```

`contracts ← adapter-sdk ← runtime ← mcp-gateway`：上游不认识下游，具体适配器只在 `runtime/src/deskweave_runtime/bootstrap.py` 一处接线。

| 组成 | 现状 |
| --- | --- |
| 适配器 | 4 个（browser、wikimedia、blender、filesystem），共 5 个能力 |
| 能力 | `collect_reference_images`（browser / wikimedia 各一个）、`create_reference_plane`（blender）、`list_dir` + `read_text`（filesystem） |
| MCP 工具 | 7 个：`runtime_health`、`list_adapters`、`list_capabilities`、`list_tasks`、`collect_reference_images`、`get_reference_task`、`cancel_reference_task` |
| 持久化 | SQLite，2 个版本化迁移 |
| 任务状态 | 6 态：pending / running / waiting_for_confirmation / succeeded / failed / cancelled |
| 决策记录 | 13 篇 ADR，每篇对应一处真实取舍 |
| 来源注册表 | `来源 → 连接器` 映射，`bing → browser` 为内置默认，新增来源不需要改工作流 / 网关 / API / UI |

**可扩展性是真的验证过，不是声称的。** Phase 2 接 wikimedia 时，改动只落在组装根的一条映射上——这是这个项目最有说服力的一条证据。

---

## 4. 能力矩阵（摘要）

README 里那张矩阵共 25 项：**17 项完整实现、1 项带条件（仅 Windows）、1 项实验性、6 项明确未实现**。下面是主干。

已实现：

- Python 核心运行时、Adapter SDK（JSON Schema 校验 + 超时 + 结构化错误）、权限层（授予 / 一次性许可 / 默认拒绝）
- SQLite 持久化与迁移、进程内事件总线
- stdio MCP Server（7 工具）、桌面外壳与确认界面
- 四个连接器全部可用，其中 browser 走 Playwright 真实搜索、wikimedia 走公开 Commons API（无需密钥，每张图带许可证与作者）
- artifact 仓库：下载防护 + 来源信息、8 MiB 上限、magic bytes 嗅探、原子落盘
- 运行时子进程监管（**仅 Windows**，Job Object + 先礼后兵）
- 令牌不落盘、中断任务按证据对账恢复、产物 `.blend` 的元信息与打开动作

明确没做（同样重要）：

| 缺口 | 状态 |
| --- | --- |
| 产物内联缩略图 | 未实现——UI 直接说明，不放占位图 |
| macOS / Linux 子进程监管 | 未实现——那边没有 Job Object，只能杀直接子进程 |
| 截图 / 页面捕获 | 未实现，没有对应能力 |
| 非图片 artifact、参考平面以外的场景编排 | 未实现 |
| 安装包 / 代码签名 | 未构建、未验证 |
| 远程或多用户 | **按设计不做** |

---

## 5. 质量证据

### 测试（本次全部实跑）

| 套件 | 结果 | 耗时 |
| --- | --- | --- |
| Python `pytest` | **285 passed, 16 skipped**（共收集 301） | 67.8 s |
| 前端 `vitest` | **31 passed**（3 个文件） | 2.2 s |
| Rust `cargo test --lib` | **7 passed** | 2.9 s |
| **合计** | **323 通过 / 339 收集** | — |

分布：unit 188、integration 82、contract 29、e2e 2。

16 个跳过项都是有原因的，不是被偷偷关掉的：blender 真实测试 7 个、browser 真实测试 5 个、wikimedia 真实 API 3 个、e2e 全链路 1 个——都需要 `DESKWEAVE_NETWORK_TESTS=1` / `DESKWEAVE_BLENDER_TESTS=1` 显式开启，因为它们要网络和本机 Blender。CI 里它们报 SKIPPED，从不报 passed。

### CI

6 次真实运行，4 绿 2 红：

| 运行 | 提交 | 事件 | 结论 | 耗时 |
| --- | --- | --- | --- | --- |
| `39450430838` | `819ceeb` | push | **failure** | 185 s |
| `39451465703` | `b07808f` | push | **failure** | 121 s |
| `39452051511` | `6c2cb29` | push | success | 177 s |
| `39454921197` | `78a723f` | push | success | 60 s |
| `39456746729` | `0dd60a4` | push | success | 80 s |
| `39457560969` | `27fd0f8` | workflow_dispatch | success | 112 s |

两次红都是**真实的跨平台缺陷**，不是环境噪声：

1. 测试夹具拿 `ping -n 60` 当长命子进程——Windows 的 `-n` 是次数，Unix 的 `-n` 是 numeric 输出，`60` 被当成主机名，ping 秒退，宽限期在断言前就结束了。本地绿、Linux 红。
2. 修复时调用了只在 `#[cfg(windows)]` 下存在的函数，而 `cargo check --all-targets` 在 Windows 上根本不编译非 windows 分支。还是本地绿、CI 红。

这两次红暴露的是同一个结构性问题，见 §7 的 P1-3。

### 覆盖率

**没有数据。** 项目里没装 coverage 工具，也没设阈值。这意味着「285 个测试通过」只说明已写的测试没坏，不说明代码被覆盖了多少。这是本报告最大的信息缺口，也是 §7 里的一项 P2。

---

## 6. 安全姿态

默认值偏保守，几条硬的：

- 控制 API **只绑定回环**，由配置校验强制；
- 每个请求要**每次启动随机生成的令牌**，webview 拿不到，且**不落盘**（历史明文令牌在首次启动时移除）；
- webview 只能调用两个 Rust 命令，且限定结构化路由白名单；
- 工具参数未声明直接拒绝，再做 schema 校验；`confirmed=true` 不构成任何授权；
- 下载有 SSRF 防护（只允许公网 IP、逐跳复检）、8 MiB 上限、magic bytes 识别、原子落盘；
- 文件访问限制在允许目录内，**路径永远不被当作授权凭证**，连接器只接收 artifact id；
- Blender 执行固定脚本 + JSON 配置，参数里塞不进 Python，结果由第二个进程复核；
- **没有通用代码执行能力**，权限层无条件拒绝 `code:execution`；
- 执行记录与权限授予落库，供审计。

已知缺口：连接器外面没有 OS 级沙箱；同一用户的其他进程能读到子进程环境变量（这是信任边界）；macOS / Linux 上强杀外壳后运行时可能残留。

---

## 7. 风险与债务

按 P0–P3 分档。**P0 为空**——没有阻塞性缺陷，没有已知的安全漏洞，构建与测试全绿。

### P1（应该尽快处理，但不需要紧急止血）

**P1-1 `main` 分支不存在，仓库默认分支是功能分支。**
仓库首次推送时是空的，GitHub 把第一个推上去的 `feat/phase-2-baseline` 设成了默认分支。后果有三层：PR 没有合适的 base；`push.branches: ["main"]` 现在等于**没有任何推送会触发 CI**；仓库首页展示的是一个未合并的分支。

**P1-2 九笔提交全部堆在一个从未合并的分支上。**
从 Phase 0 底座（`393ec1b`，2026-10-19）到现在，所有提交都在 `feat/phase-2-baseline` 上，**主干一次都没被写入过**。没有 PR、没有合并点、没有 review 记录。分支越久不落主干，回滚和追溯的粒度就越粗——现在这九笔只能整体进退。

**P1-3 Windows 是主战场，但 CI 只跑 Linux。**
产品核心价值之一是 Windows Job Object 的子进程监管，而 CI 的三个 job 全在 `ubuntu-26.04` 上。`cfg!(windows)` 分支在 CI 里**根本不进入编译**——上面两次失败的直接原因就是这个盲区。目前 Windows 的正确性靠本地手跑 `cargo test --lib` 保证，这是唯一一道防线，且没有自动化。

### P2（有明确收益，可以排期）

**P2-1 没有覆盖率度量。** 无法回答「哪些代码没被测到」，也就无法判断 23.7% 的测试占比是健康还是虚胖。加 `coverage.py` + `vitest --coverage`，先出报告不设阈值，比一上来就卡数字更实际。

**P2-2 工作流文件本身没有 lint。** YAML 拼错要到 CI 跑起来才知道。`actionlint` 一条命令就能把这类问题前移到本地，也能查出 `ubuntu-latest` 之类的隐式漂移。

**P2-3 依赖版本靠人盯。** 上周那两条注解（node20 弃用、runner 镜像迁移）都是**被动发现**的，不是主动巡检出来的。要么开 Dependabot（只对 GitHub Actions 和 lockfile），要么接受每季度人工过一遍。

**P2-4 包没有发布路径。** `packages/*` 是可复用的（contracts、adapter-sdk、plugin-sdk 明显是给别人写的），但没有版本号、没有发布流程、没有 changelog。现在它们是「内部包」，这与「Adapter 开发者体验」的定位有落差。

### P3（产品缺口，按兴趣推进）

产物内联缩略图、截图/页面捕获、非图片 artifact、场景编排、安装包与代码签名、macOS/Linux 的进程监管。这些在 README 的能力矩阵里都如实标着「未实现」，不留想象空间。

---

## 8. 建议的下一步

**先做三件收尾的事，再开新功能。** 现在代码质量不是瓶颈，工程流程是。

| 顺序 | 动作 | 为什么现在 |
| --- | --- | --- |
| 1 | 把 `feat/phase-2-baseline` 推成 `main`，或开 PR 合并 | 一次同时解决 P1-1 和 P1-2：主干有了、默认分支对了、`push` 触发的 CI 自动恢复 |
| 2 | 合并后把默认分支设回 `main`，删掉功能分支 | 不删的话 `pull_request` 的 base 选择会长期混乱 |
| 3 | 加一个 Windows job（哪怕只跑 `cargo test --lib` + `cargo check`） | 堵住 P1-3。这是唯一能防止「本地绿、CI 红」再次发生的结构性办法；代价是一次冷启动的编译时间（估计几分钟，未实测） |

技术侧如果继续往前走，性价比最高的是 **P2-1（覆盖率）**——它会给后面所有工作提供判断依据；其次是 **P2-3（Dependabot）**，把被动发现变成主动巡检。

---

## 附录：数据采集方式

报告里的每个数字都可以重跑复现。

```bash
# 规模（只统计 git 跟踪的文本文件）
git ls-files | ...                      # 182 文本 + 14 二进制
python -c "统计各目录行数"                # 见 §2 表格

# 测试
uv run pytest -q                        # 285 passed, 16 skipped
uv run pytest --collect-only -q         # 301 collected
corepack pnpm test                      # 31 passed
cd apps/desktop/src-tauri && cargo test --lib --locked   # 7 passed

# CI 历史
gh run list --limit 20 --json databaseId,headSha,event,conclusion

# 依赖方向与能力
grep -rn "CAPABILITY" adapters/*/src/*/*.py
grep -n "MIGRATIONS" runtime/src/deskweave_runtime/persistence/migrations.py
```

---

*本报告是 2026-10-23 的快照。能力矩阵与风险分档会随代码变化失效，引用前请核对当时的提交。*

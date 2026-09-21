# Task 2 独立验收报告

- 审查范围：`BASE=30a24ec`，`HEAD=2e72e99`
- 必读材料：`task-2-brief.md`、`task-2-report.md`、`review-30a24ec..2e72e99.diff`（9,339 行）均已完整阅读。
- 方法：以静态审查为主，只做了少量纯本地、内存内合同检查；未发起网络请求，未机械重跑报告中的全量测试，也未修改生产文件或测试。

## Spec Compliance

**Verdict: FAIL（不符合 Task 2 合同）**

| 合同边界 | 结论 | 依据摘要 |
|---|---|---|
| Task Runtime 唯一授权权威 | 部分符合 | 核心 grant 路径严格，但 Provider 出站绕开该边界，`ToolRegistry` 缺少 boundary 时仍可直执行业务 handler。 |
| 单次 grant、群组/任务/运行作用域、重放 | 符合核心合同 | 高风险 grant 与 invocation 在同一 SQLite 事务内消费/插入；group/task/run/tool/scope/purpose 均精确比较；恢复时高风险 effect 不自动重放。 |
| Shell 最小权限 | 不符合 | 当前只校验调用方声明的 scope，未以 OS 机制强制 read/write/network/child-process 边界。 |
| Electron main-only safeStorage | 部分符合 | `safeStorage` 仅 main 导入且 renderer 未获得明文/密文；但 SecretRef 的 purpose/audience/endpoint 绑定不足，vault/lease 生命周期也不闭合。 |
| Provider 隐私、预算、预授权 failover | 不符合 | 生产 Provider 调用直接出站；统一 DLP、Task Runtime 预算和 `ModelCapabilityRegistry` 未接入生产链路。 |
| MCP prompts 禁用与协议/授权边界 | 不符合 | prompt 空 capability 可通过；授权未绑定 server/remote operation/schema/endpoint；组件也没有生产装配点。 |
| 公开审计脱敏与完整性 | 不符合 | 旧 activity/provider diagnostic 仍可保留原始参数或计划内 PII，且没有 effect 终态审计事件。 |
| 旧入口失败关闭 | 不符合 | 正常 server 装配路径已收口，但 `ToolRegistry` 的缺省行为仍是 fail-open；旧明文环境变量/设置迁移也未完全失败关闭。 |

### 已确认的优点

- `backend/task_runtime/service.py:965-1002` 对 group/task/run/tool/risk/scope/purpose/grant/idempotency 做精确绑定；`backend/task_runtime/service.py:1058-1077` 与 `backend/task_runtime/store.py:1133-1185` 将一次性 grant 的 CAS 消费、invocation 插入和 run event 放在同一事务中。
- `backend/task_runtime/store.py:1272-1298` 以原子状态迁移领取 effect；`backend/task_runtime/service.py:2016-2040` 在重启恢复时阻断高风险或不可重放 invocation，并撤销 group grants。
- `safeStorage` 的生产导入只出现在 `electron/main.ts:14`；`electron/secretVault.ts:134-164` 不向 renderer 返回凭据值，交付后也清理 main 中的临时 buffer/string 引用。
- 私有 lease 路由采用独立双 token、严格 content type/字段/64 KiB body、无 CORS：`electron/main.ts:726-758`、`backend/server.py:434-462`、`backend/server.py:6530-6549`、`backend/server.py:6666-6699`。Python broker 使用可清零 `bytearray` 并校验 TTL/purpose：`backend/commercial_gateway.py:1184-1259`。
- MCP transport 已具备不少正确的协议硬化：stdio 1 MiB/newline/30 秒边界及服务端请求拒绝位于 `backend/commercial_gateway.py:580-623`；HTTP HTTPS host、redirect、content-type、SSE 单响应、1 MiB 上限位于 `backend/commercial_gateway.py:706-840`。
- 正常 server 创建 `ProjectEngine` 时均注入 Task Runtime（例如 `backend/server.py:3953-3960`、`backend/server.py:4186-4197`、`backend/server.py:4538-4548`），Zinnia 当前暴露的旧 effect 也在 `backend/agents/zinnia.py:966-982` 统一阻断。

## Issues

### Critical

#### C1 — 生产 Provider 出站没有进入 Task Runtime、DLP 或预算权威

**证据：**

- `backend/knowe_core/provider_client.py:353-383` 和 `backend/knowe_core/provider_client.py:468-477` 直接把完整 message/body 与凭据发往 Provider endpoint。
- 生产调用方直接构造该 client：`backend/agents/knowe_agent.py:188-204`、`backend/agents/deepseek.py:529-544`、`backend/agents/zinnia.py:887-905`、`backend/agents/zinnia.py:1288-1305`。
- PII/DLP 只在 Tool Gateway 内执行（`backend/commercial_gateway.py:287-312`）。`ModelCapabilityRegistry` 仅定义于 `backend/commercial_gateway.py:1116-1162`；排除测试后没有生产调用者。Task Runtime 的 model token/cost 预算也没有接到上述 Provider request 边界。

**影响：** 计划内学生 PII 可以在没有 Task Runtime 明示审批的情况下随普通模型消息出站；实际模型调用不受 run 的 token/cost 上限约束。所谓 failover 的隐私等级、预授权和剩余预算检查当前只是孤立库代码，不能构成生产保证。

**防御性修复：** 建立唯一的 `ProviderGateway` 并禁止 agent 直接构造/调用 `ProviderClient`。由后端从最终 message/body 派生 DLP 输入，在 Task Runtime 中预留并原子扣减 model-turn/token/cost 预算；敏感出站必须获得 purpose-bound grant。Provider/failover 能力和 endpoint 必须来自后端可信注册表，并在同一事务性 request identity 中记录，所有 direct client 出站应 fail closed。

#### C2 — MCP grant 没有绑定实际 server、远端 tool/resource、schema 与 endpoint

**证据：**

- discovery 虽临时校验 schema，却只持久保存 tool name/resource URI，丢弃 schema 与 digest：`backend/commercial_gateway.py:904-949`。
- invoke 只确认所选 server 曾发现该 name，并只确认本地 descriptor 的 `kind == "mcp"`；没有把 descriptor/grant 与 `server_id`、远端 name、operation kind、schema digest、endpoint authority 进行精确比较：`backend/commercial_gateway.py:964-996`。
- resource 路径同样只核对 discovery URI 和 `request.arguments.uri`，没有独立的 server/resource capability identity：`backend/commercial_gateway.py:998-1014`。
- 一次纯本地合同检查确认：经过批准/审计的本地 tool identity 与实际 MCP server/tool identity 不一致时，调用仍被接受，而公开审计仍记录本地 tool identity。
- 排除测试后，`McpRegistry`/两种 transport 没有生产装配调用点；因此 MCP 要么尚不可用，要么未来接线时会携带上述授权错绑。

**影响：** grant 和审计不能证明实际执行的是已检查、已批准的 MCP operation；跨 tool/server/resource 的 authority 隔离不成立，schema 变化也不会使旧授权失效。

**防御性修复：** 定义不可变的 MCP capability identity：`server_id + transport + canonical endpoint(host/port/path) + operation kind + remote name/URI + schema digest`。discovery 后保存经过深度验证的 frozen schema/digest；approval、grant、invocation 和 audit 全部携带并精确比较该 identity。arguments 必须按 frozen schema 验证，tool 与 resource 使用不同 descriptor，任何 schema/discovery 变化都要求新审批。

#### C3 — Shell/stdio 的 read/write/network/child scope 只是声明，没有执行时强制

**证据：**

- `backend/commercial_gateway.py:249-285` 只验证 `ToolExecutionRequest` 自报的 executable/cwd/read/write/network/child 字段是否为 descriptor 子集。
- stdio 实际执行在 `backend/commercial_gateway.py:557-578` 直接使用 `asyncio.create_subprocess_exec`；没有 restricted token/AppContainer/job、文件系统隔离、网络隔离或子进程树约束。`env={}` 不能限制文件、网络或 descendant process。
- `child_processes=False` 只参与授权比较，关闭时也没有 OS 执行约束。session close 只 terminate/kill 直接子进程（`backend/commercial_gateway.py:629-636`）。
- executable canonicalization 在 `backend/commercial_gateway.py:258-261`、`backend/commercial_gateway.py:665-672` 无条件 `casefold()`，且验证 canonical path 后实际启动仍使用原始 `config.command`，在大小写敏感平台存在 identity/launch 不一致窗口。

**影响：** 审批卡和 grant 中的最小权限 scope 不能约束真实进程 effect；“offline by default”、read/write roots 和禁止 child process 都只是审计声明，不是安全边界。

**防御性修复：** 在支持的平台使用真正的 OS sandbox（Windows restricted token/AppContainer + Job Object/网络策略；其他平台使用等价隔离）强制文件、网络和进程树范围，并执行已打开/已验证的 exact executable identity。canonicalization 使用 OS-aware 大小写语义并消除验证到启动的替换窗口。在强制机制可用前，生产 Shell/stdio 应 fail closed，而不是声明受限后启动普通进程。

#### C4 — SecretRef 的 purpose 不是可信的 consumer/audience/endpoint 绑定

**证据：**

- vault record 只保存 renderer 提交的 `kind/label/purpose` 与 ciphertext，没有 provider、canonical base URL、transport 或 audience：`electron/secretVault.ts:17-24`、`electron/secretVault.ts:81-110`。
- issueLease 只比较字符串 purpose：`electron/secretVault.ts:134-160`。renderer 可调用的 store/lease contract 因而决定了该标签，而不是 main 根据可信 credential slot 派生。
- Python 同一正则同时接受 `provider:*` 和 `master-key:*`，`_norm_binding` 又接受任意 provider/model/base_url/transport 组合：`backend/runtime_settings.py:52-55`、`backend/runtime_settings.py:246-267`。`resolve_provider_credential` 仅按 binding 自带的 ref/purpose 取值：`backend/runtime_settings.py:67-84`。
- `ProviderClient` 将取出的 secret 用于 binding 指定的 endpoint（`backend/knowe_core/provider_client.py:243-263`、`backend/knowe_core/provider_client.py:353-383`），而自定义 endpoint 在 renderer 甚至允许 `http://`（`src/store/settings.ts:998-1031`）。一次纯本地合同检查也确认 provider binding 会接受 master-key purpose。

**影响：** purpose 目前是可配标签而不是不可伪造的使用约束；现有 SecretRef 可以被重新关联到不同 provider/transport/endpoint，且 provider credential 没有获得强制 HTTPS/audience 保证。这破坏了“renderer 不可信、凭据只在 main 按目的短租”的核心边界。

**防御性修复：** main 必须按固定 credential slot 生成 kind/purpose，并在加密 record 中绑定 canonical provider、HTTPS endpoint authority、transport 与 audience；renderer 只能提交 value 和受限 slot intent。lease envelope 应携带 main 认证的 kind/audience，Python 按调用点派生 expected purpose 并验证 provider kind、exact endpoint/transport。Provider 凭据出站只允许 HTTPS（经明确授权的本机 broker 除外），禁止把 master-key lease 用于 Provider resolver。

#### C5 — 公开 activity/audit 与 Provider diagnostics 仍会保留原始参数或计划内 PII

**证据：**

- `backend/tool_ledger.py:217-224` 把 path/command/query/url/destination/source 原文拼入 activity token，`backend/engine.py:5460-5464` 将其作为公开 `tool_gen.tool_name` 发出。
- `backend/privacy.py:91-149` 不处理 `tool_gen.tool_name`。纯本地合同检查确认该公开字段会原样保留计划内 PII。
- Provider 独立 redactor 只覆盖少数模式（`backend/knowe_core/provider_identity.py:103-119`），缺少统一 DLP 已定义的 timetable/private-note 等类别（`backend/commercial_gateway.py:1017-1032`）；Provider 错误 body 经此处理后仍进入面向上层的错误消息：`backend/knowe_core/provider_client.py:625-645`。纯本地合同检查确认计划内 PII 可残留。

**影响：** renderer 可见事件、错误、日志或诊断链路可能包含原始学生 PII、文件/查询内容，某些参数形态还可能包含 credential。该泄露发生在新的 Task Runtime public-payload 白名单之外，因而新审计层的脱敏不能补救。

**防御性修复：** public activity 只发固定 tool name、opaque invocation id、purpose/risk/status/timing/usage/typed error；永不拼接 path/query/url/command/body。移除独立 provider regex，所有 public/log/telemetry/diagnostic 字符串统一通过同一结构化 DLP redactor，默认对未知原始 body fail closed，并补充中文 timetable/private-note/name-association 回归测试。

### Important

#### I1 — MCP prompt、JSON-RPC、schema/resource 检查仍不完整

**证据：**

- prompt 判断使用 truthiness：`backend/commercial_gateway.py:915-917`、`backend/commercial_gateway.py:952-962`。标准形态的 `capabilities: {"prompts": {}}` 会被视为 false；纯本地合同检查确认该 capability 被接受。
- schema 校验仅检查顶层 object、总大小和两个小写 `$ref` 文本片段：`backend/commercial_gateway.py:904-911`；没有深度结构/复杂度/关键字/嵌套 metadata 检查。resource discovery 只取 URI，不检查 metadata 或内容：`backend/commercial_gateway.py:934-938`。
- 共有 JSON-RPC response validator 只核对 version/id/result（`backend/commercial_gateway.py:475-498`），没有像 stdio 专用层 `backend/commercial_gateway.py:616-620` 那样拒绝同时带 `method` 的 server request，导致 HTTP 与 stdio 政策不一致。

**影响：** “prompts 完全禁用”和“无法安全表述的 schema/resource fail closed”没有成立；HTTP transport 也存在协议歧义入口。

**防御性修复：** 以 key presence（而非值 truthiness）拒绝任何 prompt capability/list；为两种 transport 使用同一个 JSON-RPC envelope validator，拒绝所有 server-initiated request/ambiguous envelope；对 schema 做深度、节点数、递归、关键字和 nested metadata 校验并冻结 digest；resource metadata/content 在进入模型上下文前执行大小、类型、prompt-injection 与 DLP 检查。

#### I2 — Task Runtime 没有完整的 effect 终态公开审计

**证据：**

- `tool_prepared` public whitelist 只有 status/invocation/tool/disposition/usage：`backend/task_runtime/contracts.py:55-62`。
- prepare event 同样不含 purpose、risk、start timing 等：`backend/task_runtime/service.py:1068-1077`。
- `complete_tool` 只更新私有 invocation row，不追加 RunEvent：`backend/task_runtime/store.py:1244-1270`。因此公开审计没有 completed/failed、duration、token/cost delta 或 typed terminal error。

**影响：** 公开 audit 无法回答 effect 是否真正执行、何时结束、耗费多少以及以何种类型失败；也无法形成合同要求的可验证端到端轨迹。

**防御性修复：** 在领取、完成、失败、恢复阻断时通过 Task Runtime 事务追加白名单 event；仅包含 opaque ids、purpose、risk、status、timing/duration、token/cost usage 和 typed error，禁止 body/secret/raw PII。完成状态更新和终态 event 必须原子提交。

#### I3 — `ToolRegistry` 在缺少 Task Runtime boundary 时默认 fail-open

**证据：**

- boundary 是可选字段，注释还保留“historical in-process seam”：`backend/knowe_core/tool_registry.py:87-100`。
- `execute` 在 boundary 为空时直接调用 handler：`backend/knowe_core/tool_registry.py:141-149`。纯本地合同检查确认未绑定 registry 会执行 effect。
- `ProjectEngine.bind_commercial_tool_registry` 在 Task Runtime 缺失时原样返回 registry：`backend/engine.py:8285-8308`。

**影响：** server 当前主装配路径虽注入 runtime，但任何遗漏、嵌入式入口或未来 factory 回归都会重新产生第二授权权威；这与“旧入口必须迁移或阻断”相冲突。

**防御性修复：** 生产 `ToolRegistry` 默认安装 deny-all boundary；缺少 Task Runtime 时确定性拒绝。若单元测试确需直调 handler，使用名称明确、不可被生产 factory 选择的显式 test-only unsafe mode。

#### I4 — Python 仍接收/加载旧明文凭据，settings 迁移失败也会继续启动

**证据：**

- `backend/config.py:65-66` 主动 `load_dotenv()`；`backend/config.py:324-326` 和 `backend/config.py:420` 仍把 `KNOWE_VISION_API_KEY`/`DEEPSEEK_API_KEY` 读成长生命周期 config 字段。
- Electron spawn 环境先展开整个 `process.env`，未剔除上述 key：`electron/main.ts:837-850`。
- legacy `settings.json` 检出明文后会调用 `_persist()`，但该写失败落入外层宽泛 catch，仅 warning 后继续且 `_loaded` 已置真：`backend/runtime_settings.py:166-218`。旧明文文件会留在磁盘，进程也不会重试/隔离它。

**影响：** “Python 只持有短期 lease”并未成立，旧明文配置可继续存在于 Python 内存或磁盘；迁移失败不是失败关闭。

**防御性修复：** Electron spawn 前显式删除所有 provider/master-key env；Python 不再从 dotenv/env 读取 secret，也移除相应 config 字段和文档回退。发现旧明文 settings 时，必须成功原子清除/迁移后才继续启动；失败则隔离文件并阻断 Provider 功能，不能把 `_loaded` 标成成功。

#### I5 — SecretRef/lease 生命周期会保留并续租已替换凭据，过期清理也是惰性的

**证据：**

- 新 API key 生成新 SecretRef 后不会删除该 slot 的旧 ref；只有显式 clear 才删除：`src/store/settings.ts:258-307`。
- main 每 240 秒对 vault 中所有 `provider_credential` 续租，而非只对当前后端权威 settings 引用的 refs：`electron/main.ts:771-809`。
- Python broker 只在 provision 同 ref/purpose 或 redeem 遍历时清理；没有独立 expiry timer，`public_snapshot` 也不 prune：`backend/commercial_gateway.py:1210-1259`、`backend/commercial_gateway.py:1261-1267`。

**影响：** 被替换、测试失败或不再引用的凭据仍长期留在 safeStorage，并可能被无限续租到 Python；不再使用的过期 lease material 也可能超过 TTL 留在内存。

**防御性修复：** 以 main/backend 权威 settings 计算 active ref 集合；slot 替换时原子删除旧 ref、撤销 broker lease并清缓存。仅对 active refs 续租；broker 增加独立到期清扫并在 shutdown/revoke 时清零全部 buffer。

#### I6 — 私有 credential route 没有强制绑定 loopback

**证据：**

- `CONFIG.health_host` 可由环境变量任意设置：`backend/config.py:138`。
- 包含 `/internal/credential-lease` 的同一 HTTP server 直接绑定该 host：`backend/server.py:10575-10578`。

**影响：** 双 token、body 限制和无 CORS 是有效纵深防御，但显式“loopback broker”不变量取决于部署配置；误配时 credential-bearing listener 会暴露到非 loopback 接口。

**防御性修复：** 将 credential ingress 放到独立、硬编码 loopback 的 listener（或更合适的本地 IPC）。若必须复用 health server，broker 启用时对解析后的 bind address 做 loopback 断言，非 loopback 直接拒绝启动。

### Minor

#### M1 — vault 对单条损坏记录静默过滤，后续保存会永久丢弃

**证据：** `electron/main.ts:693-705` 只验证 vault 根是 object；`electron/secretVault.ts:66-78` 对内部非法记录执行 `filter`，没有报错。下一次 store/delete 会把过滤后的集合覆盖写回。

**影响：** 与报告所称“vault corruption aborts”不一致；部分损坏会变成静默凭据丢失，诊断困难。

**防御性修复：** 对整个 vault 做原子 schema/base64/record 校验，任一记录非法就 fail closed 并保留原文件（可复制到受保护 quarantine）；不要在构造器中静默过滤。

## Focused verification notes

未连接外部服务。纯本地、内存内检查得到以下防御性结论：

- 不一致的本地 MCP 审批 identity 与实际 server/tool identity 未被拒绝。
- 空对象形式的 MCP prompts capability 未被拒绝。
- 公开 tool activity 字段及 Provider diagnostic redactor 均可保留计划内 PII。
- Provider binding 可接受 master-key purpose。
- 未安装 execution boundary 的 `ToolRegistry` 会调用 handler。

这些结果与上述静态控制流证据一致；无需依赖报告中既有全量测试的通过与否。

## Code Quality

**Verdict: FAIL / Needs fixes**

代码在 Task Runtime 原子性、typed contracts、HTTP/MCP framing、main-only safeStorage 和测试覆盖方面有明显优点；但关键安全组件被实现成彼此孤立、可选或只在测试中使用的库对象，实际生产 Provider/legacy/public-event 链路仍有旁路。相同 PII/secret 规则又在多个模块以不同正则重复实现，直接造成政策漂移。以上均属于架构边界缺陷，不是注释、命名或补少量单测即可修复的问题。

## Final Assessment

- **Spec Compliance:** FAIL
- **Code Quality:** FAIL / Needs fixes
- **Critical: 5**
- **Important: 6**
- **Minor: 1**
- **Overall: FAIL**（仅 `0 Critical / 0 Important` 才可 PASS）

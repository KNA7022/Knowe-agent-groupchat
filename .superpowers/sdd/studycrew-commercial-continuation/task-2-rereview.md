# Task 2 Round 1 独立防御性复验

- 复验范围：`PREVIOUS=2e72e99a81194fa30f8d42d30763b55f6efbb0af`，`HEAD=fcbfd38c030635053ff04996fa085fc1152cb3f6`。
- 必读材料：`task-2-brief.md`、`task-2-report.md`、前次 `task-2-review.md`、`review-round1-2e72e99..fcbfd38.diff`（7,661 行）均已完整阅读。
- 方法：静态追踪生产 call graph，并做少量纯本地、内存内合同检查；未发网络请求，未修改生产文件或测试，未提交。
- 工作树基线：复验前已有且全程保留 `?? skills/`；本报告是唯一复验产物。

## 结论

- **Spec Compliance：FAIL**
- **Code Quality：FAIL / Needs fixes**
- **Critical：2**
- **Important：8**
- **Minor：1**
- **Overall：FAIL**（只有 `0 Critical / 0 Important` 才可 PASS）

Round 1 确实建立了大量有效的失败关闭措施，但两条核心不变量仍未成立：Provider Gateway 可通过生产类暴露的“test transport”开关整体跳过授权；MCP 审批 identity 校验的是 registry config，却没有绑定最终执行的 transport/effect。两者都使审计所声称的 effect 与实际 effect 不能可靠对应。

## 前次 finding 逐项状态

| 前次 finding | 状态 | Round 1 复验结论 |
|---|---|---|
| C1 Provider 生产旁路 | **PARTIALLY ADDRESSED** | 普通生产调用方已迁入 Gateway，默认直连 client 也会拒绝；但 ungoverned transport seam 可整体跳过 registry/DLP/预算/审批/audit，且 model、最终 body 与预算仍不精确。残留含 Critical。 |
| C2 MCP 授权错绑 | **PARTIALLY ADDRESSED** | capability identity 已包含 server/transport/endpoint/operation/name-or-URI/schema digest，arguments 也按 frozen schema 校验；但 invoke/read 最终接受外部 transport/effect，实际 effect 未绑定该 identity。残留含 Critical。 |
| C3 Shell/stdio 无 OS enforcement | **PARTIALLY ADDRESSED** | 当前生产 call graph 在没有 trusted marker 时确实于 effect 前拒绝，且没有生产 `trusted_sandbox=True` 注册；但 marker 只绑定 tool name，`invoke_with_effect` 可替换被标记的 executor，尚不是可验证的 sandbox-to-effect 绑定。 |
| C4 Secret purpose/audience/endpoint | **PARTIALLY ADDRESSED** | Provider credential 的 kind/audience/purpose/HTTPS endpoint/transport 逐字段绑定已修复，Provider resolver 也拒绝 master key；但 main/vault/bridge 已丢失 brief 明确要求的 `master_key` 类型，slot intent 仍由 renderer 字符串表达而非 main 的封闭 catalog 派生。 |
| C5 public activity/provider diagnostics | **ADDRESSED** | activity 只发布固定 tool name；Provider diagnostics 复用结构化 DLP，并覆盖前次计划内 PII/credential/body/path 等证据。 |
| I1 MCP prompt/JSON-RPC/schema/resource | **PARTIALLY ADDRESSED** | prompt key presence、共享 envelope validator、schema/metadata 深验已明显加强；协议版本、strict id、可变 discovery schema、tool result 与 blob 内容仍未闭合。 |
| I2 effect 终态公开审计 | **PARTIALLY ADDRESSED** | Tool claim/complete/fail/recovery 的 invocation 更新与 RunEvent 已同事务；新增 Provider effect 仍只有 effect 前 `authorized` event，普通请求没有 terminal event/实际 usage。 |
| I3 ToolRegistry fail-open | **ADDRESSED** | `ToolRegistry()` 默认拒绝；只有显式 `unsafe_test_mode=True` 或已安装 boundary 才会执行。 |
| I4 Python plaintext/env/migration | **PARTIALLY ADDRESSED** | dotenv、已知 Python provider key 字段和 migration fail-open 已修复；Electron 仍复制全部 `process.env` 后用不完备的 suffix denylist 删除 secret。 |
| I5 SecretRef/lease 生命周期 | **PARTIALLY ADDRESSED** | active-ref 续租、独立 expiry sweeper、revoke/close 清零已修复；slot replacement 仍依赖 240 秒 reconcile，且某些 inactive ref 可保留 24 小时，并非替换时原子 revoke/delete。 |
| I6 broker loopback | **ADDRESSED** | broker token 启用时，listener 启动前解析并强制 loopback；非 loopback 配置拒绝启动。 |
| M1 vault 损坏记录静默过滤 | **PARTIALLY ADDRESSED** | 明显非法 record 会使整个 load 失败且不再静默 filter；但未拒绝未知字段，也未在 load 时验证 ciphertext 可解密，尚未达到报告所称的完整原子 ciphertext/schema validation。 |

## Critical

### C-R1 — Provider 的 ungoverned seam 可绕过整个生产策略边界

**内部证据：**

- `backend/commercial_gateway.py:1944,1993-1994` 将 `allow_ungoverned_test_transport` 暴露在生产 `ProviderGatewayClient` 上，并在为真时直接跳过 authority、registry、DLP、预算、审批与 audit。
- 同一对象仍以 `_gateway_managed=True` 创建实际 transport：`backend/commercial_gateway.py:1955-1967`；`backend/commercial_gateway.py:1969-1970` 又把未声明属性全量代理给底层 client。
- `backend/knowe_core/provider_client.py:202-228` 将任意 `client_factory` 自动视为 gateway-managed，并没有把它约束为不可联网的 fake/mock transport。
- 生产 Agent 类会仅凭存在任意 factory 自动开启该 seam：`backend/agents/deepseek.py:531-550`、`backend/agents/zinnia.py:1294-1313`；`ProjectEngine` 本身也保留该 factory 注入面（`backend/engine.py:1692,8419`）。

**影响：** 该代码路径上的真实 Provider 请求不再受可信 endpoint/model、最终请求 DLP、Task Runtime budget、fresh approval/grant 和公开审计约束，因此“所有生产 Provider 出站经过同一权威”仍不能成立。

**防御性修复：** 从生产 Gateway/Agent 构造器删除 ungoverned boolean 和底层全量 delegation。测试使用独立、不可联网的 fake transport 类型；底层每次 send 都必须消费 Gateway 针对同一冻结 request bytes 签发的内部逐调用 capability，而不是仅在构造时设置可伪造布尔状态。

### C-R2 — MCP capability identity 未绑定最终执行的 transport/effect

**内部证据：**

- identity 本身已正确覆盖 server、transport、canonical endpoint、operation、remote name/URI 和 schema digest：`backend/commercial_gateway.py:900-925`。
- 但 `handshake(server_id, transport)` 不验证传入 transport 属于该 server config：`backend/commercial_gateway.py:1435-1449`。
- tool invoke 在校验 registry 中的 config/descriptor 后，最终调用外部传入的任意 transport：`backend/commercial_gateway.py:1559-1591`。
- resource read 更直接接受外部 effect callback：`backend/commercial_gateway.py:1593-1622`。

**影响：** approval/grant/audit 中的 MCP server、endpoint 与 operation identity 不能证明实际调用的是同一个 peer/effect；核心 grant-to-effect 绑定仍可错位。

**防御性修复：** 由 `McpRegistry` 按注册 config 自己构造、持有并密封 transport；`invoke/read_resource` 不接受外部 transport/effect。每次 send 前从密封 transport 重新核对 exact canonical endpoint/session identity，并把该不可变 identity 放入与 grant 相同的 operation scope。

## Important

### I-R1 — Provider registry、最终 request 与 token/cost 预算不是可信的逐模型合同（关联 C1）

**内部证据：**

- 生产 registry 对每个 provider 统一注册 `models=("*",)`、`PrivacyTier.STANDARD`、`estimated_cost_usd=0.01`：`backend/commercial_gateway.py:1902-1914`；通配 model 在 `backend/commercial_gateway.py:1760-1773` 被接受。
- Gateway 扫描的是另行构造的 `{messages, tools, extra_body}`：`backend/commercial_gateway.py:1972-2018`；实际 transport 随后另建并可由 `extra_body` 覆盖最终 body（`backend/knowe_core/provider_client.py:441-472`），不是由 Gateway 冻结并发送同一份 bytes。
- `max_tokens=None` 时 output reservation 为零：`backend/commercial_gateway.py:2056-2066`；生产 Worker 明确使用该值（`backend/worker_gateway_runtime.py:297-303`），多个 Agent/aux 调用也不传 output limit。
- effect 后没有按 Provider usage/attempt 追补 Task Runtime token/cost。纯本地 MockTransport 合同中，响应报告 `50,010` tokens，而 Task Runtime 只记了 `20` tokens 和固定 `$0.01`。

**影响：** renderer 所选 model 未获得逐模型 privacy/price 预授权；缺省输出、实际 usage 与 retry 可超过 run 的 token/cost 剩余额度，预算耗尽测试不能证明真实调用受限。

**防御性修复：** 使用 exact provider+model registry，并记录逐模型 capability/privacy/pricing；Gateway 构建、冻结、扫描并发送同一最终 request；无显式 output limit 时按剩余额度施加 backend cap，effect 前原子预留最坏情况，effect 后按每次 attempt 的可信 usage 对账并释放/追补差额。

### I-R2 — trusted sandbox marker 未绑定 exact executor（关联 C3）

**内部证据：**

- 当前缺 marker 时的 fail-before-effect 是有效的：`backend/commercial_gateway.py:305-317`；MCP stdio 也要求显式 factory+test seam（`backend/commercial_gateway.py:728-747`）。
- 但 `trusted_sandbox=True` 只把 tool name 加入集合：`backend/commercial_gateway.py:230-244`。
- `_invoke`/`invoke_with_effect` 允许在通过该 marker 后用任意 effect override 替换注册 executor：`backend/commercial_gateway.py:305-365`。

**影响：** 一旦接入生产 sandbox，仅凭名称 marker 不能保证最终 process effect 仍由已验证的 OS sandbox executor 执行；授权 metadata 可能再次与实际进程边界分离。

**防御性修复：** 将 sandbox attestation、descriptor 和 exact executor 绑定为不可替换的注册对象；Shell 禁止 effect override。测试 seam 应是独立 fake 类型且不能启动 OS process。

### I-R3 — Secret main 合同缺少 master-key 与封闭 slot catalog（关联 C4）

**内部证据：**

- `electron/secretVault.ts:17-30` 的 durable record 只允许 `provider_credential/provider_gateway`；`src/shared/bridge.ts:37-75` 的公开 ref/store/lease contract 同样没有 `master_key` 分支。
- `electron/secretVault.ts:45,142-176` 接受 renderer 提交的任意语法合法 slot/provider/endpoint/transport，再由 main 规范化并派生 purpose；main 没有以封闭 slot catalog 解析 kind/audience/合法 provider binding。
- 现有 Provider 路径的 exact purpose、HTTPS endpoint、transport 和 audience 核验是有效修复；这里不是说它仍可跨 endpoint 复用，而是 brief 的完整 main-owned secret type/slot contract尚未实现。

**影响：** brief 明确要求由 safeStorage 保护的 master keys 没有可用类型合同；renderer 仍表达安全元数据意图，增加未来 consumer 接线时的权限漂移风险。报告第 304 行“`master_key` 已受 SecretVault 合同保护”与代码不符。

**防御性修复：** 在 main 中建立不可扩展的 slot catalog，用 discriminated union 同时覆盖 provider credential 与 master key；renderer 只提交 secret value 和枚举 slot intent，main 根据 catalog 派生 kind/audience/purpose/provider/HTTPS endpoint/transport，Python consumer 分类型精确兑换。

### I-R4 — MCP schema/protocol/result 内容仍未形成不可变、严格合同（关联 C2/I1）

**内部证据：**

- `accept_discovery` 把内部 `accepted/tool_records/schema` 原对象同时存储并返回：`backend/commercial_gateway.py:1417-1433`；后续 validator 使用该可变 schema，却仍比较原 identity digest（`backend/commercial_gateway.py:1492-1500`）。
- handshake 只要求任意非空且不超过 40 字符的 `protocolVersion`，也不强制 `capabilities` 为 Mapping：`backend/commercial_gateway.py:1435-1449`。
- JSON-RPC id 直接使用 Python equality：`backend/commercial_gateway.py:487-503`，没有排除 boolean id 与 integer request id 的类型混同。
- tool result 在 `backend/commercial_gateway.py:1588-1591` 原样返回，没有 CallToolResult 的 exact shape/size/content/prompt/DLP validation。
- resource blob 只做 base64 和总大小验证，只有 `text` 分支执行 prompt/DLP：`backend/commercial_gateway.py:1536-1556`。纯本地合同确认不受支持的 protocol version，以及 `text/plain` 的 base64 文本内容都会被接受。

**影响：** discovery 后的旧 digest 可与实际 validator schema 脱节；协议语义不明确；未检查的 tool/blob 文本可进入后续模型上下文，未满足恶意 MCP prompt/content fail closed。

**防御性修复：** 内部持有 canonical bytes 与 deep-frozen schema，仅向调用方返回深拷贝的只读摘要；protocol version 必须精确属于支持集合，capability/envelope/id 做严格类型检查；对 CallToolResult/resource content 执行 exact schema、节点/字节界限与所有文本（含 text MIME decoded blob）的 prompt/DLP policy，其他二进制必须经显式 typed consumer。

### I-R5 — 普通 Provider effect 没有终态公开审计（关联 I2）

**内部证据：**

- `backend/commercial_gateway.py:2074-2089` 只在网络 effect 前写 `provider_egress: authorized`。
- `backend/commercial_gateway.py:2092-2105` 只有 sensitive DLP 分支创建了 invocation id 时才写通用 tool terminal；普通 Provider 请求直接返回，不产生 completed/failed terminal。
- `backend/task_runtime/contracts.py:77-79` 的 provider audit 白名单没有 purpose/risk/start/completion/duration/error_type，`usage` 也是 effect 前预算累计值而非本次实际 usage。

**影响：** public audit 不能证明普通 Provider 请求是否执行、何时结束、实际 token/cost 或 typed failure，违反“每个 side-effect invocation”终态审计合同。

**防御性修复：** 每次 Provider request 都创建 opaque invocation，原子记录 claimed 与 completed/failed；terminal payload 只含 purpose/risk/timing/duration/actual token-cost/typed error。将 effect 状态和 actual-usage 对账放入同一 Task Runtime transaction。

### I-R6 — Electron backend env 的 suffix denylist 不能保证 Python 只收到短 lease（关联 I4）

**内部证据：**

- `electron/main.ts:940-961` 先复制整个 `process.env`，再仅删除以 `API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|AUTH_TOKEN|MASTER_KEY|SECRET_KEY|PASSWORD` 结尾的变量。
- 纯本地规则检查确认常见 credential 名 `AZURE_OPENAI_KEY`、`OPENAI_API_TOKEN`、`AWS_SECRET_ACCESS_KEY` 以及 credential file/path 变量不会被该规则删除；已知 `DEEPSEEK_API_KEY` 会被删除。

**影响：** 长生命周期 provider/cloud credential 或 credential file location 仍可能被继承到 Python 环境，“Python receives only purpose-bound short-lived credentials”没有得到进程边界保证。

**防御性修复：** 不复制再 denylist；从非敏感运行参数建立显式 env allowlist，只加入固定 runtime/config 字段。对 provider/master/cloud credential 名与 credential file/path 做启动前拒绝测试。

### I-R7 — Secret replacement/revocation 仍是 eventual，不是原子的（关联 I5）

**内部证据：**

- 新 secret 创建和 lease 后直接返回 wire，未在 settings apply acknowledgement 前 revoke/delete superseded ref：`src/store/settings.ts:300-345`。
- main 只按 240 秒 timer refresh/reconcile：`electron/main.ts:864-911`。
- inactive record 仅在存在同 slot 且自己更旧，或年龄超过 24 小时时才进入 stale：`electron/secretVault.ts:203-230`。

**影响：** 正常替换后旧 durable ciphertext 和既发 lease 仍有数分钟窗口；test/apply 失败等 inactive 新 ref 可保留 24 小时。这不等同于无限续租，但仍未达到报告声称的“slot 替换先撤销/删除旧 ref”。

**防御性修复：** settings apply 由 main/backend 返回 exact active generation 与 superseded refs；在确认新 binding 可用的同一替换协议中先撤销旧 broker lease、删除旧 ciphertext，再 acknowledgement。禁止已 supersede generation 回滚成为 active。

### I-R8 — Settings Provider probe 缺少 Task Runtime authority，正常路径必然失败（新发现）

**内部证据：**

- `backend/runtime_settings.py:1020-1045` 的 probe 使用普通 `ProviderGatewayClient`，没有提供 authority。
- Gateway 在无当前 `LegacyToolAuthority` 时于网络前拒绝：`backend/commercial_gateway.py:1995-1997`。
- HTTP handler 只是从普通 settings 请求调用 `asyncio.to_thread(runtime_settings.test_binding, ...)`：`backend/server.py:9781-9801`，没有建立 Task Runtime group/task/run authority。

**影响：** Settings 的连接测试无法完成其最小 Provider 验证功能；报告所称 settings probe 已接入并可用不成立。保存配置虽然没有被该 probe gate 阻断，但 UI 会稳定报告 governed probe unavailable。

**防御性修复：** 为 Settings test 建立 backend-authored、严格一回合/一 token/固定目的/固定 endpoint 的短命 Task Runtime run，沿同一 Gateway 留下完整 audit；不要以恢复可用性为由开启 ungoverned seam。

## Minor

### M-R1 — vault load 未验证 exact schema 与 ciphertext 可解密（关联 M1）

**内部证据：** `electron/secretVault.ts:106-139` 校验已知字段与 base64 形式后直接保存 `{...loaded}`；未拒绝未知字段，也未调用 `safeStorage.decryptString` 验证 ciphertext。解密直到 `issueLease` 的 `electron/secretVault.ts:258-266` 才发生。

**影响：** base64 合法但损坏的 ciphertext 不会在启动时原子 fail closed；未知字段会在后续 persist 中原样保留。明显畸形 record 的静默过滤问题已修复，但报告所称完整 ciphertext/schema validation 尚未实现。

**防御性修复：** load 时对 exact key set 做 runtime schema 校验并拒绝额外字段；在发布 `records` 前用临时 buffer 验证每条 ciphertext 可解密，随后清零临时内容。任一失败保留原文件并中止 vault 初始化。

## 已确认关闭的防御边界

- 默认 `ToolRegistry()` 已 fail closed：`backend/knowe_core/tool_registry.py:89-105,145-155`。
- 当前生产没有 trusted sandbox marker 时，Shell/MCP stdio 均在 effect 前拒绝；没有把普通 subprocess 伪装成 OS sandbox。
- Provider credential 已绑定 exact slot/provider/canonical HTTPS endpoint/transport/kind/audience/purpose，Python Provider resolver 不接受 master-key lease。
- broker active-ref 未知时不续租全部历史 refs；Python 具有独立 expiry sweeper、explicit revoke 与 shutdown clear。
- credential broker listener 在启动前强制 loopback：`backend/server.py:7004-7033,10591`。
- Tool claim/complete/fail/recovery-blocked 的状态与公开终态 RunEvent 已由同一 SQLite transaction 更新。
- public activity 不再拼接 path/query/url/command，Provider diagnostic 对前次计划内 PII/credential 复用统一 DLP redactor。

## 聚焦验证

- `python -m pytest backend/tests/test_task2_round1_security.py -q -p no:cacheprovider`：**15 passed in 0.88s**。
- 纯本地 MCP 合同：不受支持 protocol version 被接受；`text/plain` base64 内容未进入 prompt/DLP 分支。
- 纯本地 Provider MockTransport 合同：响应 `50,010` tokens，Task Runtime 只记 `20` tokens 与固定 `$0.01`。
- 纯本地 env 规则检查：已知 `DEEPSEEK_API_KEY` 被删除，但多类常见 provider/cloud credential 名未被删除。

以上通过项证明新增防线的一部分有效，但既有 Round 1 测试没有覆盖实际 transport/effect identity、不可联网 test seam、真实 output budget reconciliation 和终态 Provider audit，因此不能据此给出 PASS。

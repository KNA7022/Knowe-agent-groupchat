# Task 2 交付报告：Provider / Tool / MCP / PII / Secret Gateway

## 0. 失实声明更正（2026-09-16，阶段 0 补录）

`task-2-rereview.md`（独立复审，结论 FAIL / 2 Critical / 8 Important）之后，另有独立只读审计
对本文的三处自我声明做了逐行核对，**结论是它们与代码不符**。此处按"保留原表述 + 就地更正"
的原则补录，不改写历史声称。

| 位置 | 原文声称 | 代码事实 | 对应发现 |
|---|---|---|---|
| §2.2 末（原第 69 行） | "底层 `ProviderClient` 的真实网络模式离开 Gateway 会确定性拒绝，只有显式注入 `MockTransport` 的 test seam 可独立运行。" | **不成立**。`backend/knowe_core/provider_client.py:228` 把"存在任意 `client_factory`"直接判定为 `_gateway_managed`，并不校验它是否是不可联网的 fake。实测 `ProviderClient(base_url="https://api.deepseek.com", client_factory=lambda: httpx.AsyncClient())`（真实 client、无 MockTransport）**通过** `_assert_gateway_managed`。既有测试只覆盖了"不传 factory 时抛 PermissionError"这一半。 | **C-R1** |
| §10 第 5 条（原第 304 行） | "`master_key` 已受 SecretVault 合同保护，但本范围内没有上层 master-key consumer。" | **不成立**。`electron/secretVault.ts:17-30` 的 `kind`/`audience` 是写死的单值（`'provider_credential'` / `'provider_gateway'`），`src/shared/bridge.ts:37-75` 同样没有 master-key 分支，**main 侧不存在 master-key 类型、校验或存储路径**。反向的 Python broker 分支（`backend/commercial_gateway.py:2230,2250-2256`）保留着 `kind="master_key"` 却无任何调用者。准确表述是：**master-key 类型合同未实现**（有消费者、无生产者）。 | **I-R3** |
| §5 I4 行（原第 130 行） | "Electron spawn 过滤 secret-looking env" | **不足以支撑该表结论**。`electron/main.ts:940-948` 是 `{...process.env}` 全量复制 + 7 类**后缀** denylist（`API_KEY\|ACCESS_TOKEN\|REFRESH_TOKEN\|AUTH_TOKEN\|MASTER_KEY\|SECRET_KEY\|PASSWORD`）。实测未删除：`AZURE_OPENAI_KEY`、`OPENAI_API_TOKEN`、`AWS_SECRET_ACCESS_KEY`、`AWS_ACCESS_KEY_ID`、`AZURE_CLIENT_SECRET`、`GOOGLE_APPLICATION_CREDENTIALS`、`AWS_SHARED_CREDENTIALS_FILE`、`HF_TOKEN`、`GITHUB_TOKEN`、`OPENAI_API_KEY_FILE`。 | **I-R6** |

**同时更正本文 §5 C1 行的措辞**："真实底层 `ProviderClient` 离开 Gateway 拒绝"应改为
"真实底层 `ProviderClient` 在缺少 purpose-bound lease 时于网络前拒绝，但经 `client_factory`
注入的真实网络 client 会被误判为受管 transport"（这正是 C-R1 的缺口）。

**并说明 §8.1 验证集的覆盖边界**：该聚焦集（76 passed）**不覆盖** `runtime_settings` 的
Provider probe。`backend/tests` 全仓 grep `_probe_once|test_binding|governed Provider probe unavailable`
**无任何匹配**，即 I-R8 所描述的"Settings 连接测试必然失败"既无红测也无绿测，把该文件计入
"settings 已接网关"的证据属于名不副实。

> 未修改的正文声称保持原样以便追溯；凡与上表冲突者，**以上表为准**。

## 1. 结论与范围

Task 2 已在 `feature/studycrew-foundation` 上完成。实现以既有 Task Runtime 为唯一授权与审计权威，没有新建第二套 approval/grant 状态机。Renderer 和模型输入都只能提出请求，不能自行扩权。

本任务没有启动 RAG、OCR 或 Mem0，也没有修改 `skills/`。

Round 1 独立验收指出的 5 Critical / 6 Important / 1 Minor 已逐项处置；本报告后文的“Round 1 修复矩阵”和最终验证结果覆盖并取代首轮提交时的旧测试数字。安全门槛内未保留兼容旁路。

## 2. 架构与权威边界

### 2.1 统一 Tool Gateway

```text
model / renderer / legacy caller (untrusted request)
                    |
                    v
backend ToolDescriptor maximum envelope
                    |
        canonical path/host/argv + DLP
                    |
                    v
Task Runtime prepare_tool
  - durable group/run/task ownership
  - exact purpose/scope/grant binding
  - atomic single-use grant consumption
  - idempotency + budget accounting
                    |
                    v
atomic claim -> effect boundary -> typed terminal audit
```

`ToolExecutionRequest` 将 Shell 的 executable、顺序敏感 argv、cwd、read/write roots、network hosts 和 child-process 能力编译为精确 scope。`ToolGateway` 只允许 renderer/model 申请 `ToolDescriptor` 的子集；路径先绝对化和解析，域名按精确规则或显式子域通配符检查。

这些字段是授权合同，不虚假宣称为 OS sandbox。当前 Windows 交付未提供可验证的 AppContainer/restricted-token + Job Object 隔离，因此生产 Shell 和 MCP stdio 在 effect 前 fail closed。只有测试或部署方明确注入并标记的 `trusted_sandbox` / `trusted_test_seam` executor 可以越过这一可用性闸门；默认实现不会启动普通进程。

高风险副作用必须使用 Task Runtime 的 fresh approval + single-use `PermissionGrant`。Grant 同时绑定 `group_id` / `task_id` / `run_id` / tool / purpose / operation scope / expiry，在创建 `ToolInvocation` 的同一 SQLite transaction 中消费。高风险 invocation 固定 `replay_allowed=false`，进程重启后不自动重放。

### 2.2 Secret / Provider 流

```text
renderer transient input
      | typed IPC (trusted webContents only)
      v
Electron main SecretVault -> safeStorage -> encrypted secrets.v1.json
      | decrypt only while issuing <=300s lease
      v
main-only loopback broker token (no CORS, bounded body)
      v
Python SecretLeaseBroker (memory only, purpose-bound, rotated)
      | request-local resolver
      v
ProviderClient Authorization header for this request only
```

- Electron main 是唯一 import/call `safeStorage` 的进程。持久文件只包含 OS-encrypted ciphertext 和公开元数据。
- `safeStorage.isEncryptionAvailable() === false` 时 store/lease fail closed；密钥库损坏时启动中止，不静默覆盖。
- Renderer 的 get 只返回 `SecretRef` 元数据；lease IPC 只返回 `lease_id/secret_ref/purpose/expires_at`，不返回 plaintext 或 durable ciphertext。
- Main 每 240 秒旋转 300 秒 lease，使用与 renderer 可见 runtime token 不同的 256-bit broker token。私有 route 无 CORS、不接受 transfer encoding、body 上限 64 KiB。
- Python broker 无持久化 API，只保存可清零 `bytearray`，严格比对 purpose/expiry，同 SecretRef+purpose 新 lease 会清理旧 lease。
- `ProviderClient` 在每次 egress 时调用 credential resolver，对象不保留凭据或 Authorization header。
- 旧 `settings.json` 的 `api_key` 在加载时丢弃并立即单向重写；Python 设置只持久 `secret_ref` + `credential_purpose`。
- 生产 Provider 出口已移除 `DEEPSEEK_API_KEY` 明文环境回退。没有 purpose-bound lease 时在网络前 fail closed；legacy adapter 不再从 `source.api_key` 重建 client。
- Renderer 同时发起 test/apply 时只按 SHA-256 身份复用同一 in-flight `SecretRef`，不缓存 plaintext；provider/base/transport/purpose 任一改变都会产生新引用。

`ModelCapabilityRegistry` 对 failover 执行三重限制：candidate 必须 pre-authorized，privacy protection rank 不得低于 primary，估算成本不得超过剩余预算。当前生产没有自动 failover 调用点，因此也没有绕过 registry 的隐式降级。

普通模型、辅助模型、知识图谱/记忆摘要、视觉和设置探测现在都只能构造 `ProviderGatewayClient`。Gateway 从后端可信 capability registry 精确核对 provider/model/HTTPS endpoint/transport，扫描最终序列化 request body，在网络 effect 前通过当前 Task Runtime 扣减 model-turn/token/cost budget 并写结构化 provider audit；敏感请求需要同一 run/task/group 上 fresh、purpose-bound 的 `provider_egress` approval/grant。底层 `ProviderClient` 的真实网络模式离开 Gateway 会确定性拒绝，只有显式注入 `MockTransport` 的 test seam 可独立运行。

### 2.3 PII / DLP / 审计

`PiiDlpGateway` 覆盖中文学号、18 位身份证、中国大陆手机号、邮箱、成绩/绩点/排名、个人课表关联和私人笔记。对 MCP 和任何 network-capable tool，扫描内容由 backend 从结构化 arguments/argv 派生，调用方不能通过省略 `outbound_payload` 绕过。敏感出站只能是 high risk 且需 fresh approval。

公开 Tool audit 仅包含 opaque IDs、tool name、purpose、risk、timing、usage/cost 与 typed error。`tool_ledger`、provider diagnostics、runtime settings diagnostics 和 secret diagnostics 对 tool body、credential 和 raw PII 统一遮罩；MCP peer error body 从不进入公开错误。

### 2.4 MCP

MCP 实现了 registry/configuration、initialize、discovery、governed tool invocation 和 resource read：

- prompts 默认且强制禁用：从不发送 `prompts/list`，server 宣告 prompt capability 或 discovery 返回 prompts 时立即拒绝。
- discovery 最多 16 页，cursor 去重/限长，tool schema 必须是不超 64 KiB 的 object schema，远程 `$ref`、prompt-injection description、非预期 tool/resource 全部拒绝。
- stdio 仍按 high-risk Shell capability 表述，但生产在没有受信 sandbox executor 时 fail closed，不会调用普通 `create_subprocess_exec`。测试 transport 只能通过显式 `session_factory + trusted_test_seam` 注入。JSON-RPC 采用单行 UTF-8 framing，request/response 各不超 1 MiB，必须换行结束，response timeout 30 s，server-initiated request 会关闭 session。
- Streamable HTTP 仅 HTTPS，禁止 URL userinfo，配置域名需 allowlist，且 invocation/resource scope 必须与已注册 endpoint host 精确一致，不能换成同 allowlist 的其他主机。
- HTTP 每次请求显式 `follow_redirects=false`，任何 3xx 或 Location 都拒绝；request/streamed response 各 1 MiB，production timeout 30 s，只接受 `application/json` 或单 data event 的 `text/event-stream`，session id 限 256 个可显示 ASCII 字符。
- tool/resource capability identity 固定绑定 `server_id + transport + canonical endpoint + operation + remote name/URI + frozen schema/metadata digest`；arguments 按 frozen schema 深验，discovery/schema 变化会使旧 descriptor/grant 失效。
- JSON-RPC 使用 stdio/HTTP 共享的严格 validator，比对 version/id，拒绝 method/result ambiguity 和 server request，不展示 peer error body。任何 prompts key presence（包括 `{}`）即拒绝；schema 约束类型/复杂度、resource annotations/nested metadata/content/blob 编码都做深验和 DLP/prompt-injection 检查。

## 3. 合同与 Task Runtime 扩展

新增/扩展合同：

- `PrivacyTier`
- `ToolDescriptor`
- `ToolInvocation` (`task_id`, `grant_id`, typed `error_type`)
- `PermissionGrant` (single-use consumption metadata)
- `McpServerConfig`
- `PiiFinding`
- `DlpDecision`
- `SecretRef`

Task Runtime store 对历史 payload 执行向前兼容补字段，新 grant consumption、tool prepare/claim/complete 都使用原有 SQLite authority 与 group mutation fence。没有新建平行的 approval database 或 renderer authority。

## 4. Legacy 迁移点与可用性影响

| 旧入口 | 处理 | 现在的结果 |
|---|---|---|
| `knowe_core.ToolRegistry.execute` | 新增 execution boundary，`tool_call_id` 从 AgentLoop/Worker 传入 | 只有明确分类的 project-local read 可进入 Tool Gateway |
| `ProjectEngine` / Worker runtime | 从 durable TaskRun 重建 `LegacyToolAuthority`，不信任调用方传来的 ID | 伪造 group/task/run 无法扩权；缺 authority 时 fail closed |
| legacy file writes / patch / delete / external copy | 无完整结构化 scope adapter | 调用 handler 前阻断 |
| legacy Shell / web / browser / proposal tools | 无 authoritative exact scope | 调用 handler/process/network 前阻断 |
| Zinnia platform read/file/web/browser/create-project tools | platform turn 当前无 durable Group/Task/Run 三元组，老 Gate card 不是 Task Runtime authority | 统一 fail closed；需后续显式 Task Runtime adapter 才可恢复 |
| natural-language external roots | 删除自授权推断 | 返回空 root 集，不从聊天文本授权 |
| legacy provider `.env` plaintext / `source.api_key` adapter | 移除生产 egress 回退 | 必须 safeStorage SecretRef + purpose-bound lease，否则网络前失败 |

这些是有意的 fail-closed availability trade-off，不是兼容降级。

## 5. Round 1 修复矩阵

| Finding | 处置与可验证边界 | 可用性影响 |
|---|---|---|
| C1 Provider 生产旁路 | 建立唯一 `ProviderGatewayClient`；agent/aux/knowledge/memory/vision/settings probe 全部接入可信 capability registry、最终 body DLP、Task Runtime model budget/approval/audit；真实底层 `ProviderClient` 离开 Gateway 拒绝 | 没有 authoritative running TaskRun 的普通/辅助模型调用全部在网络前失败；custom provider 未进入可信 registry 前不可用 |
| C2 MCP 授权错绑 | immutable identity 精确包含 server/transport/canonical endpoint/operation/name-or-URI/frozen digest；arguments/resources 深验，discovery 变化使旧授权失效 | server schema 或 metadata 改动后必须重新审批 |
| C3 Shell scope 非 OS 强制 | 删除“已 sandbox”虚假保证；生产 Shell/MCP stdio 没有受信 OS sandbox executor 时 effect 前 fail closed；只有显式测试 seam 可执行 | 当前 Windows 生产 Shell 与 MCP stdio 暂不可用 |
| C4 Secret purpose 可伪造 | Electron main 从受限 slot/provider/canonical HTTPS endpoint/transport 派生 kind/audience/purpose；Python 逐字段端到端核验；provider resolver 明确拒绝 master-key | HTTP provider/custom endpoint、renderer 自造 purpose、跨 endpoint/transport lease 都不可用 |
| C5 activity/diagnostic 泄漏 | public activity 固定为 tool name；共享结构化 DLP 默认遮罩未知 body/path/query/url/command；provider diagnostic 复用同一规则 | 原始参数不再作为 UI activity label/错误 detail 展示 |
| I1 MCP 深验 | prompts key presence 即拒绝；stdio/HTTP 共用 strict JSON-RPC validator；schema 深度/节点/约束类型、resource annotations/nested metadata/content/base64 全部 bounded + prompt/DLP 检查 | 任意 regex pattern 等无法安全限时表达的远端 schema fail closed |
| I2 effect 终态 | claim/complete/fail/recovery-blocked 与 RunEvent 在同一 SQLite transaction 更新；公开 payload 仅 opaque id/purpose/risk/status/timing/usage/typed error | 无 |
| I3 Registry fail-open | `ToolRegistry()` 默认 deny-all；只有名称明确的 `unsafe_test_mode=True` 或显式 execution boundary 可执行 | 未装配 Task Runtime boundary 的嵌入式/legacy registry 不再工作 |
| I4 明文/env | 移除 dotenv 与 Python provider secret env 字段；Electron spawn 过滤 secret-looking env；legacy plaintext settings 必须成功原子清除，否则 `_loaded=False` 并阻断 | 旧明文凭据需要用户在 Settings 重新输入一次 |
| I5 SecretRef 生命周期 | slot 替换先撤销/删除旧 ref；main 只为 backend 权威 active refs 续租；reconcile/delete/revoke/expiry sweeper/shutdown 均清零 | 不活跃或后端状态未知时不续租 |
| I6 loopback | credential ingress 启动前解析 bind address 并硬断言 loopback；非 loopback 直接拒绝启动 | `health_host` 误配为公网地址时 backend 不启动 |
| M1 vault 部分损坏 | vault 全记录、base64/ciphertext/metadata 原子验证；任一坏记录使 vault fail closed，原文件不被过滤覆盖 | 损坏 vault 需人工恢复，不会静默丢记录 |

## 6. 最小界面

- Settings 设置提示凭据由系统加密且只以短 lease 提供给 backend。
- Review/Privacy 显示共享 Task Runtime approvals、purpose-bound credential lease 和 PII outbound protection。
- TypeScript 合同补充 Tool/MCP/PII/Secret 类型，preload 只暴露窄 IPC。

## 7. TDD 与攻击覆盖

先写红测再实现。关键红测证据包括：

- 伪造/cross-group/cross-task/cross-tool scope，grant 重用/过期，Shell executable/cwd/read/write/network/child-process 逃逸；
- Shell argv 重排、调用方省略 outbound payload 隐藏 PII；
- MCP malicious schema/prompt injection/unexpected discovery/redirect/oversize/bad framing/bad content-type/bad session/server request/endpoint-host swap；
- Secret IPC 错 sender/wrong purpose/expired lease/ciphertext 或 plaintext 串行化；
- Provider privacy downgrade/unapproved candidate/budget exhaustion/retry diagnostics 泄漏；
- auxiliary/provider 生产直连静态检查先得到 `1 failed in 0.30s`，统一接入 Provider Gateway 后得到 `1 passed in 0.22s`；
- MCP 畸形约束、nested resource metadata 与非法 blob 红测先得到 `2 failed, 13 deselected in 0.36s`，深验实现后 Round 1 文件得到 `15 passed in 0.53s`；
- Zinnia 无 Task Runtime authority 的 read/network/create 三类入口先得到 `3 failed`，阻断后 `3 passed in 0.29s`。

## 8. 验证命令与原样结果

### 8.1 Task 2 Round 1 最终聚焦集

```powershell
python -m pytest backend/tests/test_task2_round1_security.py backend/tests/test_commercial_security_gateway.py backend/tests/test_mcp_transports.py backend/tests/test_provider_secret_gateway.py backend/tests/test_runtime_settings_readiness.py backend/tests/test_secret_lease_ingress.py backend/tests/test_commercial_gateway_legacy.py backend/tests/test_batch4_web_browser_vision.py::test_vision_missing_key_is_a_message backend/tests/test_batch4_web_browser_vision.py::test_vision_empty_prompt -q
```

```text
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 3.75s
```

Task2 改动触及的既有 AgentLoop/registry/worker/vision/Zinnia 兼容组：

```text
61 passed in 1.62s
```

### 8.2 Task Runtime / Group 顺序 gate

```powershell
python -m pytest backend/tests/test_task_runtime.py backend/tests/test_task_runtime_deletion_authorization.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round5.py backend/tests/test_task_runtime_recovery_ownership.py backend/tests/test_task_runtime_recovery_ack.py backend/tests/test_task_runtime_rereview_round4.py backend/tests/test_task_runtime_rereview_round3.py backend/tests/test_task_runtime_quarantine_isolation.py backend/tests/test_task_runtime_live_event_bridge.py backend/tests/test_groups.py backend/tests/test_group_http.py -q
```

```text
239 passed in 76.88s (0:01:16)
```

第一次将此 gate 与 Task4B 并行运行时，`test_limit_two_overflow_never_starves_valid_confirmed_deletion` 的 4 秒 timing wait 超时，结果为 `1 failed, 238 passed in 74.84s`；该 case 立即单独重跑为 `1 passed in 1.30s`，随后上述顺序全组 239 通过，确认是验证机并发负载 flaky，不是 Task2 状态机失败。

### 8.3 Electron / renderer 安全合同

```powershell
npx vitest run electron/secretVault.test.ts electron/secretMainBoundary.test.ts src/contract/commercialGateway.test.ts src/store/settingsSecrets.test.ts
```

```text
Test Files  4 passed (4)
Tests       18 passed (18)
Duration    5.51s
```

```powershell
npm test
```

```text
Test Files  50 passed (50)
Tests       563 passed (563)
Duration    36.03s
```

### 8.4 静态、构建与 runtime gates

```powershell
npm run lint
npx tsc --noEmit -p electron/tsconfig.json
npm run electron:build
npm run preflight
npm run verify:runtime
npm run test:foundation
python -m compileall -q backend
git diff --check
```

```text
lint: PASS (0 warnings)
Electron tsc diagnostics: 0
electron:build: PASS
preflight: PASS
verify:runtime: PASS
test:foundation: 2 tests PASS; verify:runtime PASS
compileall: PASS (no output)
git diff --check: PASS
```

### 8.5 Task 4B / core 分区

```powershell
python -m pytest backend/tests/test_batch4_file_ops.py backend/tests/test_batch4_terminal.py backend/tests/test_batch4_web_browser_vision.py backend/tests/test_agent_loop_persistence.py backend/tests/test_runtime.py backend/tests/test_worker_completion_v1.py backend/tests/test_tools_knowe_worker.py -q
```

```text
4 failed, 128 passed, 5 skipped, 10 subtests passed in 8.92s
```

隔离结论：4 个均为当前 Windows 验证环境问题，不是 Task 2 路径：1 个 terminal case 假定 POSIX `bash` loop；3 个 browser case 因 Playwright Chromium 未安装/损坏而失败。Task 2 的 Shell/MCP 测试使用结构化假 effect/session，全部通过。

### 8.6 仓库历史 TypeScript 基线隔离

```powershell
npm run typecheck
```

root `tsc --noEmit` 仍报历史诊断，包括 `v04.test.tsx`、`components/__testkit.ts`、`DateCalendar.tsx`、`DirectoryRecoveryCard.tsx`、`TokenUsagePanel.tsx`、`PdfPreview.tsx`、`state.ts`、`store.ts`、`toolActivity.ts` 和 socket/corridor 旧 target-lib 问题。本任务的 changed-file 过滤结果为：

```text
Task 2 changed TypeScript diagnostics: 0
Electron tsc diagnostics: 0
```

因根 `tsc` 先失败，`npm run typecheck` 的 `&&` 不会继续 Electron tsc；因此另行直接运行 Electron tsc 并得到 0 diagnostics。根 tsc 输出中没有 Task 2 changed file。

### 8.7 仓库历史 backend 基线隔离

```powershell
python -m pytest backend/tests -q
```

```text
collection: 4 errors in 1.61s
```

4 个 collection error 来自历史 `ACTION_CONTRACT` import。排除这 4 个历史 collection blocker 后的广泛 checkpoint 原样结果：

```text
79 failed, 911 passed, 5 skipped, 16 errors, 10 subtests passed in 216.13s (0:03:36)
```

该 checkpoint 中 Task2 真实兼容失败集中于旧 handler-level tests 未显式声明 ToolRegistry test seam、vision binding 新合同及 Zinnia platform effect 的旧可用性预期。生产默认 deny 没有放宽；测试改为显式受信 boundary/新合同/预期 fail-closed 后，相关六文件组顺序得到 `61 passed in 1.62s`。Task Runtime/Group 又独立得到 239 全绿，Task4B 的 4 个失败仍严格隔离为 POSIX shell/本机 Chromium 环境问题。其余 broad baseline 是既有 project-directory、旧 prompt/soul/乱码契约等失败，未扩张为 Task3 或 Task6 工作。

### 8.8 敏感日志/凭据扫描

对 BASE 至当前工作树的 34 个 changed non-test source files 的 added lines 执行 log-sink + secret/PII field 模式扫描，并对 provider credential literal 做独立扫描：

```text
Changed non-test source files: 34
Added source lines scanned: 4915
Sensitive added log sink matches: 0
Added credential literal matches: 0
```

## 9. 自审

- 授权权威：只使用 Task Runtime approval/grant/store；Gate card、renderer payload、model args 都不是 authority。
- 边界：Tool Gateway 在 effect 之前执行后端 descriptor、canonical scope、DLP、budget、grant consumption 和 atomic claim。
- 密钥：`safeStorage` 只在 main，renderer 不得 plaintext/ciphertext，Python 不持久 plaintext，生产 Provider 无明文 env 回退。
- MCP：prompts 不启用，stdio/HTTP framing、timeout、size、redirect、session、domain 与 discovery 均有恶意回归。
- 审计：公开事件无 raw body/secret/PII，provider/MCP exception 无原始 peer body。
- Legacy：能获得 durable authority 的 project-local read 已迁移；无法获得的入口全部 fail closed，并在本报告显式列出受影响功能。
- 范围：未触碰 `skills/`，未开始 Task 3/RAG/OCR/Mem0。

## 10. 已知限制 / concerns

1. Zinnia 平台 read/file/web/browser/create-project 在获得 durable Task Runtime group/task/run 适配器前不可用。
2. ProjectEngine 中无结构化 scope 的 legacy write/Shell/network/proposal tools 不可用；需逐个建立 backend-authored descriptor 才能恢复，不允许加兼容旁路。
3. 升级后的旧 plaintext `settings.json` 凭据会被丢弃；用户需在 Settings 重新输入一次，才会进入 safeStorage。`DEEPSEEK_API_KEY` 不再是生产 egress authority。
4. MCP registry/transports 为 backend 安全基础设施；本任务没有暴露 renderer MCP 管理 UI，也没有默认注册外部 server。
5. `master_key` 已受 SecretVault 合同保护，但本范围内没有上层 master-key consumer。
6. 仓库全量 backend/typecheck 仍有上文记录的历史与环境失败；Task 2 focused、changed-file diagnostics、frontend、lint、Electron build/tsc、compile/runtime gates 均已通过。

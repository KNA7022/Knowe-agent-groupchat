# Task 2 交付报告：Provider / Tool / MCP / PII / Secret Gateway

## 1. 结论与范围

Task 2 已在 `feature/studycrew-foundation` 上完成。实现以既有 Task Runtime 为唯一授权与审计权威，没有新建第二套 approval/grant 状态机。Renderer 和模型输入都只能提出请求，不能自行扩权。

本任务没有启动 RAG、OCR 或 Mem0，也没有修改 `skills/`。

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

### 2.3 PII / DLP / 审计

`PiiDlpGateway` 覆盖中文学号、18 位身份证、中国大陆手机号、邮箱、成绩/绩点/排名、个人课表关联和私人笔记。对 MCP 和任何 network-capable tool，扫描内容由 backend 从结构化 arguments/argv 派生，调用方不能通过省略 `outbound_payload` 绕过。敏感出站只能是 high risk 且需 fresh approval。

公开 Tool audit 仅包含 opaque IDs、tool name、purpose、risk、timing、usage/cost 与 typed error。`tool_ledger`、provider diagnostics、runtime settings diagnostics 和 secret diagnostics 对 tool body、credential 和 raw PII 统一遮罩；MCP peer error body 从不进入公开错误。

### 2.4 MCP

MCP 实现了 registry/configuration、initialize、discovery、governed tool invocation 和 resource read：

- prompts 默认且强制禁用：从不发送 `prompts/list`，server 宣告 prompt capability 或 discovery 返回 prompts 时立即拒绝。
- discovery 最多 16 页，cursor 去重/限长，tool schema 必须是不超 64 KiB 的 object schema，远程 `$ref`、prompt-injection description、非预期 tool/resource 全部拒绝。
- stdio 必须先作为 high-risk Shell effect 通过统一 Tool Gateway；`create_subprocess_exec` 不使用 shell，空 env，stderr 直接丢弃（可视为 0-byte 安全上限）。JSON-RPC 采用单行 UTF-8 framing，request/response 各不超 1 MiB，必须换行结束，response timeout 30 s，server-initiated request 会关闭 session。
- Streamable HTTP 仅 HTTPS，禁止 URL userinfo，配置域名需 allowlist，且 invocation/resource scope 必须与已注册 endpoint host 精确一致，不能换成同 allowlist 的其他主机。
- HTTP 每次请求显式 `follow_redirects=false`，任何 3xx 或 Location 都拒绝；request/streamed response 各 1 MiB，production timeout 30 s，只接受 `application/json` 或单 data event 的 `text/event-stream`，session id 限 256 个可显示 ASCII 字符。
- JSON-RPC 严格比对 version/id，不展示 peer error body。

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

## 5. 最小界面

- Settings 设置提示凭据由系统加密且只以短 lease 提供给 backend。
- Review/Privacy 显示共享 Task Runtime approvals、purpose-bound credential lease 和 PII outbound protection。
- TypeScript 合同补充 Tool/MCP/PII/Secret 类型，preload 只暴露窄 IPC。

## 6. TDD 与攻击覆盖

先写红测再实现。关键红测证据包括：

- 伪造/cross-group/cross-task/cross-tool scope，grant 重用/过期，Shell executable/cwd/read/write/network/child-process 逃逸；
- Shell argv 重排、调用方省略 outbound payload 隐藏 PII；
- MCP malicious schema/prompt injection/unexpected discovery/redirect/oversize/bad framing/bad content-type/bad session/server request/endpoint-host swap；
- Secret IPC 错 sender/wrong purpose/expired lease/ciphertext 或 plaintext 串行化；
- Provider privacy downgrade/unapproved candidate/budget exhaustion/retry diagnostics 泄漏；
- 生产 Provider 明文环境回退检查先得到 `1 failed`，移除 8 个出口后 `1 passed in 0.12s`；
- Zinnia 无 Task Runtime authority 的 read/network/create 三类入口先得到 `3 failed`，阻断后 `3 passed in 0.29s`。

## 7. 验证命令与原样结果

### 7.1 Task 2 + Group 最终聚焦集

```powershell
python -m pytest backend/tests/test_commercial_security_gateway.py backend/tests/test_commercial_gateway_legacy.py backend/tests/test_commercial_audit_redaction.py backend/tests/test_mcp_transports.py backend/tests/test_provider_secret_gateway.py backend/tests/test_secret_lease_ingress.py backend/tests/test_runtime_settings_readiness.py backend/tests/test_task_runtime.py backend/tests/test_groups.py backend/tests/test_group_http.py -q
```

```text
........................................................................ [ 69%]
...............................                                          [100%]
103 passed in 9.93s
```

不含 Group 的最终 Task 2 安全集：

```text
79 passed in 6.14s
```

### 7.2 Task Runtime / rereview

```powershell
python -m pytest <PowerShell 枚举的 backend/tests/test_task_runtime*.py> -q
```

```text
215 passed in 77.30s
```

```powershell
python -m pytest backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py backend/tests/test_task_runtime_rereview_round4.py -q
```

```text
37 passed in 5.77s
```

### 7.3 Electron / renderer 安全合同

```powershell
npx vitest run electron/secretVault.test.ts electron/secretMainBoundary.test.ts src/contract/commercialGateway.test.ts src/store/settingsSecrets.test.ts
```

```text
Test Files  4 passed (4)
Tests       12 passed (12)
Duration    5.18s
```

```powershell
npm test
```

```text
Test Files  50 passed (50)
Tests       557 passed (557)
Duration    35.07s
```

### 7.4 静态、构建与 runtime gates

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

### 7.5 Task 4B / core 分区

```powershell
python -m pytest backend/tests/test_batch4_file_ops.py backend/tests/test_batch4_terminal.py backend/tests/test_batch4_web_browser_vision.py backend/tests/test_agent_loop_persistence.py backend/tests/test_runtime.py backend/tests/test_worker_completion_v1.py backend/tests/test_tools_knowe_worker.py -q
```

```text
4 failed, 128 passed, 5 skipped, 10 subtests passed in 8.25s
```

隔离结论：4 个均为当前 Windows 验证环境问题，不是 Task 2 路径：1 个 terminal case 假定 POSIX `bash` loop；3 个 browser case 因 Playwright Chromium 未安装/损坏而失败。Task 2 的 Shell/MCP 测试使用结构化假 effect/session，全部通过。

### 7.6 仓库历史 TypeScript 基线隔离

```powershell
npm run typecheck
```

root `tsc --noEmit` 仍报历史诊断，包括 `v04.test.tsx`、`components/__testkit.ts`、`DateCalendar.tsx`、`DirectoryRecoveryCard.tsx`、`TokenUsagePanel.tsx`、`PdfPreview.tsx`、`state.ts`、`store.ts`、`toolActivity.ts` 和 socket/corridor 旧 target-lib 问题。本任务的 changed-file 过滤结果为：

```text
Task 2 changed TypeScript diagnostics: 0
Electron tsc diagnostics: 0
```

因根 `tsc` 先失败，`npm run typecheck` 的 `&&` 不会继续 Electron tsc；因此另行直接运行 Electron tsc 并得到 0 diagnostics。根 tsc 输出中没有 Task 2 changed file。

### 7.7 仓库历史 backend 基线隔离

```powershell
python -m pytest backend/tests -q
```

```text
collection: 4 errors in 1.61s
```

4 个 collection error 来自历史 `ACTION_CONTRACT` import；按原列表 ignore 后仍有 3 个同类 collection error（0 tests run, 1.03s）。扩展排除这些历史 collection blockers 后的广泛分区原样结果：

```text
42 failed, 910 passed, 5 skipped, 16 errors, 10 subtests passed in 127.32s
```

隔离过程在该广泛运行中识别出 3 个 Task 2 兼容回归：`server.py` 向 fake/legacy `submit_tracked` 实现无条件传入新 kwarg。已改为只向真实 `ProjectEngine` 传入后端权威，且 rereview round2/3/4 重跑为 `37 passed in 5.77s`。剩余输出是已知 create-project directory contract、旧外部路径自授权预期（Task 2 现有意 fail closed）、prompt/soul 契约以及 terminal/browser 环境问题。未将这些历史项扩展为 Task 6 工作。

### 7.8 敏感日志/凭据扫描

对 31 个 changed non-test source files 执行 log-sink + secret/PII field 模式扫描，并对 provider credential literal 做独立扫描：

```text
Changed non-test source files: 31
Sensitive log sink matches: 0
Credential literal matches: 0
```

## 8. 自审

- 授权权威：只使用 Task Runtime approval/grant/store；Gate card、renderer payload、model args 都不是 authority。
- 边界：Tool Gateway 在 effect 之前执行后端 descriptor、canonical scope、DLP、budget、grant consumption 和 atomic claim。
- 密钥：`safeStorage` 只在 main，renderer 不得 plaintext/ciphertext，Python 不持久 plaintext，生产 Provider 无明文 env 回退。
- MCP：prompts 不启用，stdio/HTTP framing、timeout、size、redirect、session、domain 与 discovery 均有恶意回归。
- 审计：公开事件无 raw body/secret/PII，provider/MCP exception 无原始 peer body。
- Legacy：能获得 durable authority 的 project-local read 已迁移；无法获得的入口全部 fail closed，并在本报告显式列出受影响功能。
- 范围：未触碰 `skills/`，未开始 Task 3/RAG/OCR/Mem0。

## 9. 已知限制 / concerns

1. Zinnia 平台 read/file/web/browser/create-project 在获得 durable Task Runtime group/task/run 适配器前不可用。
2. ProjectEngine 中无结构化 scope 的 legacy write/Shell/network/proposal tools 不可用；需逐个建立 backend-authored descriptor 才能恢复，不允许加兼容旁路。
3. 升级后的旧 plaintext `settings.json` 凭据会被丢弃；用户需在 Settings 重新输入一次，才会进入 safeStorage。`DEEPSEEK_API_KEY` 不再是生产 egress authority。
4. MCP registry/transports 为 backend 安全基础设施；本任务没有暴露 renderer MCP 管理 UI，也没有默认注册外部 server。
5. `master_key` 已受 SecretVault 合同保护，但本范围内没有上层 master-key consumer。
6. 仓库全量 backend/typecheck 仍有上文记录的历史与环境失败；Task 2 focused、changed-file diagnostics、frontend、lint、Electron build/tsc、compile/runtime gates 均已通过。

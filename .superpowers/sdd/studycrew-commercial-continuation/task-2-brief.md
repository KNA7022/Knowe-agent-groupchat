# Task 2: Add Provider, Tool, MCP, PII and secret gateways

- Add `ModelCapabilityRegistry`, `ToolDescriptor`, `ToolInvocation`, `PermissionGrant`, `McpServerConfig`, `PiiFinding`, `DlpDecision` and `SecretRef` contracts.
- Protect master keys and provider credentials through Electron `safeStorage`; Python receives only purpose-bound short-lived credentials.
- Route built-in tools and Shell through Tool Gateway. Grants bind `group_id`, `task_id`, tool, executable, cwd, read/write, network and child-process capabilities.
- Require per-call approval for destructive actions, external writes/messages, privilege expansion and sensitive outbound data.
- Support MCP stdio and Streamable HTTP; prompts disabled by default; tools/resources inspected and granted before use.
- Permit provider failover only among pre-authorized candidates when privacy does not decrease and budget remains.
- Redact logs, telemetry and diagnostic packages.

## Binding decisions

- Extend the existing Task Runtime `ApprovalRequest`, `PermissionGrant`, `ToolInvocation`, budget and event contracts; do not create a second approval authority.
- Backend remains the policy authority. Renderer configuration is untrusted input and cannot grant itself permissions.
- Electron main is the only process allowed to call `safeStorage`. Expose narrow typed IPC for store/get/delete and short-lived credential leases; never return the durable secret blob to the renderer or persist plaintext in Python.
- Built-in tools, Shell and MCP must share one Tool Gateway policy/effect path. Direct legacy execution must be migrated or blocked, not left as a bypass.
- MCP transports in this task are registry/configuration, handshake, discovery and governed invocation. MCP prompts remain disabled even when tools/resources are enabled.
- Local stdio and Shell are offline by default. Streamable HTTP and any network-capable tool require explicit host/domain authorization.
- Every side-effect invocation has an idempotency key. High-risk effects are never automatically replayed after restart.
- Public audit events contain opaque IDs, purpose, risk, timing, token/cost and typed error only; no tool body, secret or raw PII.
- Keep scope bounded to security infrastructure and minimum Review/Privacy/Settings surfaces needed to configure and inspect it. Do not start RAG, OCR or Mem0.

## Required adversarial coverage

- Forged `group_id`/`task_id`, reused grants, expired grants and cross-tool grants fail closed.
- Shell executable/cwd/read/write/network/child-process escape attempts are denied before process launch.
- MCP malicious schemas, prompt injection, unexpected tools/resources, redirects and out-of-scope network hosts are rejected or require fresh approval.
- PII detection covers the planned Chinese student identifiers and prevents unapproved outbound calls.
- Secret IPC rejects renderer-origin arbitrary reads, wrong purpose, expired lease and diagnostic/log serialization.
- Provider failover rejects privacy downgrade, unapproved provider and budget exhaustion.

## Verification

- Focused backend/Electron/frontend contract tests plus existing Task Runtime, Group and Task 4B regressions.
- Full frontend, lint, Electron TypeScript, Python compile, Node/runtime gates and scoped backend partitions.
- Record repository-wide historical TypeScript/backend baseline without expanding into Task 6.

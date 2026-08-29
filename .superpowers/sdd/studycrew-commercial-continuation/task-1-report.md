# Task 1 实现报告：关闭原子 Profile 授权（Task 4B）

## 1. 基线与范围

- 工作分支：`feature/studycrew-foundation`
- 执行基线：`f06fd383eb4bec6d9c9d70b2fddfeab7069ea9ea`
- 绑定上下文基线：`3a35d8b`
- 仅实现 Task 1；未开始 Task 2。
- 未读取或修改其他 `.superpowers/sdd` 计划目录内容。
- 工作树原有未跟踪 `skills/` 保持未修改、未暂存、未提交。

## 2. 实现结果

### 2.1 generation-consistent `ProfileBundle`

`backend/knowledge_assets.py` 新增不可变 `ProfileBundle`，在一个既有
`KnowledgeGraphManager.root_lock()` 临界区中生成以下同代数据：

- Profile 正文 `body`；
- availability / `exists`；
- `source_ids`、`course_ids` contributor proof；
- `locked` 状态；
- opaque `generation_token`。

generation token 对当前 graph revision、正文、availability、contributors 和 locked
状态做稳定 SHA-256 摘要；任一授权相关输入改变都会形成不同 token。保留
`profile_snapshot()` 兼容入口，但它现在只投影已经锁内生成的 `ProfileBundle`，不再自行拼接
正文和 contributor proof。

### 2.2 Assets availability 只由锁内 bundle 授权

`backend/knowledge_api.py` 将以下路由转为 async Profile 授权路径：

- merged `GET /api/knowledge/assets`；
- project-specific `GET /api/knowledge/{project_id}/assets`；
- detail `GET /api/knowledge/{project_id}/profile`。

Assets 行仍可从既有 general snapshot 读取，但 Profile 的 `profile_exists`、
`profile_source_ids`、`profile_course_ids` 只取自 `profile_bundle()`。general snapshot
即使在竞态中返回旧 Profile 字段，也不再有授权作用。

merged route 在 bundle/engine loop 不可用时只将 Profile fail-closed 为 unavailable；direct
Assets 和 detail Profile route 返回既有 503/504 错误语义。

### 2.3 Profile POST 锁内重新授权与 generation CAS

新增 `KnowledgeAssetManager.set_profile_authorized(...)`：

1. 在注册 engine event loop 上获取既有 per-root `root_lock`；
2. 锁内读取当前 `ProfileBundle`；
3. 锁内调用 API 传入的 scope authorization predicate；
4. 锁内重新读取 token 并与已授权 generation 比较；
5. 仅在授权仍成立且 token 相同的情况下写正文、设置 `profile_locked=true` 并保存 graph/event。

Profile POST 不再先用 unlocked general snapshot 授权后另行进入 `set_profile()`。若 generation
在授权过程中改变，返回 `409 stale_profile_generation`；若当前 generation 已是 ownerless/越界，
继续返回既有 403 scope reason。`backend/server.py` 增加 409 HTTP reason mapping。

### 2.4 deterministic concurrency tests

新增 `backend/tests/test_profile_atomic_authorization.py`，用真实 manager 与
`threading.Event` barrier 固化：

- merged/direct Assets 在旧正文读后发生 allowed → locked ownerless generation swap；
- Profile POST 在旧正文读后与 ownerless writer 并发；
- auto-compilation 从 unavailable 生成 allowed bundle；
- purge 最后一个 preference 后删除 auto Profile；
- 每个 lifecycle generation 的 token、availability、contributors、locked 状态；
- foreign registered event loop 下 direct/detail/POST 不进入 `root_lock`，merged fail-closed。

原有 detail GET generation barrier 改到稳定的正文读取接缝，继续证明正文与 contributor proof
同代；原有 ownerless、narrow contributor、strict approved-global 策略测试保持通过。

## 3. 自审

### 并发与锁

- 无锁重入：`profile_bundle()` 是取锁的 public async 入口；锁内写路径只调用不再取锁的
  `_profile_bundle_sync()` / `_set_profile_sync()`。
- 没有从 worker thread 获取或等待 `asyncio.Lock`；`to_thread` 只执行持锁期间的同步磁盘读取。
- manager writer 要么在 Profile POST 之前完成并被重新授权拒绝，要么被 POST 所持锁阻塞并在
  POST 之后成为更新 generation；旧授权 POST 不可能覆盖一个已经更新的 generation。
- generation compare 与 mutation 之间没有 `await`，并且仍处在同一个 root-lock 临界区。

### Event loop、取消与超时

- bundle route 在获取 lock 前验证 `reg.loop` 与当前 loop 相同；foreign/stopped loop 不触碰 lock。
- detail/direct 继续使用 15 秒 `profile_snapshot_timeout`；POST 继续使用 15 秒
  `override_timeout`。
- `CancelledError` 继续上抛；超时取消后的 worker thread 最多完成只读 bundle 构建，结果被丢弃，
  不执行 mutation。

### API 与策略兼容

- detail GET 的 status/body/source/course 结构未改变。
- Assets response 仍保留原字段；仅 Profile 字段的授权真源改为 bundle。
- `profile_snapshot()` 保留旧 key，并新增 locked/generation 元数据供内部调用。
- ownerless Profile 仍 fail-closed；没有新增 Profile global 例外。
- approved-global 逻辑、普通 Assets/Skillpack contributor 策略未改。
- general `snapshot()` 的旧 Profile 字段为内部兼容而保留，但所有 HTTP Profile 授权点已通过搜索
  确认不再消费它们。

### 范围检查

- 未修改 RAG、memory 或 Task 2 行为。
- 未修改前端业务代码。
- 未修改/暂存/提交 `skills/`。

## 4. 测试命令与原样结果

### 4.1 TDD 红测（实现前）

```powershell
py -3.12 -m pytest backend/tests/test_profile_atomic_authorization.py -vv
```

```text
collected 3 items
backend\tests\test_profile_atomic_authorization.py::test_assets_profile_availability_reauthorizes_after_generation_swap[merged] FAILED
backend\tests\test_profile_atomic_authorization.py::test_assets_profile_availability_reauthorizes_after_generation_swap[project] FAILED
backend\tests\test_profile_atomic_authorization.py::test_profile_post_never_overwrites_a_newer_ownerless_generation FAILED
3 failed in 0.83s
```

失败证据未改写：两个 Assets response 均为 `profile_exists is True`；POST 最终正文为
`STALE AUTHORIZED EDIT\n` 而不是已写入的 `PRIVATE OWNERLESS PROFILE\n`。

### 4.2 Knowledge 聚焦分区（最终）

```powershell
py -3.12 -m pytest backend/tests/test_knowledge_graph.py backend/tests/test_knowledge_group_scope.py backend/tests/test_profile_atomic_authorization.py -q
```

```text
.......................                                                  [100%]
23 passed in 2.54s
```

### 4.3 Task Runtime 后端分区

```powershell
$taskTests = Get-ChildItem -LiteralPath 'backend\tests' -Filter 'test_task_runtime*.py' | Sort-Object Name | ForEach-Object { $_.FullName }; & py -3.12 -m pytest @taskTests -q
```

```text
........................................................................ [ 33%]
........................................................................ [ 67%]
......................................................................   [100%]
214 passed in 69.17s (0:01:09)
```

### 4.4 Task 4A 前端回归集

```powershell
npx vitest run src/store/groupDeletionAsync.test.ts src/store/groupDeletionFence.test.ts src/store/groups.test.ts src/store/incrementalSync.test.ts src/store/knowledge.test.ts src/store/knowledgeMutationDeletion.test.ts src/store/recordsIdentity.test.ts src/store/skeletonCache.test.ts src/store/task4aRound2Adversarial.test.ts
```

```text
Test Files  9 passed (9)
     Tests  72 passed (72)
  Duration  8.94s
```

测试 stderr 仍有既有一行：

```text
[displayInfo] 未识别的 agent 前缀: w1 w1
```

### 4.5 全前端

```powershell
npm test
```

```text
Test Files  46 passed (46)
     Tests  545 passed (545)
  Duration  29.54s
```

测试过程存在既有预期 stderr（Zustand deprecation、测试内 127.0.0.1:8081
`ECONNREFUSED`、contract rejection 日志），exit code 为 0。

### 4.6 静态与运行时门禁

```powershell
npm run lint
```

```text
> studycrew-desktop@1.0.36 lint
> eslint src/ electron/ --ext .ts,.tsx --max-warnings 0

exit code 0
```

```powershell
npm run preflight
```

```text
> studycrew-desktop@1.0.36 preflight
> node scripts/preflight.mjs

✅ preflight OK
```

```powershell
npm run verify:runtime
```

```text
> studycrew-desktop@1.0.36 verify:runtime
> node scripts/verify-runtime.mjs

✅ StudyCrew runtime and Windows packaging contract verified
```

```powershell
npm run typecheck
```

```text
> studycrew-desktop@1.0.36 typecheck
> tsc --noEmit && tsc --noEmit -p electron/tsconfig.json

exit code 1
```

原样错误清单（均在未修改的 TypeScript 文件）：

```text
src/__tests__/v04.test.tsx(28,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/__tests__/v04.test.tsx(200,21): error TS2345: Argument of type 'HTMLImageElement | null' is not assignable to parameter of type 'Element | Node | Window | Document'.
src/app/v04.test.tsx(28,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/app/v04.test.tsx(200,21): error TS2345: Argument of type 'HTMLImageElement | null' is not assignable to parameter of type 'Element | Node | Window | Document'.
src/app/v05b.test.tsx(22,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; projectName: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/components/__testkit.ts(37,9): error TS2741: Property 'unread' is missing in type '{ projectId: string; projectName: string; items: Item[]; members: Member[]; banner: string | null; draft: string; }' but required in type 'Conv'.
src/components/__testkit.ts(83,28): error TS2740: Type '{ connect: () => void; disconnect: () => void; sendMessage: ... }' is missing properties from type 'SocketAPI'.
src/components/batchA.test.tsx(11,42): error TS6133: 'installSocketSpy' is declared but its value is never read.
src/components/batchC.test.tsx(123,16): error TS2741: Property 'unread' is missing in type '{ projectId: string; items: never[]; members: Member[]; banner: null; draft: string; }' but required in type 'Conv'.
src/components/batchC.test.tsx(133,9): error TS2741: Property 'unread' is missing in type '{ projectId: string; items: never[]; banner: null; draft: string; members: Member[]; }' but required in type 'Conv'.
src/components/DateCalendar.tsx(60,39): error TS2345: Argument of type '{ project_id: string; date: string | undefined; page: string; page_size: string; }' is not assignable to parameter of type 'string | Record<string, string> | URLSearchParams | string[][] | undefined'.
src/components/DateCalendar.tsx(66,48): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
src/components/DirectoryRecoveryCard.tsx(140,23): error TS2769: No overload matches this call.
src/components/FileCard.fold.test.tsx(13,1): error TS6133: 'React' is declared but its value is never read.
src/components/TokenUsagePanel.m4.test.tsx(15,1): error TS6133: 'React' is declared but its value is never read.
src/components/TokenUsagePanel.m4.test.tsx(17,18): error TS6133: 'act' is declared but its value is never read.
src/components/TokenUsagePanel.tsx(679,9): error TS6133: 'latestSavedChars' is declared but its value is never read.
src/fakeserver/FakeKnoweServer.test.ts(86,31): error TS2550: Property 'at' does not exist on type 'Json[]'.
src/observe/corridor.test.ts(47,20): error TS2550: Property 'at' does not exist on type 'CorridorEntry[]'.
src/preview/renderers/PdfPreview.tsx(10,26): error TS2307: Cannot find module 'pdfjs-dist/build/pdf.worker.min.mjs?url' or its corresponding type declarations.
src/store/displayInfo.test.ts(22,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/store/filePreview.ts(236,16): error TS6133: 'sha256Hex' is declared but its value is never read.
src/store/state.test.ts(17,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; projectName: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/store/state.ts(705,40): error TS2345: inbound event union is not assignable to parameter of type 'ActivityLike'.
src/store/state.ts(724,60): error TS2345: inbound event union is not assignable to parameter of type 'ActivityLike'.
src/store/store.ts(303,38): error TS2538: Type 'undefined' cannot be used as an index type.
src/store/store.ts(323,36): error TS2538: Type 'undefined' cannot be used as an index type.
src/store/store.ts(388,7): error TS2322: Type 'number | undefined' is not assignable to type 'number'.
src/store/store.ts(498,28): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
src/store/store.ts(1182,39): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.
src/store/store.ts(1533,74): error TS2345: Argument of type 'Conv | undefined' is not assignable to parameter of type 'Conv'.
src/store/toolActivity.ts(143,11): error TS18048: 'pending' is possibly 'undefined'.
src/store/toolActivity.ts(144,9): error TS18048: 'pending' is possibly 'undefined'.
src/store/toolActivity.ts(147,9): error TS18048: 'pending' is possibly 'undefined'.
src/store/toolActivity.ts(148,9): error TS18048: 'pending' is possibly 'undefined'.
src/store/toolActivity.ts(153,56): error TS18048: 'line' is possibly 'undefined'.
src/store/toolActivity.ts(154,19): error TS18048: 'line' is possibly 'undefined'.
src/store/v23.test.ts(26,3): error TS2741: Property 'unread' is missing in type '{ projectId: string; projectName: string; items: never[]; members: never[]; banner: null; draft: string; }' but required in type 'Conv'.
src/store/v23.test.ts(112,12): error TS2532: Object is possibly 'undefined'.
src/store/v23.test.ts(251,12): error TS2532: Object is possibly 'undefined'.
src/transport/socket.wars.test.ts: multiple TS2550 errors: Property 'at' does not exist on type 'ConnStatus[]' / 'CorridorEntry[]'.
```

Task 1 未修改任何 TypeScript 文件，因此按要求只记录该既有全库 typecheck 基线。

### 4.7 全后端基线

```powershell
py -3.12 -m pytest backend/tests -q
```

```text
ERROR backend\tests\test_v10182_runtime_ui.py
ERROR backend\tests\test_v22_dispatch_reliability.py
ERROR backend\tests\test_v27_weld.py
ERROR backend\tests\test_v28_weld.py
!!!!!!!!!!!!!!!!!!! Interrupted: 4 errors during collection !!!!!!!!!!!!!!!!!!!
4 errors in 0.85s
```

四个收集错误均为原样既有基线：

```text
ImportError: cannot import name 'ACTION_CONTRACT' from 'backend.engine'
```

为取得其余测试结果，另运行：

```powershell
py -3.12 -m pytest backend/tests -q --ignore=backend/tests/test_v10182_runtime_ui.py --ignore=backend/tests/test_v22_dispatch_reliability.py --ignore=backend/tests/test_v27_weld.py --ignore=backend/tests/test_v28_weld.py
```

```text
46 failed, 874 passed, 5 skipped, 16 errors, 10 subtests passed in 108.27s (0:01:48)
```

该次失败分布在未修改的旧 browser/process、create_project directory contract、E2E fixture、
renderer/invariant/souls 与其他历史契约测试；其中包含已知 renderer 基线
`test_v1017_invariants.py::test_worker_text_is_passed_through_not_templated`。分区隔离结果仍为：

- Knowledge：23 passed；
- Task Runtime：214 passed；
- 全前端：545 passed。

按任务约束，不修复 `ACTION_CONTRACT`、renderer 或其他全库历史基线。

## 5. 结论与 concerns

范围内无未解决实现问题。已知仓库级 concerns 仅为上述既有全后端/TypeScript 静态基线；它们与
Task 1 修改文件无重叠，且本任务没有扩展处理。

# Task 1 独立并发安全与质量审查

## Verdict

- **Spec compliance: PASS**
- **Task quality: PASS（task-scoped；含下列非阻断 ⚠️）**
- **Findings:** Critical **0** / Important **0** / Minor **0**

审查范围严格限定为 `task-1-brief.md`、`task-1-report.md` 与
`review-f06fd38..30a24ec.diff`。未使用 code-review skill，也未读取或写入其他
`.superpowers/sdd` 计划目录。

## Spec compliance 证据

1. **ProfileBundle 同代性满足绑定约束。**
   `ProfileBundle` 是 frozen dataclass，包含 body、exists/availability、source/course
   contributors、locked 与 generation token。`profile_bundle()` 只获取一次既有
   `root_lock`，并在该临界区内调用不再取锁的 `_profile_bundle_sync()`；后者从同一次 graph/body
   读取构造所有字段，token 同时覆盖 revision、exists、body、contributors 与 locked。未看到把
   unlocked general snapshot 的字段混入 bundle 的路径。

2. **merged/direct Assets 不再由 unlocked snapshot 授权 Profile。**
   两条 Assets 路由仍可读取 general snapshot 以返回普通资产，但 `profile_exists`、
   `profile_source_ids` 与 `profile_course_ids` 只由 `_read_profile_bundle()` 返回的 bundle 经
   `_profile_authorization()` 产生。bundle/loop 不可用时，merged 将三项 Profile 字段清空并
   fail closed；direct 返回错误，不会回退到 `snap` 中的旧 Profile 字段。

3. **detail GET 保持正文与 contributor proof 同代。**
   detail GET 改为只消费一个 bundle，正文直接取 `bundle.body`，授权 proof 取同一 bundle；原有
   `ok/project_id/text/source_ids/course_ids` 响应结构未改变。Profile row 仍固定为 project scope，
   并继续经过既有 `_row_allowed()` / `_contributors_allowed()`，没有为 ownerless Profile 引入
   global 例外。

4. **Profile POST 的授权、比较与写入位于同一锁边界。**
   `set_profile_authorized()` 在一次 `root_lock` 内依次读取 authorized bundle、执行 scope
   predicate、再次读取并比较 generation token，然后同步调用 `_set_profile_sync()`。compare 与
   mutation 之间没有 `await`，内部 helper 不会重入 `root_lock`。因此 writer 只能在线性化点之前
   完成并被当前代际重新授权，或在 POST 之后完成；旧授权不能覆盖已经完成的 newer ownerless
   generation。generation mismatch 映射为 409，server reason phrase 也已补齐。

5. **event-loop 与取消语义 fail closed。**
   `_read_profile_bundle()` 在触碰 manager lock 前拒绝 closed/different registered loop；merged
   只隐藏 Profile，direct/detail 返回 503。POST 继续由既有 override runner 做 loop/timeout
   门禁。新增 async 方法显式重新抛出 `CancelledError`；可取消的两个 `to_thread` 阶段都只是
   bundle 读取，取消后即使后台线程收尾也没有 mutation。真正写入阶段无 await，未出现“超时已
   返回、后台线程继续写”的结构。

6. **要求的回归面有对应证据。**
   diff 含 merged/direct availability、detail GET barrier、POST/writer、auto-compilation、purge
   deletion、generation lifecycle 与 foreign-loop 用例。实现报告记录 Knowledge 23、Task Runtime
   214、Task 4A 72、全前端 545 全绿，以及 lint/preflight/runtime gates 通过；按审查约束未无理由
   重跑这些整套测试。

## Task quality 证据

- **锁与死锁：** public async 方法各只获取一次 per-root lock；锁内 helper 不再取锁；worker
  thread 不获取/等待 asyncio lock。变更中未形成新的锁顺序环或递归锁等待。
- **取消与超时：** mutation 未放入不可取消的 worker；异常路径保留 `CancelledError` 传播，普通
  异常返回 fail-closed result。下面的独立攻击覆盖了报告中未直接展示的 after-authorization
  timeout 边界。
- **API 兼容：** `profile_snapshot()` 保留既有 keys，并由 bundle 投影；detail/Assets 成功响应
  字段不变；新增 409 使用标准 `409 Conflict`；普通 Assets/Skillpack 策略未被改写。
- **测试质量：** availability 红测在 unlocked general snapshot 的正文读取接缝暂停，能稳定让
  replacement 先完成；POST 红测在正文读取接缝区分旧实现的 unlocked authorization 与新实现的
  lock serialization。lifecycle 用例检查 missing → compiled → deleted 三代的 body、availability、
  contributors、locked 与 token 变化。

## 独立定向攻击

均使用临时目录，未改写仓库文件：

1. 在 POST 第一次 lock-bound body read 中取消请求：`CancelledError` 传播，Profile 正文未变，
   `profile_locked` 仍为 false。
2. 允许首次读取和 scope authorization 完成，在第二次 generation read 中强制 50ms timeout：返回
   `504 override_timeout`；释放残留只读 worker 后，正文和 locked state 仍未变化。
3. 在第二次 bundle read 注入不同 generation token：返回
   `409 stale_profile_generation`，且正文和 locked state 无写副作用。

## Findings

未发现可归因于本 diff 的 Critical、Important 或 Minor finding。尤其未发现 unlocked Profile
authorization 回退、root-lock 重入、wrong-loop lock entry、timeout/cancel 后后台写入、detail GET
代际拆分或 ownerless/global 策略放宽。

## ⚠️ 无法仅从所给 diff 完全验证

- diff 没有展开既有 `KnowledgeGraphManager.root_lock()` 的实现及所有历史 writer；因此无法仅凭这三份
  材料形式化证明仓库内每个非本次改动 writer 都遵守同一 per-root lock。改动路径本身没有发现绕锁，
  报告的 auto/purge/GET barrier 回归与本次攻击提供了行为证据。
- 实现报告中的整套测试输出未重复执行；本审查只新增了三项针对性攻击。报告同时记录全库
  TypeScript typecheck 既有失败，以及全后端既有 collection/历史测试失败；这些失败不与 Task 1
  修改文件重叠，但“全部确为基线问题”无法由给定 diff 独立证明。因此本 PASS 是 Task 1 范围 verdict，
  不是对整个仓库全绿状态的声明。


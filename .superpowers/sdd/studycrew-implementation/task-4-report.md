# Task 4 Report: Group UI、每日入口与 Agent 状态栏

## 结果

Task 4 已把已有的 Group 与 Task Runtime 权威状态接入 Electron renderer：默认入口改为 Today；旧会话只通过 Group 明示的 `legacy_project_id` 映射；群组创建、管理、状态、审批、任务控制和公开运行详情均有生产 UI。未实现 Tool Gateway、MCP、RAG 或 SchoolGateway 内部能力。

## 改造前批判

1. `useGroupStore` 与 `useTaskRuntimeStore` 虽已具备契约和恢复能力，但 renderer 没有主要消费者，用户只能看到聊天文本，无法区分主任务、旁路任务、审批、预算或失败恢复。
2. 左栏仍把旧 Project 当作产品身份；若把 `project_id` 猜成 `group_id`，状态与权限都可能跨群泄漏。
3. `App.tsx`、Rail 与聊天头承担了过多职责。继续堆条件渲染会让每个 runtime event 扩散到整个 App，并使 Today、Review、Privacy 和管理流难以独立验证。

## 信息层级决定

唯一视觉主角是当前主任务或其阻塞异常。主任务直接位于聊天标题下方，公开摘要、状态、路由、Verifier 信任、预算与合法控制在首屏；活跃 Agent 次一级；空闲 Agent 折叠；旁路任务明确标注且不合并费用或证据；历史事件、工具活动、证据、预算、恢复和索引进入抽屉。风险同时使用图形、图标和文本，不依赖颜色。

## 实现

- 新增 `AgentStatusBar` 与 runtime drawer，使用窄 Zustand selector 和 memo 边界；支持 pause/cancel/resume/reconcile、精确 Group/Run/Approval 审批、pending/error 反馈、空闲 Agent 折叠、旁路隔离及公开字段工具活动。
- 新增四模板 `GroupCreationWizard`：工作目录必填，Coordinator/Verifier 锁定，受控专家、可选角色、预算、模型/工具覆盖及课程/资料绑定；支持焦点圈闭、Escape 和焦点恢复。
- 新增 `GroupManagementDialog`：重命名、归档/恢复、复制、阵容编辑、课程/资料链接与解除、后端删除影响预览、名称二次确认及原样提交 preview token。
- 左侧会话区改为“群组”心智模型，并显示主任务文本状态、排队、审批、旁路、索引异常和归档状态；映射仅接受 `legacy_project_id`。
- 新增 Today 最小跨群摘要以及 Review/Privacy 诚实壳层；Today 只消费 runtime summary，不读取 roster、记忆或正文，并可返回所属群组。
- App 启动只加载 Task Runtime 的 Group snapshot，由同一投影同时提供 run 与 summary；不再与旧 Group summary 请求竞写。runtime-heavy 组件保持在 App 以下。默认 StudyCrew 入口为 Today。
- 增加开发专用 `?studycrew-demo=1` 确定性场景，供作品演示与 UI 验收使用，不影响生产数据路径。
- 样式保持现有暖色画布、靛蓝与 token；公开事件行使用 `content-visibility:auto`；Task 4 区域在 `prefers-reduced-motion` 下关闭动画和过渡。

## Round 1 独立审查修复

- 所有向导、管理框和运行抽屉均经 `ModalPortal` 进入 body 顶层 overlay host；首个模态打开时 `#root` 同时设置 `inert` 与 `aria-hidden`，最后一个关闭后恢复原值。三个交互面均具备焦点圈闭、Escape 与触发器焦点恢复。
- 主任务严格由 `GroupRuntimeSummary.active_primary_run_id` 选中 exact projection；无摘要时诚实显示“无权威主任务”，`needsResync` 时只允许对账。控制矩阵与后端状态机一致。
- 审批按 exact `run_id` 分区并用其自身身份提交；旁路/排队审批不再挂到主任务名下。
- 删除预览绑定 `(group_id, updated_at)`，关闭、换群或成功 mutation 后清空；渲染和提交均校验身份与 token。
- 四模板角色默认、最多四个专家、整数预算与“创建成功后仅续跑失败绑定”均已覆盖；成功完成后重置向导，下一次打开不会复用旧 Group。
- canonical Group 隐藏旧员工入口，聊天头改读 Group roster；管理面支持受控专家增改删，Coordinator/Verifier 与平台策略锁定。
- 归档群组单独分区且不可激活；知识入口先要求选择 Group/课程范围；旧 Knowe/项目/员工 UI 品牌和心智在 canonical 路径被 StudyCrew/群组/Agent 替换。
- 完成状态/阶段本地化、Verifier 负向终态诚实措辞、完整 tab/roving keyboard 语义，以及基于 side run `last_seq` 水位的本地未读/确认投影。

## Round 2 独立复审修复

- `GroupRuntimeSnapshot.summary` 不再被 renderer 丢弃：snapshot/replay 会原子更新 Group summary；连续 runtime event 同步投影主任务、队列、审批、预算和旁路数，并在 80ms 窗口后合并为一次带认证的权威 snapshot 刷新。状态栏手动刷新也同时修复 run 与 summary。
- 新增 live snapshot A→B 主任务切换测试，状态栏始终按最新 `active_primary_run_id` exact 选择，不从数组顺序或聊天文本猜测。
- Knowledge 范围从装饰性 selector 改为真实数据边界：请求携带 `group_id`、legacy project、source 与 course；卡片、画像和项目技能包同时按 Group ownership 过滤，课程缺少精确 evidence identity 时 fail closed。跨群项目资产不可见，显式 source link 和全局已批准资产仍可使用。
- App 对 canonical Group 永不挂载 legacy `RosterPanel`，legacy→canonical 与 canonical→canonical 切换均关闭旧面板；canonical roster 只来自 Group contract。
- 向导、管理框和 runtime drawer 使用 latest-callback ref，focus trap 生命周期只依赖 `open`。父级 callback 改变或连续 10 条 runtime event 不再把输入框/tab 焦点抢回关闭按钮。
- generic `failed` 只显示“任务执行失败，Verifier 结论未上报”；只有公开 `verifier_decision` 才显示通过/未通过。旁路任务仅在 terminal/公开回答完成后产生未读水位，规划、工具与审批进度只显示“进行中”。Index tab 显示真实 `indexing_state`。
- 修复全量基线：统一 RTL cleanup；preview control origin fail closed；迁移 v24 对已删除 DOM/协议的脆弱断言；Vitest 不再误收 Node `scripts/*.test.mjs`。

## Round 3 独立复审修复

- Task Runtime 成为 renderer 的单一实时权威：每个 Group 维护 refresh generation，后发 S2(A+B) 先完成后，旧 S1(A) 响应会被忽略；直接到达的旧 reconnect snapshot 也不能删除时间更新更晚的缺席 run。run、approval 与 `groupSummaries` 在同一次 Zustand projection 内提交，UI 不再订阅另一个 summary store。
- gap replay 会在同一提交中更新 terminal run 与 active-primary summary；`resetAll` 同步清空 Group summary。连续事件、本地 control/approval 响应均实时派生主任务、队列、审批、预算、foreground side 与 index 状态，不依赖 80ms HTTP 刷新成功。
- snapshot 更新前先捕获旧 Group 投影；仅保留 `sensitivity=public`、以 `event_id+seq` 去重、每 run 最多 200 条的公开事件。fake-timer 测试执行生产 80ms refresh 后，Verifier/tool 事件仍可在抽屉使用。
- Knowledge 边界贯通 renderer 与 backend：`knowledge_api` 使用 `GroupRepository` 校验 canonical Group、legacy project、source 与 course allowlist；assets、profile、asset/node detail、search、skillpack 及写路由越界均 fail closed/403。资产从 source metadata 输出结构化 `source_ids/course_ids`，不再解析自由文本 `sourceRef`。
- Knowledge store 以稳定 scope identity 隔离请求；切 Group/课程立即关闭 preview、清空 cards/profile/skill panes，迟到的旧 scope 响应不再覆盖新 scope。跨群 global 资产仅允许用户显式批准且为 validated/core；pending、retired、ownerless 第三方默认拒绝。
- backend 与 renderer 共用一致语义：旁路活跃数只计 `kind=side + queue_class=foreground + routing/running/verifying`，index 永不进入旁路回答；index queued/running/failed/idle 由事件实时派生。failed/cancelled 旁路显示“状态通知”，不再称为“未读回答”。
- 新增单一 `teardownGroup(groupId)`：收到 confirmed-delete receipt 后取消 authority timer，并清除 run/event/evidence/approval/summary、side watermark、Knowledge scope/preview，以及 legacy Group/DM/附件投影；不会遗留已删 Group id。

## 自动化验证

最终回归命令：

```text
npm test -- --run
node --test scripts/python-command.test.mjs
```

覆盖精确 Project→Group 映射/隔离、live 主任务切换、主旁路分离、terminal answer 未读、空闲折叠、公开工具投影、权威主任务与全状态控制矩阵、resync、exact run 审批、四模板默认、绑定失败不重复创建、删除 token 身份/版本、Today summary、Knowledge cross-group exclusion、canonical roster、modal inert、连续事件焦点稳定、键盘/Escape、tabs、真实索引状态和 reduced motion。

最终全量为 **41 个 Vitest 文件、488 个测试全部通过**；独立 Node command 测试 **2/2 通过**；Task Runtime 全组加 Knowledge scope 的 backend 回归 **213 个测试全部通过**；全仓 `npm run lint` 通过。仓库全量 TypeScript 仍含 Task 4 之前的历史错误；对本轮触及路径过滤为 **0 个诊断**。全 backend suite 在收集阶段仍被四个历史测试导入已不存在的 `ACTION_CONTRACT` 阻断（`test_v10182_runtime_ui.py`、`test_v22_dispatch_reliability.py`、`test_v27_weld.py`、`test_v28_weld.py`），本轮触及后端测试不包含在该失败中。

## 实际渲染检查

初始 Task 4 曾使用应用内 Browser 在真实 Vite 页面 `?studycrew-demo=1` 检查，不使用静态 HTML 替身：

- 1440×900：Today 单一主角清晰；进入“高等数学冲刺”后，状态栏位于标题与消息流之间；七个详情分区可交互；Escape 关闭抽屉后焦点返回“详情”。
- 1024×768：页面 `scrollWidth` 等于 1024，无全局横向溢出或错误 overlay。
- 900×700：旧会话栏实际折叠为 76px；页面 `scrollWidth` 等于 900；状态栏边界为 left 148/right 886；“暂停/取消/详情/管理群组”保持可读。实测发现并修复了旧 CSS 导入顺序覆盖折叠规则，以及窄屏按钮被压成 `font-size:0` 的问题。
- 控制台无应用错误；仅有既有 Zustand equality API 弃用提示和未启动本地 backend 时的连接告警。截图已在 Browser 验收步骤中生成，viewport override 已恢复。

Round 1 修复后按 `control-in-app-browser` 的 bootstrap/documentation/troubleshooting 流程重新连接时，既有 binding 返回 unavailable，浏览器列表为空。技能明确禁止改用无关 browser-control surface，因此没有伪造本轮 modal hit-test、dark/light 或截图结论；顶层 portal、`inert`/`aria-hidden`、焦点圈闭/恢复与 Escape 由真实 React DOM 测试覆盖。待应用内 Browser 恢复后仍需补跑 900/1024/1440 的 `elementFromPoint` 验收。

Round 2 再次按同一规定流程连接，`iab` 仍返回 `Browser is not available`，唯一一次 discovery 仍为 `[]`；因此没有违规切换 standalone Playwright，也没有生成或复用伪造的 post-fix 截图。Round 2 的 live rerender focus、canonical roster、Knowledge isolation 与 runtime exact switch 均由 React/contract 测试验收，Browser viewport/hit-test 仍是唯一未执行项。

Round 3 做了本轮唯一一次 Browser 重试：复用既有 runtime 后，binding 在 documentation 阶段返回 `Browser is not available`；按 `bootstrap-troubleshooting` 只执行一次 discovery，结果仍为 `[]`。因此没有 post-fix viewport、modal hit-test 或截图证据，也没有使用 standalone Playwright 规避限制。本轮乱序 authority、80ms event retention、跨群 Knowledge detail/profile/skillpack、统一 teardown 与 side/index 语义均由自动化测试验证。

## 主要文件

- `src/components/AgentStatusBar.tsx`
- `src/components/GroupCreationWizard.tsx`
- `src/components/GroupManagementDialog.tsx`
- `src/components/TodayView.tsx`
- `src/components/ProductShell.tsx`
- `src/components/ConvList.tsx`
- `src/components/ChatStream.tsx`
- `src/app/App.tsx`
- `src/store/taskRuntime.ts`
- `src/store/groupTeardown.ts`
- `src/store/knowledge.ts`
- `src/components/KnowledgeScopedView.tsx`
- `src/components/KnowledgeView.tsx`
- `src/styles/knowe-components.css`
- `src/components/studycrew-ui.test.tsx`
- `src/components/studycrew-css.test.ts`
- `src/dev/studycrewDemo.ts`

## 剩余边界

- 工具抽屉只展示 Task Runtime 已发布的 `tool_prepared` 公共投影；完整 ToolInvocation 台账属于后续 Tool Gateway 任务，不能在 UI 猜测。
- Review 与 Privacy 明确标为后续能力，只展示当前真实 Group/runtime 计数；未伪造 RAG、PII 或评测结果。
- Knowledge 入口和现有 backend asset/profile/detail/skillpack 数据面已 enforce Group/课程安全范围；完整解析、切片、混合召回与引用 RAG 仍由 Task 5 实现。
- 900px 是计划要求的最低桌面验证宽度；不承诺移动端布局。

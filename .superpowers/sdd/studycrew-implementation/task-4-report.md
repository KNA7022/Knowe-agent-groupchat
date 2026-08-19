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
- App 启动并发加载 Group summary 与 runtime snapshot；runtime-heavy 组件保持在 App 以下。默认 StudyCrew 入口为 Today。
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

## 自动化验证

最终相关回归命令：

```text
npm test -- --run src/components/studycrew-ui.test.tsx src/components/studycrew-css.test.ts src/store/taskRuntime.test.ts src/store/groups.test.ts src/components/sessionHost.test.tsx src/transport/socket.wars.test.ts
```

覆盖精确 Project→Group 映射/隔离、主旁路分离、空闲折叠、公开工具投影、权威主任务与全状态控制矩阵、resync、exact run 审批、四模板默认、绑定失败不重复创建、删除 token 身份/版本、Today summary、modal inert、键盘/焦点/Escape、tabs、side unread 和 reduced motion。

直接触达 App/ConvList/ChatStream/Composer/Rail/Group/Runtime 的回归最终为 **8 个文件、162 个测试全绿**。额外迁移旧 accessible-name、异步头像预加载、共享 cleanup、FileCard 新边界、推理收缩动画以及 FakeServer 白名单后，全量从独立审查时的 **91 failed / 370 passed** 降到 **13 failed / 457 passed**。

生产触及文件 ESLint 通过。仓库全量 TypeScript 仍含 Task 4 之前的基线错误；按 Task 4 触及文件过滤为 0。

全量剩余只在两条与 Task 4 无关且可单文件复现的历史路径出现；`git diff 3f32bdd --name-only` 证明本轮没有修改对应生产组件：

- `src/store/v24.test.ts`：6 个脆弱源码字符串断言仍要求旧 `StreamBubble` 八阶段 DOM 和旧 `ApprovalCard` 文本/selector；本轮未修改 `StreamBubble.tsx` 或 `ApprovalCard.tsx`。
- `src/components/BackendGate.test.tsx`：6 个 bridge 状态/重试断言在现有 jsdom bridge bootstrap 下超时或取不到旧文案；本轮未修改 `BackendGate.tsx`。Vitest 汇总显示 13 failed（输出中具名断言为上述 12 个）/457 passed；未把它们误称为本轮回归。

## 实际渲染检查

初始 Task 4 曾使用应用内 Browser 在真实 Vite 页面 `?studycrew-demo=1` 检查，不使用静态 HTML 替身：

- 1440×900：Today 单一主角清晰；进入“高等数学冲刺”后，状态栏位于标题与消息流之间；七个详情分区可交互；Escape 关闭抽屉后焦点返回“详情”。
- 1024×768：页面 `scrollWidth` 等于 1024，无全局横向溢出或错误 overlay。
- 900×700：旧会话栏实际折叠为 76px；页面 `scrollWidth` 等于 900；状态栏边界为 left 148/right 886；“暂停/取消/详情/管理群组”保持可读。实测发现并修复了旧 CSS 导入顺序覆盖折叠规则，以及窄屏按钮被压成 `font-size:0` 的问题。
- 控制台无应用错误；仅有既有 Zustand equality API 弃用提示和未启动本地 backend 时的连接告警。截图已在 Browser 验收步骤中生成，viewport override 已恢复。

Round 1 修复后按 `control-in-app-browser` 的 bootstrap/documentation/troubleshooting 流程重新连接时，既有 binding 返回 unavailable，浏览器列表为空。技能明确禁止改用无关 browser-control surface，因此没有伪造本轮 modal hit-test、dark/light 或截图结论；顶层 portal、`inert`/`aria-hidden`、焦点圈闭/恢复与 Escape 由真实 React DOM 测试覆盖。待应用内 Browser 恢复后仍需补跑 900/1024/1440 的 `elementFromPoint` 验收。

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
- `src/styles/knowe-components.css`
- `src/components/studycrew-ui.test.tsx`
- `src/components/studycrew-css.test.ts`
- `src/dev/studycrewDemo.ts`

## 剩余边界

- 工具抽屉只展示 Task Runtime 已发布的 `tool_prepared` 公共投影；完整 ToolInvocation 台账属于后续 Tool Gateway 任务，不能在 UI 猜测。
- Review 与 Privacy 明确标为后续能力，只展示当前真实 Group/runtime 计数；未伪造 RAG、PII 或评测结果。
- Knowledge 入口已增加 Group/课程范围选择边界；完整 RAG 交互仍由 Task 5 实现。
- 900px 是计划要求的最低桌面验证宽度；不承诺移动端布局。

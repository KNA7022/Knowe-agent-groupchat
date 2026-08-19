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

## 自动化验证

最终相关回归命令：

```text
npm test -- --run src/components/studycrew-ui.test.tsx src/components/studycrew-css.test.ts src/store/taskRuntime.test.ts src/store/groups.test.ts src/components/sessionHost.test.tsx src/transport/socket.wars.test.ts
```

覆盖精确 Project→Group 映射/隔离、主旁路分离、空闲折叠、公开工具投影、控制矩阵、伪造审批身份、四模板与受控配置、后端错误恢复、归档/恢复、删除 token、Today summary、键盘/焦点/Escape、reduced motion 及 900px 收拢。最后一次运行：6 个文件、49 个测试通过。

触及文件 ESLint 通过。仓库全量 TypeScript 仍含 Task 4 之前的基线错误；按 Task 4 触及文件过滤为 0。此前全量 Vitest 基线为 21 个文件/364 个测试通过、16 个文件/91 个测试失败，失败集中在旧 parity 白名单、v23 旧断言、历史 UI 测试未 cleanup 造成的 DOM 累积和旧 logo/avatar 假设；本任务 focused suites 全绿，未借 Task 4 扩改这些历史基线。

## 实际渲染检查

使用应用内 Browser 在真实 Vite 页面 `?studycrew-demo=1` 检查，不使用静态 HTML 替身：

- 1440×900：Today 单一主角清晰；进入“高等数学冲刺”后，状态栏位于标题与消息流之间；七个详情分区可交互；Escape 关闭抽屉后焦点返回“详情”。
- 1024×768：页面 `scrollWidth` 等于 1024，无全局横向溢出或错误 overlay。
- 900×700：旧会话栏实际折叠为 76px；页面 `scrollWidth` 等于 900；状态栏边界为 left 148/right 886；“暂停/取消/详情/管理群组”保持可读。实测发现并修复了旧 CSS 导入顺序覆盖折叠规则，以及窄屏按钮被压成 `font-size:0` 的问题。
- 控制台无应用错误；仅有既有 Zustand equality API 弃用提示和未启动本地 backend 时的连接告警。截图已在 Browser 验收步骤中生成，viewport override 已恢复。

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
- 既有 Knowledge 视图仍复用原入口，课程/群组范围的完整 RAG 交互由 Task 5 实现。
- 900px 是计划要求的最低桌面验证宽度；不承诺移动端布局。

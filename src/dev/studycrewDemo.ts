import type { Group, GroupRuntimeSummary } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { useKnoweStore } from '../store/store';
import { useTaskRuntimeStore } from '../store/taskRuntime';

const groupId = `grp_${'1'.repeat(32)}` as const;
const runId = `run_${'2'.repeat(32)}`;
const at = '2026-08-19T08:00:00+08:00';

const group: Group = {
  schema_version: 1, group_id: groupId, name: '高等数学冲刺', template: 'exam_sprint', lifecycle: 'active',
  roster: [
    { agent_id: `gag_${'3'.repeat(32)}`, group_id: groupId, role: 'coordinator', display_name: 'Coordinator', template_key: 'coordinator', expert: null, model_override: null, tool_overrides: [], budget: null, created_at: at, updated_at: at },
    { agent_id: `gag_${'4'.repeat(32)}`, group_id: groupId, role: 'verifier', display_name: 'Verifier', template_key: 'verifier', expert: null, model_override: null, tool_overrides: [], budget: null, created_at: at, updated_at: at },
    { agent_id: `gag_${'5'.repeat(32)}`, group_id: groupId, role: 'knowledge', display_name: '课程知识 Agent', template_key: 'knowledge', expert: null, model_override: null, tool_overrides: [], budget: null, created_at: at, updated_at: at },
  ],
  course_bindings: [{ binding_id: `gcb_${'6'.repeat(32)}`, group_id: groupId, course_id: 'MATH-101', created_at: at }],
  source_links: [], budget: 5, model_overrides: {}, tool_overrides: {}, created_at: at, updated_at: at,
  archived_at: null, legacy_project_id: 'demo-calculus',
};

const summary: GroupRuntimeSummary = {
  group_id: groupId, lifecycle: 'active', active_agent_count: 3, course_binding_count: 1,
  source_count: 8, shared_source_count: 0, budget: 5, active_primary_run_id: runId,
  primary_task_status: 'running', queued_task_count: 2, active_side_task_count: 1,
  pending_approval_count: 1, budget_usage: { wall_clock_seconds: 128, model_turns: 4, tool_calls: 3, tokens: 6840, cost_usd: .18 },
  indexing_state: 'idle', updated_at: at,
};

export function seedStudyCrewDemo(): void {
  useGroupStore.getState().ingest([group]);
  useGroupStore.setState({ runtimeSummaries: { [groupId]: summary } });
  const knowe = useKnoweStore.getState();
  knowe.ensureProject('demo-calculus', '高等数学冲刺');
  useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
    schema_version: 1, group_id: groupId,
    runs: [
      {
        run_id: runId, task_id: `task_${'7'.repeat(32)}`, group_id: groupId, kind: 'primary', title: '极限与连续错题复盘',
        status: 'running', queue_class: 'foreground', route_label: 'supplement_current', read_only: false, risk: 'low', parent_run_id: null,
        version: 1, last_seq: 0, budget_limits: { wall_clock_seconds: 1800, model_turns: 20, tool_calls: 30, tokens: 100000, cost_usd: 5 },
        budget_usage: summary.budget_usage,
        agent_runs: [
          { agent_run_id: `arun_${'8'.repeat(32)}`, run_id: runId, agent_id: 'coordinator', role: 'Coordinator', phase: 'planning', operation_summary: '按薄弱概念重排复习顺序', blocking_reason: '', version: 1, updated_at: at, schema_version: 1 },
          { agent_run_id: `arun_${'9'.repeat(32)}`, run_id: runId, agent_id: 'knowledge', role: '课程知识 Agent', phase: 'retrieving', operation_summary: '检索讲义与错题证据', blocking_reason: '', version: 1, updated_at: at, schema_version: 1 },
          { agent_run_id: `arun_${'a'.repeat(32)}`, run_id: runId, agent_id: 'verifier', role: 'Verifier', phase: 'idle', operation_summary: '', blocking_reason: '', version: 1, updated_at: at, schema_version: 1 },
        ], evidence: [], failure_type: null, public_summary: '正在整理极限与连续的高频失分点', blocking_reason: '',
        data_classification: [], created_at: at, updated_at: at, started_at: at, finished_at: null, schema_version: 1,
      },
      {
        run_id: `run_${'b'.repeat(32)}`, task_id: `task_${'c'.repeat(32)}`, group_id: groupId, kind: 'side', title: '解释洛必达条件',
        status: 'running', queue_class: 'foreground', route_label: 'side_answer', read_only: true, risk: 'low', parent_run_id: runId,
        version: 1, last_seq: 0, budget_limits: { wall_clock_seconds: 180, model_turns: 3, tool_calls: 2, tokens: 5000, cost_usd: .2 },
        budget_usage: { wall_clock_seconds: 18, model_turns: 1, tool_calls: 0, tokens: 620, cost_usd: .01 }, agent_runs: [], evidence: [], failure_type: null,
        public_summary: '旁路：解释洛必达法则的适用条件', blocking_reason: '', data_classification: [], created_at: at, updated_at: at, started_at: at, finished_at: null, schema_version: 1,
      },
    ],
    pending_approvals: [{ approval_id: `apr_${'d'.repeat(32)}`, run_id: runId, group_id: groupId, kind: 'tool', summary: '允许读取课程资料目录', risk: 'medium', requested_capabilities: ['read_course_sources'], tool_name: 'local_files', operation_scope: { path: 'group/course' }, purpose: 'retrieve_course_evidence', expires_at: null, status: 'pending', created_at: at, resolved_at: null, schema_version: 1 }],
    summary: { active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 2, active_side_task_count: 1, pending_approval_count: 1, budget_usage: summary.budget_usage, indexing_state: 'idle' },
  });
}

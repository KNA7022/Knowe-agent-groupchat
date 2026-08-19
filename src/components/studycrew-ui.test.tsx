// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import type { Group } from '../contract/group';
import { taskRunFixture } from '../contract/taskRuntime.fixture';
import type { TaskRun } from '../contract/taskRuntime';
import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import { useSideTaskReadStore } from '../store/sideTaskRead';
import AgentStatusBar, {
  controlCommandsForStatus,
  groupForLegacyProject,
  primaryProjectionForSummary,
  sideAnswerSequence,
  verifierLabel,
} from './AgentStatusBar';
import GroupCreationWizard, { roleDefaultsForTemplate } from './GroupCreationWizard';
import GroupManagementDialog from './GroupManagementDialog';
import { TodayView, buildTodayRows } from './TodayView';

const groupId = `grp_${'1'.repeat(32)}` as const;
const otherGroupId = `grp_${'9'.repeat(32)}` as const;
const runId = `run_${'2'.repeat(32)}`;

function makeGroup(id = groupId, legacy = 'legacy-calculus'): Group {
  const at = '2026-08-18T00:00:00Z';
  return {
    schema_version: 1,
    group_id: id,
    name: id === groupId ? '高数冲刺' : '英语学习',
    template: 'exam_sprint',
    lifecycle: 'active',
    roster: [
      {
        agent_id: `gag_${id === groupId ? 'a' : 'c'.repeat(1)}${'a'.repeat(31)}`,
        group_id: id,
        role: 'coordinator',
        display_name: 'Coordinator',
        template_key: 'coordinator',
        expert: null,
        model_override: null,
        tool_overrides: [],
        budget: null,
        created_at: at,
        updated_at: at,
      },
      {
        agent_id: `gag_${id === groupId ? 'b' : 'd'.repeat(1)}${'b'.repeat(31)}`,
        group_id: id,
        role: 'verifier',
        display_name: 'Verifier',
        template_key: 'verifier',
        expert: null,
        model_override: null,
        tool_overrides: [],
        budget: null,
        created_at: at,
        updated_at: at,
      },
    ],
    course_bindings: [],
    source_links: [],
    budget: 5,
    model_overrides: {},
    tool_overrides: {},
    created_at: at,
    updated_at: at,
    archived_at: null,
    legacy_project_id: legacy,
  };
}

function snapshot(): unknown {
  return {
    schema_version: 1,
    group_id: groupId,
    runs: [
      taskRunFixture({
        public_summary: '正在整理极限错题',
        route_label: 'supplement_current',
        agent_runs: [
          {
            agent_run_id: `arun_${'3'.repeat(32)}`,
            run_id: runId,
            agent_id: 'coordinator',
            role: 'Coordinator',
            phase: 'planning',
            operation_summary: '拆分复习范围',
            blocking_reason: '',
            version: 1,
            updated_at: '2026-08-18T00:00:00Z',
            schema_version: 1,
          },
          {
            agent_run_id: `arun_${'4'.repeat(32)}`,
            run_id: runId,
            agent_id: 'verifier',
            role: 'Verifier',
            phase: 'idle',
            operation_summary: '',
            blocking_reason: '',
            version: 1,
            updated_at: '2026-08-18T00:00:00Z',
            schema_version: 1,
          },
        ],
      }),
      taskRunFixture({
        run_id: `run_${'5'.repeat(32)}`,
        task_id: `task_${'6'.repeat(32)}`,
        kind: 'side',
        read_only: true,
        risk: 'low',
        title: '术语解释',
        public_summary: '旁路只读回答',
      }),
    ],
    pending_approvals: [],
    summary: {
      active_primary_run_id: runId,
      primary_task_status: 'running',
      queued_task_count: 0,
      active_side_task_count: 1,
      pending_approval_count: 0,
      budget_usage: {
        wall_clock_seconds: 12, model_turns: 2, tool_calls: 1, tokens: 500, cost_usd: 0.02,
      },
      indexing_state: 'idle',
    },
  };
}

function runtimeSummary(activePrimaryRunId: string | null = runId): never {
  return {
    group_id: groupId, lifecycle: 'active', active_agent_count: 2,
    course_binding_count: 0, source_count: 0, shared_source_count: 0, budget: 5,
    active_primary_run_id: activePrimaryRunId, primary_task_status: activePrimaryRunId ? 'running' : null,
    queued_task_count: 0, active_side_task_count: 1, pending_approval_count: 0,
    budget_usage: { wall_clock_seconds: 12, model_turns: 2, tool_calls: 1, tokens: 500, cost_usd: 0.02 },
    indexing_state: 'idle', updated_at: '2026-08-18T00:00:00Z',
  } as never;
}

beforeEach(() => {
  useGroupStore.setState({
    groups: {}, order: [], runtimeSummaries: {}, sourceReferenceCounts: {},
    loading: false, error: null,
  });
  useTaskRuntimeStore.getState().resetAll();
  useSideTaskReadStore.getState().reset();
});

afterEach(() => cleanup());

describe('StudyCrew Group mapping and Agent status bar', () => {
  it('maps legacy conversations only through declared legacy_project_id', () => {
    const groups = [makeGroup(), makeGroup(otherGroupId, 'legacy-english')];
    expect(groupForLegacyProject(groups, 'legacy-calculus')?.group_id).toBe(groupId);
    expect(groupForLegacyProject(groups, groupId)).toBeNull();
    expect(groupForLegacyProject(groups, 'unknown')).toBeNull();
  });

  it('keeps primary and side tasks separate and collapses idle Agents', () => {
    useGroupStore.getState().ingest([makeGroup(), makeGroup(otherGroupId, 'legacy-english')]);
    useGroupStore.setState({ runtimeSummaries: { [groupId]: runtimeSummary() } });
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    render(<AgentStatusBar projectId="legacy-calculus" />);

    const primary = screen.getByTestId('primary-run');
    expect(primary).toHaveTextContent('正在整理极限错题');
    expect(primary).not.toHaveTextContent('旁路只读回答');
    expect(screen.getByTestId('side-runs')).toHaveTextContent('旁路只读回答');
    expect(screen.getByText('拆分复习范围')).toBeInTheDocument();
    expect(screen.queryByText('Verifier')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /空闲 Agent/ }));
    expect(screen.getByText('Verifier')).toBeInTheDocument();
    expect(screen.queryByText('英语学习')).not.toBeInTheDocument();
  });

  it('exposes only valid controls for each authoritative status', () => {
    expect(controlCommandsForStatus('queued', false)).toEqual(['cancel']);
    expect(controlCommandsForStatus('routing', false)).toEqual(['pause', 'cancel']);
    expect(controlCommandsForStatus('running', false)).toEqual(['pause', 'cancel']);
    expect(controlCommandsForStatus('waiting_approval', false)).toEqual(['cancel']);
    expect(controlCommandsForStatus('paused', false)).toEqual(['resume', 'cancel']);
    expect(controlCommandsForStatus('verifying', false)).toEqual(['pause', 'cancel']);
    expect(controlCommandsForStatus('succeeded', false)).toEqual([]);
    expect(controlCommandsForStatus('partial', false)).toEqual(['reconcile']);
    expect(controlCommandsForStatus('failed', false)).toEqual(['reconcile']);
    expect(controlCommandsForStatus('cancelled', false)).toEqual([]);
    expect(controlCommandsForStatus('running', true)).toEqual(['reconcile']);
  });

  it('selects the primary projection only from the authoritative summary id', () => {
    const first = taskRunFixture({ run_id: `run_${'a'.repeat(32)}`, status: 'queued' }) as never;
    const second = taskRunFixture({ run_id: `run_${'b'.repeat(32)}`, status: 'running' }) as never;
    const projections = [
      { run: first, events: [], needsResync: false },
      { run: second, events: [], needsResync: false },
    ];
    expect(primaryProjectionForSummary(projections, `run_${'b'.repeat(32)}`)?.run.run_id)
      .toBe(`run_${'b'.repeat(32)}`);
    expect(primaryProjectionForSummary(projections, null)).toBeNull();
    expect(primaryProjectionForSummary(projections, `run_${'c'.repeat(32)}`)).toBeNull();
  });

  it('switches the rendered primary when a live authoritative snapshot changes its exact id', () => {
    const nextRunId = `run_${'b'.repeat(32)}`;
    useGroupStore.getState().ingest([makeGroup()]);
    const first = snapshot() as any;
    (first as any).runs.push(taskRunFixture({
      run_id: nextRunId, task_id: `task_${'c'.repeat(32)}`, kind: 'primary',
      status: 'queued', title: '下一主任务', public_summary: '等待接力',
    }));
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, first);
    render(<AgentStatusBar projectId="legacy-calculus" />);
    expect(screen.getByTestId('primary-run')).toHaveTextContent('正在整理极限错题');

    const next = structuredClone(first) as any;
    next.summary.active_primary_run_id = nextRunId;
    next.summary.primary_task_status = 'running';
    next.runs = next.runs.map((run: any) => run.run_id === nextRunId
      ? { ...run, status: 'running', public_summary: '接力任务已开始', version: 2, last_seq: 2 }
      : run);
    act(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, next));
    expect(screen.getByTestId('primary-run')).toHaveTextContent('接力任务已开始');
    expect(useGroupStore.getState().runtimeSummaries[groupId]?.active_primary_run_id).toBe(nextRunId);
  });

  it('shows only the public tool projection in the details drawer', () => {
    useGroupStore.getState().ingest([makeGroup()]);
    useGroupStore.setState({ runtimeSummaries: { [groupId]: runtimeSummary() } });
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    useTaskRuntimeStore.getState().applyEvent(groupId, {
      event_id: `evt_${'7'.repeat(32)}`,
      run_id: runId,
      group_id: groupId,
      seq: 2,
      version: 2,
      event_type: 'tool_prepared',
      public_payload: {
        status: 'prepared', invocation_id: `inv_${'8'.repeat(32)}`,
        tool_name: 'local_files', disposition: 'approval_required',
        usage: { wall_clock_seconds: 13, model_turns: 2, tool_calls: 2, tokens: 510, cost_usd: 0.02 },
      },
      sensitivity: 'public',
      created_at: '2026-08-18T00:00:02Z',
      schema_version: 1,
    });
    render(<AgentStatusBar projectId="legacy-calculus" />);
    fireEvent.click(screen.getByRole('button', { name: /详情/ }));
    fireEvent.click(screen.getByRole('tab', { name: '工具' }));
    expect(screen.getByText('local_files')).toBeInTheDocument();
    expect(screen.getByText('approval_required')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('private_payload');
    const toolTab = screen.getByRole('tab', { name: '工具' });
    fireEvent.keyDown(toolTab, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: '证据' })).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', expect.stringContaining('tab-evidence'));
    act(() => {
      for (let seq = 3; seq <= 12; seq += 1) {
        useTaskRuntimeStore.getState().applyEvent(groupId, {
          event_id: `evt_${seq.toString(16).padStart(32, '0')}`,
          run_id: runId, group_id: groupId, seq, version: seq,
          event_type: 'budget_updated',
          public_payload: {
            usage: { wall_clock_seconds: seq, model_turns: 2, tool_calls: 2, tokens: 500 + seq, cost_usd: 0.02 },
            status: 'running',
          },
          sensitivity: 'public', created_at: `2026-08-18T00:00:${seq.toString().padStart(2, '0')}Z`, schema_version: 1,
        });
      }
    });
    expect(screen.getByRole('tab', { name: '证据' })).toHaveFocus();
    const indexRunId = `run_${'e'.repeat(32)}`;
    const withIndex = structuredClone(snapshot()) as any;
    withIndex.runs = (useTaskRuntimeStore.getState().byGroup[groupId] || [])
      .map((id) => useTaskRuntimeStore.getState().byRun[id]!.run);
    withIndex.runs.push(taskRunFixture({
      run_id: indexRunId, task_id: `task_${'e'.repeat(32)}`, kind: 'side', queue_class: 'index',
      read_only: true, risk: 'low', status: 'queued', updated_at: '2026-08-18T00:00:13Z',
    }));
    withIndex.summary.indexing_state = 'queued';
    act(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, withIndex));
    act(() => useTaskRuntimeStore.getState().applyEvent(groupId, {
      event_id: `evt_${'e'.repeat(32)}`, run_id: indexRunId, group_id: groupId, seq: 2, version: 2,
      event_type: 'status_changed', public_payload: { status: 'running' }, sensitivity: 'public',
      created_at: '2026-08-18T00:00:14Z', schema_version: 1,
    }));
    fireEvent.click(screen.getByRole('tab', { name: '索引' }));
    expect(screen.getByRole('tabpanel')).toHaveTextContent('正在为当前群组建立索引');
  });

  it('addresses primary and side approvals with their exact run identities', async () => {
    const sideRunId = `run_${'5'.repeat(32)}`;
    const primaryApproval = { approval_id: `apr_${'a'.repeat(32)}`, run_id: runId, group_id: groupId,
      kind: 'tool', summary: '主任务审批', risk: 'high', requested_capabilities: ['read'], tool_name: 'files',
      operation_scope: {}, purpose: 'read', expires_at: null, status: 'pending', created_at: '2026-08-18T00:00:00Z', resolved_at: null, schema_version: 1 };
    const sideApproval = { ...primaryApproval, approval_id: `apr_${'b'.repeat(32)}`, run_id: sideRunId, summary: '旁路审批' };
    useGroupStore.getState().ingest([makeGroup()]);
    useGroupStore.setState({ runtimeSummaries: { [groupId]: runtimeSummary() } });
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    const resolveApproval = vi.fn().mockResolvedValue({});
    useTaskRuntimeStore.setState({ approvals: {
      [primaryApproval.approval_id]: primaryApproval as never,
      [sideApproval.approval_id]: sideApproval as never,
    }, resolveApproval });
    render(<AgentStatusBar projectId="legacy-calculus" />);
    const sideRegion = screen.getByRole('region', { name: '其他任务审批' });
    fireEvent.click(within(sideRegion).getByRole('button', { name: /批准/ }));
    await waitFor(() => expect(resolveApproval).toHaveBeenCalledWith(groupId, sideRunId, sideApproval.approval_id, true));
  });

  it('marks only terminal side answers unread, not planning or tool progress', () => {
    useGroupStore.getState().ingest([makeGroup()]);
    useGroupStore.setState({ runtimeSummaries: { [groupId]: runtimeSummary() } });
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    render(<AgentStatusBar projectId="legacy-calculus" />);
    expect(screen.getByRole('button', { name: /进行中/ })).not.toHaveClass('side-run-unread');
    const terminal = structuredClone(snapshot()) as any;
    terminal.runs = terminal.runs.map((run: any) => run.kind === 'side'
      ? { ...run, status: 'succeeded', version: 2, last_seq: 2, finished_at: '2026-08-18T00:00:03Z' }
      : run);
    terminal.summary.active_side_task_count = 0;
    act(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, terminal));
    const unread = screen.getByRole('button', { name: /未读回答，点击标记已读/ });
    fireEvent.click(unread);
    expect(screen.queryByRole('button', { name: /未读回答/ })).not.toBeInTheDocument();
  });

  it('does not infer a Verifier rejection from a generic executor failure', () => {
    const failed = taskRunFixture({ status: 'failed', failure_type: 'provider_failure' }) as TaskRun;
    expect(verifierLabel(failed, [])).toBe('任务执行失败，Verifier 结论未上报');
    expect(sideAnswerSequence({ run: { ...failed, kind: 'side' }, events: [], needsResync: false })).toBeNull();
  });
});

describe('Group creation wizard', () => {
  it('uses distinct role defaults for all four templates', () => {
    expect(roleDefaultsForTemplate('course_study')).toEqual({ knowledge: true, planning: true, coach: true });
    expect(roleDefaultsForTemplate('exam_sprint')).toEqual({ knowledge: true, planning: true, coach: true });
    expect(roleDefaultsForTemplate('general_assistant')).toEqual({ knowledge: false, planning: false, coach: false });
    expect(roleDefaultsForTemplate('custom')).toEqual({ knowledge: false, planning: false, coach: false });
  });
  it('moves focus into the dialog, closes with Escape, and restores the trigger', () => {
    const onClose = vi.fn();
    const trigger = document.createElement('button');
    trigger.textContent = '打开创建向导';
    document.body.append(trigger);
    trigger.focus();

    const view = render(<GroupCreationWizard open onClose={onClose} />);
    expect(screen.getByRole('button', { name: '关闭创建群组' })).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledOnce();

    view.rerender(<GroupCreationWizard open={false} onClose={onClose} />);
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it('does not steal focus when the wizard parent rerenders with a new callback', () => {
    const view = render(<GroupCreationWizard open onClose={() => undefined} />);
    const input = screen.getByLabelText('群组名称');
    input.focus();
    view.rerender(<GroupCreationWizard open onClose={() => undefined} />);
    expect(input).toHaveFocus();
  });

  it('portals above the app root and makes the background inert while open', () => {
    const appRoot = document.createElement('div');
    appRoot.id = 'root';
    document.body.append(appRoot);
    const view = render(<GroupCreationWizard open onClose={() => undefined} />);
    expect(screen.getByRole('dialog')).toHaveProperty('parentElement');
    expect(screen.getByRole('dialog').closest('#studycrew-overlay-host')).not.toBeNull();
    expect(appRoot).toHaveAttribute('aria-hidden', 'true');
    expect((appRoot as HTMLElement & { inert: boolean }).inert).toBe(true);
    view.unmount();
    expect(appRoot).not.toHaveAttribute('aria-hidden');
    expect((appRoot as HTMLElement & { inert: boolean }).inert).toBe(false);
    appRoot.remove();
  });

  it('keeps Coordinator and Verifier mandatory and validates the workspace directory', async () => {
    const createGroup = vi.fn().mockResolvedValue(makeGroup());
    useGroupStore.setState({ createGroup } as never);
    render(<GroupCreationWizard open onClose={() => undefined} />);

    expect(screen.getByRole('checkbox', { name: 'Coordinator' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'Verifier' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('群组名称'), { target: { value: '线代复习' } });
    fireEvent.click(screen.getByRole('button', { name: '创建群组' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('工作目录');
    expect(createGroup).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('工作目录'), { target: { value: 'D:\\StudyCrew\\Linear' } });
    fireEvent.click(screen.getByRole('button', { name: '创建群组' }));
    await waitFor(() => expect(createGroup).toHaveBeenCalledWith(expect.objectContaining({
      name: '线代复习', template: 'course_study', project_dir: 'D:\\StudyCrew\\Linear',
    })));
  });

  it('offers all templates, sends constrained model/tool overrides, and recovers after backend failure', async () => {
    const createGroup = vi.fn()
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce(makeGroup());
    useGroupStore.setState({ createGroup } as never);
    render(<GroupCreationWizard open onClose={() => undefined} />);

    expect(screen.getAllByRole('radio')).toHaveLength(4);
    fireEvent.click(screen.getByRole('radio', { name: /自定义组/ }));
    fireEvent.change(screen.getByLabelText('群组名称'), { target: { value: '实验室' } });
    fireEvent.change(screen.getByLabelText('工作目录'), { target: { value: 'D:\\StudyCrew\\Lab' } });
    fireEvent.change(screen.getByLabelText('首选聊天模型'), { target: { value: 'provider/chat-safe' } });
    fireEvent.change(screen.getByLabelText('群组允许工具'), { target: { value: 'local_files, calculator' } });
    fireEvent.click(screen.getByRole('button', { name: /添加受控专家 Agent/ }));
    fireEvent.change(screen.getByLabelText('专家 1 名称'), { target: { value: '线代专家' } });
    fireEvent.change(screen.getByLabelText('专家 1 专业范围'), { target: { value: '线性代数概念讲解' } });
    fireEvent.click(screen.getByRole('button', { name: '创建群组' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('backend unavailable');
    expect(screen.getByRole('button', { name: '创建群组' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: '创建群组' }));
    await waitFor(() => expect(createGroup).toHaveBeenLastCalledWith(expect.objectContaining({
      template: 'custom',
      model_overrides: { chat: 'provider/chat-safe' },
      tool_overrides: { group: ['local_files', 'calculator'] },
      experts: [expect.objectContaining({ name: '线代专家', specialty: '线性代数概念讲解' })],
    })));
  });

  it('resumes failed initial bindings without creating a duplicate Group', async () => {
    const createGroup = vi.fn().mockResolvedValue(makeGroup());
    const bindCourse = vi.fn().mockRejectedValueOnce(new Error('course offline')).mockResolvedValueOnce({});
    const onClose = vi.fn();
    useGroupStore.setState({ createGroup, bindCourse } as never);
    render(<GroupCreationWizard open onClose={onClose} />);
    fireEvent.change(screen.getByLabelText('群组名称'), { target: { value: '高数绑定' } });
    fireEvent.change(screen.getByLabelText('工作目录'), { target: { value: 'D:\\StudyCrew\\Calculus' } });
    fireEvent.change(screen.getByLabelText('课程 ID'), { target: { value: 'course-calculus' } });
    fireEvent.click(screen.getByRole('button', { name: '创建群组' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('群组已创建，但初始绑定未完成');
    expect(createGroup).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '继续完成绑定' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(createGroup).toHaveBeenCalledTimes(1);
    expect(bindCourse).toHaveBeenCalledTimes(2);
  });
});

describe('Group lifecycle management', () => {
  it('does not restart the focus trap when management receives a new callback', () => {
    const view = render(<GroupManagementDialog group={makeGroup()} open onClose={() => undefined} />);
    const input = screen.getByLabelText('群组名称');
    input.focus();
    view.rerender(<GroupManagementDialog group={makeGroup()} open onClose={() => undefined} />);
    expect(input).toHaveFocus();
  });
  it('dispatches archive and restore through the canonical Group identity', async () => {
    const archiveGroup = vi.fn().mockResolvedValue(makeGroup());
    const restoreGroup = vi.fn().mockResolvedValue(makeGroup());
    useGroupStore.setState({ archiveGroup, restoreGroup } as never);
    const onClose = () => undefined;
    const view = render(<GroupManagementDialog group={makeGroup()} open onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: '归档群组' }));
    await waitFor(() => expect(archiveGroup).toHaveBeenCalledWith(groupId));

    view.rerender(<GroupManagementDialog group={{ ...makeGroup(), lifecycle: 'archived' }} open onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: '恢复群组' }));
    await waitFor(() => expect(restoreGroup).toHaveBeenCalledWith(groupId));
  });

  it('requires the backend delete preview token and exact second confirmation', async () => {
    const confirmationToken = 'f'.repeat(64);
    const previewDelete = vi.fn().mockResolvedValue({
      group_id: groupId, confirmation_token: confirmationToken, agent_count: 2,
      course_binding_count: 0, source_link_count: 0,
      exclusive_source_ids: [], shared_source_ids: [], source_reference_counts: {},
    });
    const confirmedDelete = vi.fn().mockResolvedValue({});
    useGroupStore.setState({ previewDelete, confirmedDelete } as never);
    render(<GroupManagementDialog group={makeGroup()} open onClose={() => undefined} />);

    fireEvent.click(screen.getByRole('button', { name: /查看删除影响/ }));
    expect(await screen.findByTestId('delete-preview')).toHaveTextContent('共享资料保留');
    const deleteButton = screen.getByRole('button', { name: /永久删除/ });
    expect(deleteButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText('确认群组名称'), { target: { value: '高数冲刺' } });
    fireEvent.click(deleteButton);
    await waitFor(() => expect(confirmedDelete).toHaveBeenCalledWith(groupId, confirmationToken));
  });

  it('invalidates a delete preview when Group identity or version changes', async () => {
    const previewDelete = vi.fn().mockImplementation(async (id: string) => ({
      group_id: id, confirmation_token: 'e'.repeat(64), agent_count: 2,
      course_binding_count: 0, source_link_count: 0, exclusive_source_ids: [], shared_source_ids: [], source_reference_counts: {},
    }));
    useGroupStore.setState({ previewDelete } as never);
    const close = () => undefined;
    const view = render(<GroupManagementDialog group={makeGroup()} open onClose={close} />);
    fireEvent.click(screen.getByRole('button', { name: /查看删除影响/ }));
    expect(await screen.findByTestId('delete-preview')).toBeInTheDocument();
    view.rerender(<GroupManagementDialog group={makeGroup(otherGroupId, 'legacy-english')} open onClose={close} />);
    await waitFor(() => expect(screen.queryByTestId('delete-preview')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /查看删除影响/ })).toBeInTheDocument();
  });
});

describe('Today summary', () => {
  it('aggregates summary fields only and preserves owning Group navigation', () => {
    const first = makeGroup();
    const second = makeGroup(otherGroupId, 'legacy-english');
    const summaries = {
      [groupId]: {
        group_id: groupId, lifecycle: 'active', active_agent_count: 2,
        course_binding_count: 1, source_count: 2, shared_source_count: 0, budget: 5,
        active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 2,
        active_side_task_count: 1, pending_approval_count: 1,
        budget_usage: { wall_clock_seconds: 5, model_turns: 1, tool_calls: 0, tokens: 20, cost_usd: 0.01 },
        indexing_state: 'failed', updated_at: '2026-08-18T00:00:00Z',
      },
    } as const;
    const rows = buildTodayRows([first, second], summaries);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ groupId, groupName: '高数冲刺', pendingApprovals: 1 });
    expect(JSON.stringify(rows)).not.toContain('roster');

    const onOpenGroup = vi.fn();
    render(<TodayView groups={[first, second]} summaries={summaries} onOpenGroup={onOpenGroup} />);
    fireEvent.click(within(screen.getByText('高数冲刺').closest('article')!).getByRole('button'));
    expect(onOpenGroup).toHaveBeenCalledWith(groupId, 'legacy-calculus');
  });
});

// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import type { Group } from '../contract/group';
import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import AgentStatusBar, {
  controlCommandsForStatus,
  groupForLegacyProject,
} from './AgentStatusBar';
import GroupCreationWizard from './GroupCreationWizard';
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

beforeEach(() => {
  useGroupStore.setState({
    groups: {}, order: [], runtimeSummaries: {}, sourceReferenceCounts: {},
    loading: false, error: null,
  });
  useTaskRuntimeStore.getState().resetAll();
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
    expect(controlCommandsForStatus('running')).toEqual(['pause', 'cancel']);
    expect(controlCommandsForStatus('paused')).toEqual(['resume', 'cancel']);
    expect(controlCommandsForStatus('failed')).toEqual(['reconcile']);
    expect(controlCommandsForStatus('succeeded')).toEqual([]);
  });

  it('shows only the public tool projection in the details drawer', () => {
    useGroupStore.getState().ingest([makeGroup()]);
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
  });
});

describe('Group creation wizard', () => {
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
    fireEvent.change(screen.getByLabelText('专家名称'), { target: { value: '线代专家' } });
    fireEvent.change(screen.getByLabelText('专业范围'), { target: { value: '线性代数概念讲解' } });
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
});

describe('Group lifecycle management', () => {
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

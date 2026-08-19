import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { useTaskRuntimeStore } from './taskRuntime';

const groupId = `grp_${'1'.repeat(32)}` as const;
const otherGroupId = `grp_${'9'.repeat(32)}` as const;
const runId = `run_${'2'.repeat(32)}`;

function event(seq: number, version = seq, overrides: Record<string, unknown> = {}): unknown {
  return {
    event_id: `evt_${seq.toString(16).padStart(32, '0')}`,
    run_id: runId,
    group_id: groupId,
    seq,
    version,
    event_type: 'status_changed',
    public_payload: { status: 'running' },
    sensitivity: 'public',
    created_at: '2026-08-18T00:00:01Z',
    schema_version: 1,
    ...overrides,
  };
}

function snapshot(runs: unknown[] = [taskRunFixture()], pendingApprovals: unknown[] = []): unknown {
  return {
    schema_version: 1,
    group_id: groupId,
    runs,
    pending_approvals: pendingApprovals,
    summary: {
      active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 0,
      active_side_task_count: 0, pending_approval_count: pendingApprovals.length,
      budget_usage: { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 },
      indexing_state: 'idle',
    },
  };
}

describe('authoritative Task Runtime projection', () => {
  beforeEach(() => {
    useTaskRuntimeStore.setState({ byRun: {}, byGroup: {}, approvals: {}, error: null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('ingests a group snapshot and applies only contiguous monotonic events', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(1);
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(2))).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.version).toBe(2);
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(2))).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.events).toHaveLength(1);

    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(4))).toBe(false);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.needsResync).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(2);
  });

  it('rejects forged group snapshots and events without mutating projection', () => {
    expect(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture({ group_id: otherGroupId }),
    ]))).toThrow(/crosses/);
    expect(useTaskRuntimeStore.getState().byRun).toEqual({});

    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    expect(() => useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2, {
      group_id: otherGroupId,
    }))).toThrow(/crosses/);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(1);
  });

  it('rejects a stale reconnect snapshot rather than rolling local state back', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    useTaskRuntimeStore.getState().applyEvent(groupId, event(2));
    expect(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot()))
      .toThrow(/Stale/);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.version).toBe(2);
  });

  it('projects budget and approval resolution and replaces absent approvals', () => {
    const approval = {
      approval_id: `apr_${'4'.repeat(32)}`, run_id: runId, group_id: groupId,
      kind: 'tool', summary: 'Confirm', risk: 'high', requested_capabilities: ['send'],
      tool_name: 'send', operation_scope: { destination: 'portal' }, purpose: 'send_result',
      expires_at: '2026-08-18T01:00:00Z', status: 'pending',
      created_at: '2026-08-18T00:00:00Z', resolved_at: null, schema_version: 1,
    };
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([], [approval]));
    expect(useTaskRuntimeStore.getState().approvals[approval.approval_id]).toBeDefined();
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    expect(useTaskRuntimeStore.getState().approvals[approval.approval_id]).toBeUndefined();
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2, {
      event_type: 'budget_updated',
      public_payload: {
        status: 'running', usage: { wall_clock_seconds: 1, model_turns: 1, tool_calls: 0, tokens: 5, cost_usd: 0.1 },
        limits: { wall_clock_seconds: 900, model_turns: 20, tool_calls: 30, tokens: 100000, cost_usd: 5 },
        exhausted_dimension: null,
      },
    }))).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.budget_usage.tokens).toBe(5);
  });

  it('forces resync on an equal-version lower-seq or version jump', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture({ version: 3, last_seq: 3 }),
    ]));
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 3))).toBe(false);
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(4, 5))).toBe(false);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.needsResync).toBe(true);
  });

  it('installs reconnect snapshot before buffered events and converges without refresh', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture({ version: 3, last_seq: 3 }),
    ]));
    // Events already represented by the snapshot are harmless replay duplicates.
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2))).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.needsResync).toBe(false);
    // The first post-snapshot event and the next live event remain contiguous.
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(4, 4))).toBe(true);
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, event(5, 5))).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(5);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.events).toHaveLength(2);
    expect(useTaskRuntimeStore.getState().error).toBeNull();
  });

  it.each([
    {
      name: 'gap snapshot',
      replay: {
        schema_version: 1, run_id: runId, group_id: groupId,
        after_seq: 1, last_seq: 3, version: 3, gap: true, events: [],
        snapshot: {
          schema_version: 1,
          run: taskRunFixture({ run_id: `run_${'8'.repeat(32)}`, version: 3, last_seq: 3 }),
          last_seq: 3, version: 3,
        },
      },
    },
    {
      name: 'contiguous event',
      replay: {
        schema_version: 1, run_id: runId, group_id: groupId,
        after_seq: 1, last_seq: 2, version: 2, gap: false, snapshot: null,
        events: [event(2, 2, { run_id: `run_${'8'.repeat(32)}` })],
      },
    },
  ])('rejects a forged nested $name without cross-run mutation', async ({ replay }) => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(replay), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));
    await expect(useTaskRuntimeStore.getState().reconcile(groupId, runId)).rejects.toThrow(/replay/i);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(1);
    expect(useTaskRuntimeStore.getState().byRun[`run_${'8'.repeat(32)}`]).toBeUndefined();
  });

  it('refuses forged Group/Run/Approval identity before approval dispatch', async () => {
    const approval = {
      approval_id: `apr_${'4'.repeat(32)}`, run_id: runId, group_id: groupId,
      kind: 'tool', summary: 'Confirm export', risk: 'high', requested_capabilities: ['export'],
      tool_name: 'export', operation_scope: { destination: 'outside' }, purpose: 'export_notes',
      expires_at: null, status: 'pending', created_at: '2026-08-18T00:00:00Z',
      resolved_at: null, schema_version: 1,
    };
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([taskRunFixture()], [approval]));
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(useTaskRuntimeStore.getState().resolveApproval(
      otherGroupId, runId, approval.approval_id, true,
    )).rejects.toThrow(/Unknown run/);
    await expect(useTaskRuntimeStore.getState().resolveApproval(
      groupId, runId, `apr_${'8'.repeat(32)}`, true,
    )).rejects.toThrow(/identity/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

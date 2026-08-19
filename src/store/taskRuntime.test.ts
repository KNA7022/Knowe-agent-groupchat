import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { scheduleTaskRuntimeAuthorityRefresh, useTaskRuntimeStore } from './taskRuntime';

const groupId = `grp_${'1'.repeat(32)}` as const;
const otherGroupId = `grp_${'9'.repeat(32)}` as const;
const runId = `run_${'2'.repeat(32)}`;
const authorityEpoch = `epoch_${'a'.repeat(32)}`;
let snapshotRevision = 0;

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

function snapshot(runs: unknown[] = [taskRunFixture()], pendingApprovals: unknown[] = [], revision = ++snapshotRevision): unknown {
  return {
    schema_version: 1,
    group_id: groupId,
    authority_epoch: authorityEpoch,
    group_snapshot_revision: revision,
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
    snapshotRevision = 0;
    useTaskRuntimeStore.getState().resetAll();
  });

  afterEach(() => {
    vi.useRealTimers();
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

  it('keeps the latest per-Group authority when S2(A+B) resolves before stale S1(A)', async () => {
    const secondRunId = `run_${'7'.repeat(32)}`;
    const deferred: Array<(response: Response) => void> = [];
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => deferred.push(resolve))));

    const s1 = useTaskRuntimeStore.getState().refreshGroup(groupId);
    const s2 = useTaskRuntimeStore.getState().refreshGroup(groupId);
    await vi.waitFor(() => expect(deferred).toHaveLength(2));
    const s2Snapshot = snapshot([
      taskRunFixture(),
      taskRunFixture({ run_id: secondRunId, task_id: `task_${'7'.repeat(32)}`, updated_at: '2026-08-18T00:00:02Z' }),
    ], [], 2) as { summary: { active_primary_run_id: string | null } };
    s2Snapshot.summary.active_primary_run_id = secondRunId;
    deferred[1]!(new Response(JSON.stringify(s2Snapshot), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await s2;
    deferred[0]!(new Response(JSON.stringify(snapshot([taskRunFixture()], [], 1)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    await s1;

    expect(useTaskRuntimeStore.getState().byRun[secondRunId]).toBeDefined();
    expect(useTaskRuntimeStore.getState().byGroup[groupId]).toContain(secondRunId);
    expect(useTaskRuntimeStore.getState().groupSummaries[groupId]?.active_primary_run_id).toBe(secondRunId);
  });

  it('rejects an older direct Group snapshot as one whole authority projection', () => {
    const secondRunId = `run_${'7'.repeat(32)}`;
    const indexRunId = `run_${'6'.repeat(32)}`;
    const approval = {
      approval_id: `apr_${'4'.repeat(32)}`, run_id: secondRunId, group_id: groupId,
      kind: 'tool', summary: 'Confirm', risk: 'high', requested_capabilities: ['send'],
      tool_name: 'send', operation_scope: {}, purpose: 'send', expires_at: null,
      status: 'pending', created_at: '2026-08-18T00:00:00Z', resolved_at: null, schema_version: 1,
    };
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture({ budget_usage: { wall_clock_seconds: 1, model_turns: 1, tool_calls: 1, tokens: 1, cost_usd: 0.01 } }),
      taskRunFixture({ run_id: secondRunId, task_id: `task_${'7'.repeat(32)}`, kind: 'side',
        read_only: true, risk: 'low', status: 'running', updated_at: '2026-08-18T00:00:03Z',
        budget_usage: { wall_clock_seconds: 2, model_turns: 2, tool_calls: 2, tokens: 2, cost_usd: 0.02 } }),
      taskRunFixture({ run_id: indexRunId, task_id: `task_${'6'.repeat(32)}`, kind: 'side',
        queue_class: 'index', read_only: true, risk: 'low', status: 'queued',
        budget_usage: { wall_clock_seconds: 3, model_turns: 3, tool_calls: 3, tokens: 3, cost_usd: 0.03 } }),
    ], [approval], 2));
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture({ updated_at: '2026-08-18T00:00:01Z' }),
    ], [], 1));
    const state = useTaskRuntimeStore.getState();
    expect(state.byRun[secondRunId]).toBeDefined();
    expect(state.byRun[indexRunId]).toBeDefined();
    expect(state.approvals[approval.approval_id]).toBeDefined();
    expect(state.groupSummaries[groupId]).toMatchObject({
      active_primary_run_id: runId, queued_task_count: 1, active_side_task_count: 1,
      pending_approval_count: 1, indexing_state: 'queued', budget_usage: { tokens: 6 },
    });
  });

  it('accepts a new authority epoch only after an explicit runtime reset', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    const nextEpoch = {
      ...(snapshot([taskRunFixture({ status: 'paused', version: 2, last_seq: 2 })], [], 1) as Record<string, unknown>),
      authority_epoch: `epoch_${'b'.repeat(32)}`,
    };
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, nextEpoch);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.status).toBe('running');
    useTaskRuntimeStore.getState().resetAll();
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, nextEpoch);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.status).toBe('paused');
  });

  it('atomically clears active primary after a terminal gap snapshot and retains public history', async () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2, {
      event_type: 'tool_prepared',
      public_payload: {
        status: 'running', invocation_id: `tool_${'6'.repeat(32)}`, tool_name: 'search',
        disposition: 'created', usage: { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 },
      },
    }));
    const terminalGroupSnapshot = snapshot([taskRunFixture({
      status: 'succeeded', version: 4, last_seq: 4, finished_at: '2026-08-18T00:00:04Z',
    })], [], 2);
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => new Response(JSON.stringify(
      String(input).includes('/replay') ? {
      schema_version: 1, run_id: runId, group_id: groupId,
      authority_epoch: authorityEpoch, group_snapshot_revision: 2,
      after_seq: 2, last_seq: 4, version: 4, gap: true, events: [],
      snapshot: {
        schema_version: 1,
        authority_epoch: authorityEpoch, group_snapshot_revision: 2,
        run: taskRunFixture({ status: 'succeeded', version: 4, last_seq: 4, finished_at: '2026-08-18T00:00:04Z' }),
        last_seq: 4, version: 4,
      },
    } : terminalGroupSnapshot), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    await useTaskRuntimeStore.getState().reconcile(groupId, runId);
    const state = useTaskRuntimeStore.getState();
    expect(state.groupSummaries[groupId]?.active_primary_run_id).toBeNull();
    expect(state.byRun[runId]?.events.map((item) => item.event_type)).toContain('tool_prepared');
  });

  it('preserves bounded public events across the scheduled authority refresh', async () => {
    vi.useFakeTimers();
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2, {
      event_type: 'verifier_decision',
      public_payload: { status: 'running', decision_id: `vfd_${'8'.repeat(32)}`, outcome: 'continue', approved: true, evidence_count: 2 },
    }));
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot([
      taskRunFixture({ version: 2, last_seq: 2 }),
    ])), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    scheduleTaskRuntimeAuthorityRefresh(groupId, 80);
    await vi.advanceTimersByTimeAsync(80);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.events).toHaveLength(1);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.events[0]?.event_type).toBe('verifier_decision');
  });

  it('retains the newest 200 public events, deduplicates replay, and keeps tool/verifier/approval history after refresh', async () => {
    vi.useFakeTimers();
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot());
    for (let seq = 2; seq <= 203; seq += 1) {
      useTaskRuntimeStore.getState().applyEvent(groupId, event(seq));
    }
    const tool = event(204, 204, {
      event_type: 'tool_prepared',
      public_payload: {
        status: 'running', invocation_id: `tool_${'6'.repeat(32)}`, tool_name: 'search',
        disposition: 'created', usage: { wall_clock_seconds: 1, model_turns: 1, tool_calls: 1, tokens: 8, cost_usd: 0.01 },
      },
    });
    const verifier = event(205, 205, {
      event_type: 'verifier_decision',
      public_payload: { status: 'running', decision_id: `vfd_${'8'.repeat(32)}`, outcome: 'continue', approved: true, evidence_count: 2 },
    });
    const approval = {
      approval_id: `apr_${'4'.repeat(32)}`, run_id: runId, group_id: groupId,
      kind: 'tool', summary: 'Confirm', risk: 'high', requested_capabilities: ['send'],
      tool_name: 'send', operation_scope: { destination: 'portal' }, purpose: 'send_result',
      expires_at: null, status: 'pending', created_at: '2026-08-18T00:00:00Z',
      resolved_at: null, schema_version: 1,
    };
    const approvalEvent = event(206, 206, {
      event_type: 'approval_requested', public_payload: { status: 'waiting_approval', approval },
    });
    for (const item of [tool, verifier, approvalEvent]) {
      expect(useTaskRuntimeStore.getState().applyEvent(groupId, item)).toBe(true);
    }
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, approvalEvent)).toBe(true);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.events).toHaveLength(200);

    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot([
      taskRunFixture({ version: 206, last_seq: 206, status: 'waiting_approval' }),
    ], [approval], 206)), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    scheduleTaskRuntimeAuthorityRefresh(groupId, 80);
    await vi.advanceTimersByTimeAsync(80);
    const events = useTaskRuntimeStore.getState().byRun[runId]?.events || [];
    expect(events).toHaveLength(200);
    expect(events.filter((item) => item.seq === 206)).toHaveLength(1);
    expect(events.map((item) => item.event_type)).toEqual(expect.arrayContaining([
      'tool_prepared', 'verifier_decision', 'approval_requested',
    ]));
    expect(useTaskRuntimeStore.getState().approvals[approval.approval_id]).toBeDefined();
  });

  it('projects foreground side and index states from events even when authority refresh fails', async () => {
    const sideRunId = `run_${'6'.repeat(32)}`;
    const indexRunId = `run_${'7'.repeat(32)}`;
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, snapshot([
      taskRunFixture(),
      taskRunFixture({ run_id: sideRunId, task_id: `task_${'6'.repeat(32)}`, kind: 'side', read_only: true, risk: 'low', status: 'queued' }),
      taskRunFixture({ run_id: indexRunId, task_id: `task_${'7'.repeat(32)}`, kind: 'side', queue_class: 'index', read_only: true, risk: 'low', status: 'queued' }),
    ]));
    expect(useTaskRuntimeStore.getState().groupSummaries[groupId]?.indexing_state).toBe('queued');
    const statusEvent = (targetRunId: string, seq: number, status: string) => event(seq, seq, {
      run_id: targetRunId, event_id: `evt_${targetRunId === sideRunId ? '6' : '7'.repeat(1)}${seq.toString(16).padStart(31, '0')}`,
      event_type: 'status_changed', public_payload: { status },
    });
    useTaskRuntimeStore.getState().applyEvent(groupId, statusEvent(sideRunId, 2, 'running'));
    useTaskRuntimeStore.getState().applyEvent(groupId, statusEvent(indexRunId, 2, 'running'));
    expect(useTaskRuntimeStore.getState().groupSummaries[groupId]).toMatchObject({
      active_side_task_count: 1, indexing_state: 'running',
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'offline' }), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    })));
    await expect(useTaskRuntimeStore.getState().refreshGroup(groupId)).rejects.toThrow('offline');
    expect(useTaskRuntimeStore.getState().groupSummaries[groupId]?.indexing_state).toBe('running');
    useTaskRuntimeStore.getState().applyEvent(groupId, statusEvent(sideRunId, 3, 'paused'));
    useTaskRuntimeStore.getState().applyEvent(groupId, statusEvent(indexRunId, 3, 'failed'));
    expect(useTaskRuntimeStore.getState().groupSummaries[groupId]).toMatchObject({
      active_side_task_count: 0, indexing_state: 'failed',
    });
  });
});

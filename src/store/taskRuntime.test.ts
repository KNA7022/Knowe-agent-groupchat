import { beforeEach, describe, expect, it } from 'vitest';

import { taskRunFixture } from '../contract/taskRuntime.test';
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

describe('authoritative Task Runtime projection', () => {
  beforeEach(() => {
    useTaskRuntimeStore.setState({ byRun: {}, byGroup: {}, approvals: {}, error: null });
  });

  it('ingests a group snapshot and applies only contiguous monotonic events', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      schema_version: 1,
      group_id: groupId,
      runs: [taskRunFixture()],
      pending_approvals: [],
    });
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
    expect(() => useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      runs: [taskRunFixture({ group_id: otherGroupId })], pending_approvals: [],
    })).toThrow(/crosses/);
    expect(useTaskRuntimeStore.getState().byRun).toEqual({});

    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      runs: [taskRunFixture()], pending_approvals: [],
    });
    expect(() => useTaskRuntimeStore.getState().applyEvent(groupId, event(2, 2, {
      group_id: otherGroupId,
    }))).toThrow(/crosses/);
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.last_seq).toBe(1);
  });

  it('does not roll a newer local version back with a stale reconnect snapshot', () => {
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      runs: [taskRunFixture()], pending_approvals: [],
    });
    useTaskRuntimeStore.getState().applyEvent(groupId, event(2));
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      runs: [taskRunFixture()], pending_approvals: [],
    });
    expect(useTaskRuntimeStore.getState().byRun[runId]?.run.version).toBe(2);
  });
});

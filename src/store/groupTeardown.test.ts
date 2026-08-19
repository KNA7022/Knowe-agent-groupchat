import { beforeEach, describe, expect, it, vi } from 'vitest';

import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { teardownGroup } from './groupTeardown';
import { useGroupStore } from './groups';
import { useKnowledgeStore } from './knowledge';
import { getState as getDirectoryState, openRequest } from './directoryRecovery';
import { useRecordsStore } from './records';
import { agentBindingKey, useSettingsStore } from './settings';
import { useSideTaskReadStore } from './sideTaskRead';
import { pendingTransientFrameFallbackCount, useKnoweStore } from './store';
import { scheduleTaskRuntimeAuthorityRefresh, useTaskRuntimeStore } from './taskRuntime';

const groupId = `grp_${'4'.repeat(32)}` as const;
const runId = `run_${'2'.repeat(32)}`;

describe('confirmed Group renderer teardown', () => {
  beforeEach(() => {
    useTaskRuntimeStore.getState().resetAll();
    useSideTaskReadStore.getState().reset();
    useGroupStore.setState({ groups: {}, order: [], runtimeSummaries: {}, sourceReferenceCounts: {} });
    useKnowledgeStore.setState({ activeScope: null, activeScopeKey: 'legacy:all', preview: null });
  });

  it('purges runs, approvals, events, evidence, watermarks, preview and legacy conversations', () => {
    vi.useFakeTimers();
    const approvalId = `apr_${'3'.repeat(32)}`;
    useKnoweStore.getState().ensureProject('legacy-deleted', 'Deleted');
    useKnoweStore.getState().applyIncrementalEvents('legacy-deleted', [
      { type: 'agent_thinking', project_id: 'legacy-deleted', agent_id: 'coordinator', seq: 1 },
      { type: 'message', project_id: 'legacy-deleted', agent_id: 'coordinator',
        content: 'Late frame must not commit', seq: 2 },
    ]);
    expect(pendingTransientFrameFallbackCount('legacy-deleted')).toBe(1);
    useKnoweStore.getState().enterDm('legacy-deleted', 'coordinator');
    useKnowledgeStore.getState().setScope({
      groupIds: [groupId], projectIds: ['legacy-deleted'], sourceIds: [], courseId: null,
    });
    useKnowledgeStore.getState().openPreview({ kind: 'profile', card: null, projectId: 'legacy-deleted' });
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      schema_version: 1, group_id: groupId,
      runs: [taskRunFixture({ group_id: groupId, evidence: [{
        evidence_id: 'evidence', source_id: 'source', version_id: 'version', locator: 'page:1', excerpt_ref: '', schema_version: 1,
      }] })],
      pending_approvals: [{
        approval_id: approvalId, run_id: runId, group_id: groupId, kind: 'tool', summary: 'read',
        risk: 'low', requested_capabilities: ['read'], tool_name: 'files', operation_scope: {}, purpose: 'read',
        expires_at: null, status: 'pending', created_at: '2026-08-18T00:00:00Z', resolved_at: null, schema_version: 1,
      }],
      summary: {
        active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 0,
        active_side_task_count: 0, pending_approval_count: 1,
        budget_usage: { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 }, indexing_state: 'idle',
      },
    });
    useSideTaskReadStore.getState().acknowledge(runId, 9);
    useGroupStore.setState({ sourceReferenceCounts: { [groupId]: { source: 1 } } });
    openRequest({ project_id: 'legacy-deleted', project_name: 'Deleted', previous_dir: 'x',
      reason: 'missing', request_id: 'req-1' });
    useSettingsStore.setState((state) => ({
      groupApprovalTimeouts: { ...state.groupApprovalTimeouts, [groupId]: 10, 'legacy-deleted': 20 },
      agentModels: { ...state.agentModels, [agentBindingKey('legacy-deleted', 'agent')]: {
        provider: 'openai', model: 'test', apiKey: '', hasApiKey: false, clearApiKey: false,
        baseUrl: '', sealed: true,
      } },
    }));
    useRecordsStore.setState({ open: true, items: [{ seq: 1, type: 'message', agent_id: 'agent',
      content: 'legacy-deleted', ts: null, has_files: false, has_images: false,
      has_videos: false, has_links: false }], handoffs: [] });
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    scheduleTaskRuntimeAuthorityRefresh(groupId, 80);

    teardownGroup(groupId, 'legacy-deleted');
    expect(useTaskRuntimeStore.getState().applyEvent(groupId, {
      event_id: `evt_${'9'.repeat(32)}`, run_id: runId, group_id: groupId, seq: 2, version: 2,
      event_type: 'status_changed', public_payload: { status: 'running' }, sensitivity: 'public',
      created_at: '2026-08-18T00:00:02Z', schema_version: 1,
    })).toBe(false);
    useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId, {
      schema_version: 1, group_id: groupId, authority_epoch: `epoch_${'a'.repeat(32)}`,
      group_snapshot_revision: 99, runs: [taskRunFixture({ group_id: groupId })],
      pending_approvals: [], summary: {
        active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 0,
        active_side_task_count: 0, pending_approval_count: 0,
        budget_usage: { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 }, indexing_state: 'idle',
      },
    });
    vi.advanceTimersByTime(100);
    expect(pendingTransientFrameFallbackCount('legacy-deleted')).toBe(0);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/task-runtime/snapshot'))).toBe(false);

    const runtime = useTaskRuntimeStore.getState();
    expect(runtime.byGroup[groupId]).toBeUndefined();
    expect(runtime.byRun[runId]).toBeUndefined();
    expect(runtime.approvals[approvalId]).toBeUndefined();
    expect(runtime.groupSummaries[groupId]).toBeUndefined();
    expect(useGroupStore.getState().runtimeSummaries[groupId]).toBeUndefined();
    expect(useSideTaskReadStore.getState().watermarks[runId]).toBeUndefined();
    expect(useKnowledgeStore.getState().preview).toBeNull();
    expect(getDirectoryState().entries['legacy-deleted']).toBeUndefined();
    expect(useRecordsStore.getState().items).toEqual([]);
    expect(useSettingsStore.getState().groupApprovalTimeouts[groupId]).toBeUndefined();
    expect(useSettingsStore.getState().agentModels[agentBindingKey('legacy-deleted', 'agent')]).toBeUndefined();
    expect(useGroupStore.getState().sourceReferenceCounts[groupId]).toBeUndefined();
    expect(Object.keys(useKnoweStore.getState().convs).some((id) => id.includes('legacy-deleted'))).toBe(false);
    const serializedStores = JSON.stringify({ runtime, groups: useGroupStore.getState(),
      knowledge: useKnowledgeStore.getState(), records: useRecordsStore.getState(),
      settings: useSettingsStore.getState(), legacy: useKnoweStore.getState(),
      directory: getDirectoryState() });
    for (const secret of [groupId, 'legacy-deleted', runId, 'evidence']) {
      expect(serializedStores).not.toContain(secret);
    }
    vi.useRealTimers();
  });
});

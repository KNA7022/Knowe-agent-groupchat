import { beforeEach, describe, expect, it } from 'vitest';

import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { teardownGroup } from './groupTeardown';
import { useGroupStore } from './groups';
import { useKnowledgeStore } from './knowledge';
import { useSideTaskReadStore } from './sideTaskRead';
import { useKnoweStore } from './store';
import { useTaskRuntimeStore } from './taskRuntime';

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
    const approvalId = `apr_${'3'.repeat(32)}`;
    useKnoweStore.getState().ensureProject('legacy-deleted', 'Deleted');
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

    teardownGroup(groupId, 'legacy-deleted');

    const runtime = useTaskRuntimeStore.getState();
    expect(runtime.byGroup[groupId]).toBeUndefined();
    expect(runtime.byRun[runId]).toBeUndefined();
    expect(runtime.approvals[approvalId]).toBeUndefined();
    expect(runtime.groupSummaries[groupId]).toBeUndefined();
    expect(useGroupStore.getState().runtimeSummaries[groupId]).toBeUndefined();
    expect(useSideTaskReadStore.getState().watermarks[runId]).toBeUndefined();
    expect(useKnowledgeStore.getState().preview).toBeNull();
    expect(Object.keys(useKnoweStore.getState().convs).some((id) => id.includes('legacy-deleted'))).toBe(false);
    expect(JSON.stringify(runtime)).not.toContain(groupId);
  });
});

import { describe, expect, it } from 'vitest';

import {
  RunEventSchema,
  RuntimeReplaySchema,
  TaskRunSchema,
  ToolInvocationSchema,
} from './taskRuntime';
import { taskRunFixture } from './taskRuntime.fixture';
import { TaskRuntimeCmdSchema, TaskRuntimeResultSchema, validateInbound } from './envelope';

const groupId = `grp_${'1'.repeat(32)}`;
const runId = `run_${'2'.repeat(32)}`;

describe('Task Runtime public contracts', () => {
  it('accepts exact planned states and rejects unsafe side tasks', () => {
    expect(TaskRunSchema.parse(taskRunFixture()).status).toBe('running');
    expect(() => TaskRunSchema.parse(taskRunFixture({
      kind: 'side', read_only: false,
    }))).toThrow(/low-risk and read-only/);
    expect(() => TaskRunSchema.parse(taskRunFixture({ status: 'thinking' }))).toThrow();
  });

  it('rejects chain-of-thought and sensitive fields at the public event boundary', () => {
    const base = {
      event_id: `evt_${'4'.repeat(32)}`,
      run_id: runId,
      group_id: groupId,
      seq: 2,
      version: 2,
      event_type: 'status_changed',
      public_payload: { status: 'running' },
      sensitivity: 'public',
      created_at: '2026-08-18T00:00:01Z',
      schema_version: 1,
    };
    expect(RunEventSchema.parse(base).seq).toBe(2);
    expect(() => RunEventSchema.parse({
      ...base, public_payload: { nested: { reasoning_content: 'private' } },
    })).toThrow(/private data/);
    expect(() => RunEventSchema.parse({
      ...base, sensitive_payload: { document: 'private' },
    })).toThrow();
  });

  it('requires high-risk invocations to be non-replayable', () => {
    const tool = {
      invocation_id: `tool_${'5'.repeat(32)}`,
      run_id: runId,
      group_id: groupId,
      task_id: `task_${'3'.repeat(32)}`,
      tool_name: 'send',
      idempotency_key: 'send-1',
      risk: 'high',
      status: 'prepared',
      replay_allowed: false,
      public_summary: 'Approved send',
      operation_scope: { destination: 'example.edu' },
      purpose: 'send_result',
      grant_id: `grant_${'7'.repeat(32)}`,
      disposition: 'created',
      error_type: '',
      created_at: '2026-08-18T00:00:01Z',
      completed_at: null,
      schema_version: 1,
    };
    expect(ToolInvocationSchema.parse(tool).replay_allowed).toBe(false);
    expect(() => ToolInvocationSchema.parse({ ...tool, replay_allowed: true })).toThrow();
  });

  it('rejects unsupported versions, forged ids, nested credentials and PII values', () => {
    expect(() => TaskRunSchema.parse(taskRunFixture({ schema_version: 2 }))).toThrow();
    expect(() => TaskRunSchema.parse(taskRunFixture({ run_id: 'run_forged' }))).toThrow();
    const base = {
      event_id: `evt_${'6'.repeat(32)}`, run_id: runId, group_id: groupId,
      seq: 2, version: 2, event_type: 'status_changed', sensitivity: 'public',
      created_at: '2026-08-18T00:00:01Z', schema_version: 1,
    };
    expect(() => RunEventSchema.parse({
      ...base, public_payload: { status: 'running', summary: { access_token: 'secret' } },
    })).toThrow(/private data/);
    expect(() => RunEventSchema.parse({
      ...base, public_payload: { status: 'running', summary: 'student@example.edu' },
    })).toThrow(/private data/);
    for (const value of ['张三成绩95分，排名第2', 'Authorization: Bearer sk-secret']) {
      expect(() => RunEventSchema.parse({
        ...base, event_type: 'message_routed',
        public_payload: { route_label: 'queue_new', summary: value },
      })).toThrow(/private data/);
      expect(() => TaskRunSchema.parse(taskRunFixture({ title: value }))).toThrow(/sensitive/);
    }
  });

  it('binds every replay snapshot/event and cursor to the addressed run', () => {
    const snapshot = taskRunFixture({ version: 3, last_seq: 3 });
    const base = {
      schema_version: 1, run_id: runId, group_id: groupId,
      after_seq: 1, last_seq: 3, version: 3, gap: true, events: [],
      snapshot: { schema_version: 1, run: snapshot, last_seq: 3, version: 3 },
    };
    expect(RuntimeReplaySchema.parse(base).snapshot?.run.run_id).toBe(runId);
    expect(() => RuntimeReplaySchema.parse({
      ...base,
      snapshot: {
        ...base.snapshot,
        run: taskRunFixture({ run_id: `run_${'8'.repeat(32)}`, version: 3, last_seq: 3 }),
      },
    })).toThrow(/identity/);
    expect(() => RuntimeReplaySchema.parse({
      ...base, snapshot: { ...base.snapshot, last_seq: 2 },
    })).toThrow(/cursor/);
    expect(() => RuntimeReplaySchema.parse({
      ...base, gap: false, snapshot: null, last_seq: 2, version: 2,
      events: [{
        event_id: `evt_${'9'.repeat(32)}`, run_id: `run_${'8'.repeat(32)}`,
        group_id: groupId, seq: 2, version: 2, event_type: 'status_changed',
        public_payload: { status: 'running' }, sensitivity: 'public',
        created_at: '2026-08-18T00:00:01Z', schema_version: 1,
      }],
    })).toThrow(/identity/);
  });

  it('rejects a forged live envelope whose group differs from its RunEvent', () => {
    expect(validateInbound({
      type: 'task_runtime_event', project_id: 'project_live', project_name: 'Live',
      group_id: groupId, seq: 5, ts: '2026-08-18T00:00:01Z',
      event: {
        event_id: `evt_${'7'.repeat(32)}`, run_id: runId,
        group_id: `grp_${'8'.repeat(32)}`, seq: 2, version: 2,
        event_type: 'status_changed', public_payload: { status: 'running' },
        sensitivity: 'public', created_at: '2026-08-18T00:00:01Z', schema_version: 1,
      },
    })).toBeNull();
  });

  it('accepts the explicit recovered-continuation reapproval transport action', () => {
    expect(TaskRuntimeCmdSchema.parse({
      type: 'task_runtime', action: 'reapprove-recovered', group_id: groupId,
      continuation_id: `cont_${'a'.repeat(32)}`,
    }).action).toBe('reapprove-recovered');
    expect(() => TaskRuntimeCmdSchema.parse({
      type: 'task_runtime', action: 'reapprove-recovered', group_id: groupId,
      expires_at: 'not-a-date',
    })).toThrow();
    expect(() => TaskRuntimeCmdSchema.parse({
      type: 'task_runtime', action: 'reapprove-recovered', group_id: groupId,
      ttl_seconds: Number.POSITIVE_INFINITY,
    })).toThrow();
    expect(TaskRuntimeResultSchema.parse({
      type: 'task_runtime_result', action: 'reapprove-recovered',
      schema_version: 1, group_id: groupId,
    }).action).toBe('reapprove-recovered');
  });

  it('treats validated control ids and request digests as opaque, not PII text', () => {
    const opaqueGroup = 'grp_03a132975893445697135f16b50a5694';
    expect(RunEventSchema.parse({
      event_id: `evt_${'a'.repeat(32)}`, run_id: runId, group_id: opaqueGroup,
      seq: 1, version: 1, event_type: 'approval_requested', sensitivity: 'public',
      created_at: '2026-08-19T00:00:00Z', schema_version: 1,
      public_payload: {
        status: 'waiting_approval',
        approval: {
          approval_id: `apr_${'b'.repeat(32)}`, run_id: runId, group_id: opaqueGroup,
          kind: 'recovered_message_route', summary: 'Confirm recovered request', risk: 'high',
          requested_capabilities: ['route_message'], tool_name: '',
          operation_scope: { route: 'queue_new', request_digest: 'c'.repeat(64) },
          purpose: 'confirm_route', expires_at: '2026-08-19T00:10:00Z',
          status: 'pending', created_at: '2026-08-19T00:00:00Z', resolved_at: null,
          schema_version: 1,
        },
      },
    }).group_id).toBe(opaqueGroup);
  });
});

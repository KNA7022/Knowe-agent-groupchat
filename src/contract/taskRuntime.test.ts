import { describe, expect, it } from 'vitest';

import {
  RunEventSchema,
  TaskRunSchema,
  ToolInvocationSchema,
} from './taskRuntime';
import { taskRunFixture } from './taskRuntime.fixture';
import { validateInbound } from './envelope';

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
      tool_name: 'send',
      idempotency_key: 'send-1',
      risk: 'high',
      status: 'prepared',
      replay_allowed: false,
      public_summary: 'Approved send',
      operation_scope: { destination: 'example.edu' },
      purpose: 'send_result',
      disposition: 'created',
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
});

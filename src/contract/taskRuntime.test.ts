import { describe, expect, it } from 'vitest';

import {
  RunEventSchema,
  TaskRunSchema,
  ToolInvocationSchema,
} from './taskRuntime';

const groupId = `grp_${'1'.repeat(32)}`;
const runId = `run_${'2'.repeat(32)}`;

export function taskRunFixture(overrides: Record<string, unknown> = {}): unknown {
  return {
    run_id: runId,
    task_id: `task_${'3'.repeat(32)}`,
    group_id: groupId,
    kind: 'primary',
    title: 'Review calculus',
    status: 'running',
    queue_class: 'foreground',
    route_label: 'queue_new',
    read_only: false,
    risk: 'low',
    parent_run_id: null,
    version: 1,
    last_seq: 1,
    budget_limits: {
      wall_clock_seconds: 900, model_turns: 20, tool_calls: 30, tokens: 100000, cost_usd: 5,
    },
    budget_usage: {
      wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0,
    },
    agent_runs: [],
    evidence: [],
    failure_type: null,
    public_summary: 'Running',
    blocking_reason: '',
    created_at: '2026-08-18T00:00:00Z',
    updated_at: '2026-08-18T00:00:00Z',
    started_at: '2026-08-18T00:00:00Z',
    finished_at: null,
    schema_version: 1,
    ...overrides,
  };
}

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
      created_at: '2026-08-18T00:00:01Z',
      completed_at: null,
      schema_version: 1,
    };
    expect(ToolInvocationSchema.parse(tool).replay_allowed).toBe(false);
    expect(() => ToolInvocationSchema.parse({ ...tool, replay_allowed: true })).toThrow();
  });
});

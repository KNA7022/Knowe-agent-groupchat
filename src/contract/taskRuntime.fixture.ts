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

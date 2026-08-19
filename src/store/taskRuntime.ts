import { create } from 'zustand';

import {
  ApprovalRequestSchema,
  AgentPhaseSchema,
  BudgetUsageSchema,
  FailureTypeSchema,
  GroupRuntimeSnapshotSchema,
  GroupRuntimeSnapshotResponseSchema,
  RunEventSchema,
  RouteLabelSchema,
  RuntimeReplaySchema,
  TaskRunSchema,
  TaskStatusSchema,
  type ApprovalRequest,
  type RunEvent,
  type TaskRun,
} from '../contract/taskRuntime';
import type { GroupId } from '../contract/group';
import { useGroupStore } from './groups';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';

interface RunProjection {
  run: TaskRun;
  events: RunEvent[];
  needsResync: boolean;
}

interface TaskRuntimeState {
  byRun: Record<string, RunProjection>;
  byGroup: Record<string, string[]>;
  approvals: Record<string, ApprovalRequest>;
  error: string | null;
  resetAll: () => void;
  ingestGroupSnapshot: (groupId: GroupId, raw: unknown) => void;
  applyEvent: (groupId: GroupId, raw: unknown) => boolean;
  reconcile: (groupId: GroupId, runId: string) => Promise<void>;
  refreshGroup: (groupId: GroupId) => Promise<void>;
  control: (groupId: GroupId, runId: string, command: 'pause' | 'cancel' | 'resume') => Promise<TaskRun>;
  resolveApproval: (
    groupId: GroupId, runId: string, approvalId: string, approved: boolean,
  ) => Promise<TaskRun>;
}

async function taskRuntimeJson(path: string, body: object): Promise<unknown> {
  const response = await runtimeFetch(`${runtimeHttpBase()}/task-runtime/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload: unknown = await response.json();
  if (!response.ok) {
    const message = payload && typeof payload === 'object' && 'error' in payload
      ? String((payload as { error?: unknown }).error || 'Task Runtime request failed')
      : `Task Runtime request failed (${response.status})`;
    throw new Error(message);
  }
  return payload;
}

function requestKey(prefix: string): string {
  return `${prefix}_${globalThis.crypto?.randomUUID?.().replace(/-/g, '') || Date.now().toString(36)}`;
}

const authorityRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>();

/** Coalesce noisy WS transitions into one authenticated snapshot+summary refresh. */
export function scheduleTaskRuntimeAuthorityRefresh(groupId: GroupId, delayMs = 80): void {
  const pending = authorityRefreshTimers.get(groupId);
  if (pending) clearTimeout(pending);
  authorityRefreshTimers.set(groupId, setTimeout(() => {
    authorityRefreshTimers.delete(groupId);
    void useTaskRuntimeStore.getState().refreshGroup(groupId).catch(() => undefined);
  }, delayMs));
}

export const useTaskRuntimeStore = create<TaskRuntimeState>((set, get) => ({
  byRun: {},
  byGroup: {},
  approvals: {},
  error: null,

  resetAll(): void {
    for (const timer of authorityRefreshTimers.values()) clearTimeout(timer);
    authorityRefreshTimers.clear();
    set({ byRun: {}, byGroup: {}, approvals: {}, error: null });
  },

  ingestGroupSnapshot(groupId, raw): void {
    const outer = raw && typeof raw === 'object' ? raw as Record<string, unknown> : null;
    if (outer && typeof outer.group_id === 'string' && outer.group_id !== groupId) {
      throw new Error('Task Runtime snapshot envelope crosses the addressed group_id boundary');
    }
    const envelope = outer && 'task_runtime' in outer
      ? outer.task_runtime
      : outer && outer.ok === true
        ? (() => {
          const response = GroupRuntimeSnapshotResponseSchema.parse(outer);
          const { ok: _ok, ...snapshot } = response;
          return snapshot;
        })()
        : raw;
    const snapshot = GroupRuntimeSnapshotSchema.parse(envelope);
    const rows = snapshot.runs;
    const approvals = snapshot.pending_approvals;
    if (snapshot.group_id !== groupId || rows.some((run) => run.group_id !== groupId)
        || approvals.some((approval) => approval.group_id !== groupId)) {
      throw new Error('Task Runtime snapshot crosses the addressed group_id boundary');
    }
    set((state) => {
      for (const run of rows) {
        const existing = state.byRun[run.run_id];
        if (existing && (run.version < existing.run.version
            || (run.version === existing.run.version && run.last_seq < existing.run.last_seq))) {
          throw new Error('Stale Task Runtime snapshot cannot replace a newer projection');
        }
      }
      const nextRuns = Object.fromEntries(
        Object.entries(state.byRun).filter(([, projection]) => projection.run.group_id !== groupId),
      );
      for (const run of rows) {
        const existing = nextRuns[run.run_id];
        nextRuns[run.run_id] = { run, events: existing?.events || [], needsResync: false };
      }
      const nextApprovals = Object.fromEntries(
        Object.entries(state.approvals).filter(([, approval]) => approval.group_id !== groupId),
      );
      for (const approval of approvals) nextApprovals[approval.approval_id] = approval;
      return {
        byRun: nextRuns,
        byGroup: { ...state.byGroup, [groupId]: rows.map((run) => run.run_id) },
        approvals: nextApprovals,
        error: null,
      };
    });
    useGroupStore.getState().ingestRuntimeProjection(
      groupId,
      {
        ...snapshot.summary,
        active_agent_count: new Set(rows.flatMap((run) => run.agent_runs
          .filter((agent) => agent.phase !== 'idle').map((agent) => agent.agent_id))).size,
      },
      rows.reduce((latest, run) => run.updated_at > latest ? run.updated_at : latest, ''),
    );
  },

  applyEvent(groupId, raw): boolean {
    const event = RunEventSchema.parse(raw);
    if (event.group_id !== groupId) {
      throw new Error('Task Runtime event crosses the addressed group_id boundary');
    }
    const current = get().byRun[event.run_id];
    if (!current) return false;
    if (event.seq === current.run.last_seq && event.version === current.run.version) return true;
    if (event.seq < current.run.last_seq && event.version < current.run.version) return true;
    if (event.seq !== current.run.last_seq + 1 || event.version !== current.run.version + 1) {
      set((state) => ({
        byRun: {
          ...state.byRun,
          [event.run_id]: { ...state.byRun[event.run_id]!, needsResync: true },
        },
      }));
      return false;
    }
    const payload = event.public_payload as Record<string, unknown>;
    if (event.event_type === 'agent_phase_changed') {
      const agentId = String(payload.agent_id || '');
      if (!current.run.agent_runs.some((agent) => agent.agent_id === agentId)) {
        set((state) => ({ byRun: {
          ...state.byRun,
          [event.run_id]: { ...state.byRun[event.run_id]!, needsResync: true },
        } }));
        return false;
      }
    }
    set((state) => {
      const projection = state.byRun[event.run_id]!;
      let run: TaskRun = {
        ...projection.run,
        version: event.version,
        last_seq: event.seq,
        updated_at: event.created_at,
      };
      const approvalChanges: Record<string, ApprovalRequest> = { ...state.approvals };
      if (event.event_type === 'status_changed' || event.event_type === 'restart_recovery'
          || event.event_type === 'budget_updated' || event.event_type === 'budget_exhausted'
          || event.event_type === 'approval_requested' || event.event_type === 'approval_resolved') {
        if (typeof payload.status === 'string') run.status = TaskStatusSchema.parse(payload.status);
      }
      if (run.status === 'running' && run.started_at === null) run.started_at = event.created_at;
      if (['succeeded', 'partial', 'failed', 'cancelled'].includes(run.status)) {
        run.finished_at = event.created_at;
      }
      if (event.event_type === 'status_changed') {
        if (typeof payload.summary === 'string' && payload.summary) run.public_summary = payload.summary;
        if (typeof payload.blocking_reason === 'string') run.blocking_reason = payload.blocking_reason;
        run.failure_type = payload.failure_type == null ? null : FailureTypeSchema.parse(payload.failure_type);
      } else if (event.event_type === 'restart_recovery') {
        if (typeof payload.blocking_reason === 'string') run.blocking_reason = payload.blocking_reason;
      } else if (event.event_type === 'budget_updated' || event.event_type === 'budget_exhausted') {
        run.budget_usage = BudgetUsageSchema.parse(payload.usage);
      } else if (event.event_type === 'tool_prepared') {
        run.budget_usage = BudgetUsageSchema.parse(payload.usage);
      } else if (event.event_type === 'message_routed') {
        run.route_label = RouteLabelSchema.parse(payload.route_label);
        if (typeof payload.summary === 'string') run.public_summary = payload.summary;
      } else if (event.event_type === 'agent_phase_changed') {
        const agentId = String(payload.agent_id);
        const phase = AgentPhaseSchema.parse(payload.phase);
        run = {
          ...run,
          agent_runs: run.agent_runs.map((agent) => agent.agent_id === agentId
            ? { ...agent, phase, version: agent.version + 1,
              operation_summary: String(payload.operation_summary || ''),
              blocking_reason: String(payload.blocking_reason || ''), updated_at: event.created_at }
            : agent),
        };
      } else if (event.event_type === 'approval_requested') {
        const approval = ApprovalRequestSchema.parse(payload.approval);
        approvalChanges[approval.approval_id] = approval;
        run.blocking_reason = 'User approval required';
      } else if (event.event_type === 'approval_resolved') {
        delete approvalChanges[String(payload.approval_id || '')];
        if (payload.failure_type != null) run.failure_type = FailureTypeSchema.parse(payload.failure_type);
        run.public_summary = payload.resolution === 'approved'
          ? 'Approval granted; task returned to queue' : 'Approval rejected';
      } else if (event.event_type === 'run_queued') {
        run.status = TaskStatusSchema.parse(payload.status);
        run.route_label = RouteLabelSchema.parse(payload.route_label);
        if (typeof payload.summary === 'string') run.public_summary = payload.summary;
      } else if (event.event_type === 'verifier_decision' && typeof payload.status === 'string') {
        run.status = TaskStatusSchema.parse(payload.status);
      }
      return {
        byRun: {
          ...state.byRun,
          [event.run_id]: {
            run: TaskRunSchema.parse(run),
            events: [...projection.events, event],
            needsResync: false,
          },
        },
        approvals: approvalChanges,
      };
    });
    const state = get();
    const runIds = state.byGroup[groupId] || [];
    const runs = runIds.map((runId) => state.byRun[runId]?.run).filter((run): run is TaskRun => Boolean(run));
    const previousSummary = useGroupStore.getState().runtimeSummaries[groupId];
    const activePrimary = previousSummary?.active_primary_run_id
      ? runs.find((run) => run.run_id === previousSummary.active_primary_run_id) : undefined;
    const eventRun = state.byRun[event.run_id]?.run;
    const eventPrimaryIsActive = eventRun?.kind === 'primary' && ![
      'succeeded', 'partial', 'failed', 'cancelled',
    ].includes(eventRun.status);
    const authoritativePrimary = activePrimary && ![
      'succeeded', 'partial', 'failed', 'cancelled',
    ].includes(activePrimary.status)
      ? activePrimary
      : eventPrimaryIsActive ? eventRun : null;
    const budgetUsage = runs.reduce((usage, run) => ({
      wall_clock_seconds: usage.wall_clock_seconds + run.budget_usage.wall_clock_seconds,
      model_turns: usage.model_turns + run.budget_usage.model_turns,
      tool_calls: usage.tool_calls + run.budget_usage.tool_calls,
      tokens: usage.tokens + run.budget_usage.tokens,
      cost_usd: usage.cost_usd + run.budget_usage.cost_usd,
    }), { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 });
    useGroupStore.getState().ingestRuntimeProjection(groupId, {
      active_agent_count: new Set(runs.flatMap((run) => run.agent_runs
        .filter((agent) => agent.phase !== 'idle').map((agent) => agent.agent_id))).size,
      active_primary_run_id: authoritativePrimary?.run_id || null,
      primary_task_status: authoritativePrimary?.status || null,
      queued_task_count: runs.filter((run) => run.status === 'queued').length,
      active_side_task_count: runs.filter((run) => run.kind === 'side' && ![
        'succeeded', 'partial', 'failed', 'cancelled',
      ].includes(run.status)).length,
      pending_approval_count: Object.values(state.approvals).filter(
        (approval) => approval.group_id === groupId && approval.status === 'pending',
      ).length,
      budget_usage: budgetUsage,
      indexing_state: previousSummary?.indexing_state || 'idle',
    }, event.created_at);
    return true;
  },

  async reconcile(groupId, runId): Promise<void> {
    const current = get().byRun[runId];
    if (!current || current.run.group_id !== groupId) throw new Error(`Unknown run: ${runId}`);
    try {
      const raw = await taskRuntimeJson('replay', {
        group_id: groupId, run_id: runId,
        after_seq: current.run.last_seq, after_version: current.run.version,
      });
      const replay = RuntimeReplaySchema.parse(raw);
      if (replay.group_id !== groupId || replay.run_id !== runId) {
        throw new Error('Task Runtime replay crosses the addressed identity boundary');
      }
      if (replay.after_seq !== current.run.last_seq) {
        throw new Error('Task Runtime replay does not match the requested local cursor');
      }
      if (replay.snapshot && (
        replay.snapshot.run.run_id !== runId
        || replay.snapshot.run.group_id !== groupId
        || replay.snapshot.run.last_seq !== replay.snapshot.last_seq
        || replay.snapshot.run.version !== replay.snapshot.version
      )) {
        throw new Error('Task Runtime replay snapshot identity/cursor mismatch');
      }
      if (replay.events.some((event) => event.run_id !== runId || event.group_id !== groupId)) {
        throw new Error('Task Runtime replay event identity mismatch');
      }
      if (replay.gap) {
        if (!replay.snapshot) throw new Error('Replay gap has no authoritative snapshot');
        set((state) => ({
          byRun: {
            ...state.byRun,
            [runId]: { run: replay.snapshot!.run, events: [], needsResync: false },
          },
        }));
        return;
      }
      for (const event of replay.events) get().applyEvent(groupId, event);
    } catch (error) {
      set({ error: error instanceof Error ? error.message : 'Task Runtime reconciliation failed' });
      throw error;
    }
  },

  async refreshGroup(groupId): Promise<void> {
    try {
      const raw = await taskRuntimeJson('snapshot', { group_id: groupId });
      get().ingestGroupSnapshot(groupId, raw);
    } catch (error) {
      set({ error: error instanceof Error ? error.message : 'Task Runtime snapshot refresh failed' });
      throw error;
    }
  },

  async control(groupId, runId, command): Promise<TaskRun> {
    const current = get().byRun[runId];
    if (!current || current.run.group_id !== groupId) throw new Error(`Unknown run: ${runId}`);
    const raw = await taskRuntimeJson('control', {
      group_id: groupId, run_id: runId, command, request_key: requestKey(command),
    });
    const run = TaskRunSchema.parse((raw as { run?: unknown }).run);
    if (run.group_id !== groupId || run.run_id !== runId) {
      throw new Error('Task Runtime control response crosses the addressed identity boundary');
    }
    set((state) => ({
      byRun: {
        ...state.byRun,
        [runId]: { run, events: state.byRun[runId]?.events || [], needsResync: false },
      },
    }));
    return run;
  },

  async resolveApproval(groupId, runId, approvalId, approved): Promise<TaskRun> {
    const current = get().byRun[runId];
    const approval = get().approvals[approvalId];
    if (!current || current.run.group_id !== groupId) throw new Error(`Unknown run: ${runId}`);
    if (!approval || approval.group_id !== groupId || approval.run_id !== runId
        || approval.status !== 'pending') {
      throw new Error('Approval identity does not match the addressed Group and run');
    }
    const raw = await taskRuntimeJson('approval', {
      group_id: groupId,
      run_id: runId,
      approval_id: approvalId,
      approved,
      capabilities: approved ? approval.requested_capabilities : [],
      request_key: requestKey(approved ? 'approve' : 'reject'),
    });
    const envelope = raw as { run?: unknown; approval?: unknown };
    const run = TaskRunSchema.parse(envelope.run);
    const resolved = ApprovalRequestSchema.parse(envelope.approval);
    if (run.group_id !== groupId || run.run_id !== runId
        || resolved.group_id !== groupId || resolved.run_id !== runId
        || resolved.approval_id !== approvalId) {
      throw new Error('Task Runtime approval response crosses the addressed identity boundary');
    }
    set((state) => {
      const approvals = { ...state.approvals };
      delete approvals[approvalId];
      return {
        byRun: {
          ...state.byRun,
          [runId]: { run, events: state.byRun[runId]?.events || [], needsResync: false },
        },
        approvals,
        error: null,
      };
    });
    return run;
  },
}));

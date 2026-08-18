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

export const useTaskRuntimeStore = create<TaskRuntimeState>((set, get) => ({
  byRun: {},
  byGroup: {},
  approvals: {},
  error: null,

  resetAll(): void {
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
}));

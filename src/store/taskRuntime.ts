import { create } from 'zustand';

import {
  ApprovalRequestSchema,
  AgentPhaseSchema,
  RunEventSchema,
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
  ingestGroupSnapshot: (groupId: GroupId, raw: unknown) => void;
  applyEvent: (groupId: GroupId, raw: unknown) => boolean;
  reconcile: (groupId: GroupId, runId: string) => Promise<void>;
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

  ingestGroupSnapshot(groupId, raw): void {
    const envelope = raw && typeof raw === 'object' && 'task_runtime' in raw
      ? (raw as { task_runtime?: unknown }).task_runtime : raw;
    if (!envelope || typeof envelope !== 'object') throw new Error('Invalid Task Runtime snapshot');
    const rows = Array.isArray((envelope as { runs?: unknown }).runs)
      ? (envelope as { runs: unknown[] }).runs.map((item) => TaskRunSchema.parse(item)) : [];
    const approvals = Array.isArray((envelope as { pending_approvals?: unknown }).pending_approvals)
      ? (envelope as { pending_approvals: unknown[] }).pending_approvals.map(
        (item) => ApprovalRequestSchema.parse(item),
      ) : [];
    if (rows.some((run) => run.group_id !== groupId)
        || approvals.some((approval) => approval.group_id !== groupId)) {
      throw new Error('Task Runtime snapshot crosses the addressed group_id boundary');
    }
    set((state) => {
      const nextRuns = { ...state.byRun };
      for (const run of rows) {
        const existing = nextRuns[run.run_id];
        // A stale reconnect snapshot must never roll an already-applied projection back.
        if (!existing || run.version >= existing.run.version) {
          nextRuns[run.run_id] = { run, events: existing?.events || [], needsResync: false };
        }
      }
      return {
        byRun: nextRuns,
        byGroup: { ...state.byGroup, [groupId]: rows.map((run) => run.run_id) },
        approvals: {
          ...state.approvals,
          ...Object.fromEntries(approvals.map((approval) => [approval.approval_id, approval])),
        },
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
    if (event.seq <= current.run.last_seq) return true;
    if (event.seq !== current.run.last_seq + 1 || event.version <= current.run.version) {
      set((state) => ({
        byRun: {
          ...state.byRun,
          [event.run_id]: { ...state.byRun[event.run_id]!, needsResync: true },
        },
      }));
      return false;
    }
    const status = event.public_payload.status;
    const agentId = event.public_payload.agent_id;
    const phase = event.public_payload.phase;
    set((state) => {
      const projection = state.byRun[event.run_id]!;
      let run: TaskRun = {
        ...projection.run,
        version: event.version,
        last_seq: event.seq,
        status: typeof status === 'string'
          ? TaskStatusSchema.parse(status) : projection.run.status,
      };
      if (typeof agentId === 'string' && typeof phase === 'string') {
        run = {
          ...run,
          agent_runs: run.agent_runs.map((agent) => agent.agent_id === agentId
            ? { ...agent, phase: AgentPhaseSchema.parse(phase) }
            : agent),
        };
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

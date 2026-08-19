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
import type { GroupRuntimeSummary } from '../contract/group';
import {
  clearRuntimeGroupSummaries, mirrorRuntimeGroupSummary, readRuntimeGroup,
} from './groupRuntimeBridge';
import {
  fenceDeletedGroup, groupCommitAllowed, groupDeletionGeneration, isGroupDeletionFenced,
} from './groupDeletionFence';
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
  groupSummaries: Record<string, GroupRuntimeSummary>;
  refreshGenerations: Record<string, number>;
  authorityByGroup: Record<string, { epoch: string; revision: number }>;
  legacyHandshakeGroups: Record<string, true>;
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
  teardownGroupProjection: (groupId: GroupId) => string[];
}

const TERMINAL = new Set(['succeeded', 'partial', 'failed', 'cancelled']);
const EXECUTING = new Set(['routing', 'running', 'verifying']);
const MAX_PUBLIC_EVENTS_PER_RUN = 200;

function retainedPublicEvents(events: RunEvent[], throughSeq = Number.POSITIVE_INFINITY): RunEvent[] {
  const unique = new Map<string, RunEvent>();
  for (const item of events) {
    if (item.sensitivity !== 'public' || item.seq > throughSeq) continue;
    unique.set(`${item.event_id}:${item.seq}`, item);
  }
  return [...unique.values()].sort((a, b) => a.seq - b.seq).slice(-MAX_PUBLIC_EVENTS_PER_RUN);
}

function indexingState(runs: TaskRun[]): GroupRuntimeSummary['indexing_state'] {
  const indexRuns = runs.filter((run) => run.queue_class === 'index');
  if (indexRuns.some((run) => EXECUTING.has(run.status))) return 'running';
  if (indexRuns.some((run) => run.status === 'queued')) return 'queued';
  const latest = [...indexRuns].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
  return latest?.status === 'failed' ? 'failed' : 'idle';
}

function summarizeGroup(
  groupId: GroupId,
  runs: TaskRun[],
  approvals: Record<string, ApprovalRequest>,
  previous: GroupRuntimeSummary | undefined,
  preferredPrimaryId?: string | null,
  updatedAt?: string,
): GroupRuntimeSummary {
  const group = readRuntimeGroup(groupId);
  const preferred = preferredPrimaryId
    ? runs.find((run) => run.run_id === preferredPrimaryId && run.kind === 'primary' && !TERMINAL.has(run.status))
    : undefined;
  const activePrimary = preferred || [...runs]
    .filter((run) => run.kind === 'primary' && !TERMINAL.has(run.status))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
  const budgetUsage = runs.reduce((usage, run) => ({
    wall_clock_seconds: usage.wall_clock_seconds + run.budget_usage.wall_clock_seconds,
    model_turns: usage.model_turns + run.budget_usage.model_turns,
    tool_calls: usage.tool_calls + run.budget_usage.tool_calls,
    tokens: usage.tokens + run.budget_usage.tokens,
    cost_usd: usage.cost_usd + run.budget_usage.cost_usd,
  }), { wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0 });
  return {
    group_id: groupId,
    lifecycle: group?.lifecycle || previous?.lifecycle || 'active',
    active_agent_count: new Set(runs.flatMap((run) => run.agent_runs
      .filter((agent) => agent.phase !== 'idle').map((agent) => agent.agent_id))).size,
    course_binding_count: group?.course_bindings.length || previous?.course_binding_count || 0,
    source_count: group?.source_links.length || previous?.source_count || 0,
    shared_source_count: previous?.shared_source_count || 0,
    budget: group?.budget ?? previous?.budget ?? null,
    active_primary_run_id: activePrimary?.run_id || null,
    primary_task_status: activePrimary?.status || null,
    queued_task_count: runs.filter((run) => run.status === 'queued').length,
    active_side_task_count: runs.filter((run) => run.kind === 'side'
      && run.queue_class === 'foreground' && EXECUTING.has(run.status)).length,
    pending_approval_count: Object.values(approvals).filter(
      (approval) => approval.group_id === groupId && approval.status === 'pending',
    ).length,
    budget_usage: budgetUsage,
    indexing_state: indexingState(runs),
    updated_at: updatedAt || runs.reduce(
      (latest, run) => run.updated_at > latest ? run.updated_at : latest,
      previous?.updated_at || new Date(0).toISOString(),
    ),
  };
}

function mirrorSummary(groupId: GroupId, summary: GroupRuntimeSummary | undefined): void {
  mirrorRuntimeGroupSummary(groupId, summary);
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
export function cancelTaskRuntimeAuthorityRefresh(groupId: GroupId): void {
  const timer = authorityRefreshTimers.get(groupId);
  if (timer) clearTimeout(timer);
  authorityRefreshTimers.delete(groupId);
}

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
  groupSummaries: {},
  refreshGenerations: {},
  authorityByGroup: {},
  legacyHandshakeGroups: {},
  error: null,

  resetAll(): void {
    for (const timer of authorityRefreshTimers.values()) clearTimeout(timer);
    authorityRefreshTimers.clear();
    set({ byRun: {}, byGroup: {}, approvals: {}, groupSummaries: {}, refreshGenerations: {},
      authorityByGroup: {}, legacyHandshakeGroups: {}, error: null });
    clearRuntimeGroupSummaries();
  },

  ingestGroupSnapshot(groupId, raw): void {
    if (isGroupDeletionFenced(groupId)) return;
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
    let installedSummary: GroupRuntimeSummary | undefined;
    let accepted = false;
    set((state) => {
      if (isGroupDeletionFenced(groupId)) return state;
      const authority = snapshot.authority_epoch === undefined ? null : {
        epoch: snapshot.authority_epoch,
        revision: snapshot.group_snapshot_revision!,
      };
      const currentAuthority = state.authorityByGroup[groupId];
      if (!authority) {
        // Unversioned transports are a one-shot legacy handshake only. They may establish an
        // empty projection, but can never replace a projection or cursor learned afterwards.
        if (currentAuthority || state.legacyHandshakeGroups[groupId]) return state;
      } else if (currentAuthority) {
        if (authority.epoch !== currentAuthority.epoch || authority.revision <= currentAuthority.revision) {
          return state;
        }
      }
      const priorGroupRuns = Object.fromEntries(Object.entries(state.byRun)
        .filter(([, projection]) => projection.run.group_id === groupId));
      for (const run of rows) {
        const existing = state.byRun[run.run_id];
        if (existing && (run.version < existing.run.version
            || (run.version === existing.run.version && run.last_seq < existing.run.last_seq))) {
          throw new Error('Stale Task Runtime snapshot cannot replace a newer projection');
        }
      }
      // Task runs form an append-only public ledger within one runtime epoch. Absence from a
      // snapshot is not a deletion signal; epoch reset and confirmed Group teardown are the only
      // removal paths. This prevents an older A-only reconnect snapshot from deleting newer B.
      const preservedAbsent = Object.values(priorGroupRuns).filter((projection) => (
        !rows.some((run) => run.run_id === projection.run.run_id)
      ));
      const effectiveRows = [...rows, ...preservedAbsent.map((projection) => projection.run)];
      const nextRuns = Object.fromEntries(
        Object.entries(state.byRun).filter(([, projection]) => projection.run.group_id !== groupId),
      );
      for (const run of effectiveRows) {
        const existing = priorGroupRuns[run.run_id];
        nextRuns[run.run_id] = {
          run,
          events: retainedPublicEvents(existing?.events || [], run.last_seq),
          needsResync: false,
        };
      }
      const preservedRunIds = new Set(preservedAbsent.map((projection) => projection.run.run_id));
      const nextApprovals = Object.fromEntries(Object.entries(state.approvals).filter(([, approval]) => (
        approval.group_id !== groupId || preservedRunIds.has(approval.run_id)
      )));
      for (const approval of approvals) nextApprovals[approval.approval_id] = approval;
      const previousSummary = state.groupSummaries[groupId];
      installedSummary = summarizeGroup(
        groupId,
        effectiveRows,
        nextApprovals,
        previousSummary,
        preservedAbsent.length ? previousSummary?.active_primary_run_id : snapshot.summary.active_primary_run_id,
      );
      accepted = true;
      return {
        byRun: nextRuns,
        byGroup: { ...state.byGroup, [groupId]: effectiveRows.map((run) => run.run_id) },
        approvals: nextApprovals,
        groupSummaries: { ...state.groupSummaries, [groupId]: installedSummary },
        authorityByGroup: authority ? { ...state.authorityByGroup, [groupId]: authority } : state.authorityByGroup,
        legacyHandshakeGroups: authority ? state.legacyHandshakeGroups
          : { ...state.legacyHandshakeGroups, [groupId]: true },
        error: null,
      };
    });
    if (accepted) mirrorSummary(groupId, installedSummary);
  },

  applyEvent(groupId, raw): boolean {
    if (isGroupDeletionFenced(groupId)) return false;
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
    let eventSummary: GroupRuntimeSummary | undefined;
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
      const nextByRun = {
          ...state.byRun,
          [event.run_id]: {
            run: TaskRunSchema.parse(run),
            events: retainedPublicEvents([...projection.events, event]),
            needsResync: false,
          },
      };
      const runIds = state.byGroup[groupId] || [];
      const runs = runIds.map((runId) => nextByRun[runId]?.run)
        .filter((candidate): candidate is TaskRun => Boolean(candidate));
      eventSummary = summarizeGroup(
        groupId, runs, approvalChanges, state.groupSummaries[groupId],
        state.groupSummaries[groupId]?.active_primary_run_id, event.created_at,
      );
      return {
        byRun: nextByRun,
        approvals: approvalChanges,
        groupSummaries: { ...state.groupSummaries, [groupId]: eventSummary },
      };
    });
    mirrorSummary(groupId, eventSummary);
    return true;
  },

  async reconcile(groupId, runId): Promise<void> {
    if (isGroupDeletionFenced(groupId)) return;
    const generation = groupDeletionGeneration(groupId);
    const current = get().byRun[runId];
    if (!current || current.run.group_id !== groupId) throw new Error(`Unknown run: ${runId}`);
    try {
      const raw = await taskRuntimeJson('replay', {
        group_id: groupId, run_id: runId,
        after_seq: current.run.last_seq, after_version: current.run.version,
      });
      const replay = RuntimeReplaySchema.parse(raw);
      if (!groupCommitAllowed(groupId, generation)) return;
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
        // A per-run replay cursor cannot authorize the rest of the Group ledger. Resolve a gap
        // through the full Group snapshot so run, approvals and summary share one revision.
        await get().refreshGroup(groupId);
        return;
      }
      for (const event of replay.events) get().applyEvent(groupId, event);
    } catch (error) {
      if (!groupCommitAllowed(groupId, generation)) return;
      set({ error: error instanceof Error ? error.message : 'Task Runtime reconciliation failed' });
      throw error;
    }
  },

  async refreshGroup(groupId): Promise<void> {
    if (isGroupDeletionFenced(groupId)) return;
    const deletionGeneration = groupDeletionGeneration(groupId);
    const generation = (get().refreshGenerations[groupId] || 0) + 1;
    set((state) => ({ refreshGenerations: { ...state.refreshGenerations, [groupId]: generation } }));
    try {
      const raw = await taskRuntimeJson('snapshot', { group_id: groupId });
      if (get().refreshGenerations[groupId] !== generation
          || !groupCommitAllowed(groupId, deletionGeneration)) return;
      get().ingestGroupSnapshot(groupId, raw);
    } catch (error) {
      if (get().refreshGenerations[groupId] !== generation
          || !groupCommitAllowed(groupId, deletionGeneration)) return;
      set({ error: error instanceof Error ? error.message : 'Task Runtime snapshot refresh failed' });
      throw error;
    }
  },

  async control(groupId, runId, command): Promise<TaskRun> {
    if (isGroupDeletionFenced(groupId)) throw new Error('Deleted Group cannot be controlled');
    const generation = groupDeletionGeneration(groupId);
    const current = get().byRun[runId];
    if (!current || current.run.group_id !== groupId) throw new Error(`Unknown run: ${runId}`);
    const raw = await taskRuntimeJson('control', {
      group_id: groupId, run_id: runId, command, request_key: requestKey(command),
    });
    const run = TaskRunSchema.parse((raw as { run?: unknown }).run);
    if (!groupCommitAllowed(groupId, generation)) throw new Error('Group was deleted while control was in flight');
    if (run.group_id !== groupId || run.run_id !== runId) {
      throw new Error('Task Runtime control response crosses the addressed identity boundary');
    }
    let controlSummary: GroupRuntimeSummary | undefined;
    set((state) => {
      if (!groupCommitAllowed(groupId, generation)) return state;
      const byRun = {
        ...state.byRun,
        [runId]: { run, events: state.byRun[runId]?.events || [], needsResync: false },
      };
      const runs = (state.byGroup[groupId] || []).map((id) => byRun[id]?.run)
        .filter((candidate): candidate is TaskRun => Boolean(candidate));
      controlSummary = summarizeGroup(
        groupId, runs, state.approvals, state.groupSummaries[groupId],
        state.groupSummaries[groupId]?.active_primary_run_id, run.updated_at,
      );
      return { byRun, groupSummaries: { ...state.groupSummaries, [groupId]: controlSummary } };
    });
    mirrorSummary(groupId, controlSummary);
    return run;
  },

  async resolveApproval(groupId, runId, approvalId, approved): Promise<TaskRun> {
    if (isGroupDeletionFenced(groupId)) throw new Error('Deleted Group cannot resolve approvals');
    const generation = groupDeletionGeneration(groupId);
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
    if (!groupCommitAllowed(groupId, generation)) throw new Error('Group was deleted while approval was in flight');
    if (run.group_id !== groupId || run.run_id !== runId
        || resolved.group_id !== groupId || resolved.run_id !== runId
        || resolved.approval_id !== approvalId) {
      throw new Error('Task Runtime approval response crosses the addressed identity boundary');
    }
    let approvalSummary: GroupRuntimeSummary | undefined;
    set((state) => {
      if (!groupCommitAllowed(groupId, generation)) return state;
      const approvals = { ...state.approvals };
      delete approvals[approvalId];
      const byRun = {
        ...state.byRun,
        [runId]: { run, events: state.byRun[runId]?.events || [], needsResync: false },
      };
      const runs = (state.byGroup[groupId] || []).map((id) => byRun[id]?.run)
        .filter((candidate): candidate is TaskRun => Boolean(candidate));
      approvalSummary = summarizeGroup(
        groupId, runs, approvals, state.groupSummaries[groupId],
        state.groupSummaries[groupId]?.active_primary_run_id, run.updated_at,
      );
      return {
        byRun,
        approvals,
        groupSummaries: { ...state.groupSummaries, [groupId]: approvalSummary },
        error: null,
      };
    });
    mirrorSummary(groupId, approvalSummary);
    return run;
  },

  teardownGroupProjection(groupId): string[] {
    cancelTaskRuntimeAuthorityRefresh(groupId);
    const removedRunIds = get().byGroup[groupId] || [];
    const removedSet = new Set(removedRunIds);
    set((state) => {
      const byRun = Object.fromEntries(Object.entries(state.byRun)
        .filter(([runId, projection]) => !removedSet.has(runId) && projection.run.group_id !== groupId));
      const byGroup = { ...state.byGroup };
      delete byGroup[groupId];
      const approvals = Object.fromEntries(Object.entries(state.approvals)
        .filter(([, approval]) => approval.group_id !== groupId));
      const groupSummaries = { ...state.groupSummaries };
      delete groupSummaries[groupId];
      const refreshGenerations = { ...state.refreshGenerations };
      delete refreshGenerations[groupId];
      const authorityByGroup = { ...state.authorityByGroup };
      delete authorityByGroup[groupId];
      const legacyHandshakeGroups = { ...state.legacyHandshakeGroups };
      delete legacyHandshakeGroups[groupId];
      fenceDeletedGroup(groupId);
      return { byRun, byGroup, approvals, groupSummaries, refreshGenerations,
        authorityByGroup, legacyHandshakeGroups };
    });
    mirrorSummary(groupId, undefined);
    return removedRunIds;
  },
}));

import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { shallow } from 'zustand/shallow';

import type { Group, GroupId } from '../contract/group';
import type { RunEvent, TaskRun, TaskStatus } from '../contract/taskRuntime';
import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import { IconAlert, IconCheck, IconChevR, IconRecover, IconTask, IconX } from './icons';
import GroupManagementDialog from './GroupManagementDialog';

const EMPTY_IDS: string[] = [];
const EMPTY_EVENTS: RunEvent[] = [];
const TERMINAL = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'cancelled']);

export function groupForLegacyProject(groups: Group[], projectId: string): Group | null {
  return groups.find((group) => group.legacy_project_id === projectId) || null;
}

export function controlCommandsForStatus(
  status: TaskStatus,
): Array<'pause' | 'cancel' | 'resume' | 'reconcile'> {
  if (status === 'running' || status === 'routing' || status === 'verifying'
      || status === 'waiting_approval' || status === 'queued') return ['pause', 'cancel'];
  if (status === 'paused') return ['resume', 'cancel'];
  if (status === 'failed' || status === 'partial') return ['reconcile'];
  return [];
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中', routing: '正在路由', running: '执行中', waiting_approval: '等待审批',
  paused: '已暂停', verifying: '核验中', succeeded: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消',
};

const ROUTE_LABEL: Record<TaskRun['route_label'], string> = {
  supplement_current: '补充当前主任务', side_answer: '旁路回答', queue_new: '新任务排队',
};

function ratio(used: number, limit: number): string {
  if (limit <= 0) return `${used}`;
  return `${used}/${limit}`;
}

interface RuntimeDrawerProps {
  open: boolean;
  onClose: () => void;
  groupId: GroupId;
  primary: TaskRun;
  sideRuns: TaskRun[];
}

const RuntimeDrawer = memo(function RuntimeDrawer({
  open, onClose, groupId, primary, sideRuns,
}: RuntimeDrawerProps): React.ReactElement | null {
  const drawerRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const events = useTaskRuntimeStore((state) => state.byRun[primary.run_id]?.events || EMPTY_EVENTS, shallow);
  const approvals = useTaskRuntimeStore((state) => Object.values(state.approvals).filter(
    (approval) => approval.group_id === groupId && approval.run_id === primary.run_id,
  ), shallow);
  const [tab, setTab] = useState<'events' | 'approvals' | 'tools' | 'evidence' | 'budget' | 'recovery' | 'indexing'>('events');

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose();
      if (event.key !== 'Tab' || !drawerRef.current) return;
      const focusable = [...drawerRef.current.querySelectorAll<HTMLElement>('button:not([disabled])')];
      if (!focusable.length) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, open]);

  if (!open) return null;
  const tabs = [
    ['events', '事件'], ['approvals', '审批'], ['tools', '工具'], ['evidence', '证据'],
    ['budget', '预算'], ['recovery', '恢复'], ['indexing', '索引'],
  ] as const;
  return (
    <aside ref={drawerRef} className="runtime-drawer" role="dialog" aria-modal="true" aria-label="任务运行详情">
      <div className="runtime-drawer-head">
        <div><strong>运行详情</strong><span>{primary.run_id.slice(0, 12)}</span></div>
        <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="关闭运行详情"><IconX /></button>
      </div>
      <div className="runtime-tabs" role="tablist" aria-label="运行详情分区">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" role="tab" aria-selected={tab === key}
            className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>
      <div className="runtime-drawer-body">
        {tab === 'events' ? (
          <ol className="runtime-event-list">
            {events.length ? events.map((event) => (
              <li key={event.event_id}>
                <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time>
                <strong>{event.event_type.split('_').join(' ')}</strong>
                <span>{typeof event.public_payload.summary === 'string' ? event.public_payload.summary : ''}</span>
              </li>
            )) : <li className="runtime-empty">暂无公开事件</li>}
          </ol>
        ) : null}
        {tab === 'approvals' ? (
          <div>{approvals.length ? approvals.map((approval) => (
            <article key={approval.approval_id} className="runtime-detail-card">
              <strong>{approval.summary || approval.purpose}</strong>
              <span>{approval.risk} · {approval.tool_name}</span>
            </article>
          )) : <p className="runtime-empty">没有待处理审批</p>}</div>
        ) : null}
        {tab === 'tools' ? (
          <div>{events.some((event) => event.event_type === 'tool_prepared') ? events
            .filter((event) => event.event_type === 'tool_prepared')
            .map((event) => (
              <article key={event.event_id} className="runtime-detail-card">
                <strong>{String(event.public_payload.tool_name || '受控工具调用')}</strong>
                <span>{String(event.public_payload.summary || event.public_payload.disposition || '等待公开状态')}</span>
              </article>
            )) : <p className="runtime-empty">没有公开的工具活动</p>}</div>
        ) : null}
        {tab === 'evidence' ? (
          <div>{primary.evidence.length ? primary.evidence.map((item) => (
            <article key={item.evidence_id} className="runtime-detail-card">
              <strong>{item.locator}</strong><span>{item.source_id} · 版本 {item.version_id}</span>
            </article>
          )) : <p className="runtime-empty">本任务尚未登记证据</p>}</div>
        ) : null}
        {tab === 'budget' ? (
          <dl className="runtime-budget-list">
            <div><dt>Token</dt><dd>{ratio(primary.budget_usage.tokens, primary.budget_limits.tokens)}</dd></div>
            <div><dt>工具调用</dt><dd>{ratio(primary.budget_usage.tool_calls, primary.budget_limits.tool_calls)}</dd></div>
            <div><dt>模型轮次</dt><dd>{ratio(primary.budget_usage.model_turns, primary.budget_limits.model_turns)}</dd></div>
            <div><dt>费用</dt><dd>${primary.budget_usage.cost_usd.toFixed(3)} / ${primary.budget_limits.cost_usd.toFixed(2)}</dd></div>
          </dl>
        ) : null}
        {tab === 'recovery' ? <p className="runtime-empty">{primary.blocking_reason || '当前没有恢复阻塞；异常时可执行对账。'}</p> : null}
        {tab === 'indexing' ? <p className="runtime-empty">索引状态属于 Group 摘要；此抽屉不推断未上报的索引结果。</p> : null}
        {sideRuns.length ? <p className="runtime-side-note">另有 {sideRuns.length} 个旁路任务，证据和费用保持独立。</p> : null}
      </div>
    </aside>
  );
});

export interface AgentStatusBarProps { projectId: string }

export const AgentStatusBar = memo(function AgentStatusBar({ projectId }: AgentStatusBarProps): React.ReactElement | null {
  const group = useGroupStore((state) => {
    for (const id of state.order) {
      const candidate = state.groups[id];
      if (candidate?.legacy_project_id === projectId) return candidate;
    }
    return null;
  });
  const groupId = group?.group_id;
  const runIds = useTaskRuntimeStore((state) => (groupId ? state.byGroup[groupId] || EMPTY_IDS : EMPTY_IDS), shallow);
  const projections = useTaskRuntimeStore((state) => runIds.map((id) => state.byRun[id]).filter(Boolean), shallow);
  const approvals = useTaskRuntimeStore((state) => groupId ? Object.values(state.approvals).filter(
    (approval) => approval.group_id === groupId,
  ) : [], shallow);
  const [idleOpen, setIdleOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState('');
  const detailsTriggerRef = useRef<HTMLButtonElement>(null);
  const managerTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!groupId || useTaskRuntimeStore.getState().byGroup[groupId]) return;
    void useTaskRuntimeStore.getState().refreshGroup(groupId).catch(() => undefined);
  }, [groupId]);

  const runs = useMemo(() => projections.map((projection) => projection!.run), [projections]);
  const primary = runs.find((run) => run.kind === 'primary' && !TERMINAL.has(run.status))
    || runs.find((run) => run.kind === 'primary');
  const sideRuns = runs.filter((run) => run.kind === 'side' && !TERMINAL.has(run.status));
  const activeAgents = primary?.agent_runs.filter((agent) => agent.phase !== 'idle') || [];
  const idleAgents = primary?.agent_runs.filter((agent) => agent.phase === 'idle') || [];

  const runControl = useCallback(async (command: 'pause' | 'cancel' | 'resume' | 'reconcile') => {
    if (!groupId || !primary) return;
    setPendingAction(command);
    setActionError('');
    try {
      if (command === 'reconcile') await useTaskRuntimeStore.getState().reconcile(groupId, primary.run_id);
      else await useTaskRuntimeStore.getState().control(groupId, primary.run_id, command);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '操作失败');
    } finally {
      setPendingAction(null);
    }
  }, [groupId, primary]);

  const resolve = useCallback(async (approvalId: string, approved: boolean) => {
    if (!groupId || !primary) return;
    setPendingAction(approvalId);
    setActionError('');
    try {
      await useTaskRuntimeStore.getState().resolveApproval(groupId, primary.run_id, approvalId, approved);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '审批失败');
    } finally {
      setPendingAction(null);
    }
  }, [groupId, primary]);

  if (!group || !primary) return null;
  const controls = controlCommandsForStatus(primary.status);
  const verifierLabel = primary.status === 'verifying' ? 'Verifier 核验中'
    : primary.status === 'succeeded' ? 'Verifier 已通过' : 'Verifier 待核验';
  return (
    <section className={`agent-status status-${primary.status}`} aria-label="Agent 运行状态">
      <div className="agent-status-primary" data-testid="primary-run">
        <div className="agent-status-mark" aria-hidden="true"><IconTask /></div>
        <div className="agent-status-copy">
          <div className="agent-status-kicker"><span>{STATUS_LABEL[primary.status]}</span><span>{ROUTE_LABEL[primary.route_label]}</span></div>
          <strong>{primary.public_summary || primary.title}</strong>
          <div className="agent-status-meta">
            <span>{verifierLabel}</span>
            <span>Token {ratio(primary.budget_usage.tokens, primary.budget_limits.tokens)}</span>
            <span>工具 {ratio(primary.budget_usage.tool_calls, primary.budget_limits.tool_calls)}</span>
            <span>{Math.round(primary.budget_usage.wall_clock_seconds)} 秒</span>
          </div>
          {primary.blocking_reason ? <p className="agent-status-block"><IconAlert />{primary.blocking_reason}</p> : null}
        </div>
        <div className="agent-status-actions">
          {controls.map((command) => (
            <button key={command} type="button" disabled={pendingAction !== null} onClick={() => void runControl(command)}>
              {command === 'pause' ? '暂停' : command === 'cancel' ? '取消' : command === 'resume' ? '恢复' : '对账重试'}
            </button>
          ))}
          <button ref={detailsTriggerRef} type="button" aria-expanded={drawerOpen} onClick={() => setDrawerOpen(true)}>详情 <IconChevR /></button>
          <button ref={managerTriggerRef} type="button" aria-expanded={managerOpen} onClick={() => setManagerOpen(true)}>管理群组</button>
        </div>
      </div>

      {activeAgents.length ? <div className="agent-status-workers">
        {activeAgents.map((agent) => <div key={agent.agent_run_id} className="agent-status-worker">
          <span className="status-shape" aria-hidden="true" />
          <strong>{agent.role}</strong><span>{agent.phase}</span>
          <span>{agent.operation_summary}</span>
          {agent.blocking_reason ? <em>{agent.blocking_reason}</em> : null}
        </div>)}
      </div> : null}
      {idleAgents.length ? <div className="agent-status-idle">
        <button type="button" aria-expanded={idleOpen} onClick={() => setIdleOpen((value) => !value)}>
          空闲 Agent（{idleAgents.length}）
        </button>
        {idleOpen ? idleAgents.map((agent) => <span key={agent.agent_run_id}>{agent.role}</span>) : null}
      </div> : null}
      {sideRuns.length ? <div className="agent-status-side" data-testid="side-runs">
        <strong>旁路任务</strong>{sideRuns.map((run) => <span key={run.run_id}>{run.public_summary || run.title} · {STATUS_LABEL[run.status]}</span>)}
      </div> : null}
      {approvals.map((approval) => <div key={approval.approval_id} className="agent-status-approval">
        <IconAlert /><div><strong>{approval.summary || approval.purpose}</strong><span>{approval.risk} · {approval.tool_name}</span></div>
        <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.approval_id, false)}><IconX />拒绝</button>
        <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.approval_id, true)}><IconCheck />批准</button>
      </div>)}
      {actionError ? <p className="agent-status-error" role="alert"><IconRecover />{actionError}</p> : null}
      <RuntimeDrawer open={drawerOpen} onClose={() => {
        setDrawerOpen(false);
        requestAnimationFrame(() => detailsTriggerRef.current?.focus());
      }} groupId={group.group_id} primary={primary} sideRuns={sideRuns} />
      <GroupManagementDialog group={group} open={managerOpen} onClose={() => {
        setManagerOpen(false);
        requestAnimationFrame(() => managerTriggerRef.current?.focus());
      }} />
    </section>
  );
});

export default AgentStatusBar;

import React, { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { shallow } from 'zustand/shallow';

import type { Group, GroupId } from '../contract/group';
import type { RunEvent, TaskRun, TaskStatus } from '../contract/taskRuntime';
import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import { useSideTaskReadStore } from '../store/sideTaskRead';
import { IconAlert, IconCheck, IconChevR, IconRecover, IconTask, IconX } from './icons';
import GroupManagementDialog from './GroupManagementDialog';
import ModalPortal from './ModalPortal';

const EMPTY_IDS: string[] = [];
const EMPTY_EVENTS: RunEvent[] = [];
const TERMINAL = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'cancelled']);

export function groupForLegacyProject(groups: Group[], projectId: string): Group | null {
  return groups.find((group) => group.legacy_project_id === projectId) || null;
}

export function controlCommandsForStatus(
  status: TaskStatus,
  needsResync = false,
): Array<'pause' | 'cancel' | 'resume' | 'reconcile'> {
  if (needsResync) return ['reconcile'];
  if (status === 'routing' || status === 'running' || status === 'verifying') return ['pause', 'cancel'];
  if (status === 'queued' || status === 'waiting_approval') return ['cancel'];
  if (status === 'paused') return ['resume', 'cancel'];
  if (status === 'failed' || status === 'partial') return ['reconcile'];
  return [];
}

interface RuntimeProjection {
  run: TaskRun;
  events: RunEvent[];
  needsResync: boolean;
}

export function primaryProjectionForSummary(
  projections: RuntimeProjection[], activePrimaryRunId: string | null | undefined,
): RuntimeProjection | null {
  if (!activePrimaryRunId) return null;
  return projections.find((projection) => (
    projection.run.kind === 'primary' && projection.run.run_id === activePrimaryRunId
  )) || null;
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中', routing: '正在路由', running: '执行中', waiting_approval: '等待审批',
  paused: '已暂停', verifying: '核验中', succeeded: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消',
};

const ROUTE_LABEL: Record<TaskRun['route_label'], string> = {
  supplement_current: '补充当前主任务', side_answer: '旁路回答', queue_new: '新任务排队',
};

const PHASE_LABEL: Record<TaskRun['agent_runs'][number]['phase'], string> = {
  idle: '空闲', planning: '规划中', retrieving: '检索中', executing: '执行中',
  waiting: '等待中', verifying: '核验中', responding: '回复中', failed: '失败',
};

function verifierLabel(status: TaskStatus): string {
  if (status === 'verifying') return 'Verifier 正在核验';
  if (status === 'succeeded') return 'Verifier 已通过';
  if (status === 'partial') return 'Verifier 仅部分通过';
  if (status === 'failed') return 'Verifier 未通过';
  if (status === 'cancelled') return '任务已取消，未形成核验结论';
  if (status === 'waiting_approval') return 'Verifier 等待审批结果';
  return 'Verifier 尚未开始核验';
}

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
  const tabsId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

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
  return <ModalPortal>{(
    <aside ref={drawerRef} className="runtime-drawer" role="dialog" aria-modal="true" aria-label="任务运行详情">
      <div className="runtime-drawer-head">
        <div><strong>运行详情</strong><span>{primary.run_id.slice(0, 12)}</span></div>
        <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="关闭运行详情"><IconX /></button>
      </div>
      <div className="runtime-tabs" role="tablist" aria-label="运行详情分区">
        {tabs.map(([key, label], index) => (
          <button key={key} type="button" role="tab" id={`${tabsId}-tab-${key}`}
            ref={(node) => { tabRefs.current[index] = node; }}
            aria-selected={tab === key} aria-controls={`${tabsId}-panel-${key}`}
            tabIndex={tab === key ? 0 : -1}
            className={tab === key ? 'active' : ''} onClick={() => setTab(key)}
            onKeyDown={(event) => {
              let next = index;
              if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
              else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
              else if (event.key === 'Home') next = 0;
              else if (event.key === 'End') next = tabs.length - 1;
              else return;
              event.preventDefault();
              const nextKey = tabs[next]![0];
              setTab(nextKey);
              tabRefs.current[next]?.focus();
            }}>{label}</button>
        ))}
      </div>
      <div className="runtime-drawer-body">
        {tab === 'events' ? (
          <ol className="runtime-event-list" role="tabpanel" id={`${tabsId}-panel-events`} aria-labelledby={`${tabsId}-tab-events`}>
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
          <div role="tabpanel" id={`${tabsId}-panel-approvals`} aria-labelledby={`${tabsId}-tab-approvals`}>{approvals.length ? approvals.map((approval) => (
            <article key={approval.approval_id} className="runtime-detail-card">
              <strong>{approval.summary || approval.purpose}</strong>
              <span>{approval.risk} · {approval.tool_name}</span>
            </article>
          )) : <p className="runtime-empty">没有待处理审批</p>}</div>
        ) : null}
        {tab === 'tools' ? (
          <div role="tabpanel" id={`${tabsId}-panel-tools`} aria-labelledby={`${tabsId}-tab-tools`}>{events.some((event) => event.event_type === 'tool_prepared') ? events
            .filter((event) => event.event_type === 'tool_prepared')
            .map((event) => (
              <article key={event.event_id} className="runtime-detail-card">
                <strong>{String(event.public_payload.tool_name || '受控工具调用')}</strong>
                <span>{String(event.public_payload.summary || event.public_payload.disposition || '等待公开状态')}</span>
              </article>
            )) : <p className="runtime-empty">没有公开的工具活动</p>}</div>
        ) : null}
        {tab === 'evidence' ? (
          <div role="tabpanel" id={`${tabsId}-panel-evidence`} aria-labelledby={`${tabsId}-tab-evidence`}>{primary.evidence.length ? primary.evidence.map((item) => (
            <article key={item.evidence_id} className="runtime-detail-card">
              <strong>{item.locator}</strong><span>{item.source_id} · 版本 {item.version_id}</span>
            </article>
          )) : <p className="runtime-empty">本任务尚未登记证据</p>}</div>
        ) : null}
        {tab === 'budget' ? (
          <dl className="runtime-budget-list" role="tabpanel" id={`${tabsId}-panel-budget`} aria-labelledby={`${tabsId}-tab-budget`}>
            <div><dt>Token</dt><dd>{ratio(primary.budget_usage.tokens, primary.budget_limits.tokens)}</dd></div>
            <div><dt>工具调用</dt><dd>{ratio(primary.budget_usage.tool_calls, primary.budget_limits.tool_calls)}</dd></div>
            <div><dt>模型轮次</dt><dd>{ratio(primary.budget_usage.model_turns, primary.budget_limits.model_turns)}</dd></div>
            <div><dt>费用</dt><dd>${primary.budget_usage.cost_usd.toFixed(3)} / ${primary.budget_limits.cost_usd.toFixed(2)}</dd></div>
          </dl>
        ) : null}
        {tab === 'recovery' ? <p className="runtime-empty" role="tabpanel" id={`${tabsId}-panel-recovery`} aria-labelledby={`${tabsId}-tab-recovery`}>{primary.blocking_reason || '当前没有恢复阻塞；异常时可执行对账。'}</p> : null}
        {tab === 'indexing' ? <p className="runtime-empty" role="tabpanel" id={`${tabsId}-panel-indexing`} aria-labelledby={`${tabsId}-tab-indexing`}>索引状态属于群组摘要；此抽屉不推断未上报的索引结果。</p> : null}
        {sideRuns.length ? <p className="runtime-side-note">另有 {sideRuns.length} 个旁路任务，证据和费用保持独立。</p> : null}
      </div>
    </aside>
  )}</ModalPortal>;
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
  const summary = useGroupStore((state) => (groupId ? state.runtimeSummaries[groupId] : undefined));
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
  const sideReadWatermarks = useSideTaskReadStore((state) => state.watermarks);
  const detailsTriggerRef = useRef<HTMLButtonElement>(null);
  const managerTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!groupId || useTaskRuntimeStore.getState().byGroup[groupId]) return;
    void useTaskRuntimeStore.getState().refreshGroup(groupId).catch(() => undefined);
  }, [groupId]);

  const runs = useMemo(() => projections.map((projection) => projection!.run), [projections]);
  const primaryProjection = primaryProjectionForSummary(
    projections as RuntimeProjection[], summary?.active_primary_run_id,
  );
  const primary = primaryProjection?.run;
  const sideRuns = runs.filter((run) => run.kind === 'side' && (
    !TERMINAL.has(run.status) || run.last_seq > (sideReadWatermarks[run.run_id] || 0)
  ));
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

  const resolve = useCallback(async (runId: string, approvalId: string, approved: boolean) => {
    if (!groupId) return;
    setPendingAction(approvalId);
    setActionError('');
    try {
      await useTaskRuntimeStore.getState().resolveApproval(groupId, runId, approvalId, approved);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '审批失败');
    } finally {
      setPendingAction(null);
    }
  }, [groupId]);

  if (!group) return null;
  if (!primary) return (
    <section className="agent-status status-sync" aria-label="Agent 运行状态">
      <div className="agent-status-sync-copy">
        <strong>{summary?.active_primary_run_id ? '主任务状态正在同步' : '当前没有权威主任务'}</strong>
        <span>{summary ? `群组摘要：${summary.primary_task_status ? STATUS_LABEL[summary.primary_task_status] : '空闲'}` : '尚未取得群组运行摘要，不推断聊天文本。'}</span>
      </div>
      <button type="button" onClick={() => void useTaskRuntimeStore.getState().refreshGroup(group.group_id)}>刷新状态</button>
      <button ref={managerTriggerRef} type="button" aria-expanded={managerOpen} onClick={() => setManagerOpen(true)}>管理群组</button>
      <GroupManagementDialog group={group} open={managerOpen} onClose={() => setManagerOpen(false)} />
    </section>
  );
  const controls = controlCommandsForStatus(primary.status, primaryProjection.needsResync);
  const primaryApprovals = approvals.filter((approval) => approval.run_id === primary.run_id);
  const otherApprovals = approvals.filter((approval) => approval.run_id !== primary.run_id);
  return (
    <section className={`agent-status status-${primary.status}`} aria-label="Agent 运行状态">
      <div className="agent-status-primary" data-testid="primary-run">
        <div className="agent-status-mark" aria-hidden="true"><IconTask /></div>
        <div className="agent-status-copy">
          <div className="agent-status-kicker"><span>{STATUS_LABEL[primary.status]}</span><span>{ROUTE_LABEL[primary.route_label]}</span></div>
          <strong>{primary.public_summary || primary.title}</strong>
          <div className="agent-status-meta">
            <span>{verifierLabel(primary.status)}</span>
            <span>Token {ratio(primary.budget_usage.tokens, primary.budget_limits.tokens)}</span>
            <span>工具 {ratio(primary.budget_usage.tool_calls, primary.budget_limits.tool_calls)}</span>
            <span>{Math.round(primary.budget_usage.wall_clock_seconds)} 秒</span>
          </div>
          {primary.blocking_reason ? <p className="agent-status-block"><IconAlert />{primary.blocking_reason}</p> : null}
          {primaryProjection.needsResync ? <p className="agent-status-block"><IconRecover />事件序列存在缺口；已禁用过期控制，请先对账。</p> : null}
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
          <strong>{agent.role}</strong><span>{PHASE_LABEL[agent.phase]}</span>
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
        <strong>旁路任务</strong>{sideRuns.map((run) => {
          const unread = run.last_seq > (sideReadWatermarks[run.run_id] || 0);
          return <button type="button" key={run.run_id} className={unread ? 'side-run-unread' : ''}
            aria-label={`${run.public_summary || run.title}，${unread ? '未读，点击标记已读' : '已读'}`}
            onClick={() => useSideTaskReadStore.getState().acknowledge(run.run_id, run.last_seq)}>
            {run.public_summary || run.title} · {STATUS_LABEL[run.status]}{unread ? ' · 未读' : ''}
          </button>;
        })}
      </div> : null}
      {primaryApprovals.map((approval) => <div key={approval.approval_id} className="agent-status-approval">
        <IconAlert /><div><strong>{approval.summary || approval.purpose}</strong><span>{approval.risk} · {approval.tool_name}</span></div>
        <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.run_id, approval.approval_id, false)}><IconX />拒绝</button>
        <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.run_id, approval.approval_id, true)}><IconCheck />批准</button>
      </div>)}
      {otherApprovals.length ? <div className="agent-status-other-approvals" role="region" aria-label="其他任务审批">
        <strong>旁路或排队任务审批</strong>
        {otherApprovals.map((approval) => {
          const owner = runs.find((run) => run.run_id === approval.run_id);
          return <div key={approval.approval_id} className="agent-status-approval">
            <IconAlert /><div><strong>{approval.summary || approval.purpose}</strong><span>{owner?.kind === 'side' ? '旁路任务' : '排队任务'} · {owner?.title || approval.run_id.slice(0, 12)}</span></div>
            <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.run_id, approval.approval_id, false)}><IconX />拒绝</button>
            <button type="button" disabled={pendingAction !== null} onClick={() => void resolve(approval.run_id, approval.approval_id, true)}><IconCheck />批准</button>
          </div>;
        })}
      </div> : null}
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

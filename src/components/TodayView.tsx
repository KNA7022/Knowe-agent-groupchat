import React from 'react';
import { LearningPanel } from './LearningPanel';

import type { Group, GroupRuntimeSummary } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import { useKnoweStore } from '../store/store';
import { IconAlert, IconCheck, IconChevR, IconTask } from './icons';

const STATUS_ZH: Record<string, string> = {
  queued: '排队中', routing: '路由中', running: '执行中', waiting_approval: '等待审批',
  paused: '已暂停', verifying: '核验中', succeeded: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消',
};

export interface TodayRow {
  groupId: Group['group_id'];
  groupName: string;
  legacyProjectId: string | null;
  status: GroupRuntimeSummary['primary_task_status'];
  queued: number;
  pendingApprovals: number;
  activeSideTasks: number;
  indexingState: GroupRuntimeSummary['indexing_state'];
}

export function buildTodayRows(
  groups: Group[], summaries: Record<string, GroupRuntimeSummary>,
): TodayRow[] {
  return groups.filter((group) => group.lifecycle === 'active').map((group) => {
    const summary = summaries[group.group_id];
    return {
      groupId: group.group_id,
      groupName: group.name,
      legacyProjectId: group.legacy_project_id,
      status: summary?.primary_task_status || null,
      queued: summary?.queued_task_count || 0,
      pendingApprovals: summary?.pending_approval_count || 0,
      activeSideTasks: summary?.active_side_task_count || 0,
      indexingState: summary?.indexing_state || 'idle',
    };
  }).sort((a, b) => {
    const weight = (row: TodayRow): number => row.pendingApprovals * 100
      + (row.indexingState === 'failed' ? 50 : 0) + (row.status ? 10 : 0) + row.queued;
    return weight(b) - weight(a);
  });
}

interface TodayViewProps {
  groups: Group[];
  summaries: Record<string, GroupRuntimeSummary>;
  onOpenGroup: (groupId: Group['group_id'], legacyProjectId: string | null) => void;
}

export const TodayView: React.FC<TodayViewProps> = ({ groups, summaries, onOpenGroup }) => {
  const rows = buildTodayRows(groups, summaries);
  const approvalCount = rows.reduce((sum, row) => sum + row.pendingApprovals, 0);
  const queuedCount = rows.reduce((sum, row) => sum + row.queued, 0);
  const issueCount = rows.filter((row) => row.indexingState === 'failed').length;
  return <div className="today-view">
    <header className="today-hero">
      <span className="study-eyebrow">StudyCrew 学伴团</span>
      <h1>今天先完成最重要的一件事</h1>
      <p>这里仅汇总各群组的公开运行摘要，不合并课程记忆、聊天正文或学习结论。</p>
      <dl>
        <div><dt>待审批</dt><dd>{approvalCount}</dd></div>
        <div><dt>排队任务</dt><dd>{queuedCount}</dd></div>
        <div><dt>索引异常</dt><dd>{issueCount}</dd></div>
      </dl>
    </header>
    <section className="today-groups" aria-labelledby="today-groups-title">
      <div className="today-section-title"><div><span className="study-eyebrow">工作区</span><h2 id="today-groups-title">群组动态</h2></div><span>{rows.length} 个活跃群组</span></div>
      {rows.length ? rows.map((row, index) => <article key={row.groupId} className={index === 0 ? 'today-group primary' : 'today-group'}>
        <div className="today-group-mark" aria-hidden="true">{row.pendingApprovals ? <IconAlert /> : <IconTask />}</div>
        <div><h3>{row.groupName}</h3><p>{row.status ? `主任务：${STATUS_ZH[row.status] || row.status}` : '当前没有运行中的主任务'}</p>
          <ul>
            {row.pendingApprovals ? <li>{row.pendingApprovals} 个待审批</li> : null}
            {row.queued ? <li>{row.queued} 个任务排队</li> : null}
            {row.activeSideTasks ? <li>{row.activeSideTasks} 个旁路任务</li> : null}
            {row.indexingState === 'failed' ? <li>索引异常</li> : null}
            {!row.pendingApprovals && !row.queued && !row.activeSideTasks && row.indexingState !== 'failed' ? <li><IconCheck />暂无异常</li> : null}
          </ul>
        </div>
        <button type="button" disabled={!row.legacyProjectId} aria-label={`打开群组 ${row.groupName}`}
          onClick={() => onOpenGroup(row.groupId, row.legacyProjectId)}>进入群组 <IconChevR /></button>
      </article>) : <div className="today-zero"><IconTask /><h2>还没有群组</h2><p>从聊天页创建课程学习组或考试冲刺组。</p></div>}
    </section><LearningPanel />
  </div>;
};

export const TodayShell: React.FC = () => {
  const error = useGroupStore((state) => state.error);
  const groups = useGroupStore((state) => state.order.map((id) => state.groups[id]).filter(
    (group): group is Group => group !== undefined,
  ));
  const summaries = useTaskRuntimeStore((state) => state.groupSummaries);
  return <>{error && <p role="alert">群组加载失败：{error} <button onClick={() => {void useGroupStore.getState().load().catch(() => undefined);}}>重试加载群组</button></p>}<TodayView groups={groups} summaries={summaries} onOpenGroup={(_groupId, legacyProjectId) => {
    if (!legacyProjectId) return;
    const store = useKnoweStore.getState();
    store.switchProject(legacyProjectId);
    store.setView('chats');
  }} /></>;
};

export default TodayShell;

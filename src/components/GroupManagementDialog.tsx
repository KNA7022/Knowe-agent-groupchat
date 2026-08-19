import React, { useEffect, useRef, useState } from 'react';

import type { Group, GroupAgent, GroupDeletePreview } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { IconAlert, IconPlus, IconTrash, IconX } from './icons';
import ModalPortal from './ModalPortal';

let fallbackId = 0;
function newId(): string {
  const generated = globalThis.crypto?.randomUUID?.().split('-').join('');
  fallbackId += 1;
  return `gag_${generated || fallbackId.toString(16).padStart(32, '0')}`;
}

interface PreviewState { value: GroupDeletePreview; groupId: string; updatedAt: string }
interface ExpertEdit { name: string; specialty: string; responsibilities: string; tools: string; model: string; budget: string }

function expertEdit(agent?: GroupAgent): ExpertEdit {
  return {
    name: agent?.display_name || '', specialty: agent?.expert?.specialty || '',
    responsibilities: agent?.expert?.responsibilities.join(', ') || '',
    tools: agent?.tool_overrides.join(', ') || '', model: agent?.model_override || '',
    budget: agent?.budget === null || agent?.budget === undefined ? '' : String(agent.budget),
  };
}

export const GroupManagementDialog: React.FC<{ group: Group; open: boolean; onClose: () => void }> = ({
  group, open, onClose,
}) => {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const [name, setName] = useState(group.name);
  const [duplicateDir, setDuplicateDir] = useState('');
  const [courseId, setCourseId] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [expertDrafts, setExpertDrafts] = useState<Record<string, ExpertEdit>>({});
  const [newExpert, setNewExpert] = useState<ExpertEdit>(() => expertEdit());
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  useEffect(() => setName(group.name), [group.name]);
  useEffect(() => {
    setPreview(null); setConfirmation('');
    setExpertDrafts(Object.fromEntries(group.roster.filter((agent) => agent.role === 'expert')
      .map((agent) => [agent.agent_id, expertEdit(agent)])));
  }, [group.group_id, group.updated_at, open]);
  useEffect(() => {
    if (!open) return undefined;
    restoreFocusRef.current = document.activeElement as HTMLElement;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose();
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])',
      )];
      if (!focusable.length) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      restoreFocusRef.current?.focus();
    };
  }, [onClose, open]);
  if (!open) return null;

  const act = async (key: string, operation: () => Promise<unknown>, preservePreview = false): Promise<void> => {
    setBusy(key); setError('');
    try {
      await operation();
      if (!preservePreview) { setPreview(null); setConfirmation(''); }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '群组操作失败');
    } finally { setBusy(''); }
  };
  const updateExpertDraft = (agentId: string, patch: Partial<ExpertEdit>): void => {
    setExpertDrafts((current) => ({ ...current, [agentId]: { ...current[agentId]!, ...patch } }));
  };
  const buildExpertAgent = (draft: ExpertEdit, existing?: GroupAgent): GroupAgent => {
    const now = new Date().toISOString();
    const tools = draft.tools.split(',').map((item) => item.trim()).filter(Boolean);
    const responsibilities = draft.responsibilities.split(',').map((item) => item.trim()).filter(Boolean);
    return {
      agent_id: existing?.agent_id || newId(), group_id: group.group_id, role: 'expert',
      display_name: draft.name.trim(), template_key: 'expert',
      expert: {
        name: draft.name.trim(), specialty: draft.specialty.trim(),
        responsibilities: responsibilities.length ? responsibilities : [`负责${draft.specialty.trim()}相关支持`],
        allowed_tools: tools, model_override: draft.model.trim() || null,
        budget: draft.budget ? Number(draft.budget) : null, platform_policy_locked: true,
      },
      model_override: draft.model.trim() || null, tool_overrides: tools,
      budget: draft.budget ? Number(draft.budget) : null,
      created_at: existing?.created_at || now, updated_at: now,
    };
  };
  const expertValid = (draft: ExpertEdit): boolean => Boolean(
    draft.name.trim() && draft.specialty.trim() && (!draft.budget || /^\d+$/.test(draft.budget)),
  );
  const toggleRole = async (role: 'knowledge' | 'planning' | 'coach', enabled: boolean): Promise<void> => {
    const existing = group.roster.filter((agent) => agent.role === role);
    let roster: GroupAgent[];
    if (!enabled) roster = group.roster.filter((agent) => agent.role !== role);
    else if (existing.length) return;
    else {
      const now = new Date().toISOString();
      roster = [...group.roster, {
        agent_id: newId(), group_id: group.group_id, role,
        display_name: role === 'knowledge' ? '课程知识 Agent' : role === 'planning' ? '规划 Agent' : '学习教练 Agent',
        template_key: role, expert: null, model_override: null, tool_overrides: [], budget: null,
        created_at: now, updated_at: now,
      }];
    }
    await act(`role-${role}`, () => useGroupStore.getState().updateRoster(group.group_id, roster));
  };

  return <ModalPortal><div className="study-modal-backdrop" role="presentation">
    <div ref={dialogRef} className="study-modal group-manager" role="dialog" aria-modal="true" aria-label={`管理群组 ${group.name}`}>
      <header><div><span className="study-eyebrow">Group settings</span><h2>管理「{group.name}」</h2></div>
        <button ref={closeRef} type="button" className="icon-btn" aria-label="关闭群组管理" onClick={onClose}><IconX /></button></header>
      <div className="group-manager-body">
        <section><h3>基本信息</h3><div className="study-inline-form">
          <label><span>群组名称</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <button type="button" disabled={busy !== '' || !name.trim()} onClick={() => void act('rename', () => useGroupStore.getState().renameGroup(group.group_id, name.trim()))}>保存名称</button>
        </div><div className="group-manager-actions">
          <button type="button" disabled={busy !== ''} onClick={() => void act('lifecycle', () => group.lifecycle === 'active'
            ? useGroupStore.getState().archiveGroup(group.group_id) : useGroupStore.getState().restoreGroup(group.group_id))}>
            {group.lifecycle === 'active' ? '归档群组' : '恢复群组'}
          </button>
        </div></section>
        <section><h3>复制工作区</h3><div className="study-inline-form">
          <label><span>新工作目录</span><input value={duplicateDir} onChange={(event) => setDuplicateDir(event.target.value)} /></label>
          <button type="button" disabled={busy !== '' || !duplicateDir.trim()} onClick={() => void act('duplicate', () => useGroupStore.getState().duplicateGroup(group.group_id, duplicateDir.trim(), `${group.name} 副本`))}>复制</button>
        </div></section>
        <section><h3>Agent 阵容</h3><div className="role-options compact">
          <label><input type="checkbox" checked disabled />Coordinator <small>必选</small></label>
          <label><input type="checkbox" checked disabled />Verifier <small>必选</small></label>
          {(['knowledge', 'planning', 'coach'] as const).map((role) => <label key={role}><input type="checkbox"
            checked={group.roster.some((agent) => agent.role === role)} disabled={busy !== ''}
            onChange={(event) => void toggleRole(role, event.target.checked)} />
            {role === 'knowledge' ? '课程知识' : role === 'planning' ? '规划' : '学习教练'}</label>)}
        </div>
          <p className="manager-policy-note">Coordinator、Verifier、PII 与学术诚信策略不可覆盖。</p>
          <div className="manager-experts">
            {group.roster.filter((agent) => agent.role === 'expert').map((agent, index) => {
              const draft = expertDrafts[agent.agent_id] || expertEdit(agent);
              return <div className="manager-expert" key={agent.agent_id}>
                <strong>专家 Agent {index + 1}</strong>
                <div className="study-form-grid">
                  <label><span>名称</span><input aria-label={`编辑专家 ${index + 1} 名称`} value={draft.name} onChange={(event) => updateExpertDraft(agent.agent_id, { name: event.target.value })} /></label>
                  <label><span>专业范围</span><input aria-label={`编辑专家 ${index + 1} 专业范围`} value={draft.specialty} onChange={(event) => updateExpertDraft(agent.agent_id, { specialty: event.target.value })} /></label>
                  <label><span>职责（逗号分隔）</span><input value={draft.responsibilities} onChange={(event) => updateExpertDraft(agent.agent_id, { responsibilities: event.target.value })} /></label>
                  <label><span>允许工具（逗号分隔）</span><input value={draft.tools} onChange={(event) => updateExpertDraft(agent.agent_id, { tools: event.target.value })} /></label>
                  <label><span>模型覆盖</span><input value={draft.model} onChange={(event) => updateExpertDraft(agent.agent_id, { model: event.target.value })} /></label>
                  <label><span>预算（非负整数）</span><input type="number" min="0" step="1" value={draft.budget} onChange={(event) => updateExpertDraft(agent.agent_id, { budget: event.target.value })} /></label>
                </div>
                <div className="group-manager-actions">
                  <button type="button" disabled={busy !== '' || !expertValid(draft)} onClick={() => void act(`expert-${agent.agent_id}`, () => useGroupStore.getState().updateRoster(
                    group.group_id, group.roster.map((item) => item.agent_id === agent.agent_id ? buildExpertAgent(draft, agent) : item),
                  ))}>保存专家</button>
                  <button type="button" className="danger-button" disabled={busy !== ''} onClick={() => void act(`expert-remove-${agent.agent_id}`, () => useGroupStore.getState().updateRoster(
                    group.group_id, group.roster.filter((item) => item.agent_id !== agent.agent_id),
                  ))}>移除专家</button>
                </div>
              </div>;
            })}
            {group.roster.filter((agent) => agent.role === 'expert').length < 6 ? <div className="manager-expert manager-expert-new">
              <strong>添加受控专家</strong>
              <div className="study-form-grid">
                <label><span>名称</span><input aria-label="新专家名称" value={newExpert.name} onChange={(event) => setNewExpert((value) => ({ ...value, name: event.target.value }))} /></label>
                <label><span>专业范围</span><input aria-label="新专家专业范围" value={newExpert.specialty} onChange={(event) => setNewExpert((value) => ({ ...value, specialty: event.target.value }))} /></label>
                <label><span>职责（逗号分隔）</span><input value={newExpert.responsibilities} onChange={(event) => setNewExpert((value) => ({ ...value, responsibilities: event.target.value }))} /></label>
                <label><span>允许工具（逗号分隔）</span><input value={newExpert.tools} onChange={(event) => setNewExpert((value) => ({ ...value, tools: event.target.value }))} /></label>
                <label><span>模型覆盖</span><input value={newExpert.model} onChange={(event) => setNewExpert((value) => ({ ...value, model: event.target.value }))} /></label>
                <label><span>预算（非负整数）</span><input type="number" min="0" step="1" value={newExpert.budget} onChange={(event) => setNewExpert((value) => ({ ...value, budget: event.target.value }))} /></label>
              </div>
              <button type="button" disabled={busy !== '' || !expertValid(newExpert)} onClick={() => void act('expert-add', async () => {
                await useGroupStore.getState().updateRoster(group.group_id, [...group.roster, buildExpertAgent(newExpert)]);
                setNewExpert(expertEdit());
              })}><IconPlus />添加专家</button>
            </div> : <p className="manager-policy-note">专家 Agent 已达到上限（6）。</p>}
          </div>
        </section>
        <section><h3>课程与资料绑定</h3>
          <div className="study-inline-form"><label><span>课程 ID</span><input value={courseId} onChange={(event) => setCourseId(event.target.value)} /></label>
            <button type="button" disabled={busy !== '' || !courseId.trim()} onClick={() => void act('course', async () => { await useGroupStore.getState().bindCourse(group.group_id, courseId.trim()); setCourseId(''); })}><IconPlus />绑定</button></div>
          <div className="manager-tags">{group.course_bindings.map((binding) => <button type="button" key={binding.binding_id}
            onClick={() => void act('course-remove', () => useGroupStore.getState().unbindCourse(group.group_id, binding.course_id))}>{binding.course_id}<IconX /></button>)}</div>
          <div className="study-inline-form"><label><span>资料源 ID</span><input value={sourceId} onChange={(event) => setSourceId(event.target.value)} /></label>
            <button type="button" disabled={busy !== '' || !sourceId.trim()} onClick={() => void act('source', async () => { await useGroupStore.getState().linkSource(group.group_id, sourceId.trim()); setSourceId(''); })}><IconPlus />链接</button></div>
          <div className="manager-tags">{group.source_links.map((link) => <button type="button" key={link.link_id}
            onClick={() => void act('source-remove', () => useGroupStore.getState().unlinkSource(group.group_id, link.source_id))}>{link.source_id}<IconX /></button>)}</div>
        </section>
        <section className="danger-zone"><h3><IconAlert />删除群组</h3>
          {!(preview && preview.groupId === group.group_id && preview.updatedAt === group.updated_at && preview.value.group_id === group.group_id) ? <><p>删除前先读取后端影响预览。没有与当前群组版本绑定的确认令牌，不会发送删除请求。</p>
            <button type="button" disabled={busy !== ''} onClick={() => void act('preview', async () => {
              const value = await useGroupStore.getState().previewDelete(group.group_id);
              if (value.group_id !== group.group_id) throw new Error('删除预览身份不匹配');
              setPreview({ value, groupId: group.group_id, updatedAt: group.updated_at });
            }, true)}><IconTrash />查看删除影响</button></>
            : <div className="delete-preview" data-testid="delete-preview">
              <dl><div><dt>Agent</dt><dd>{preview.value.agent_count}</dd></div><div><dt>课程绑定</dt><dd>{preview.value.course_binding_count}</dd></div><div><dt>独占资料</dt><dd>{preview.value.exclusive_source_ids.length}</dd></div><div><dt>共享资料保留</dt><dd>{preview.value.shared_source_ids.length}</dd></div></dl>
              <label><span>输入群组名称以确认</span><input aria-label="确认群组名称" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
              <button type="button" className="danger-button" disabled={busy !== '' || confirmation !== group.name}
                onClick={() => void act('delete', async () => {
                  if (preview.groupId !== group.group_id || preview.updatedAt !== group.updated_at || preview.value.group_id !== group.group_id) throw new Error('删除预览已过期，请重新读取');
                  await useGroupStore.getState().confirmedDelete(group.group_id, preview.value.confirmation_token); onClose();
                }, true)}><IconTrash />永久删除</button>
            </div>}
        </section>
        {busy ? <p className="manager-progress" role="status">正在处理…</p> : null}
        {error ? <p className="study-form-error" role="alert">{error}</p> : null}
      </div>
    </div>
  </div></ModalPortal>;
};

export default GroupManagementDialog;

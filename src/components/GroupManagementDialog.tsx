import React, { useEffect, useRef, useState } from 'react';

import type { Group, GroupAgent, GroupDeletePreview } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { IconAlert, IconPlus, IconTrash, IconX } from './icons';

function newId(): string {
  return `gag_${globalThis.crypto?.randomUUID?.().split('-').join('') || '0'.repeat(32)}`;
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
  const [preview, setPreview] = useState<GroupDeletePreview | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  useEffect(() => setName(group.name), [group.name]);
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

  const act = async (key: string, operation: () => Promise<unknown>): Promise<void> => {
    setBusy(key); setError('');
    try { await operation(); } catch (reason) {
      setError(reason instanceof Error ? reason.message : '群组操作失败');
    } finally { setBusy(''); }
  };
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

  return <div className="study-modal-backdrop" role="presentation">
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
        </div></section>
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
          {!preview ? <><p>删除前先读取后端影响预览。没有确认令牌不会发送删除请求。</p>
            <button type="button" disabled={busy !== ''} onClick={() => void act('preview', async () => setPreview(await useGroupStore.getState().previewDelete(group.group_id)))}><IconTrash />查看删除影响</button></>
            : <div className="delete-preview" data-testid="delete-preview">
              <dl><div><dt>Agent</dt><dd>{preview.agent_count}</dd></div><div><dt>课程绑定</dt><dd>{preview.course_binding_count}</dd></div><div><dt>独占资料</dt><dd>{preview.exclusive_source_ids.length}</dd></div><div><dt>共享资料保留</dt><dd>{preview.shared_source_ids.length}</dd></div></dl>
              <label><span>输入群组名称以确认</span><input aria-label="确认群组名称" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
              <button type="button" className="danger-button" disabled={busy !== '' || confirmation !== group.name}
                onClick={() => void act('delete', async () => { await useGroupStore.getState().confirmedDelete(group.group_id, preview.confirmation_token); onClose(); })}><IconTrash />永久删除</button>
            </div>}
        </section>
        {busy ? <p className="manager-progress" role="status">正在处理…</p> : null}
        {error ? <p className="study-form-error" role="alert">{error}</p> : null}
      </div>
    </div>
  </div>;
};

export default GroupManagementDialog;

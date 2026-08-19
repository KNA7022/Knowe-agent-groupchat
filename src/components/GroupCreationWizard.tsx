import React, { useEffect, useId, useRef, useState } from 'react';

import type { Group, GroupTemplate } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { IconCheck, IconPlus, IconX } from './icons';
import ModalPortal from './ModalPortal';

const TEMPLATES: Array<{ key: GroupTemplate; name: string; description: string }> = [
  { key: 'course_study', name: '课程学习组', description: '一门课程，知识、规划和教练协作' },
  { key: 'exam_sprint', name: '考试冲刺组', description: '多课程复习、验证与进度追踪' },
  { key: 'general_assistant', name: '通用助手组', description: '处理文件、天气等非课程任务' },
  { key: 'custom', name: '自定义组', description: '按需选择角色和能力边界' },
];

type OptionalRoles = { knowledge: boolean; planning: boolean; coach: boolean };
type ExpertDraft = { id: number; name: string; specialty: string; tools: string; model: string; budget: string };
const MAX_EXPERTS = 4;

export function roleDefaultsForTemplate(template: GroupTemplate): OptionalRoles {
  if (template === 'course_study' || template === 'exam_sprint') {
    return { knowledge: true, planning: true, coach: true };
  }
  return { knowledge: false, planning: false, coach: false };
}

export interface GroupCreationWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (groupId: string, legacyProjectId: string | null) => void;
}

export const GroupCreationWizard: React.FC<GroupCreationWizardProps> = ({ open, onClose, onCreated }) => {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const expertSequence = useRef(0);
  const createdNotified = useRef(false);
  const [template, setTemplate] = useState<GroupTemplate>('course_study');
  const [name, setName] = useState('');
  const [directory, setDirectory] = useState('');
  const [roles, setRoles] = useState<OptionalRoles>(() => roleDefaultsForTemplate('course_study'));
  const [budget, setBudget] = useState('5');
  const [chatModel, setChatModel] = useState('');
  const [groupTools, setGroupTools] = useState('');
  const [experts, setExperts] = useState<ExpertDraft[]>([]);
  const [courseId, setCourseId] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [createdGroup, setCreatedGroup] = useState<Group | null>(null);
  const [courseBound, setCourseBound] = useState(false);
  const [sourceBound, setSourceBound] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    restoreFocusRef.current = document.activeElement as HTMLElement;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onCloseRef.current();
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
  }, [open]);

  if (!open) return null;
  const locked = Boolean(createdGroup);
  const updateExpert = (id: number, patch: Partial<ExpertDraft>): void => {
    setExperts((rows) => rows.map((row) => row.id === id ? { ...row, ...patch } : row));
  };
  const resetCompletedWizard = (): void => {
    setTemplate('course_study'); setName(''); setDirectory('');
    setRoles(roleDefaultsForTemplate('course_study')); setBudget('5');
    setChatModel(''); setGroupTools(''); setExperts([]); setCourseId(''); setSourceId('');
    setCreatedGroup(null); setCourseBound(false); setSourceBound(false); setError('');
    createdNotified.current = false;
  };

  const submit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    if (!name.trim()) { setError('请填写群组名称。'); return; }
    if (!directory.trim()) { setError('请选择或填写工作目录。'); return; }
    if (experts.some((expert) => !expert.name.trim() || !expert.specialty.trim())) {
      setError('每个专家 Agent 都需要名称和专业范围。'); return;
    }
    if (budget && (!/^\d+$/.test(budget) || Number(budget) < 0)) {
      setError('默认费用预算必须是非负整数。'); return;
    }
    if (experts.some((expert) => expert.budget && !/^\d+$/.test(expert.budget))) {
      setError('专家预算必须是非负整数。'); return;
    }
    setSubmitting(true);
    setError('');
    let group = createdGroup;
    try {
      if (!group) {
        try {
          group = await useGroupStore.getState().createGroup({
            name: name.trim(), template, project_dir: directory.trim(),
            optional_roles: (Object.entries(roles) as Array<[keyof OptionalRoles, boolean]>)
              .filter(([, enabled]) => enabled).map(([role]) => role),
            experts: experts.map((expert) => ({
              name: expert.name.trim(), specialty: expert.specialty.trim(),
              responsibilities: [`负责${expert.specialty.trim()}相关的受控学习支持`],
              allowed_tools: expert.tools.split(',').map((item) => item.trim()).filter(Boolean),
              model_override: expert.model.trim() || undefined,
              budget: expert.budget ? Number(expert.budget) : undefined,
            })),
            budget: budget ? Number(budget) : undefined,
            model_overrides: chatModel.trim() ? { chat: chatModel.trim() } : undefined,
            tool_overrides: groupTools.trim() ? { group: groupTools.split(',').map((item) => item.trim()).filter(Boolean) } : undefined,
          });
        } catch (reason) {
          setError(`群组未创建：${reason instanceof Error ? reason.message : '后端创建失败'}`);
          return;
        }
        setCreatedGroup(group);
        if (!createdNotified.current) {
          createdNotified.current = true;
          onCreated?.(group.group_id, group.legacy_project_id);
        }
      }
      try {
        if (courseId.trim() && !courseBound) {
          await useGroupStore.getState().bindCourse(group.group_id, courseId.trim());
          setCourseBound(true);
        }
        if (sourceId.trim() && !sourceBound) {
          await useGroupStore.getState().linkSource(group.group_id, sourceId.trim());
          setSourceBound(true);
        }
      } catch (reason) {
        setError(`群组已创建，但初始绑定未完成：${reason instanceof Error ? reason.message : '绑定失败'}。可继续绑定，不会重复创建群组。`);
        return;
      }
      resetCompletedWizard();
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  return <ModalPortal><div className="study-modal-backdrop" role="presentation" onMouseDown={(event) => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <div ref={dialogRef} className="study-modal group-wizard" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <header><div><span className="study-eyebrow">新建工作区</span><h2 id={titleId}>创建 Agent 群组</h2></div>
        <button ref={closeRef} type="button" className="icon-btn" aria-label="关闭创建群组" onClick={onClose}><IconX /></button></header>
      <form onSubmit={(event) => void submit(event)}>
        <fieldset className="group-template-list" disabled={locked}><legend>选择模板</legend>
          {TEMPLATES.map((item) => <label key={item.key} className={template === item.key ? 'selected' : ''}>
            <input type="radio" name="group-template" value={item.key} checked={template === item.key}
              onChange={() => { setTemplate(item.key); setRoles(roleDefaultsForTemplate(item.key)); }} />
            <span><strong>{item.name}</strong><small>{item.description}</small></span>{template === item.key ? <IconCheck /> : null}
          </label>)}</fieldset>
        <div className="study-form-grid">
          <label><span>群组名称</span><input aria-label="群组名称" value={name} maxLength={160} disabled={locked} onChange={(event) => setName(event.target.value)} /></label>
          <label><span>工作目录</span><input aria-label="工作目录" value={directory} disabled={locked} placeholder="D:\\StudyCrew\\课程名" onChange={(event) => setDirectory(event.target.value)} /></label>
          <label><span>默认费用预算（USD 整数）</span><input aria-label="默认费用预算" type="number" min="0" step="1" value={budget} disabled={locked} onChange={(event) => setBudget(event.target.value)} /></label>
          <label><span>课程 ID（可选）</span><input aria-label="课程 ID" value={courseId} disabled={locked || courseBound} onChange={(event) => setCourseId(event.target.value)} /></label>
          <label><span>首选聊天模型（可选）</span><input aria-label="首选聊天模型" value={chatModel} disabled={locked} placeholder="provider/model" onChange={(event) => setChatModel(event.target.value)} /></label>
          <label><span>群组允许工具（逗号分隔）</span><input aria-label="群组允许工具" value={groupTools} disabled={locked} onChange={(event) => setGroupTools(event.target.value)} /></label>
          <label className="study-form-wide"><span>资料源 ID（可选）</span><input aria-label="资料源 ID" value={sourceId} disabled={locked || sourceBound} onChange={(event) => setSourceId(event.target.value)} /></label>
        </div>
        <fieldset className="role-options" disabled={locked}><legend>Agent 阵容</legend>
          <label><input type="checkbox" aria-label="Coordinator" checked readOnly disabled />Coordinator <small>必选</small></label>
          <label><input type="checkbox" aria-label="Verifier" checked readOnly disabled />Verifier <small>必选</small></label>
          {(Object.keys(roles) as Array<keyof OptionalRoles>).map((role) => <label key={role}><input type="checkbox" checked={roles[role]} onChange={(event) => setRoles((current) => ({ ...current, [role]: event.target.checked }))} />{role === 'knowledge' ? '课程知识' : role === 'planning' ? '规划' : '学习教练'}</label>)}
        </fieldset>
        <button type="button" className="study-link-button" disabled={locked || experts.length >= MAX_EXPERTS}
          onClick={() => setExperts((current) => [...current, { id: ++expertSequence.current, name: '', specialty: '', tools: '', model: '', budget: '' }])}>
          <IconPlus />添加受控专家 Agent（{experts.length}/{MAX_EXPERTS}）</button>
        {experts.map((expert, index) => <div className="expert-fields" key={expert.id}>
          <label><span>专家 {index + 1} 名称</span><input aria-label={`专家 ${index + 1} 名称`} value={expert.name} maxLength={80} disabled={locked} onChange={(event) => updateExpert(expert.id, { name: event.target.value })} /></label>
          <label><span>专业范围</span><input aria-label={`专家 ${index + 1} 专业范围`} value={expert.specialty} maxLength={200} disabled={locked} onChange={(event) => updateExpert(expert.id, { specialty: event.target.value })} /></label>
          <label><span>模型覆盖（可选）</span><input aria-label={`专家 ${index + 1} 模型覆盖`} value={expert.model} disabled={locked} onChange={(event) => updateExpert(expert.id, { model: event.target.value })} /></label>
          <label><span>预算（整数，可选）</span><input aria-label={`专家 ${index + 1} 预算`} type="number" min="0" step="1" value={expert.budget} disabled={locked} onChange={(event) => updateExpert(expert.id, { budget: event.target.value })} /></label>
          <label className="study-form-wide"><span>允许工具（逗号分隔）</span><input aria-label={`专家 ${index + 1} 允许工具`} value={expert.tools} disabled={locked} onChange={(event) => updateExpert(expert.id, { tools: event.target.value })} /></label>
          <button type="button" className="study-secondary" disabled={locked} onClick={() => setExperts((rows) => rows.filter((row) => row.id !== expert.id))}>移除专家 {index + 1}</button>
          <p>平台权限、Verifier、PII 与学术诚信策略始终锁定。</p>
        </div>)}
        {createdGroup ? <p className="study-created-notice" role="status">群组已创建；这里只会重试尚未完成的课程/资料绑定。</p> : null}
        {error ? <p className="study-form-error" role="alert">{error}</p> : null}
        <footer><button type="button" className="study-secondary" onClick={onClose}>取消</button>
          <button type="submit" className="study-primary" disabled={submitting}>{submitting ? '正在处理…' : createdGroup ? '继续完成绑定' : '创建群组'}</button></footer>
      </form>
    </div>
  </div></ModalPortal>;
};

export default GroupCreationWizard;

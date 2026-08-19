import React, { useEffect, useId, useRef, useState } from 'react';

import type { GroupTemplate } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { IconCheck, IconPlus, IconX } from './icons';

const TEMPLATES: Array<{ key: GroupTemplate; name: string; description: string }> = [
  { key: 'course_study', name: '课程学习组', description: '一门课程，知识、规划和教练协作' },
  { key: 'exam_sprint', name: '考试冲刺组', description: '多课程复习、验证与进度追踪' },
  { key: 'general_assistant', name: '通用助手组', description: '处理文件、天气等非课程任务' },
  { key: 'custom', name: '自定义组', description: '按需选择角色和能力边界' },
];

export interface GroupCreationWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (groupId: string, legacyProjectId: string | null) => void;
}

export const GroupCreationWizard: React.FC<GroupCreationWizardProps> = ({ open, onClose, onCreated }) => {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const [template, setTemplate] = useState<GroupTemplate>('course_study');
  const [name, setName] = useState('');
  const [directory, setDirectory] = useState('');
  const [roles, setRoles] = useState({ knowledge: true, planning: true, coach: true });
  const [budget, setBudget] = useState('5');
  const [chatModel, setChatModel] = useState('');
  const [groupTools, setGroupTools] = useState('');
  const [expertOpen, setExpertOpen] = useState(false);
  const [expert, setExpert] = useState({ name: '', specialty: '', tools: '' });
  const [courseId, setCourseId] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

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
  const submit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    const cleanName = name.trim();
    const cleanDirectory = directory.trim();
    if (!cleanName) { setError('请填写群组名称。'); return; }
    if (!cleanDirectory) { setError('请选择或填写工作目录。'); return; }
    if (expertOpen && (!expert.name.trim() || !expert.specialty.trim())) {
      setError('专家 Agent 需要名称和专业范围。'); return;
    }
    setSubmitting(true);
    setError('');
    try {
      const group = await useGroupStore.getState().createGroup({
        name: cleanName,
        template,
        project_dir: cleanDirectory,
        optional_roles: (Object.entries(roles) as Array<[keyof typeof roles, boolean]>)
          .filter(([, enabled]) => enabled).map(([role]) => role),
        experts: expertOpen ? [{
          name: expert.name.trim(),
          specialty: expert.specialty.trim(),
          responsibilities: [`负责${expert.specialty.trim()}相关的受控学习支持`],
          allowed_tools: expert.tools.split(',').map((item) => item.trim()).filter(Boolean),
        }] : [],
        budget: budget ? Number(budget) : undefined,
        model_overrides: chatModel.trim() ? { chat: chatModel.trim() } : undefined,
        tool_overrides: groupTools.trim() ? {
          group: groupTools.split(',').map((item) => item.trim()).filter(Boolean),
        } : undefined,
      });
      if (courseId.trim()) await useGroupStore.getState().bindCourse(group.group_id, courseId.trim());
      if (sourceId.trim()) await useGroupStore.getState().linkSource(group.group_id, sourceId.trim());
      onCreated?.(group.group_id, group.legacy_project_id);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '群组创建失败，请重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="study-modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div ref={dialogRef} className="study-modal group-wizard" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header>
          <div><span className="study-eyebrow">新建工作区</span><h2 id={titleId}>创建 Agent 群组</h2></div>
          <button ref={closeRef} type="button" className="icon-btn" aria-label="关闭创建群组" onClick={onClose}><IconX /></button>
        </header>
        <form onSubmit={(event) => void submit(event)}>
          <fieldset className="group-template-list">
            <legend>选择模板</legend>
            {TEMPLATES.map((item) => <label key={item.key} className={template === item.key ? 'selected' : ''}>
              <input type="radio" name="group-template" value={item.key} checked={template === item.key}
                onChange={() => setTemplate(item.key)} />
              <span><strong>{item.name}</strong><small>{item.description}</small></span>
              {template === item.key ? <IconCheck /> : null}
            </label>)}
          </fieldset>
          <div className="study-form-grid">
            <label><span>群组名称</span><input aria-label="群组名称" value={name} maxLength={160}
              onChange={(event) => setName(event.target.value)} /></label>
            <label><span>工作目录</span><input aria-label="工作目录" value={directory}
              placeholder="D:\\StudyCrew\\课程名" onChange={(event) => setDirectory(event.target.value)} /></label>
            <label><span>默认费用预算（USD）</span><input aria-label="默认费用预算" type="number" min="0" step="0.5"
              value={budget} onChange={(event) => setBudget(event.target.value)} /></label>
            <label><span>课程 ID（可选）</span><input aria-label="课程 ID" value={courseId}
              onChange={(event) => setCourseId(event.target.value)} /></label>
            <label><span>首选聊天模型（可选）</span><input aria-label="首选聊天模型" value={chatModel}
              placeholder="provider/model" onChange={(event) => setChatModel(event.target.value)} /></label>
            <label><span>群组允许工具（逗号分隔）</span><input aria-label="群组允许工具" value={groupTools}
              onChange={(event) => setGroupTools(event.target.value)} /></label>
            <label className="study-form-wide"><span>资料源 ID（可选）</span><input aria-label="资料源 ID" value={sourceId}
              onChange={(event) => setSourceId(event.target.value)} /></label>
          </div>
          <fieldset className="role-options">
            <legend>Agent 阵容</legend>
            <label><input type="checkbox" aria-label="Coordinator" checked disabled />Coordinator <small>必选</small></label>
            <label><input type="checkbox" aria-label="Verifier" checked disabled />Verifier <small>必选</small></label>
            {(Object.keys(roles) as Array<keyof typeof roles>).map((role) => <label key={role}>
              <input type="checkbox" checked={roles[role]} onChange={(event) => setRoles((current) => ({ ...current, [role]: event.target.checked }))} />
              {role === 'knowledge' ? '课程知识' : role === 'planning' ? '规划' : '学习教练'}
            </label>)}
          </fieldset>
          <button type="button" className="study-link-button" aria-expanded={expertOpen}
            onClick={() => setExpertOpen((value) => !value)}><IconPlus />添加受控专家 Agent</button>
          {expertOpen ? <div className="expert-fields">
            <label><span>专家名称</span><input value={expert.name} maxLength={80}
              onChange={(event) => setExpert((value) => ({ ...value, name: event.target.value }))} /></label>
            <label><span>专业范围</span><input value={expert.specialty} maxLength={200}
              onChange={(event) => setExpert((value) => ({ ...value, specialty: event.target.value }))} /></label>
            <label className="study-form-wide"><span>允许工具（逗号分隔）</span><input value={expert.tools}
              onChange={(event) => setExpert((value) => ({ ...value, tools: event.target.value }))} /></label>
            <p>平台权限、Verifier、PII 与学术诚信策略始终锁定。</p>
          </div> : null}
          {error ? <p className="study-form-error" role="alert">{error}</p> : null}
          <footer><button type="button" className="study-secondary" onClick={onClose}>取消</button>
            <button type="submit" className="study-primary" disabled={submitting}>{submitting ? '正在创建…' : '创建群组'}</button></footer>
        </form>
      </div>
    </div>
  );
};

export default GroupCreationWizard;

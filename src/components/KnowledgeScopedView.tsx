import React, { useEffect, useMemo, useState } from 'react';
import { shallow } from 'zustand/shallow';

import type { Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import KnowledgeView from './KnowledgeView';
import LocalKnowledgePanel from './LocalKnowledgePanel';

export const KnowledgeScopedView: React.FC = () => {
  const groups = useGroupStore((state) => state.order.map((id) => state.groups[id])
    .filter((group): group is Group => group !== undefined && (group.lifecycle === 'active' || group.lifecycle === 'archived')), shallow);
  const [groupId, setGroupId] = useState('');
  const selectedGroup = groups.find((group) => group.group_id === groupId) || null;
  const [courseId, setCourseId] = useState('');

  const dataScope = useMemo(() => ({
    groupIds: selectedGroup ? [selectedGroup.group_id] : [],
    projectIds: (selectedGroup ? [selectedGroup] : [])
      .map((group) => group.legacy_project_id).filter((id): id is string => Boolean(id)),
    // This is the whole selected Group, not an explicit narrow source filter.
    // The backend derives authorized sources from canonical GroupRepository links.
    sourceIds: [],
    courseId: courseId || null,
  }), [courseId, selectedGroup]);

  useEffect(() => {
    if (groupId && !groups.some((group) => group.group_id === groupId)) setGroupId('');
  }, [groupId, groups]);
  useEffect(() => setCourseId(''), [groupId]);
  const courseStillBound = !courseId || Boolean(selectedGroup?.course_bindings.some((binding) => binding.course_id === courseId));
  useEffect(() => { if (!courseStillBound) setCourseId(''); }, [courseStillBound]);

  return <div className="knowledge-scoped-view" data-group-scope={groupId || 'all'} data-course-scope={courseId || 'all'}>
    <form className="knowledge-scope-bar" aria-label="知识中心群组与课程范围">
      <strong>资料范围</strong>
      <label><span>群组</span><select aria-label="知识群组范围" value={groupId} onChange={(event) => setGroupId(event.target.value)}>
        <option value="">请选择群组</option>
        {groups.map((group) => <option key={group.group_id} value={group.group_id}>{group.name}</option>)}
      </select></label>
      <label><span>课程</span><select aria-label="知识课程范围" value={courseId} disabled={!selectedGroup}
        onChange={(event) => setCourseId(event.target.value)}>
        <option value="">{selectedGroup ? '群组内全部课程' : '请先选择群组'}</option>
        {selectedGroup?.course_bindings.map((binding) => <option key={binding.binding_id} value={binding.course_id}>{binding.course_id}</option>)}
      </select></label>
      <small>仅显示所选群组绑定或显式链接的资料；课程范围缺少可靠归属元数据时会安全隐藏。</small>
    </form>
    {selectedGroup && courseStillBound ? <LocalKnowledgePanel key={`${groupId}:${courseId}`} groupId={groupId} courseId={courseId}
      readOnly={selectedGroup.lifecycle === 'archived'} /> : <p className="local-knowledge-hint">选择一个群组后，可导入本地资料并检索证据。</p>}
    {selectedGroup && courseStillBound ? <div className="knowledge-scoped-body"><KnowledgeView dataScope={dataScope} /></div> : null}
  </div>;
};

export default KnowledgeScopedView;

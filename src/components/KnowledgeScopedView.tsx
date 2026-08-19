import React, { useEffect, useState } from 'react';
import { shallow } from 'zustand/shallow';

import type { Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import KnowledgeView from './KnowledgeView';

export const KnowledgeScopedView: React.FC = () => {
  const groups = useGroupStore((state) => state.order.map((id) => state.groups[id])
    .filter((group): group is Group => group !== undefined && group.lifecycle === 'active'), shallow);
  const [groupId, setGroupId] = useState('');
  const selectedGroup = groups.find((group) => group.group_id === groupId) || null;
  const [courseId, setCourseId] = useState('');

  useEffect(() => {
    if (groupId && !groups.some((group) => group.group_id === groupId)) setGroupId('');
  }, [groupId, groups]);
  useEffect(() => setCourseId(''), [groupId]);

  return <div className="knowledge-scoped-view" data-group-scope={groupId || 'all'} data-course-scope={courseId || 'all'}>
    <form className="knowledge-scope-bar" aria-label="知识中心群组与课程范围">
      <strong>资料范围</strong>
      <label><span>群组</span><select aria-label="知识群组范围" value={groupId} onChange={(event) => setGroupId(event.target.value)}>
        <option value="">全部已授权群组</option>
        {groups.map((group) => <option key={group.group_id} value={group.group_id}>{group.name}</option>)}
      </select></label>
      <label><span>课程</span><select aria-label="知识课程范围" value={courseId} disabled={!selectedGroup}
        onChange={(event) => setCourseId(event.target.value)}>
        <option value="">{selectedGroup ? '群组内全部课程' : '请先选择群组'}</option>
        {selectedGroup?.course_bindings.map((binding) => <option key={binding.binding_id} value={binding.course_id}>{binding.course_id}</option>)}
      </select></label>
      <small>当前范围只约束此知识入口；跨群资料仍需显式授权。</small>
    </form>
    <div className="knowledge-scoped-body"><KnowledgeView /></div>
  </div>;
};

export default KnowledgeScopedView;

import { describe, expect, it } from 'vitest';

import type { KnowCard } from '../store/knowledge';
import { knowledgeScopeQuery } from '../store/knowledge';
import { cardAllowedByKnowledgeScope, type KnowledgeDataScope } from './KnowledgeView';

function card(projectId: string, sourceRef: string, scope: 'global' | 'project' = 'project', sourceIds: string[] = [], courseIds: string[] = []): KnowCard {
  return {
    id: `${projectId}:asset`, assetId: 'asset', projectId,
    cls: 'fact', clsZh: '事实', cat: '清单', chip: 'ok', title: '资料', body: '正文',
    appliesWhen: '', status: 'validated', st: 'ok', needsReview: false,
    retireSuggested: false, conflictWith: [], scope, scopeSetBy: 'system', utility: 1,
    cites: 0, citedOk: 0, citedBad: 0, sourceCount: 1, firstSeen: null, lastSeen: null,
    evidence: [{ sourceRef, nodeId: 'node', sourceKind: 'document', step: null,
      observedAt: null, excerpt: '', agentId: null }],
    sourceIds, courseIds,
  };
}

const scope: KnowledgeDataScope = {
  groupIds: [`grp_${'1'.repeat(32)}`],
  projectIds: ['legacy-calculus'],
  sourceIds: [`src_${'2'.repeat(32)}`],
  courseId: null,
};

describe('Knowledge canonical scope boundary', () => {
  it('excludes another Group project while allowing owned project and explicit source links', () => {
    expect(cardAllowedByKnowledgeScope(card('legacy-calculus', 'notes/a.md', 'project', [scope.sourceIds[0]!]), scope)).toBe(true);
    expect(cardAllowedByKnowledgeScope(card('legacy-english', 'notes/a.md'), scope)).toBe(false);
    expect(cardAllowedByKnowledgeScope(
      card('unmapped', 'opaque/path.md', 'project', [scope.sourceIds[0]!]), scope,
    )).toBe(true);
  });

  it('fails closed for a selected course without exact evidence ownership', () => {
    const courseScope = { ...scope, courseId: 'course-calculus' };
    expect(cardAllowedByKnowledgeScope(
      card('legacy-calculus', 'opaque/path.md', 'project', [scope.sourceIds[0]!], ['course-calculus']), courseScope,
    )).toBe(true);
    expect(cardAllowedByKnowledgeScope(
      card('legacy-calculus', 'courses/course-calculus/chapter-1.md', 'project', [scope.sourceIds[0]!], ['course-english']), courseScope,
    )).toBe(false);
  });

  it('allows only explicitly user-approved global assets across Groups', () => {
    const candidate = card('legacy-english', 'opaque.md', 'global');
    expect(cardAllowedByKnowledgeScope(candidate, { ...scope, sourceIds: [] })).toBe(false);
    expect(cardAllowedByKnowledgeScope({ ...candidate, scopeSetBy: 'user' }, { ...scope, sourceIds: [] })).toBe(true);
    expect(cardAllowedByKnowledgeScope({ ...candidate, scopeSetBy: 'user', st: 'pending', status: 'candidate' }, { ...scope, sourceIds: [] })).toBe(false);
  });

  it('carries canonical scope on the request boundary', () => {
    const query = new URLSearchParams(knowledgeScopeQuery({ ...scope, courseId: 'course-calculus' }).slice(1));
    expect(query.getAll('group_id')).toEqual(scope.groupIds);
    expect(query.getAll('legacy_project_id')).toEqual(scope.projectIds);
    expect(query.getAll('source_id')).toEqual(scope.sourceIds);
    expect(query.get('course_id')).toBe('course-calculus');
  });
});

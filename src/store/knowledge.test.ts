import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  KnowledgeQueryScope, KnowCard, KnowPreviewState, SkillPack,
} from './knowledge';
import { useKnowledgeStore } from './knowledge';

const groupA = `grp_${'1'.repeat(32)}`;
const groupB = `grp_${'2'.repeat(32)}`;
const scopeA: KnowledgeQueryScope = { groupIds: [groupA], projectIds: ['project-a'], sourceIds: [], courseId: null };
const scopeB: KnowledgeQueryScope = { groupIds: [groupB], projectIds: ['project-b'], sourceIds: [], courseId: null };

function asset(projectId: string, assetId: string): Record<string, unknown> {
  return {
    project_id: projectId, asset_id: assetId, class: 'fact', category: '清单', title: assetId,
    status: 'validated', view_state: 'ok', scope: 'project', scope_set_by: 'system',
    evidence: [], source_ids: [], course_ids: [],
  };
}

describe('Knowledge request identity and preview boundary', () => {
  beforeEach(() => {
    useKnowledgeStore.setState({
      loading: false, loadedAt: 0, error: null, cards: [], profileProjects: [],
      activeScope: null, activeScopeKey: 'legacy:all', preview: null,
      skillpacks: { systemBuiltin: [], projectExperience: [], thirdParty: [] },
    });
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('closes an A preview immediately and ignores A data that resolves after switching to B', async () => {
    const pending: Array<{ url: string; resolve: (response: Response) => void }> = [];
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => new Promise<Response>((resolve) => {
      pending.push({ url: String(input), resolve });
    })));
    useKnowledgeStore.getState().setScope(scopeA);
    useKnowledgeStore.getState().openPreview({ kind: 'profile', card: null, projectId: 'project-a' } as KnowPreviewState);
    const loadA = useKnowledgeStore.getState().load({ scope: scopeA });
    await vi.waitFor(() => expect(pending).toHaveLength(1));

    useKnowledgeStore.getState().setScope(scopeB);
    expect(useKnowledgeStore.getState().preview).toBeNull();
    expect(useKnowledgeStore.getState().cards).toEqual([]);
    const loadB = useKnowledgeStore.getState().load({ scope: scopeB });
    await vi.waitFor(() => expect(pending).toHaveLength(2));
    pending[1]!.resolve(new Response(JSON.stringify({
      ok: true, assets: [asset('project-b', 'asset-b')], projects: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await loadB;
    pending[0]!.resolve(new Response(JSON.stringify({
      ok: true, assets: [asset('project-a', 'asset-a')], projects: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await loadA;

    expect(useKnowledgeStore.getState().cards.map((card) => card.projectId)).toEqual(['project-b']);
    expect(pending[1]!.url).toContain(`group_id=${groupB}`);
    expect(pending[1]!.url).not.toContain(groupA);
  });

  it('rejects stale A detail/profile/pack objects under active B before dispatch', async () => {
    const urls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(JSON.stringify({ ok: false, error: 'outside_scope' }), {
        status: 403, headers: { 'Content-Type': 'application/json' },
      });
    }));
    useKnowledgeStore.getState().setScope(scopeB);
    const pack: SkillPack = {
      packId: 'pack-a', name: 'A', description: '', kind: 'project_experience', source: '',
      projectId: 'project-a', assetId: '', scope: 'project', status: 'active', st: 'ok',
      immutable: false, createdAt: null, updatedAt: null,
    };
    const card = {
      id: 'project-a:asset-a', assetId: 'asset-a', projectId: 'project-a',
      sourceIds: [], courseIds: [], scope: 'project', status: 'validated', st: 'ok',
      scopeSetBy: 'system',
    } as unknown as KnowCard;
    expect(await useKnowledgeStore.getState().fetchDetail(card)).toBeNull();
    expect(await useKnowledgeStore.getState().fetchProfile('project-a')).toBeNull();
    expect(await useKnowledgeStore.getState().fetchSkillpackDetail(pack)).toBeNull();
    expect(urls).toHaveLength(0);
  });

  it.each([
    ['foreign source', { sourceIds: ['source-foreign'], courseIds: [] }],
    ['foreign course', { sourceIds: [], courseIds: ['course-foreign'] }],
    ['unapproved global', {
      sourceIds: ['source-b'], courseIds: ['course-b'], scope: 'global',
      scopeSetBy: 'system', status: 'validated', st: 'ok',
    }],
  ])('rejects a same-project %s asset object before detail dispatch', async (_name, patch) => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const narrowB = { ...scopeB, sourceIds: ['source-b'], courseId: 'course-b' };
    useKnowledgeStore.getState().setScope(narrowB);
    const baseCard = {
      id: 'project-b:asset-b', assetId: 'asset-b', projectId: 'project-b',
      sourceIds: ['source-b'], courseIds: ['course-b'], scope: 'project',
      scopeSetBy: 'system', status: 'validated', st: 'ok',
    };
    const card = { ...baseCard, ...patch } as unknown as KnowCard;
    expect(await useKnowledgeStore.getState().fetchDetail(card)).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('rejects a same-project skill pack with a foreign contributor before dispatch', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      ...scopeB, sourceIds: ['source-b'], courseId: 'course-b',
    });
    const pack = {
      packId: 'pack-b', name: 'B', description: '', kind: 'project_experience', source: '',
      projectId: 'project-b', assetId: 'asset-b', scope: 'project', status: 'active', st: 'ok',
      immutable: false, sourceIds: ['source-foreign'], courseIds: ['course-b'],
      createdAt: null, updatedAt: null,
    } as SkillPack;
    expect(await useKnowledgeStore.getState().fetchSkillpackDetail(pack)).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('does not revive an A detail after an A → B → A scope epoch change', async () => {
    let resolveDetail!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { resolveDetail = resolve; })));
    useKnowledgeStore.getState().setScope(scopeA);
    const card = {
      id: 'project-a:asset-a', assetId: 'asset-a', projectId: 'project-a',
      sourceIds: [], courseIds: [], scope: 'project', scopeSetBy: 'system',
      status: 'validated', st: 'ok',
    } as unknown as KnowCard;
    const detail = useKnowledgeStore.getState().fetchDetail(card);
    await vi.waitFor(() => expect(resolveDetail).toBeTypeOf('function'));
    useKnowledgeStore.getState().setScope(scopeB);
    useKnowledgeStore.getState().setScope(scopeA);
    resolveDetail(new Response(JSON.stringify({ found: true, asset: {
      ...asset('project-a', 'asset-a'), body_md: 'STALE-A-BODY',
    } }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    expect(await detail).toBeNull();
  });
});

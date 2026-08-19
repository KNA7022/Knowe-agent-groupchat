import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { KnowledgeQueryScope, KnowPreviewState, SkillPack } from './knowledge';
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

  it('carries the active canonical scope on profile and skillpack detail requests', async () => {
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
    expect(await useKnowledgeStore.getState().fetchProfile('project-a')).toBeNull();
    expect(await useKnowledgeStore.getState().fetchSkillpackDetail(pack)).toBeNull();
    expect(urls).toHaveLength(2);
    for (const url of urls) {
      expect(url).toContain(`group_id=${groupB}`);
      expect(url).toContain('legacy_project_id=project-b');
    }
  });
});


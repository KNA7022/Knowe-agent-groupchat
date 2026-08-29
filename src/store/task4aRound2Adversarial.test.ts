import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { KnowCard, SkillPack } from './knowledge';
import { useKnowledgeStore } from './knowledge';

const groupB = `grp_${'8'.repeat(32)}`;

function response(body: object): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

describe('Task4A round2 independent scope attacks', () => {
  beforeEach(() => {
    useKnowledgeStore.setState({
      loading: false, loadedAt: 0, error: null, cards: [], profileProjects: [],
      activeScope: null, activeScopeKey: 'legacy:all', preview: null,
      skillpacks: { systemBuiltin: [], projectExperience: [], thirdParty: [] },
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it('does not dispatch ownerless aggregates inside a narrow source/course boundary', async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/profile')) return response({ text: 'PRIVATE PROFILE' });
      if (url.includes('/skillpacks/')) return response({ found: true, pack: {
        pack_id: 'pack-b', project_id: 'project-b', name: 'pack', kind: 'project_experience',
        scope: 'project', status: 'active', view_state: 'ok',
      }, body_md: 'PRIVATE PACK' });
      return response({ found: true, asset: {
        project_id: 'project-b', asset_id: 'asset-b', class: 'fact', category: '清单',
        title: 'asset', scope: 'project', scope_set_by: 'system', status: 'validated',
        view_state: 'ok', source_ids: [], course_ids: [], body_md: 'PRIVATE ASSET',
      } });
    });
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: ['source-b'], courseId: 'course-b',
    });
    const card = {
      id: 'project-b:asset-b', projectId: 'project-b', assetId: 'asset-b',
      sourceIds: [], courseIds: [], scope: 'project', scopeSetBy: 'system',
      status: 'validated', st: 'ok',
    } as unknown as KnowCard;
    const pack = {
      packId: 'pack-b', projectId: 'project-b', assetId: 'asset-b', name: 'pack',
      kind: 'project_experience', scope: 'project', status: 'active', st: 'ok',
      immutable: false, sourceIds: [], courseIds: [],
    } as SkillPack;

    const profile = await useKnowledgeStore.getState().fetchProfile('project-b');
    const detail = await useKnowledgeStore.getState().fetchDetail(card);
    const skill = await useKnowledgeStore.getState().fetchSkillpackDetail(pack);
    expect({ profile, detail, skill }).toEqual({ profile: null, detail: null, skill: null });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('keeps an explicitly approved clean global asset readable in its bound project', async () => {
    const fetchSpy = vi.fn(async () => response({ found: true, asset: {
      project_id: 'project-b', asset_id: 'global-b', class: 'fact', category: '清单',
      title: 'global', scope: 'global', scope_set_by: 'user', status: 'validated',
      view_state: 'ok', source_ids: [], course_ids: [], body_md: 'APPROVED GLOBAL',
    } }));
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: [], courseId: null,
    });
    const card = {
      id: 'project-b:global-b', projectId: 'project-b', assetId: 'global-b',
      sourceIds: [], courseIds: [], scope: 'global', scopeSetBy: 'user',
      status: 'validated', st: 'ok', needsReview: false,
    } as unknown as KnowCard;

    expect((await useKnowledgeStore.getState().fetchDetail(card))?.bodyMd).toBe('APPROVED GLOBAL');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('keeps only positively proven asset and pack rows in a narrow scope', async () => {
    const matchingAsset = {
      project_id: 'project-b', asset_id: 'asset-matching', class: 'fact', category: '清单',
      title: 'matching', scope: 'project', scope_set_by: 'system', status: 'validated',
      view_state: 'ok', source_ids: ['source-b'], course_ids: ['course-b'], body_md: 'MATCHING ASSET',
    };
    const matchingPack = {
      pack_id: 'pack-matching', project_id: 'project-b', name: 'matching',
      kind: 'project_experience', scope: 'project', status: 'active', view_state: 'ok',
      source_ids: ['source-b'], course_ids: ['course-b'],
    };
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/skillpacks?')) return response({
        project_experience: [
          matchingPack,
          { ...matchingPack, pack_id: 'pack-ownerless', source_ids: [], course_ids: [] },
          { ...matchingPack, pack_id: 'pack-foreign', source_ids: ['source-foreign'] },
        ],
      });
      if (url.includes('/skillpacks/')) return response({ found: true, pack: matchingPack, body_md: 'MATCHING PACK' });
      if (url.includes('/assets?')) return response({
        assets: [
          matchingAsset,
          { ...matchingAsset, asset_id: 'asset-ownerless', source_ids: [], course_ids: [] },
          { ...matchingAsset, asset_id: 'asset-foreign', course_ids: ['course-foreign'] },
        ],
        projects: [{ project_id: 'project-b', profile_exists: true }],
      });
      return response({ found: true, asset: matchingAsset });
    });
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: ['source-b'], courseId: 'course-b',
    });

    await useKnowledgeStore.getState().load();
    await useKnowledgeStore.getState().loadSkillpacks();
    expect(useKnowledgeStore.getState().cards.map((card) => card.assetId)).toEqual(['asset-matching']);
    expect(useKnowledgeStore.getState().skillpacks.projectExperience.map((pack) => pack.packId))
      .toEqual(['pack-matching']);
    expect(useKnowledgeStore.getState().profileProjects).toEqual([]);

    const beforeProfile = fetchSpy.mock.calls.length;
    expect(await useKnowledgeStore.getState().fetchProfile('project-b')).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(beforeProfile);
    expect((await useKnowledgeStore.getState().fetchDetail(useKnowledgeStore.getState().cards[0]!))?.bodyMd)
      .toBe('MATCHING ASSET');
    expect((await useKnowledgeStore.getState().fetchSkillpackDetail(
      useKnowledgeStore.getState().skillpacks.projectExperience[0]!,
    ))?.bodyMd).toBe('MATCHING PACK');
  });

  it('does not dispatch ownerless asset/pack writes inside a narrow boundary', async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/assets?')) return response({ assets: [], projects: [] });
      if (url.includes('/skillpacks?')) return response({ project_experience: [] });
      return response({ ok: true });
    });
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: ['source-b'], courseId: 'course-b',
    });
    const card = {
      id: 'project-b:asset-b', projectId: 'project-b', assetId: 'asset-b',
      sourceIds: [], courseIds: [], scope: 'project', scopeSetBy: 'system',
      status: 'validated', st: 'ok',
    } as unknown as KnowCard;
    const pack = {
      packId: 'pack-b', projectId: 'project-b', assetId: 'asset-b', name: 'pack',
      kind: 'project_experience', scope: 'project', status: 'active', st: 'ok',
      immutable: false, sourceIds: [], courseIds: [],
    } as SkillPack;

    const results = await Promise.all([
      useKnowledgeStore.getState().override(card, { title: 'changed' }),
      useKnowledgeStore.getState().review(card, 'approve'),
      useKnowledgeStore.getState().purge(card),
      useKnowledgeStore.getState().reviewSkillpack(pack, 'approve'),
      useKnowledgeStore.getState().setSkillpackStatus(pack, 'active'),
      useKnowledgeStore.getState().purgeSkillpack(pack),
    ]);
    expect(results).toEqual([false, false, false, false, false, false]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

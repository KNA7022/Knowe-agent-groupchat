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

  afterEach(() => { vi.unstubAllGlobals(); });

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
      description: '', source: '', createdAt: null, updatedAt: null,
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
        projects: [{
          project_id: 'project-b', profile_exists: true,
          profile_source_ids: ['source-b'], profile_course_ids: ['course-b'],
        }],
      });
      if (url.includes('/profile')) return response({
        project_id: 'project-b', source_ids: ['source-b'], course_ids: ['course-b'],
        text: 'MATCHING PROFILE',
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
    expect(useKnowledgeStore.getState().profileProjects).toEqual(['project-b']);

    const beforeProfile = fetchSpy.mock.calls.length;
    expect(await useKnowledgeStore.getState().fetchProfile('project-b')).toBe('MATCHING PROFILE');
    expect(fetchSpy).toHaveBeenCalledTimes(beforeProfile + 1);
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
      description: '', source: '', createdAt: null, updatedAt: null,
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

  it('fails closed for every ownerless Project route in a broad Group scope', async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/skillpacks?')) return response({
        project_experience: [{
          pack_id: 'pack-ownerless', project_id: 'project-b', name: 'ownerless pack',
          kind: 'project_experience', scope: 'project', status: 'active', view_state: 'ok',
          source_ids: [], course_ids: [],
        }],
      });
      if (url.includes('/assets?')) return response({
        assets: [{
          project_id: 'project-b', asset_id: 'asset-ownerless', class: 'fact', category: '清单',
          title: 'ownerless asset', scope: 'project', scope_set_by: 'system',
          status: 'validated', view_state: 'ok', source_ids: [], course_ids: [],
        }],
        projects: [{
          project_id: 'project-b', profile_exists: true,
          profile_source_ids: [], profile_course_ids: [],
        }],
      });
      if (url.includes('/profile')) return response({ text: 'OWNERLESS PROFILE BODY' });
      if (url.includes('/skillpacks/')) return response({
        found: true,
        pack: {
          pack_id: 'pack-ownerless', project_id: 'project-b', name: 'ownerless pack',
          kind: 'project_experience', scope: 'project', status: 'active', view_state: 'ok',
          source_ids: [], course_ids: [],
        },
        body_md: 'OWNERLESS PACK BODY',
      });
      if (url.includes('/assets/')) return response({
        found: true,
        asset: {
          project_id: 'project-b', asset_id: 'asset-ownerless', class: 'fact', category: '清单',
          title: 'ownerless asset', scope: 'project', scope_set_by: 'system',
          status: 'validated', view_state: 'ok', source_ids: [], course_ids: [],
          body_md: 'OWNERLESS ASSET BODY',
        },
      });
      return response({ ok: true });
    });
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: [], courseId: null,
    });

    await useKnowledgeStore.getState().load();
    await useKnowledgeStore.getState().loadSkillpacks();
    expect(useKnowledgeStore.getState().cards).toEqual([]);
    expect(useKnowledgeStore.getState().profileProjects).toEqual([]);
    expect(useKnowledgeStore.getState().skillpacks.projectExperience).toEqual([]);

    const card = {
      id: 'project-b:asset-ownerless', projectId: 'project-b', assetId: 'asset-ownerless',
      sourceIds: [], courseIds: [], scope: 'project', scopeSetBy: 'system',
      status: 'validated', st: 'ok',
    } as unknown as KnowCard;
    const pack = {
      packId: 'pack-ownerless', projectId: 'project-b', assetId: 'asset-ownerless',
      name: 'ownerless pack', description: '', source: '', createdAt: null, updatedAt: null,
      kind: 'project_experience', scope: 'project', status: 'active',
      st: 'ok', immutable: false, sourceIds: [], courseIds: [],
    } as SkillPack;
    const requestsAfterLists = fetchSpy.mock.calls.length;

    useKnowledgeStore.getState().openPreview({
      kind: 'asset', card, projectId: 'project-b',
    });
    useKnowledgeStore.getState().openPreview({
      kind: 'profile', card: null, projectId: 'project-b',
    });
    useKnowledgeStore.getState().openPreview({
      kind: 'skill', card: null, pack, projectId: 'project-b',
    });
    expect(useKnowledgeStore.getState().preview).toBeNull();

    const [profile, detail, packDetail, ...writes] = await Promise.all([
      useKnowledgeStore.getState().fetchProfile('project-b'),
      useKnowledgeStore.getState().fetchDetail(card),
      useKnowledgeStore.getState().fetchSkillpackDetail(pack),
      useKnowledgeStore.getState().override(card, { title: 'changed' }),
      useKnowledgeStore.getState().review(card, 'approve'),
      useKnowledgeStore.getState().purge(card),
      useKnowledgeStore.getState().reviewSkillpack(pack, 'approve'),
      useKnowledgeStore.getState().setSkillpackStatus(pack, 'active'),
      useKnowledgeStore.getState().purgeSkillpack(pack),
    ]);
    expect({ profile, detail, packDetail }).toEqual({ profile: null, detail: null, packDetail: null });
    expect(writes).toEqual([false, false, false, false, false, false]);
    expect(fetchSpy).toHaveBeenCalledTimes(requestsAfterLists);
  });

  it('does not treat ownerless unapproved/non-live or immutable system SkillPacks as approved globals', async () => {
    const rawGlobalPack = {
      pack_id: 'pack-global-ownerless', project_id: 'project-b', name: 'global pack',
      kind: 'project_experience', scope: 'global', status: 'active', view_state: 'ok',
      source_ids: [], course_ids: [],
    };
    const rawPendingGlobalPack = {
      pack_id: 'pack-global-pending', project_id: 'project-b', name: 'pending global pack',
      kind: 'project_experience', scope: 'global', status: 'active', view_state: 'pending',
      scope_set_by: 'user', approval_status: 'core', source_ids: [], course_ids: [],
    };
    const rawSystemPack = {
      pack_id: 'pack-system-ownerless', project_id: '', name: 'system pack',
      kind: 'system_builtin', scope: 'global', status: 'active', view_state: 'ok', immutable: true,
      source_ids: [], course_ids: [],
    };
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/skillpacks?')) return response({
        system_builtin: [rawSystemPack],
        project_experience: [rawGlobalPack, rawPendingGlobalPack],
      });
      return response({ found: true, pack: rawGlobalPack, body_md: 'UNAPPROVED GLOBAL PACK' });
    });
    vi.stubGlobal('fetch', fetchSpy);
    useKnowledgeStore.getState().setScope({
      groupIds: [groupB], projectIds: ['project-b'], sourceIds: [], courseId: null,
    });

    await useKnowledgeStore.getState().loadSkillpacks();
    expect(useKnowledgeStore.getState().skillpacks.systemBuiltin).toEqual([]);
    expect(useKnowledgeStore.getState().skillpacks.projectExperience).toEqual([]);

    const packs = [
      {
        packId: 'pack-global-ownerless', projectId: 'project-b', assetId: '', name: 'global pack',
        description: '', source: '', createdAt: null, updatedAt: null,
        kind: 'project_experience', scope: 'global', status: 'active', st: 'ok', immutable: false,
        sourceIds: [], courseIds: [],
      },
      {
        packId: 'pack-system-ownerless', projectId: '', assetId: '', name: 'system pack',
        description: '', source: '', createdAt: null, updatedAt: null,
        kind: 'system_builtin', scope: 'global', status: 'active', st: 'ok', immutable: true,
        sourceIds: [], courseIds: [],
      },
      {
        packId: 'pack-global-pending', projectId: 'project-b', assetId: '',
        name: 'pending global pack', description: '', source: '', createdAt: null, updatedAt: null,
        kind: 'project_experience', scope: 'global', status: 'active', st: 'pending', immutable: false,
        scopeSetBy: 'user', approvalStatus: 'core', sourceIds: [], courseIds: [],
      },
    ] as SkillPack[];
    const requestsAfterList = fetchSpy.mock.calls.length;
    for (const pack of packs) {
      useKnowledgeStore.getState().openPreview({
        kind: 'skill', card: null, pack, projectId: pack.projectId,
      });
      expect(useKnowledgeStore.getState().preview).toBeNull();
      expect(await useKnowledgeStore.getState().fetchSkillpackDetail(pack)).toBeNull();
      expect(await useKnowledgeStore.getState().reviewSkillpack(pack, 'approve')).toBe(false);
      expect(await useKnowledgeStore.getState().setSkillpackStatus(pack, 'active')).toBe(false);
      expect(await useKnowledgeStore.getState().purgeSkillpack(pack)).toBe(false);
    }
    expect(fetchSpy).toHaveBeenCalledTimes(requestsAfterList);
  });
});

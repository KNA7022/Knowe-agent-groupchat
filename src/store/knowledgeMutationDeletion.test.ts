import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { KnowCard, SkillPack } from './knowledge';
import { teardownGroup } from './groupTeardown';
import { useKnowledgeStore } from './knowledge';

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

describe('Knowledge mutations retain their original deletion fence', () => {
  beforeEach(() => useKnowledgeStore.setState({
    loading: false, loadedAt: 0, error: null, cards: [], profileProjects: [], preview: null,
    activeScope: null, activeScopeKey: 'legacy:all',
    skillpacks: { systemBuiltin: [], projectExperience: [], thirdParty: [] },
  }));
  afterEach(() => { vi.unstubAllGlobals(); });

  const operations = [
    ['skill-review', (pack: SkillPack, _card: KnowCard) => useKnowledgeStore.getState().reviewSkillpack(pack, 'approve')],
    ['skill-status', (pack: SkillPack, _card: KnowCard) => useKnowledgeStore.getState().setSkillpackStatus(pack, 'active')],
    ['skill-purge', (pack: SkillPack, _card: KnowCard) => useKnowledgeStore.getState().purgeSkillpack(pack)],
    ['asset-override', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().override(card, { title: 'changed' })],
    ['asset-review', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().review(card, 'approve')],
    ['asset-purge', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().purge(card)],
  ] as const;

  it.each(operations)('%s issues no follow-up after a late success', async (_name, invoke) => {
    await runCase(invoke, true);
  });

  it.each(operations)('%s issues no follow-up after a late failure', async (_name, invoke) => {
    await runCase(invoke, false);
  });

  it.each(operations)('%s rejects an A object under active B without dispatch', async (_name, invoke) => {
    caseSequence += 1;
    const groupId = `grp_${caseSequence.toString(16).padStart(32, '0')}`;
    useKnowledgeStore.getState().setScope({
      groupIds: [groupId], projectIds: ['project-b'], sourceIds: [], courseId: null,
    });
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const pack = { packId: 'pack-a', projectId: 'project-a', name: 'pack-a' } as SkillPack;
    const card = {
      id: 'project-a:asset-a', assetId: 'asset-a', projectId: 'project-a',
      sourceIds: [], courseIds: [], scope: 'project', status: 'validated', st: 'ok',
      scopeSetBy: 'system',
    } as unknown as KnowCard;
    expect(await invoke(pack, card)).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['skill-review', (pack: SkillPack) => useKnowledgeStore.getState().reviewSkillpack(pack, 'approve')],
    ['skill-status', (pack: SkillPack) => useKnowledgeStore.getState().setSkillpackStatus(pack, 'active')],
  ] as const)('%s rechecks the original fence after its nested reload', async (_name, invoke) => {
    caseSequence += 1;
    const suffix = caseSequence.toString(16).padStart(32, '0');
    const groupId = `grp_${suffix}`;
    const legacyId = `legacy-nested-${suffix}`;
    const scope = { groupIds: [groupId], projectIds: [legacyId], sourceIds: [], courseId: null };
    useKnowledgeStore.getState().setScope(scope);
    let resolveReload!: (response: Response) => void;
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/settings')) return Promise.resolve(response({ ok: true }));
      requests.push(url);
      if (requests.length === 1) return Promise.resolve(response({ ok: true }));
      return new Promise<Response>((resolve) => { resolveReload = resolve; });
    }));
    const operation = invoke({ packId: 'pack', projectId: legacyId, name: 'pack' } as SkillPack);
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    teardownGroup(groupId, legacyId);
    resolveReload(response({ ok: true, project_experience: [] }));
    expect(await operation).toBe(false);
    expect(useKnowledgeStore.getState().skillpacks.projectExperience).toEqual([]);
  });

  it.each([
    ['asset-override', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().override(card, { title: 'changed' })],
    ['asset-review', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().review(card, 'approve')],
    ['asset-purge', (_pack: SkillPack, card: KnowCard) => useKnowledgeStore.getState().purge(card)],
  ] as const)('%s rejects foreign contributors and unapproved global state before dispatch', async (_name, invoke) => {
    caseSequence += 1;
    const groupId = `grp_${caseSequence.toString(16).padStart(32, '0')}`;
    useKnowledgeStore.getState().setScope({
      groupIds: [groupId], projectIds: ['project-b'], sourceIds: ['source-b'], courseId: 'course-b',
    });
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const card = {
      id: 'project-b:asset-b', assetId: 'asset-b', projectId: 'project-b',
      sourceIds: ['source-foreign'], courseIds: ['course-b'], scope: 'global',
      scopeSetBy: 'system', status: 'validated', st: 'ok',
    } as KnowCard;
    expect(await invoke({} as SkillPack, card)).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['skill-review', (pack: SkillPack) => useKnowledgeStore.getState().reviewSkillpack(pack, 'approve')],
    ['skill-status', (pack: SkillPack) => useKnowledgeStore.getState().setSkillpackStatus(pack, 'active')],
  ] as const)('%s rejects after scope switching during a failed nested reload', async (_name, invoke) => {
    caseSequence += 1;
    const suffix = caseSequence.toString(16).padStart(32, '0');
    const groupId = `grp_${suffix}`;
    const legacyId = `legacy-switch-${suffix}`;
    useKnowledgeStore.getState().setScope({
      groupIds: [groupId], projectIds: [legacyId], sourceIds: [], courseId: null,
    });
    let rejectReload!: (reason: Error) => void;
    let requestCount = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/settings')) return Promise.resolve(response({ ok: true }));
      requestCount += 1;
      if (requestCount === 1) return Promise.resolve(response({ ok: true }));
      return new Promise<Response>((_resolve, reject) => { rejectReload = reject; });
    }));
    const operation = invoke({ packId: 'pack', projectId: legacyId, name: 'pack' } as SkillPack);
    await vi.waitFor(() => expect(requestCount).toBe(2));
    const otherGroup = `grp_${(caseSequence + 100).toString(16).padStart(32, '0')}`;
    useKnowledgeStore.getState().setScope({
      groupIds: [otherGroup], projectIds: ['other-project'], sourceIds: [], courseId: null,
    });
    rejectReload(new Error('reload failed'));
    expect(await operation).toBe(false);
    expect(useKnowledgeStore.getState().activeScope?.projectIds).toEqual(['other-project']);
  });

  it.each([
    ['skill-review', (pack: SkillPack) => useKnowledgeStore.getState().reviewSkillpack(pack, 'approve')],
    ['skill-status', (pack: SkillPack) => useKnowledgeStore.getState().setSkillpackStatus(pack, 'active')],
  ] as const)('%s does not commit when deletion occurs during its catch-triggered reload', async (_name, invoke) => {
    caseSequence += 1;
    const suffix = caseSequence.toString(16).padStart(32, '0');
    const groupId = `grp_${suffix}`;
    const legacyId = `legacy-catch-${suffix}`;
    useKnowledgeStore.getState().setScope({
      groupIds: [groupId], projectIds: [legacyId], sourceIds: [], courseId: null,
    });
    let resolveReload!: (response: Response) => void;
    let requestCount = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/settings')) return Promise.resolve(response({ ok: true }));
      requestCount += 1;
      if (requestCount === 1) return Promise.reject(new Error('mutation failed'));
      return new Promise<Response>((resolve) => { resolveReload = resolve; });
    }));
    const operation = invoke({ packId: 'pack', projectId: legacyId, name: 'pack' } as SkillPack);
    await vi.waitFor(() => expect(requestCount).toBe(2));
    teardownGroup(groupId, legacyId);
    resolveReload(response({ ok: true, project_experience: [{
      pack_id: 'stale-pack', project_id: legacyId, name: 'stale', kind: 'project_experience',
    }] }));
    expect(await operation).toBe(false);
    expect(JSON.stringify(useKnowledgeStore.getState())).not.toContain('stale-pack');
  });
});

async function runCase(
  invoke: (pack: SkillPack, card: KnowCard) => Promise<boolean>, succeeds: boolean,
): Promise<void> {
  caseSequence += 1;
  const suffix = caseSequence.toString(16).padStart(32, '0');
  const groupId = `grp_${suffix}`;
  const legacyId = `legacy-mutation-${suffix}`;
  const scope = { groupIds: [groupId], projectIds: [legacyId], sourceIds: [], courseId: null };
  useKnowledgeStore.getState().setScope(scope);
  const knowledgeRequests: string[] = [];
  let resolveOriginal!: (value: Response) => void;
  let rejectOriginal!: (reason: Error) => void;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/settings')) return Promise.resolve(response({ ok: true }));
    knowledgeRequests.push(url);
    if (knowledgeRequests.length > 1) return Promise.resolve(response({ ok: true }));
    return new Promise<Response>((resolve, reject) => {
      resolveOriginal = resolve;
      rejectOriginal = reject;
    });
  }));
  const pack = { packId: 'pack', projectId: legacyId, name: 'pack' } as SkillPack;
  const card = { id: `${legacyId}:asset`, assetId: 'asset', projectId: legacyId } as KnowCard;
  const operation = invoke(pack, card);
  await vi.waitFor(() => expect(knowledgeRequests).toHaveLength(1));
  teardownGroup(groupId, legacyId);
  if (succeeds) resolveOriginal(response({ ok: true }));
  else rejectOriginal(new Error('late failure'));
  expect(await operation).toBe(false);
  await Promise.resolve();
  expect(knowledgeRequests).toHaveLength(1);
  expect(useKnowledgeStore.getState().error).toBeNull();
}

let caseSequence = 0;

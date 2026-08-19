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

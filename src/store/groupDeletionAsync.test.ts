import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { KnowCard, SkillPack } from './knowledge';
import { teardownGroup } from './groupTeardown';
import { getState as getDirectoryState, openRequest, syncFromProjectCreated } from './directoryRecovery';
import { useKnowledgeStore } from './knowledge';
import { useRecordsStore } from './records';
import { agentBindingKey, useSettingsStore } from './settings';
import { useKnoweStore } from './store';

const groupId = `grp_${'7'.repeat(32)}`;
const legacyId = 'legacy-task4a-deleted';
const marker = 'TASK4A-FOREIGN-DELETED-CONTENT';

interface DeferredResponse {
  url: string;
  resolve: (response: Response) => void;
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

describe('durable deletion generation fence', () => {
  beforeEach(() => {
    useKnowledgeStore.setState({
      loading: false, loadedAt: 0, error: null, cards: [], profileProjects: [], preview: null,
      profileContributors: {},
      activeScope: null, activeScopeKey: 'legacy:all',
      skillpacks: { systemBuiltin: [], projectExperience: [], thirdParty: [] },
    });
    useRecordsStore.getState().reset();
  });

  it('drops deferred records, settings, Knowledge and directory responses after teardown', async () => {
    const deferred: DeferredResponse[] = [];
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if ((init?.method || 'GET') === 'POST') {
        return Promise.resolve(json({ ok: true, settings_revision: 1 }));
      }
      return new Promise<Response>((resolve) => deferred.push({ url, resolve }));
    }));

    const scope = { groupIds: [groupId], projectIds: [legacyId], sourceIds: [], courseId: null };
    useKnowledgeStore.getState().setScope(scope);
    useKnowledgeStore.setState({
      profileProjects: [legacyId],
      profileContributors: {
        [legacyId]: { sourceIds: ['source-task4a'], courseIds: ['course-task4a'] },
      },
    });
    useKnoweStore.getState().ensureProject(legacyId, 'Deleted');

    const history = useRecordsStore.getState().load(legacyId);
    useRecordsStore.setState({ loading: false, items: [{
      seq: 1, type: 'message', agent_id: 'agent', content: 'seed', ts: null,
      has_files: false, has_images: false, has_videos: false, has_links: false,
    }], total: 2, page: 1 });
    const more = useRecordsStore.getState().loadMore(legacyId);
    useRecordsStore.setState({ loading: false });
    const handoffs = useRecordsStore.getState().loadHandoffs(legacyId);
    const settings = useSettingsStore.getState().reconcileFromBackend();

    const card = {
      id: `${legacyId}:asset`, assetId: 'asset', projectId: legacyId, title: marker,
      scope: 'project', sourceIds: ['source-task4a'], courseIds: ['course-task4a'],
    } as KnowCard;
    const pack = {
      packId: 'pack', projectId: legacyId, name: marker, kind: 'project_experience',
      scope: 'project', sourceIds: ['source-task4a'], courseIds: ['course-task4a'],
    } as SkillPack;
    const detail = useKnowledgeStore.getState().fetchDetail(card);
    const profile = useKnowledgeStore.getState().fetchProfile(legacyId);
    const packDetail = useKnowledgeStore.getState().fetchSkillpackDetail(pack);
    for (let index = 0; index < 10 && deferred.length < 7; index += 1) await Promise.resolve();
    expect(deferred).toHaveLength(7);
    teardownGroup(groupId, legacyId);
    for (const request of deferred) {
      if (request.url.includes('/history/handoffs')) {
        request.resolve(json({ items: [{ id: marker, title: marker, instruction: marker, report: marker }] }));
      } else if (request.url.includes('/history?')) {
        request.resolve(json({ items: [{ seq: 2, type: 'message', agent_id: 'agent', content: marker }], total: 2 }));
      } else if (request.url.endsWith('/settings')) {
        request.resolve(json({
          ok: true, settings_revision: 99,
          main_model: { provider: 'openai', model: 'gpt-test' },
          agent_models: { [agentBindingKey(legacyId, 'agent')]: { provider: 'openai', model: marker } },
          group_approval_timeouts: { [groupId]: 77, [legacyId]: 88 },
        }));
      } else if (request.url.includes('/profile')) {
        request.resolve(json({ ok: true, text: marker }));
      } else if (request.url.includes('/skillpacks/')) {
        request.resolve(json({ ok: true, found: true, pack: { pack_id: 'pack', name: marker }, body_md: marker }));
      } else {
        request.resolve(json({ ok: true, found: true, asset: {
          project_id: legacyId, asset_id: 'asset', title: marker, body_md: marker,
          status: 'validated', view_state: 'ok', scope: 'project', evidence: [],
        } }));
      }
    }
    const operations = { history, more, handoffs, settings, detail, profile, packDetail };
    const completed: string[] = [];
    const completion = Promise.all(Object.entries(operations).map(async ([name, operation]) => {
      await operation;
      completed.push(name);
    }));
    await completion;
    expect(completed.sort()).toEqual(Object.keys(operations).sort());

    openRequest({ project_id: legacyId, project_name: marker, previous_dir: marker,
      reason: marker, request_id: marker });
    syncFromProjectCreated({ project_id: legacyId, project_name: marker,
      directory_required: { previous_dir: marker, reason: marker, request_id: marker } });
    const serialized = JSON.stringify({
      records: useRecordsStore.getState(), settings: useSettingsStore.getState(),
      knowledge: useKnowledgeStore.getState(), legacy: useKnoweStore.getState(),
      directory: getDirectoryState(),
    });
    expect(serialized).not.toContain(marker);
    expect(serialized).not.toContain(groupId);
    expect(serialized).not.toContain(legacyId);
    expect(useRecordsStore.getState().items).toEqual([]);
    expect(useRecordsStore.getState().handoffs).toEqual([]);
    expect(useSettingsStore.getState().groupApprovalTimeouts[groupId]).toBeUndefined();
    expect(useSettingsStore.getState().groupApprovalTimeouts[legacyId]).toBeUndefined();
    expect(useSettingsStore.getState().agentModels[agentBindingKey(legacyId, 'agent')]).toBeUndefined();

    const callCount = vi.mocked(fetch).mock.calls.length;
    await useRecordsStore.getState().load(legacyId);
    await useKnowledgeStore.getState().fetchProfile(legacyId);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(callCount);
  });

  it('keeps 1000 out-of-order late Knowledge responses at zero-write after deletion', async () => {
    const pending: Array<(response: Response) => void> = [];
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method || 'GET') === 'POST') return Promise.resolve(json({ ok: true }));
      return new Promise<Response>((resolve) => pending.push(resolve));
    }));
    const uniqueGroup = `grp_${'8'.repeat(32)}`;
    const uniqueLegacy = 'legacy-task4a-stress';
    const scope = { groupIds: [uniqueGroup], projectIds: [uniqueLegacy], sourceIds: [], courseId: null };
    useKnowledgeStore.getState().setScope(scope);
    useKnowledgeStore.setState({
      profileProjects: [uniqueLegacy],
      profileContributors: {
        [uniqueLegacy]: { sourceIds: ['source-stress'], courseIds: ['course-stress'] },
      },
    });
    const requests = Array.from({ length: 1000 }, () => (
      useKnowledgeStore.getState().fetchProfile(uniqueLegacy)
    ));
    await vi.waitFor(() => expect(pending).toHaveLength(1000));
    teardownGroup(uniqueGroup, uniqueLegacy);
    for (let index = pending.length - 1; index >= 0; index -= 1) {
      pending[index]!(json({ ok: true, text: `${marker}-${index}` }));
    }
    expect((await Promise.all(requests)).every((value) => value === null)).toBe(true);
    expect(JSON.stringify(useKnowledgeStore.getState())).not.toContain(marker);
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Group } from '../contract/group';
import {
  pendingGroupRequestCacheCount, pendingGroupTeardownCount, retryPendingGroupTeardowns,
  setGroupTeardownLoaderForTests, useGroupStore,
} from './groups';
import { fenceDeletedGroup, isGroupDeletionFenced } from './groupDeletionFence';
import { teardownGroup } from './groupTeardown';
import { useRecordsStore } from './records';
import { agentBindingKey, useSettingsStore } from './settings';

const firstId = `grp_${'1'.repeat(32)}` as const;
const secondId = `grp_${'2'.repeat(32)}` as const;

function group(groupId: typeof firstId | typeof secondId): Group {
  const now = '2026-08-18T00:00:00Z';
  return {
    schema_version: 1,
    group_id: groupId,
    name: groupId === firstId ? 'First' : 'Second',
    template: 'custom',
    lifecycle: 'active',
    roster: [
      {
        agent_id: `gag_${groupId === firstId ? 'a'.repeat(32) : 'c'.repeat(32)}`,
        group_id: groupId,
        role: 'coordinator',
        display_name: 'Coordinator',
        template_key: 'coordinator',
        expert: null,
        model_override: null,
        tool_overrides: [],
        budget: null,
        created_at: now,
        updated_at: now,
      },
      {
        agent_id: `gag_${groupId === firstId ? 'b'.repeat(32) : 'd'.repeat(32)}`,
        group_id: groupId,
        role: 'verifier',
        display_name: 'Verifier',
        template_key: 'verifier',
        expert: null,
        model_override: null,
        tool_overrides: [],
        budget: null,
        created_at: now,
        updated_at: now,
      },
    ],
    course_bindings: [],
    source_links: [],
    budget: null,
    model_overrides: {},
    tool_overrides: {},
    created_at: now,
    updated_at: now,
    archived_at: null,
    legacy_project_id: null,
  };
}

describe('canonical Group store', () => {
  beforeEach(() => useGroupStore.setState({
    groups: {}, order: [], runtimeSummaries: {}, sourceReferenceCounts: {},
    loading: false, error: null,
  }));
  afterEach(() => {
    setGroupTeardownLoaderForTests();
    vi.unstubAllGlobals();
  });

  it('keeps independent groups isolated by opaque group_id', () => {
    useGroupStore.getState().ingest([group(firstId), group(secondId)]);
    const state = useGroupStore.getState();
    expect(state.order).toEqual([firstId, secondId]);
    expect(state.groups[firstId]?.name).toBe('First');
    expect(state.groups[secondId]?.roster.every((agent) => agent.group_id === secondId)).toBe(true);
  });

  it('rejects a roster member projected from another group', () => {
    const invalid = group(firstId);
    invalid.roster[1] = { ...invalid.roster[1]!, group_id: secondId };
    expect(() => useGroupStore.getState().ingest([invalid])).toThrow();
  });

  it('scopes binding, roster, source, and runtime operations to the addressed group_id', async () => {
    useGroupStore.getState().ingest([group(firstId), group(secondId)]);
    const secondBefore = structuredClone(useGroupStore.getState().groups[secondId]);
    const now = '2026-08-18T00:00:00Z';
    const binding = {
      binding_id: `gcb_${'3'.repeat(32)}`,
      group_id: firstId,
      course_id: 'course-a',
      created_at: now,
    };
    const link = {
      link_id: `gsl_${'4'.repeat(32)}`,
      group_id: firstId,
      source_id: `src_${'5'.repeat(32)}`,
      created_at: now,
    };
    const updated = group(firstId);
    updated.roster[0] = { ...updated.roster[0]!, display_name: 'Lead' };
    const summary = {
      group_id: firstId,
      lifecycle: 'active',
      active_agent_count: 2,
      course_binding_count: 0,
      source_count: 0,
      shared_source_count: 0,
      budget: null,
      active_primary_run_id: null,
      primary_task_status: null,
      queued_task_count: 0,
      active_side_task_count: 0,
      pending_approval_count: 0,
      budget_usage: {
        wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0,
      },
      indexing_state: 'idle',
      updated_at: now,
    };

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      const payload = path.endsWith('/course-bind') ? { binding }
        : path.endsWith('/course-unbind') ? {
          ok: true, group_id: firstId, project_id: firstId, removed: true,
        }
          : path.endsWith('/roster-update') ? { group: updated }
            : path.endsWith('/source-link') ? { link, reference_count: 2 }
              : path.endsWith('/source-unlink') ? {
                ok: true, group_id: firstId, project_id: firstId,
                removed: true, reference_count: 1,
              }
                : path.endsWith('/runtime-summary') ? { summary }
                  : { error: `unexpected path: ${path}` };
      return new Response(JSON.stringify(payload), {
        status: 'error' in payload ? 404 : 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));

    await useGroupStore.getState().bindCourse(firstId, 'course-a');
    expect(useGroupStore.getState().groups[firstId]?.course_bindings).toEqual([binding]);
    await useGroupStore.getState().unbindCourse(firstId, 'course-a');
    expect(useGroupStore.getState().groups[firstId]?.course_bindings).toEqual([]);
    await useGroupStore.getState().updateRoster(firstId, updated.roster);
    expect(useGroupStore.getState().groups[firstId]?.roster[0]?.display_name).toBe('Lead');
    await useGroupStore.getState().linkSource(firstId, link.source_id);
    expect(useGroupStore.getState().sourceReferenceCounts[firstId]?.[link.source_id]).toBe(2);
    await useGroupStore.getState().unlinkSource(firstId, link.source_id);
    expect(useGroupStore.getState().sourceReferenceCounts[firstId]?.[link.source_id]).toBe(1);
    await useGroupStore.getState().loadRuntimeSummary(firstId);
    expect(useGroupStore.getState().runtimeSummaries[firstId]).toEqual(summary);
    expect(useGroupStore.getState().groups[secondId]).toEqual(secondBefore);
    expect(useGroupStore.getState().runtimeSummaries[secondId]).toBeUndefined();
    expect(useGroupStore.getState().sourceReferenceCounts[secondId]).toBeUndefined();
  });

  it('rejects an operation response projected for a different group_id', async () => {
    useGroupStore.getState().ingest([group(firstId), group(secondId)]);
    const before = structuredClone(useGroupStore.getState().groups);
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      link: {
        link_id: `gsl_${'6'.repeat(32)}`,
        group_id: secondId,
        source_id: `src_${'7'.repeat(32)}`,
        created_at: '2026-08-18T00:00:00Z',
      },
      reference_count: 1,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    await expect(useGroupStore.getState().linkSource(firstId)).rejects.toThrow('addressed group_id');
    expect(useGroupStore.getState().groups).toEqual(before);
    expect(useGroupStore.getState().sourceReferenceCounts).toEqual({});
  });

  it('rejects malformed or cross-group unbind responses before state mutation', async () => {
    const seeded = group(firstId);
    seeded.course_bindings = [{
      binding_id: `gcb_${'8'.repeat(32)}`,
      group_id: firstId,
      course_id: 'course-a',
      created_at: '2026-08-18T00:00:00Z',
    }];
    seeded.source_links = [{
      link_id: `gsl_${'9'.repeat(32)}`,
      group_id: firstId,
      source_id: `src_${'a'.repeat(32)}`,
      created_at: '2026-08-18T00:00:00Z',
    }];
    useGroupStore.getState().ingest([seeded, group(secondId)]);
    useGroupStore.setState({
      sourceReferenceCounts: { [firstId]: { [seeded.source_links[0]!.source_id]: 2 } },
    });
    const beforeGroups = structuredClone(useGroupStore.getState().groups);
    const beforeCounts = structuredClone(useGroupStore.getState().sourceReferenceCounts);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true, group_id: secondId, project_id: secondId, removed: true,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true, group_id: secondId, project_id: secondId,
        removed: true, reference_count: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true, group_id: firstId, project_id: firstId,
        removed: true, reference_count: 1.5,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(useGroupStore.getState().unbindCourse(firstId, 'course-a'))
      .rejects.toThrow('addressed group_id');
    await expect(useGroupStore.getState().unlinkSource(firstId, seeded.source_links[0]!.source_id))
      .rejects.toThrow('addressed group_id');
    await expect(useGroupStore.getState().unlinkSource(firstId, seeded.source_links[0]!.source_id))
      .rejects.toThrow();
    expect(useGroupStore.getState().groups).toEqual(beforeGroups);
    expect(useGroupStore.getState().sourceReferenceCounts).toEqual(beforeCounts);
  });

  it('does not resurrect a Group when rename or load resolves after deletion', async () => {
    const renamedId = `grp_${'e'.repeat(32)}` as typeof firstId;
    const loadedId = `grp_${'f'.repeat(32)}` as typeof firstId;
    const deletedRename = structuredClone(group(firstId));
    deletedRename.group_id = renamedId;
    deletedRename.name = 'Deleted rename';
    deletedRename.roster = deletedRename.roster.map((agent) => ({ ...agent, group_id: renamedId }));
    useGroupStore.getState().ingest([deletedRename]);

    let resolveRename!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { resolveRename = resolve; })));
    const rename = useGroupStore.getState().renameGroup(renamedId, 'Late name');
    await vi.waitFor(() => expect(resolveRename).toBeTypeOf('function'));
    fenceDeletedGroup(renamedId);
    useGroupStore.setState((state) => {
      const groups = { ...state.groups }; delete groups[renamedId];
      return { groups, order: state.order.filter((id) => id !== renamedId) };
    });
    resolveRename(new Response(JSON.stringify({
      group: { ...deletedRename, name: 'Late name', updated_at: '2026-08-18T00:00:01Z' },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await expect(rename).rejects.toThrow(/deleted/i);
    expect(useGroupStore.getState().groups[renamedId]).toBeUndefined();

    const deletedLoad = structuredClone(group(firstId));
    deletedLoad.group_id = loadedId;
    deletedLoad.name = 'Deleted load';
    deletedLoad.roster = deletedLoad.roster.map((agent) => ({ ...agent, group_id: loadedId }));
    let resolveLoad!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { resolveLoad = resolve; })));
    const load = useGroupStore.getState().load();
    await vi.waitFor(() => expect(resolveLoad).toBeTypeOf('function'));
    fenceDeletedGroup(loadedId);
    resolveLoad(new Response(JSON.stringify({ groups: [deletedLoad] }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    await load;
    expect(useGroupStore.getState().groups[loadedId]).toBeUndefined();
    expect(JSON.stringify(useGroupStore.getState())).not.toContain(loadedId);
  });

  it('teardown clears an in-flight duplicate idempotency cache', async () => {
    const deletedId = `grp_${'b'.repeat(32)}` as typeof firstId;
    const seeded = structuredClone(group(firstId));
    seeded.group_id = deletedId;
    seeded.roster = seeded.roster.map((agent) => ({ ...agent, group_id: deletedId }));
    useGroupStore.getState().ingest([seeded]);
    useSettingsStore.setState((state) => ({
      groupApprovalTimeouts: { ...state.groupApprovalTimeouts, [deletedId]: 30 },
      agentModels: { ...state.agentModels, [agentBindingKey(deletedId, 'expert')]: {
        provider: 'openai', model: 'test', apiKey: '', hasApiKey: false, clearApiKey: false,
        baseUrl: '', sealed: true,
      } },
    }));
    useRecordsStore.setState({ open: true, items: [{ seq: 1, type: 'message', agent_id: 'agent',
      content: deletedId, ts: null, has_files: false, has_images: false,
      has_videos: false, has_links: false }], handoffs: [] });
    let resolveDuplicate!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => (
      String(input).includes('/groups/duplicate')
        ? new Promise<Response>((resolve) => { resolveDuplicate = resolve; })
        : Promise.resolve(new Response(JSON.stringify({ ok: true }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }))
    )));
    const duplicate = useGroupStore.getState().duplicateGroup(deletedId, 'D:\\StudyCrew\\copy');
    await vi.waitFor(() => expect(resolveDuplicate).toBeTypeOf('function'));
    expect(pendingGroupRequestCacheCount(deletedId)).toBe(1);
    teardownGroup(deletedId);
    expect(pendingGroupRequestCacheCount(deletedId)).toBe(0);
    expect(useSettingsStore.getState().groupApprovalTimeouts[deletedId]).toBeUndefined();
    expect(useSettingsStore.getState().agentModels[agentBindingKey(deletedId, 'expert')]).toBeUndefined();
    expect(useRecordsStore.getState().items).toEqual([]);
    resolveDuplicate(new Response(JSON.stringify({ group: group(secondId) }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    await expect(duplicate).rejects.toThrow(/deleted/i);
    expect(JSON.stringify(useGroupStore.getState())).not.toContain(deletedId);
  });

  it('fences canonical and legacy identities before the teardown import can resolve', async () => {
    const deletedId = `grp_${'c'.repeat(32)}` as typeof firstId;
    const legacyId = 'legacy-import-boundary';
    const seeded = structuredClone(group(firstId));
    seeded.group_id = deletedId;
    seeded.legacy_project_id = legacyId;
    seeded.roster = seeded.roster.map((agent) => ({ ...agent, group_id: deletedId }));
    useGroupStore.getState().ingest([seeded]);

    let resolveHistory!: (response: Response) => void;
    let resolveLoader!: (module: typeof import('./groupTeardown')) => void;
    setGroupTeardownLoaderForTests(() => new Promise((resolve) => { resolveLoader = resolve; }));
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/history?')) {
        return new Promise<Response>((resolve) => { resolveHistory = resolve; });
      }
      if (url.includes('/groups/delete')) {
        return Promise.resolve(new Response(JSON.stringify({ receipt: {
          schema_version: 1, receipt_id: `gdr_${'d'.repeat(32)}`, group_id: deletedId,
          deleted_at: '2026-08-19T00:00:00Z', removed_agent_count: 2,
          removed_course_binding_count: 0, removed_source_link_count: 0,
          deleted_source_ids: [], retained_shared_source_ids: [],
        } }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    }));

    const history = useRecordsStore.getState().load(legacyId);
    await vi.waitFor(() => expect(resolveHistory).toBeTypeOf('function'));
    const deleting = useGroupStore.getState().confirmedDelete(deletedId, 'token');
    await vi.waitFor(() => expect(resolveLoader).toBeTypeOf('function'));
    expect(isGroupDeletionFenced(deletedId)).toBe(true);
    expect(isGroupDeletionFenced(legacyId)).toBe(true);
    resolveHistory(new Response(JSON.stringify({ items: [{
      seq: 1, type: 'message', agent_id: 'agent', content: 'LATE-LEGACY-BODY',
    }], total: 1 }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await history;
    expect(JSON.stringify(useRecordsStore.getState())).not.toContain('LATE-LEGACY-BODY');
    resolveLoader({ teardownGroup });
    await deleting;
    expect(pendingGroupTeardownCount()).toBe(0);
  });

  it('retains and retries cleanup when the teardown import fails', async () => {
    const deletedId = `grp_${'d'.repeat(32)}` as typeof firstId;
    const legacyId = 'legacy-import-retry';
    const seeded = structuredClone(group(firstId));
    seeded.group_id = deletedId;
    seeded.legacy_project_id = legacyId;
    seeded.roster = seeded.roster.map((agent) => ({ ...agent, group_id: deletedId }));
    useGroupStore.getState().ingest([seeded]);
    setGroupTeardownLoaderForTests(() => Promise.reject(new Error('import unavailable')));
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ receipt: {
      schema_version: 1, receipt_id: `gdr_${'e'.repeat(32)}`, group_id: deletedId,
      deleted_at: '2026-08-19T00:00:00Z', removed_agent_count: 2,
      removed_course_binding_count: 0, removed_source_link_count: 0,
      deleted_source_ids: [], retained_shared_source_ids: [],
    } }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    await useGroupStore.getState().confirmedDelete(deletedId, 'token');
    expect(pendingGroupTeardownCount()).toBe(1);
    expect(isGroupDeletionFenced(deletedId)).toBe(true);
    expect(isGroupDeletionFenced(legacyId)).toBe(true);
    setGroupTeardownLoaderForTests(async () => ({ teardownGroup }));
    await retryPendingGroupTeardowns();
    expect(pendingGroupTeardownCount()).toBe(0);
  });
});

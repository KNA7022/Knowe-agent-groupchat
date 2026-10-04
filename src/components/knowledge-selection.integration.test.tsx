// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { useKnowledgeStore } from '../store/knowledge';
import KnowledgeScopedView from './KnowledgeScopedView';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('does not issue invalid aggregate legacy queries or treat linked local sources as an explicit narrow filter', async () => {
  const groupId = `grp_${'38'.repeat(16)}`;
  const sourceId = `src_${'48'.repeat(16)}`;
  const group: Group = { group_id: groupId, schema_version: 1, name: '数学', template: 'custom', lifecycle: 'active',
    roster: [], course_bindings: [], source_links: [{ link_id: `gsl_${'58'.repeat(16)}`, group_id: groupId, source_id: sourceId, created_at: '2026-10-04T00:00:00Z' }],
    budget: null, model_overrides: {}, tool_overrides: {}, created_at: '2026-10-04T00:00:00Z', updated_at: '2026-10-04T00:00:00Z', archived_at: null,
    legacy_project_id: 'legacy-math' };
  useGroupStore.setState({ groups: { [groupId]: group }, order: [groupId] });
  useKnowledgeStore.getState().setScope(undefined);
  const calls: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input); calls.push(url);
    return { ok: true, json: async () => ({ ok: true, assets: [], projects: [], sources: [] }) } as Response;
  }));
  render(<KnowledgeScopedView />);
  expect(calls).toEqual([]);
  fireEvent.change(screen.getByLabelText('知识群组范围'), { target: { value: groupId } });
  await waitFor(() => expect(calls.some((url) => url.pathname === '/api/knowledge/assets')).toBe(true));
  const legacy = calls.find((url) => url.pathname === '/api/knowledge/assets')!;
  expect(legacy.searchParams.getAll('group_id')).toEqual([groupId]);
  expect(legacy.searchParams.get('legacy_project_id')).toBe('legacy-math');
  expect(legacy.searchParams.getAll('source_id')).toEqual([]);
});

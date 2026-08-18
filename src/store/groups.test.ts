import { beforeEach, describe, expect, it } from 'vitest';

import type { Group } from '../contract/group';
import { useGroupStore } from './groups';

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
  beforeEach(() => useGroupStore.setState({ groups: {}, order: [], loading: false, error: null }));

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
});

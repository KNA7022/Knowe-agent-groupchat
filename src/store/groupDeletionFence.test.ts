import { describe, expect, it, vi } from 'vitest';

describe('Group deletion tombstone hydration', () => {
  it('fails closed for canonical and legacy identities after a renderer restart', async () => {
    const groupId = `grp_${'9'.repeat(32)}`;
    const legacyId = 'legacy-restart-tombstone';
    localStorage.setItem('studycrew.deleted-group-fence.v1', JSON.stringify([groupId, legacyId]));
    vi.resetModules();
    const fence = await import('./groupDeletionFence');
    expect(fence.isGroupDeletionFenced(groupId)).toBe(true);
    expect(fence.isGroupDeletionFenced(legacyId)).toBe(true);
    expect(fence.groupCommitAllowed(groupId, 0)).toBe(false);
    expect(fence.groupCommitAllowed(legacyId, 0)).toBe(false);
  });

  it('filters tombstoned model and approval settings during same-version hydration', async () => {
    const groupId = `grp_${'a'.repeat(32)}`;
    const legacyId = 'legacy-restart-settings';
    localStorage.setItem('studycrew.deleted-group-fence.v1', JSON.stringify([groupId, legacyId]));
    localStorage.setItem('knowe-settings-v1', JSON.stringify({
      version: 3,
      state: {
        agentModels: {
          [`${groupId}::agent`]: { provider: 'openai', model: 'deleted', sealed: true },
          [`${legacyId}::agent`]: { provider: 'openai', model: 'deleted', sealed: true },
          'live::agent': { provider: 'openai', model: 'live', sealed: true },
        },
        groupApprovalTimeouts: { [groupId]: 11, [legacyId]: 12, live: 13 },
      },
    }));
    vi.resetModules();
    const { useSettingsStore } = await import('./settings');
    await Promise.resolve();
    expect(Object.keys(useSettingsStore.getState().agentModels)).toEqual(['live::agent']);
    expect(useSettingsStore.getState().groupApprovalTimeouts).toEqual({ live: 13 });
    expect(localStorage.getItem('knowe-settings-v1')).not.toContain('deleted');
  });
});

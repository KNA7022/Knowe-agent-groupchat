import type { GroupId } from '../contract/group';
import { clearGroupRequestCaches, useGroupStore } from './groups';
import { fenceDeletedGroup } from './groupDeletionFence';
import { teardownGroupProjections } from './groupTeardownCore';

/** Purge every renderer projection only after the backend confirms durable deletion. */
export function teardownGroup(groupId: GroupId, legacyProjectId?: string | null): void {
  // Fence both public identities before touching any store.  A response resolving during teardown
  // must observe the new generation and is never allowed to repopulate cleared state.
  fenceDeletedGroup(groupId, legacyProjectId);
  clearGroupRequestCaches(groupId);
  useGroupStore.setState((state) => {
    const groups = { ...state.groups }; delete groups[groupId];
    const runtimeSummaries = { ...state.runtimeSummaries }; delete runtimeSummaries[groupId];
    const sourceReferenceCounts = { ...state.sourceReferenceCounts }; delete sourceReferenceCounts[groupId];
    return { groups, runtimeSummaries, sourceReferenceCounts,
      order: state.order.filter((id) => id !== groupId) };
  });
  teardownGroupProjections(groupId, legacyProjectId);
}

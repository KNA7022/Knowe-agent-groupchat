import type { Group, GroupId, GroupRuntimeSummary } from '../contract/group';

interface GroupRuntimeBridge {
  readGroup: (groupId: GroupId) => Group | undefined;
  mirrorSummary: (groupId: GroupId, summary: GroupRuntimeSummary | undefined) => void;
  clearSummaries: () => void;
}

const emptyBridge: GroupRuntimeBridge = {
  readGroup: () => undefined,
  mirrorSummary: () => undefined,
  clearSummaries: () => undefined,
};

let bridge = emptyBridge;

/** Break the TaskRuntime → Group store module cycle while retaining the read/mirror projection. */
export function registerGroupRuntimeBridge(next: GroupRuntimeBridge): void {
  bridge = next;
}

export function readRuntimeGroup(groupId: GroupId): Group | undefined {
  return bridge.readGroup(groupId);
}

export function mirrorRuntimeGroupSummary(
  groupId: GroupId, summary: GroupRuntimeSummary | undefined,
): void {
  bridge.mirrorSummary(groupId, summary);
}

export function clearRuntimeGroupSummaries(): void {
  bridge.clearSummaries();
}

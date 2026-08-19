import type { GroupId } from '../contract/group';

const STORAGE_KEY = 'studycrew.deleted-group-fence.v1';

function load(): Set<string> {
  try {
    const raw = JSON.parse(globalThis.localStorage?.getItem(STORAGE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw.filter((item): item is string => typeof item === 'string') : []);
  } catch { return new Set(); }
}

const tombstones = load();
const generations = new Map<string, number>();
let revision = 0;

function persist(): void {
  try { globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify([...tombstones])); } catch { /* best effort */ }
}

export function groupDeletionGeneration(groupId: GroupId): number {
  return generations.get(groupId) || 0;
}

/** Monotonic renderer-wide deletion epoch for responses containing several Group identities. */
export function groupDeletionFenceRevision(): number {
  return revision;
}

export function isGroupDeletionFenced(groupId: string): boolean {
  return tombstones.has(groupId);
}

export function groupCommitAllowed(groupId: GroupId, generation: number): boolean {
  return !tombstones.has(groupId) && groupDeletionGeneration(groupId) === generation;
}

export function fenceDeletedGroup(groupId: GroupId, legacyProjectId?: string | null): void {
  let changed = false;
  for (const identity of [groupId, legacyProjectId].filter(
    (value): value is string => typeof value === 'string' && value.length > 0,
  )) {
    if (tombstones.has(identity)) continue;
    tombstones.add(identity);
    generations.set(identity, groupDeletionGeneration(identity) + 1);
    changed = true;
  }
  if (!changed) return;
  revision += 1;
  persist();
}

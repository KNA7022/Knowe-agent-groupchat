import { create } from 'zustand';

import {
  CourseUnbindResponseSchema,
  GroupDeletionReceiptSchema,
  GroupDeletePreviewSchema,
  GroupCourseBindingSchema,
  GroupRuntimeSummarySchema,
  GroupSchema,
  GroupSourceLinkSchema,
  SourceUnlinkResponseSchema,
  type Group,
  type GroupAgent,
  type GroupCourseBinding,
  type GroupDeletePreview,
  type GroupDeletionReceipt,
  type GroupId,
  type GroupRuntimeSummary,
  type GroupSourceLink,
  type GroupTemplate,
  withGroupTransportAlias,
} from '../contract/group';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import {
  fenceDeletedGroup, groupCommitAllowed, groupDeletionGeneration, isGroupDeletionFenced,
} from './groupDeletionFence';

export interface CreateGroupInput {
  name: string;
  template: GroupTemplate;
  project_dir: string;
  optional_roles?: Array<'knowledge' | 'planning' | 'coach'>;
  experts?: Array<{
    name: string;
    specialty: string;
    responsibilities?: string[];
    allowed_tools?: string[];
    model_override?: string;
    budget?: number;
  }>;
  budget?: number;
  model_overrides?: Record<string, string>;
  tool_overrides?: Record<string, string[]>;
}

interface GroupStoreState {
  groups: Record<string, Group>;
  order: GroupId[];
  runtimeSummaries: Record<string, GroupRuntimeSummary>;
  sourceReferenceCounts: Record<string, Record<string, number>>;
  loading: boolean;
  error: string | null;
  ingest: (groups: Group[]) => void;
  load: () => Promise<void>;
  createGroup: (input: CreateGroupInput) => Promise<Group>;
  renameGroup: (groupId: GroupId, name: string) => Promise<Group>;
  archiveGroup: (groupId: GroupId) => Promise<Group>;
  restoreGroup: (groupId: GroupId) => Promise<Group>;
  duplicateGroup: (groupId: GroupId, projectDir: string, name?: string) => Promise<Group>;
  bindCourse: (groupId: GroupId, courseId: string) => Promise<GroupCourseBinding>;
  unbindCourse: (groupId: GroupId, courseId: string) => Promise<boolean>;
  updateRoster: (groupId: GroupId, roster: GroupAgent[]) => Promise<Group>;
  linkSource: (groupId: GroupId, sourceId?: string) => Promise<GroupSourceLink>;
  unlinkSource: (groupId: GroupId, sourceId: string) => Promise<boolean>;
  loadRuntimeSummary: (groupId: GroupId) => Promise<GroupRuntimeSummary>;
  ingestRuntimeProjection: (
    groupId: GroupId,
    projection: Pick<GroupRuntimeSummary,
      'active_primary_run_id' | 'primary_task_status' | 'queued_task_count'
      | 'active_side_task_count' | 'pending_approval_count' | 'budget_usage' | 'indexing_state'>
      & Partial<Pick<GroupRuntimeSummary, 'active_agent_count'>>,
    updatedAt?: string,
  ) => void;
  previewDelete: (groupId: GroupId) => Promise<GroupDeletePreview>;
  confirmedDelete: (
    groupId: GroupId, confirmationToken: string,
  ) => Promise<GroupDeletionReceipt>;
}

function parseGroupResponse(raw: unknown): Group {
  if (!raw || typeof raw !== 'object') throw new Error('Invalid Group response');
  return GroupSchema.parse((raw as { group?: unknown }).group);
}

async function groupJson(path: string, body?: object): Promise<unknown> {
  const response = await runtimeFetch(`${runtimeHttpBase()}${path}`, body
    ? {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
    : { cache: 'no-store' });
  const payload: unknown = await response.json();
  if (!response.ok) {
    const message = payload && typeof payload === 'object' && 'error' in payload
      ? String((payload as { error?: unknown }).error || 'Group request failed')
      : `Group request failed (${response.status})`;
    throw new Error(message);
  }
  return payload;
}

function transportIdentity(group: Group): { group_id: GroupId; project_id?: string } {
  return withGroupTransportAlias(group.group_id, group.legacy_project_id);
}

function assertAddressedGroup(groupId: GroupId, actualGroupId: GroupId): void {
  if (actualGroupId !== groupId) {
    throw new Error('Group response crosses the addressed group_id boundary');
  }
}

const duplicateRequestIds = new Map<string, string>();

type GroupTeardownModule = typeof import('./groupTeardown');
type GroupTeardownLoader = () => Promise<GroupTeardownModule>;

const defaultGroupTeardownLoader: GroupTeardownLoader = () => import('./groupTeardown');
let groupTeardownLoader: GroupTeardownLoader = defaultGroupTeardownLoader;
const pendingGroupTeardowns = new Map<GroupId, string | null>();

/** Test seam for pausing/failing the dynamic import boundary; production always uses default. */
export function setGroupTeardownLoaderForTests(loader?: GroupTeardownLoader): void {
  groupTeardownLoader = loader ?? defaultGroupTeardownLoader;
}

export function pendingGroupTeardownCount(): number {
  return pendingGroupTeardowns.size;
}

export async function retryPendingGroupTeardowns(): Promise<void> {
  if (!pendingGroupTeardowns.size) return;
  const module = await groupTeardownLoader();
  for (const [groupId, legacyProjectId] of [...pendingGroupTeardowns]) {
    module.teardownGroup(groupId, legacyProjectId);
    pendingGroupTeardowns.delete(groupId);
  }
}

export function clearGroupRequestCaches(groupId: GroupId): void {
  for (const key of duplicateRequestIds.keys()) {
    if (key.startsWith(`${groupId}\u0000`)) duplicateRequestIds.delete(key);
  }
}

export function pendingGroupRequestCacheCount(groupId: GroupId): number {
  return [...duplicateRequestIds.keys()].filter((key) => key.startsWith(`${groupId}\u0000`)).length;
}

function startGroupRequest(groupId: GroupId): number {
  if (isGroupDeletionFenced(groupId)) throw new Error('Deleted Group cannot be mutated');
  return groupDeletionGeneration(groupId);
}

function assertGroupRequestCurrent(groupId: GroupId, generation: number): void {
  if (!groupCommitAllowed(groupId, generation)) {
    throw new Error('Group was deleted while request was in flight');
  }
}

function duplicateRequestId(groupId: GroupId, projectDir: string, name?: string): string {
  const key = `${groupId}\u0000${projectDir}\u0000${name || ''}`;
  const existing = duplicateRequestIds.get(key);
  if (existing) return existing;
  const value = `gdup_${globalThis.crypto?.randomUUID?.().replace(/-/g, '') || Date.now().toString(36)}`;
  duplicateRequestIds.set(key, value);
  return value;
}

export const useGroupStore = create<GroupStoreState>((set, get) => ({
  groups: {},
  order: [],
  runtimeSummaries: {},
  sourceReferenceCounts: {},
  loading: false,
  error: null,

  ingest(groups): void {
    // Each Group schema validates that roster/bindings/links carry the same group_id,
    // preventing an API projection from leaking one group's state into another.
    const parsed = groups.map((group) => GroupSchema.parse(group))
      .filter((group) => !isGroupDeletionFenced(group.group_id));
    set({
      groups: Object.fromEntries(parsed.map((group) => [group.group_id, group])),
      order: parsed.map((group) => group.group_id),
      error: null,
    });
  },

  async load(): Promise<void> {
    await retryPendingGroupTeardowns().catch(() => undefined);
    set({ loading: true, error: null });
    try {
      const raw = await groupJson('/groups');
      const rows = raw && typeof raw === 'object' && Array.isArray((raw as { groups?: unknown }).groups)
        ? (raw as { groups: unknown[] }).groups.map((row) => GroupSchema.parse(row))
        : [];
      get().ingest(rows);
    } catch (error) {
      set({ error: error instanceof Error ? error.message : 'Group request failed' });
      throw error;
    } finally {
      set({ loading: false });
    }
  },

  async createGroup(input): Promise<Group> {
    const group = parseGroupResponse(await groupJson('/groups/create', input));
    if (isGroupDeletionFenced(group.group_id)) throw new Error('Created Group identity is deletion-fenced');
    set((state) => isGroupDeletionFenced(group.group_id) ? state : ({
      groups: { ...state.groups, [group.group_id]: group },
      order: state.order.includes(group.group_id) ? state.order : [group.group_id, ...state.order],
    }));
    return group;
  },

  async renameGroup(groupId, name): Promise<Group> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/rename', {
      ...transportIdentity(current), name,
    }));
    assertAddressedGroup(groupId, group.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation)
      ? { groups: { ...state.groups, [group.group_id]: group } } : state);
    return group;
  },

  async archiveGroup(groupId): Promise<Group> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/archive', transportIdentity(current)));
    assertAddressedGroup(groupId, group.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation)
      ? { groups: { ...state.groups, [group.group_id]: group } } : state);
    return group;
  },

  async restoreGroup(groupId): Promise<Group> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/restore', transportIdentity(current)));
    assertAddressedGroup(groupId, group.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation)
      ? { groups: { ...state.groups, [group.group_id]: group } } : state);
    return group;
  },

  async duplicateGroup(groupId, projectDir, name): Promise<Group> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const requestId = duplicateRequestId(groupId, projectDir, name);
    const group = parseGroupResponse(await groupJson('/groups/duplicate', {
      ...transportIdentity(current),
      project_dir: projectDir,
      request_id: requestId,
      ...(name ? { name } : {}),
    }));
    assertGroupRequestCurrent(groupId, generation);
    if (isGroupDeletionFenced(group.group_id)) throw new Error('Duplicated Group identity is deletion-fenced');
    set((state) => groupCommitAllowed(groupId, generation) ? ({
      groups: { ...state.groups, [group.group_id]: group },
      order: [group.group_id, ...state.order],
    }) : state);
    duplicateRequestIds.delete(`${groupId}\u0000${projectDir}\u0000${name || ''}`);
    return group;
  },

  async bindCourse(groupId, courseId): Promise<GroupCourseBinding> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/course-bind', {
      ...transportIdentity(current), course_id: courseId,
    });
    const binding = GroupCourseBindingSchema.parse((raw as { binding?: unknown }).binding);
    assertAddressedGroup(groupId, binding.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation) ? ({ groups: {
      ...state.groups,
      [groupId]: {
        ...state.groups[groupId]!,
        course_bindings: [
          ...state.groups[groupId]!.course_bindings.filter((item) => item.course_id !== courseId),
          binding,
        ],
      },
    } }) : state);
    return binding;
  },

  async unbindCourse(groupId, courseId): Promise<boolean> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = CourseUnbindResponseSchema.parse(await groupJson('/groups/course-unbind', {
      ...transportIdentity(current), course_id: courseId,
    }));
    assertAddressedGroup(groupId, raw.group_id);
    assertGroupRequestCurrent(groupId, generation);
    const removed = raw.removed;
    if (removed) set((state) => groupCommitAllowed(groupId, generation) ? ({ groups: {
      ...state.groups,
      [groupId]: {
        ...state.groups[groupId]!,
        course_bindings: state.groups[groupId]!.course_bindings.filter(
          (item) => item.course_id !== courseId,
        ),
      },
    } }) : state);
    return removed;
  },

  async updateRoster(groupId, roster): Promise<Group> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    if (roster.some((agent) => agent.group_id !== groupId)) {
      throw new Error('Roster update crosses the group isolation boundary');
    }
    const group = parseGroupResponse(await groupJson('/groups/roster-update', {
      ...transportIdentity(current), roster,
    }));
    assertAddressedGroup(groupId, group.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation)
      ? { groups: { ...state.groups, [groupId]: group } } : state);
    return group;
  },

  async linkSource(groupId, sourceId): Promise<GroupSourceLink> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/source-link', {
      ...transportIdentity(current), ...(sourceId ? { source_id: sourceId } : {}),
    }) as { link?: unknown; reference_count?: unknown };
    const link = GroupSourceLinkSchema.parse(raw.link);
    assertAddressedGroup(groupId, link.group_id);
    assertGroupRequestCurrent(groupId, generation);
    const referenceCount = typeof raw.reference_count === 'number' ? raw.reference_count : 1;
    set((state) => groupCommitAllowed(groupId, generation) ? ({
      groups: {
        ...state.groups,
        [groupId]: {
          ...state.groups[groupId]!,
          source_links: [
            ...state.groups[groupId]!.source_links.filter((item) => item.source_id !== link.source_id),
            link,
          ],
        },
      },
      sourceReferenceCounts: {
        ...state.sourceReferenceCounts,
        [groupId]: { ...state.sourceReferenceCounts[groupId], [link.source_id]: referenceCount },
      },
    }) : state);
    return link;
  },

  async unlinkSource(groupId, sourceId): Promise<boolean> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = SourceUnlinkResponseSchema.parse(await groupJson('/groups/source-unlink', {
      ...transportIdentity(current), source_id: sourceId,
    }));
    assertAddressedGroup(groupId, raw.group_id);
    assertGroupRequestCurrent(groupId, generation);
    const removed = raw.removed;
    if (removed) set((state) => {
      if (!groupCommitAllowed(groupId, generation)) return state;
      const counts = { ...state.sourceReferenceCounts[groupId] };
      const referenceCount = raw.reference_count;
      if (referenceCount > 0) counts[sourceId] = referenceCount;
      else delete counts[sourceId];
      return {
        groups: {
          ...state.groups,
          [groupId]: {
            ...state.groups[groupId]!,
            source_links: state.groups[groupId]!.source_links.filter(
              (item) => item.source_id !== sourceId,
            ),
          },
        },
        sourceReferenceCounts: { ...state.sourceReferenceCounts, [groupId]: counts },
      };
    });
    return removed;
  },

  async loadRuntimeSummary(groupId): Promise<GroupRuntimeSummary> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/runtime-summary', transportIdentity(current));
    const summary = GroupRuntimeSummarySchema.parse((raw as { summary?: unknown }).summary);
    assertAddressedGroup(groupId, summary.group_id);
    assertGroupRequestCurrent(groupId, generation);
    set((state) => groupCommitAllowed(groupId, generation) ? ({
      runtimeSummaries: { ...state.runtimeSummaries, [groupId]: summary },
    }) : state);
    return summary;
  },

  ingestRuntimeProjection(groupId, projection, updatedAt): void {
    if (isGroupDeletionFenced(groupId)) return;
    const group = get().groups[groupId];
    if (!group) return;
    const previous = get().runtimeSummaries[groupId];
    const summary = GroupRuntimeSummarySchema.parse({
      group_id: groupId,
      lifecycle: group.lifecycle,
      active_agent_count: projection.active_agent_count ?? previous?.active_agent_count ?? 0,
      course_binding_count: group.course_bindings.length,
      source_count: group.source_links.length,
      shared_source_count: previous?.shared_source_count ?? 0,
      budget: group.budget,
      ...projection,
      updated_at: updatedAt || previous?.updated_at || group.updated_at,
    });
    set((state) => isGroupDeletionFenced(groupId) ? state : ({
      runtimeSummaries: { ...state.runtimeSummaries, [groupId]: summary },
    }));
  },

  async previewDelete(groupId): Promise<GroupDeletePreview> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete-preview', transportIdentity(current));
    const preview = GroupDeletePreviewSchema.parse((raw as { preview?: unknown }).preview);
    assertAddressedGroup(groupId, preview.group_id);
    assertGroupRequestCurrent(groupId, generation);
    return preview;
  },

  async confirmedDelete(groupId, confirmationToken): Promise<GroupDeletionReceipt> {
    const generation = startGroupRequest(groupId);
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete', {
      ...transportIdentity(current), confirmation_token: confirmationToken,
    });
    const receipt = GroupDeletionReceiptSchema.parse((raw as { receipt?: unknown }).receipt);
    assertAddressedGroup(groupId, receipt.group_id);
    assertGroupRequestCurrent(groupId, generation);
    const legacyProjectId = current.legacy_project_id;
    // The backend receipt is the irreversible boundary. Fence both identities synchronously
    // before the dynamic import can yield or fail, then retain a retryable cleanup obligation.
    fenceDeletedGroup(groupId, legacyProjectId);
    pendingGroupTeardowns.set(groupId, legacyProjectId);
    set((state) => {
      const groups = { ...state.groups };
      delete groups[groupId];
      const runtimeSummaries = { ...state.runtimeSummaries };
      delete runtimeSummaries[groupId];
      const sourceReferenceCounts = { ...state.sourceReferenceCounts };
      delete sourceReferenceCounts[groupId];
      return { groups, runtimeSummaries, sourceReferenceCounts,
        order: state.order.filter((id) => id !== groupId) };
    });
    await retryPendingGroupTeardowns().catch(() => undefined);
    return receipt;
  },
}));

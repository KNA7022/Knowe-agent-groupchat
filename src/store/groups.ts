import { create } from 'zustand';

import {
  GroupDeletionReceiptSchema,
  GroupDeletePreviewSchema,
  GroupCourseBindingSchema,
  GroupRuntimeSummarySchema,
  GroupSchema,
  GroupSourceLinkSchema,
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
    const parsed = groups.map((group) => GroupSchema.parse(group));
    set({
      groups: Object.fromEntries(parsed.map((group) => [group.group_id, group])),
      order: parsed.map((group) => group.group_id),
      error: null,
    });
  },

  async load(): Promise<void> {
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
    set((state) => ({
      groups: { ...state.groups, [group.group_id]: group },
      order: state.order.includes(group.group_id) ? state.order : [group.group_id, ...state.order],
    }));
    return group;
  },

  async renameGroup(groupId, name): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/rename', {
      ...transportIdentity(current), name,
    }));
    assertAddressedGroup(groupId, group.group_id);
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async archiveGroup(groupId): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/archive', transportIdentity(current)));
    assertAddressedGroup(groupId, group.group_id);
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async restoreGroup(groupId): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/restore', transportIdentity(current)));
    assertAddressedGroup(groupId, group.group_id);
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async duplicateGroup(groupId, projectDir, name): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const requestId = duplicateRequestId(groupId, projectDir, name);
    const group = parseGroupResponse(await groupJson('/groups/duplicate', {
      ...transportIdentity(current),
      project_dir: projectDir,
      request_id: requestId,
      ...(name ? { name } : {}),
    }));
    set((state) => ({
      groups: { ...state.groups, [group.group_id]: group },
      order: [group.group_id, ...state.order],
    }));
    duplicateRequestIds.delete(`${groupId}\u0000${projectDir}\u0000${name || ''}`);
    return group;
  },

  async bindCourse(groupId, courseId): Promise<GroupCourseBinding> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/course-bind', {
      ...transportIdentity(current), course_id: courseId,
    });
    const binding = GroupCourseBindingSchema.parse((raw as { binding?: unknown }).binding);
    assertAddressedGroup(groupId, binding.group_id);
    set((state) => ({ groups: {
      ...state.groups,
      [groupId]: {
        ...state.groups[groupId]!,
        course_bindings: [
          ...state.groups[groupId]!.course_bindings.filter((item) => item.course_id !== courseId),
          binding,
        ],
      },
    } }));
    return binding;
  },

  async unbindCourse(groupId, courseId): Promise<boolean> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/course-unbind', {
      ...transportIdentity(current), course_id: courseId,
    }) as { removed?: unknown };
    const removed = raw.removed === true;
    if (removed) set((state) => ({ groups: {
      ...state.groups,
      [groupId]: {
        ...state.groups[groupId]!,
        course_bindings: state.groups[groupId]!.course_bindings.filter(
          (item) => item.course_id !== courseId,
        ),
      },
    } }));
    return removed;
  },

  async updateRoster(groupId, roster): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    if (roster.some((agent) => agent.group_id !== groupId)) {
      throw new Error('Roster update crosses the group isolation boundary');
    }
    const group = parseGroupResponse(await groupJson('/groups/roster-update', {
      ...transportIdentity(current), roster,
    }));
    assertAddressedGroup(groupId, group.group_id);
    set((state) => ({ groups: { ...state.groups, [groupId]: group } }));
    return group;
  },

  async linkSource(groupId, sourceId): Promise<GroupSourceLink> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/source-link', {
      ...transportIdentity(current), ...(sourceId ? { source_id: sourceId } : {}),
    }) as { link?: unknown; reference_count?: unknown };
    const link = GroupSourceLinkSchema.parse(raw.link);
    assertAddressedGroup(groupId, link.group_id);
    const referenceCount = typeof raw.reference_count === 'number' ? raw.reference_count : 1;
    set((state) => ({
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
    }));
    return link;
  },

  async unlinkSource(groupId, sourceId): Promise<boolean> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/source-unlink', {
      ...transportIdentity(current), source_id: sourceId,
    }) as { removed?: unknown; reference_count?: unknown };
    const removed = raw.removed === true;
    if (removed) set((state) => {
      const counts = { ...state.sourceReferenceCounts[groupId] };
      const referenceCount = typeof raw.reference_count === 'number' ? raw.reference_count : 0;
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
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/runtime-summary', transportIdentity(current));
    const summary = GroupRuntimeSummarySchema.parse((raw as { summary?: unknown }).summary);
    assertAddressedGroup(groupId, summary.group_id);
    set((state) => ({
      runtimeSummaries: { ...state.runtimeSummaries, [groupId]: summary },
    }));
    return summary;
  },

  async previewDelete(groupId): Promise<GroupDeletePreview> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete-preview', transportIdentity(current));
    const preview = GroupDeletePreviewSchema.parse((raw as { preview?: unknown }).preview);
    assertAddressedGroup(groupId, preview.group_id);
    return preview;
  },

  async confirmedDelete(groupId, confirmationToken): Promise<GroupDeletionReceipt> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete', {
      ...transportIdentity(current), confirmation_token: confirmationToken,
    });
    const receipt = GroupDeletionReceiptSchema.parse((raw as { receipt?: unknown }).receipt);
    assertAddressedGroup(groupId, receipt.group_id);
    set((state) => {
      const groups = { ...state.groups };
      delete groups[groupId];
      return { groups, order: state.order.filter((id) => id !== groupId) };
    });
    return receipt;
  },
}));

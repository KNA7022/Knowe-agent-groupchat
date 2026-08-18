import { create } from 'zustand';

import {
  GroupDeletionReceiptSchema,
  GroupDeletePreviewSchema,
  GroupSchema,
  type Group,
  type GroupDeletePreview,
  type GroupDeletionReceipt,
  type GroupId,
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
  loading: boolean;
  error: string | null;
  ingest: (groups: Group[]) => void;
  load: () => Promise<void>;
  createGroup: (input: CreateGroupInput) => Promise<Group>;
  renameGroup: (groupId: GroupId, name: string) => Promise<Group>;
  archiveGroup: (groupId: GroupId) => Promise<Group>;
  restoreGroup: (groupId: GroupId) => Promise<Group>;
  duplicateGroup: (groupId: GroupId, name?: string) => Promise<Group>;
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

export const useGroupStore = create<GroupStoreState>((set, get) => ({
  groups: {},
  order: [],
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
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async archiveGroup(groupId): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/archive', transportIdentity(current)));
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async restoreGroup(groupId): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/restore', transportIdentity(current)));
    set((state) => ({ groups: { ...state.groups, [group.group_id]: group } }));
    return group;
  },

  async duplicateGroup(groupId, name): Promise<Group> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const group = parseGroupResponse(await groupJson('/groups/duplicate', {
      ...transportIdentity(current), ...(name ? { name } : {}),
    }));
    set((state) => ({
      groups: { ...state.groups, [group.group_id]: group },
      order: [group.group_id, ...state.order],
    }));
    return group;
  },

  async previewDelete(groupId): Promise<GroupDeletePreview> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete-preview', transportIdentity(current));
    return GroupDeletePreviewSchema.parse((raw as { preview?: unknown }).preview);
  },

  async confirmedDelete(groupId, confirmationToken): Promise<GroupDeletionReceipt> {
    const current = get().groups[groupId];
    if (!current) throw new Error(`Unknown group: ${groupId}`);
    const raw = await groupJson('/groups/delete', {
      ...transportIdentity(current), confirmation_token: confirmationToken,
    });
    const receipt = GroupDeletionReceiptSchema.parse((raw as { receipt?: unknown }).receipt);
    set((state) => {
      const groups = { ...state.groups };
      delete groups[groupId];
      return { groups, order: state.order.filter((id) => id !== groupId) };
    });
    return receipt;
  },
}));

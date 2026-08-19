import type { GroupId } from '../contract/group';
import { clearGroupRequestCaches, useGroupStore } from './groups';
import { useKnowledgeStore } from './knowledge';
import { resolve as resolveDirectoryRecovery } from './directoryRecovery';
import { useRecordsStore } from './records';
import { useSettingsStore } from './settings';
import { PLATFORM_PROJECT_ID } from './avatar';
import { parseDmId } from './chat';
import { useSideTaskReadStore } from './sideTaskRead';
import { teardownLegacyProjectCaches, useKnoweStore } from './store';
import { useTaskRuntimeStore } from './taskRuntime';

/** Purge every renderer projection only after the backend confirms durable deletion. */
export function teardownGroup(groupId: GroupId, legacyProjectId?: string | null): void {
  clearGroupRequestCaches(groupId);
  useGroupStore.setState((state) => {
    const groups = { ...state.groups }; delete groups[groupId];
    const runtimeSummaries = { ...state.runtimeSummaries }; delete runtimeSummaries[groupId];
    const sourceReferenceCounts = { ...state.sourceReferenceCounts }; delete sourceReferenceCounts[groupId];
    return { groups, runtimeSummaries, sourceReferenceCounts,
      order: state.order.filter((id) => id !== groupId) };
  });
  const runIds = useTaskRuntimeStore.getState().teardownGroupProjection(groupId);
  useSideTaskReadStore.getState().clearRuns(runIds);
  useKnowledgeStore.getState().teardownGroupScope(groupId, legacyProjectId);
  useRecordsStore.setState({
    open: false, items: [], handoffs: [], total: 0, page: 1, loading: false,
    handoffsLoading: false, jumpSeq: null, error: null,
  });
  useSettingsStore.setState((state) => {
    const groupApprovalTimeouts = { ...state.groupApprovalTimeouts };
    delete groupApprovalTimeouts[groupId];
    if (legacyProjectId) delete groupApprovalTimeouts[legacyProjectId];
    const agentModels = Object.fromEntries(Object.entries(state.agentModels)
      .filter(([key]) => !key.startsWith(`${groupId}::`)
        && (!legacyProjectId || !key.startsWith(`${legacyProjectId}::`))));
    return { groupApprovalTimeouts, agentModels, modelApplyState: 'stale' };
  });
  void useSettingsStore.getState().pushToBackend().catch(() => undefined);
  if (!legacyProjectId) return;

  resolveDirectoryRecovery(legacyProjectId);
  teardownLegacyProjectCaches(legacyProjectId);

  useKnoweStore.setState((state) => {
    const belongs = (conversationId: string): boolean => conversationId === legacyProjectId
      || parseDmId(conversationId)?.projectId === legacyProjectId;
    const convs = Object.fromEntries(Object.entries(state.convs)
      .filter(([conversationId]) => !belongs(conversationId)));
    const projectOrder = state.projectOrder.filter((id) => id !== legacyProjectId);
    const pinnedProjects = { ...state.pinnedProjects };
    const mutedProjects = { ...state.mutedProjects };
    const foldedProjects = { ...state.foldedProjects };
    const attachments = { ...state._attachments };
    delete pinnedProjects[legacyProjectId];
    delete mutedProjects[legacyProjectId];
    delete foldedProjects[legacyProjectId];
    for (const id of Object.keys(attachments)) if (belongs(id)) delete attachments[id];
    const activeProjectId = state.activeProjectId && belongs(state.activeProjectId)
      ? (convs[PLATFORM_PROJECT_ID] ? PLATFORM_PROJECT_ID : projectOrder[0] || null)
      : state.activeProjectId;
    const quote = state.quote && belongs(state.quote.projectId) ? null : state.quote;
    return {
      convs, projectOrder, pinnedProjects, mutedProjects, foldedProjects,
      _attachments: attachments, activeProjectId, quote,
      ...(activeProjectId !== state.activeProjectId
        ? { selecting: false, selectedKeys: {} }
        : {}),
    };
  });
}

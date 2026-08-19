import type { GroupId } from '../contract/group';
import { useKnowledgeStore } from './knowledge';
import { PLATFORM_PROJECT_ID } from './avatar';
import { parseDmId } from './chat';
import { useSideTaskReadStore } from './sideTaskRead';
import { useKnoweStore } from './store';
import { useTaskRuntimeStore } from './taskRuntime';

/** Purge every renderer projection only after the backend confirms durable deletion. */
export function teardownGroup(groupId: GroupId, legacyProjectId?: string | null): void {
  const runIds = useTaskRuntimeStore.getState().teardownGroupProjection(groupId);
  useSideTaskReadStore.getState().clearRuns(runIds);
  useKnowledgeStore.getState().teardownGroupScope(groupId, legacyProjectId);
  if (!legacyProjectId) return;

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

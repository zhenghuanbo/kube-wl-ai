/** Workspace endpoints (`app/api/v1/workspaces.py`). */
import { api } from '@/api/client'
import type { ApiMessage, ListQuery, Page } from '@/types/api'
import type {
  WorkspaceCreatePayload,
  WorkspaceDetail,
  WorkspaceMember,
  WorkspaceMemberAssignPayload,
  WorkspaceQuotaSync,
  WorkspaceResourceGrant,
  WorkspaceStatus,
  WorkspaceSummary,
  WorkspaceUpdatePayload,
} from '@/types/resource'

export interface WorkspaceListQuery extends ListQuery {
  groupId?: number
  status?: WorkspaceStatus
  isActive?: boolean
}

export function listWorkspaces(
  query: WorkspaceListQuery = {},
): Promise<Page<WorkspaceSummary>> {
  return api.get<Page<WorkspaceSummary>>('/workspaces', { params: query })
}

/**
 * The caller's own workspace, or `null` when they have not been assigned one.
 *
 * A dedicated endpoint rather than a filtered list: one user belongs to exactly
 * one workspace, so this is the only correct answer and it costs one query.
 */
export function getMyWorkspace(): Promise<WorkspaceSummary | null> {
  return api.get<WorkspaceSummary | null>('/workspaces/mine')
}

export function getWorkspace(workspaceId: number): Promise<WorkspaceDetail> {
  return api.get<WorkspaceDetail>(`/workspaces/${workspaceId}`)
}

export function createWorkspace(payload: WorkspaceCreatePayload): Promise<WorkspaceDetail> {
  return api.post<WorkspaceDetail>('/workspaces', payload)
}

export function updateWorkspace(
  workspaceId: number,
  payload: WorkspaceUpdatePayload,
): Promise<WorkspaceDetail> {
  return api.patch<WorkspaceDetail>(`/workspaces/${workspaceId}`, payload)
}

export function deleteWorkspace(workspaceId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/workspaces/${workspaceId}`)
}

/** Re-apply the namespace, quota, limit range and network policy. Idempotent. */
export function reconcileWorkspace(workspaceId: number): Promise<WorkspaceQuotaSync> {
  return api.post<WorkspaceQuotaSync>(`/workspaces/${workspaceId}/reconcile`)
}

// --- members -----------------------------------------------------------------
export function listWorkspaceMembers(workspaceId: number): Promise<WorkspaceMember[]> {
  return api.get<WorkspaceMember[]>(`/workspaces/${workspaceId}/members`)
}

export function assignWorkspaceMember(
  workspaceId: number,
  payload: WorkspaceMemberAssignPayload,
): Promise<WorkspaceMember> {
  return api.post<WorkspaceMember>(`/workspaces/${workspaceId}/members`, payload)
}

export function removeWorkspaceMember(
  workspaceId: number,
  userId: number,
): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/workspaces/${workspaceId}/members/${userId}`)
}

// --- resource grants ---------------------------------------------------------
export function listWorkspaceGrants(
  workspaceId: number,
): Promise<WorkspaceResourceGrant[]> {
  return api.get<WorkspaceResourceGrant[]>(`/workspaces/${workspaceId}/resources`)
}

export function grantResourceGroup(
  workspaceId: number,
  resourceGroupId: number,
): Promise<WorkspaceResourceGrant> {
  return api.post<WorkspaceResourceGrant>(`/workspaces/${workspaceId}/resources`, {
    resourceGroupId,
  })
}

export function revokeResourceGroup(
  workspaceId: number,
  resourceGroupId: number,
): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/workspaces/${workspaceId}/resources/${resourceGroupId}`)
}

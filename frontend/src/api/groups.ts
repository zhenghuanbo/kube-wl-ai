/** User-group management endpoints (`app/api/v1/groups.py`). */
import { api } from '@/api/client'
import type { GroupKind, GroupRole } from '@/types/auth'
import type { ApiMessage, ListQuery, Page } from '@/types/api'

export interface GroupSummary {
  id: number
  name: string
  directoryName: string | null
  displayName: string
  description: string | null
  kind: GroupKind
  isActive: boolean
  ownerUserId: number | null
  memberCount: number
  createdAt: string
  updatedAt: string
}

export interface GroupListQuery extends ListQuery {
  kind?: GroupKind
  isActive?: boolean
}

export interface GroupCreatePayload {
  name: string
  directoryName?: string | null
  displayName?: string
  description?: string | null
  kind?: GroupKind
  ownerUserId?: number | null
}

export interface GroupUpdatePayload {
  directoryName?: string | null
  displayName?: string
  description?: string | null
  kind?: GroupKind
  ownerUserId?: number | null
  isActive?: boolean
}

export interface GroupMember {
  userId: number
  username: string
  fullName: string
  displayName: string
  email: string | null
  isActive: boolean
  roleInGroup: GroupRole
  isPrimary: boolean
  isDirectoryManaged: boolean
  joinedAt: string
}

export interface GroupSyncResult {
  directoryGroups: string[]
  matchedGroups: string[]
  unmatchedGroups: string[]
  membershipsAdded: number
  membershipsRemoved: number
  membershipsUpdated: number
  errors: string[]
  dryRun: boolean
}

export function listGroups(query: GroupListQuery = {}): Promise<Page<GroupSummary>> {
  return api.get<Page<GroupSummary>>('/groups', { params: query })
}

export function getGroup(groupId: number): Promise<GroupSummary> {
  return api.get<GroupSummary>(`/groups/${groupId}`)
}

export function createGroup(payload: GroupCreatePayload): Promise<GroupSummary> {
  return api.post<GroupSummary>('/groups', payload)
}

export function updateGroup(groupId: number, payload: GroupUpdatePayload): Promise<GroupSummary> {
  return api.patch<GroupSummary>(`/groups/${groupId}`, payload)
}

export function deleteGroup(groupId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/groups/${groupId}`)
}

export function listMembers(
  groupId: number,
  query: ListQuery = {},
): Promise<Page<GroupMember>> {
  return api.get<Page<GroupMember>>(`/groups/${groupId}/members`, { params: query })
}

export function addMember(
  groupId: number,
  payload: { userId: number; roleInGroup?: GroupRole; isPrimary?: boolean },
): Promise<GroupMember> {
  return api.post<GroupMember>(`/groups/${groupId}/members`, payload)
}

export function updateMember(
  groupId: number,
  userId: number,
  payload: { roleInGroup?: GroupRole; isPrimary?: boolean },
): Promise<GroupMember> {
  return api.patch<GroupMember>(`/groups/${groupId}/members/${userId}`, payload)
}

export function removeMember(groupId: number, userId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/groups/${groupId}/members/${userId}`)
}

export function syncGroups(dryRun = false): Promise<GroupSyncResult> {
  return api.post<GroupSyncResult>('/groups/sync', { dryRun })
}

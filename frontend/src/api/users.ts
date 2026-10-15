/** User management endpoints (`app/api/v1/users.py`). */
import { api } from '@/api/client'
import type { GroupMembership, Role, RoleAssignment, UserSource } from '@/types/auth'
import type { ApiMessage, ListQuery, Page } from '@/types/api'

export interface UserSummary {
  id: number
  username: string
  fullName: string
  displayName: string
  email: string | null
  source: UserSource
  isActive: boolean
  isSuperuser: boolean
  expiresAt: string | null
  lastLoginAt: string | null
  roles: Role[]
  primaryGroupId: number | null
  primaryGroupName: string | null
  /**
   * The quota template this user follows, or `null` for the default. `null` is
   * "follow the default", not "no quota": promoting a new default moves everyone
   * who never asked for a template of their own.
   */
  quotaProfileId: number | null
}

export interface UserDetail extends UserSummary {
  externalId: string | null
  createdAt: string
  updatedAt: string
  groups: GroupMembership[]
  roleAssignments: RoleAssignment[]
}

export interface UserListQuery extends ListQuery {
  role?: string
  groupId?: number
  isActive?: boolean
  source?: UserSource
}

export interface UserCreatePayload {
  username: string
  password?: string
  email?: string | null
  fullName?: string
  isActive?: boolean
  expiresAt?: string | null
  roleCodes?: string[]
  groupIds?: number[]
}

export interface UserUpdatePayload {
  email?: string | null
  fullName?: string
  isActive?: boolean
  expiresAt?: string | null
  password?: string
}

export interface UserRoleGrantPayload {
  roleCode: string
  scope?: 'global' | 'group'
  groupId?: number | null
}

export function listUsers(query: UserListQuery = {}): Promise<Page<UserSummary>> {
  return api.get<Page<UserSummary>>('/users', { params: query })
}

export function getUser(userId: number): Promise<UserDetail> {
  return api.get<UserDetail>(`/users/${userId}`)
}

export function createUser(payload: UserCreatePayload): Promise<UserDetail> {
  return api.post<UserDetail>('/users', payload)
}

export function updateUser(userId: number, payload: UserUpdatePayload): Promise<UserDetail> {
  return api.patch<UserDetail>(`/users/${userId}`, payload)
}

export function deleteUser(userId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/users/${userId}`)
}

export function setUserActive(userId: number, isActive: boolean): Promise<UserDetail> {
  return api.patch<UserDetail>(`/users/${userId}/status`, { isActive })
}

export function resetPassword(userId: number, password: string): Promise<ApiMessage> {
  return api.post<ApiMessage>(`/users/${userId}/password`, { password })
}

export function setUserGroups(
  userId: number,
  payload: { groupIds: number[]; primaryGroupId?: number | null },
): Promise<UserDetail> {
  return api.put<UserDetail>(`/users/${userId}/groups`, payload)
}

export function grantRole(userId: number, payload: UserRoleGrantPayload): Promise<UserDetail> {
  return api.post<UserDetail>(`/users/${userId}/roles`, payload)
}

export function revokeRole(
  userId: number,
  roleCode: string,
  groupId?: number | null,
): Promise<UserDetail> {
  return api.delete<UserDetail>(`/users/${userId}/roles/${roleCode}`, {
    params: groupId != null ? { groupId } : {},
  })
}

export function getUserPermissions(userId: number): Promise<string[]> {
  return api.get<string[]>(`/users/${userId}/permissions`)
}

export function updateOwnProfile(payload: {
  fullName?: string
  email?: string | null
}): Promise<UserDetail> {
  return api.patch<UserDetail>('/users/me', payload)
}

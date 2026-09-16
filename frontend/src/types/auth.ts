/** Role and identity types shared across the frontend. */

/** The four business roles (see plan §5.3). */
export type Role = 'observer' | 'user' | 'group_admin' | 'global_admin'

export const ALL_ROLES: readonly Role[] = ['observer', 'user', 'group_admin', 'global_admin']

export const ROLE_LABELS: Record<Role, string> = {
  observer: '观察用户',
  user: '普通用户',
  group_admin: '组管理员',
  global_admin: '全局管理员',
}

/** Data scope resolved by the backend for the current user (plan §5.2). */
export type DataScope = 'self' | 'group' | 'all'

export interface WorkspaceRef {
  id: number
  name: string
  groupId: number
  groupName: string
}

/** Where an identity came from — decides whether a password can be set here. */
export type UserSource = 'local' | 'ipa' | 'oidc'

export const SOURCE_LABELS: Record<UserSource, string> = {
  local: '本地账户',
  ipa: 'FreeIPA',
  oidc: 'OIDC',
}

export type GroupKind = 'lab' | 'public'
export type GroupRole = 'member' | 'group_admin'

export const GROUP_ROLE_LABELS: Record<GroupRole, string> = {
  member: '成员',
  group_admin: '组管理员',
}

/** A role assignment. Global ones have no `groupId`. */
export interface RoleAssignment {
  code: string
  name: string
  scope: 'global' | 'group'
  groupId: number | null
}

/** One of the caller's group memberships. */
export interface GroupMembership {
  groupId: number
  name: string
  displayName: string
  roleInGroup: GroupRole
  isPrimary: boolean
  isDirectoryManaged: boolean
}

/** Mirrors `CurrentUserResponse` in `app/schemas/auth.py`. */
export interface CurrentUser {
  id: number
  username: string
  email: string | null
  fullName: string
  displayName: string
  source: UserSource
  isActive: boolean
  isSuperuser: boolean
  expiresAt: string | null
  lastLoginAt: string | null
  roles: Role[]
  groups: GroupMembership[]
  roleAssignments: RoleAssignment[]
  /** A user belongs to exactly one workspace (plan D7). Populated from Phase 2. */
  workspace: WorkspaceRef | null
}

/** Mirrors `PermissionsResponse` in `app/schemas/auth.py`. */
export interface PermissionsPayload {
  permissions: string[]
  roles: Role[]
  scope: {
    kind: DataScope
    allAccess: boolean
    groupIds: number[]
  }
  isGlobalAdmin: boolean
}

/** Permission codes are `resource:action` strings (plan §5.3). */
export type PermissionCode = string

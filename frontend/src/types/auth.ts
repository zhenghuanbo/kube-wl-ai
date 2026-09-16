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

export interface CurrentUser {
  id: number
  username: string
  fullName: string
  email: string
  roles: Role[]
  isSuperuser: boolean
  /** A user belongs to exactly one workspace (plan D7). */
  workspace: WorkspaceRef | null
}

/** Permission codes are `resource:action` strings (plan §5.3). */
export type PermissionCode = string

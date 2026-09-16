/** RBAC catalogue endpoints (`app/api/v1/rbac.py`). Read-only by design. */
import { api } from '@/api/client'

export interface RoleDetail {
  code: string
  name: string
  description: string | null
  isBuiltin: boolean
  permissions: string[]
}

export interface PermissionDetail {
  code: string
  category: string
  description: string | null
}

export interface PermissionCatalogue {
  categories: Record<string, PermissionDetail[]>
  total: number
}

export function listRoles(): Promise<RoleDetail[]> {
  return api.get<RoleDetail[]>('/roles')
}

export function getRole(roleCode: string): Promise<RoleDetail> {
  return api.get<RoleDetail>(`/roles/${roleCode}`)
}

export function listPermissions(): Promise<PermissionCatalogue> {
  return api.get<PermissionCatalogue>('/permissions')
}

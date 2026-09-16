/**
 * Permission helpers for components.
 *
 * Mirrors the backend policy surface (plan §5.5): the same permission codes and
 * role checks used server-side. Frontend checks are UX only — the backend
 * remains the security boundary.
 */
import { computed, type ComputedRef } from 'vue'

import { useAuthStore } from '@/stores/auth'
import type { DataScope, GroupMembership, PermissionCode, Role } from '@/types/auth'

export interface UsePermissions {
  roles: ComputedRef<Role[]>
  /** Strict: a code the caller does not hold returns false. */
  can: (code: PermissionCode) => boolean
  canAny: (codes: readonly PermissionCode[]) => boolean
  hasRole: (role: Role) => boolean
  hasAnyRole: (candidates: readonly Role[]) => boolean
  isObserver: ComputedRef<boolean>
  isAdmin: ComputedRef<boolean>
  isGroupAdmin: ComputedRef<boolean>
  isGlobalAdmin: ComputedRef<boolean>
  scope: ComputedRef<DataScope>
  /** Groups the caller administers — drives the group picker in admin forms. */
  adminGroupIds: ComputedRef<number[]>
  /** Workspaces the caller may target, straight from the server's data scope. */
  scopeWorkspaceIds: ComputedRef<number[]>
  primaryGroup: ComputedRef<GroupMembership | null>
}

export function usePermissions(): UsePermissions {
  const auth = useAuthStore()

  return {
    roles: computed(() => auth.roles),
    can: (code: PermissionCode) => auth.can(code),
    canAny: (codes: readonly PermissionCode[]) => auth.canAny(codes),
    hasRole: (role: Role) => auth.hasRole(role),
    hasAnyRole: (candidates: readonly Role[]) => auth.hasAnyRole(candidates),
    isObserver: computed(() => auth.hasRole('observer')),
    isAdmin: computed(() => auth.hasAnyRole(['group_admin', 'global_admin'])),
    isGroupAdmin: computed(() => auth.hasRole('group_admin')),
    isGlobalAdmin: computed(() => auth.globalAdmin || auth.hasRole('global_admin')),
    scope: computed(() => (auth.globalAdmin ? 'all' : auth.scopeKind)),
    adminGroupIds: computed(() => auth.scopeGroupIds),
    scopeWorkspaceIds: computed(() => auth.scopeWorkspaceIds),
    primaryGroup: computed(() => auth.primaryGroup),
  }
}

/**
 * Permission helpers for components.
 *
 * Mirrors the backend policy surface (plan §5.5): the same permission codes
 * and role checks used server-side. Frontend checks are UX only — the backend
 * remains the security boundary.
 */
import { computed, type ComputedRef } from 'vue'

import { useAuthStore } from '@/stores/auth'
import type { PermissionCode, Role } from '@/types/auth'

export interface UsePermissions {
  roles: ComputedRef<Role[]>
  can: (code: PermissionCode) => boolean
  hasRole: (role: Role) => boolean
  hasAnyRole: (candidates: readonly Role[]) => boolean
  isObserver: ComputedRef<boolean>
  isAdmin: ComputedRef<boolean>
  isGlobalAdmin: ComputedRef<boolean>
}

export function usePermissions(): UsePermissions {
  const auth = useAuthStore()

  return {
    roles: computed(() => auth.roles),
    can: (code: PermissionCode) => auth.can(code),
    hasRole: (role: Role) => auth.hasRole(role),
    hasAnyRole: (candidates: readonly Role[]) => auth.hasAnyRole(candidates),
    isObserver: computed(() => auth.hasRole('observer')),
    isAdmin: computed(() => auth.hasAnyRole(['group_admin', 'global_admin'])),
    isGlobalAdmin: computed(() => auth.hasRole('global_admin')),
  }
}

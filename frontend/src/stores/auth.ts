/**
 * Authentication / identity store.
 *
 * NOTE (Phase 0): this is a scaffold. The real implementation — `/auth/me`,
 * silent refresh via the httpOnly refresh cookie, and logout — lands in
 * Phase 1 together with the FastAPI auth endpoints. Until then the store
 * exposes a development placeholder identity so the role-driven UI can be
 * exercised. The placeholder is never used in production builds.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { CurrentUser, PermissionCode, Role } from '@/types/auth'

const DEV_PLACEHOLDER_USER: CurrentUser = {
  id: 0,
  username: 'dev',
  fullName: '开发占位用户',
  email: 'dev@kube-wl-ai.local',
  roles: ['global_admin'],
  isSuperuser: true,
  workspace: null,
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  /** Effective permission codes issued by `/auth/permissions` (Phase 1). */
  const permissions = ref<Set<PermissionCode>>(new Set())
  const loading = ref(false)

  const roles = computed<Role[]>(() => user.value?.roles ?? [])
  const isAuthenticated = computed(() => user.value !== null)

  function hasRole(role: Role): boolean {
    return roles.value.includes(role)
  }

  function hasAnyRole(candidates: readonly Role[]): boolean {
    return candidates.some((role) => roles.value.includes(role))
  }

  /**
   * Permission check.
   *
   * Phase 0 has no backend yet, so an empty permission set means "not
   * restricted" — Phase 1 switches this to strict evaluation against
   * `permissions`, which remains the single source of truth shared with the
   * backend (plan §5.6).
   */
  function can(code: PermissionCode): boolean {
    if (permissions.value.size === 0) return true
    return permissions.value.has(code)
  }

  function setUser(next: CurrentUser | null): void {
    user.value = next
  }

  function setPermissions(codes: Iterable<PermissionCode>): void {
    permissions.value = new Set(codes)
  }

  function clear(): void {
    user.value = null
    permissions.value = new Set()
  }

  /** Phase 0 only: install the development placeholder identity. */
  function useDevPlaceholder(): void {
    if (import.meta.env.DEV) {
      user.value = DEV_PLACEHOLDER_USER
    }
  }

  return {
    user,
    permissions,
    loading,
    roles,
    isAuthenticated,
    hasRole,
    hasAnyRole,
    can,
    setUser,
    setPermissions,
    clear,
    useDevPlaceholder,
  }
})

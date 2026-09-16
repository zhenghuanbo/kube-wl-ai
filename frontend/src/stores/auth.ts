/**
 * Authentication and authorisation state.
 *
 * The backend is the only enforcement point (plan §5.6); everything here exists
 * so the UI can avoid offering actions that would fail. `can()` is therefore a
 * UX helper, never a security control.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  fetchCurrentUser,
  fetchPermissions,
  login as loginRequest,
  logout as logoutRequest,
  refresh as refreshRequest,
} from '@/api/auth'
import { ApiError, setAccessToken } from '@/api/client'
import type { CurrentUser, DataScope, PermissionCode, Role } from '@/types/auth'

/**
 * `unknown` until bootstrap finishes, so the router can wait instead of
 * flashing the login screen at an already-authenticated user.
 */
export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const permissions = ref<Set<PermissionCode>>(new Set())
  const scopeKind = ref<DataScope>('self')
  const scopeGroupIds = ref<number[]>([])
  const scopeWorkspaceIds = ref<number[]>([])
  const globalAdmin = ref(false)
  const status = ref<AuthStatus>('unknown')
  const busy = ref(false)

  const roles = computed<Role[]>(() => user.value?.roles ?? [])
  const isAuthenticated = computed(() => status.value === 'authenticated' && user.value !== null)
  const isReady = computed(() => status.value !== 'unknown')
  const primaryGroup = computed(
    () => user.value?.groups.find((group) => group.isPrimary) ?? user.value?.groups[0] ?? null,
  )

  function hasRole(role: Role): boolean {
    return roles.value.includes(role)
  }

  function hasAnyRole(candidates: readonly Role[]): boolean {
    return candidates.some((role) => roles.value.includes(role))
  }

  /** Strict check against the permission set issued by `/auth/permissions`. */
  function can(code: PermissionCode): boolean {
    return permissions.value.has(code)
  }

  function canAny(codes: readonly PermissionCode[]): boolean {
    return codes.some((code) => permissions.value.has(code))
  }

  function applyIdentity(nextUser: CurrentUser): void {
    user.value = nextUser
    status.value = 'authenticated'
  }

  function applyPermissions(payload: {
    permissions: string[]
    roles: Role[]
    scope: { kind: DataScope; allAccess: boolean; groupIds: number[]; workspaceIds: number[] }
    isGlobalAdmin: boolean
  }): void {
    permissions.value = new Set(payload.permissions)
    scopeKind.value = payload.scope.kind
    scopeGroupIds.value = payload.scope.groupIds
    scopeWorkspaceIds.value = payload.scope.workspaceIds
    globalAdmin.value = payload.isGlobalAdmin
    if (user.value) {
      user.value.roles = payload.roles
    }
  }

  function clear(): void {
    user.value = null
    permissions.value = new Set()
    scopeKind.value = 'self'
    scopeGroupIds.value = []
    scopeWorkspaceIds.value = []
    globalAdmin.value = false
    status.value = 'anonymous'
    setAccessToken(null)
  }

  /** Re-read identity and permissions from the server. */
  async function loadIdentity(): Promise<void> {
    const [identity, permissionPayload] = await Promise.all([
      fetchCurrentUser(),
      fetchPermissions(),
    ])
    applyIdentity(identity)
    applyPermissions(permissionPayload)
  }

  async function loginWithPassword(username: string, password: string): Promise<void> {
    busy.value = true
    try {
      // The login response already carries the user; permissions still need a
      // separate call because they depend on role→permission expansion.
      const result = await loginRequest({ username, password })
      applyIdentity(result.user)
      applyPermissions(await fetchPermissions())
    } finally {
      busy.value = false
    }
  }

  /**
   * Restore a session on page load.
   *
   * The access token is memory-only, so a reload always starts empty; the
   * httpOnly refresh cookie is what makes the session survive. A failure here
   * is the normal "not logged in" case, not an error worth reporting.
   */
  async function bootstrap(): Promise<void> {
    if (status.value !== 'unknown') return
    try {
      await refreshRequest()
      await loadIdentity()
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      clear()
    }
  }

  async function logout(): Promise<void> {
    try {
      await logoutRequest()
    } finally {
      clear()
    }
  }

  return {
    user,
    permissions,
    scopeKind,
    scopeGroupIds,
    scopeWorkspaceIds,
    globalAdmin,
    status,
    busy,
    roles,
    isAuthenticated,
    isReady,
    primaryGroup,
    hasRole,
    hasAnyRole,
    can,
    canAny,
    loadIdentity,
    loginWithPassword,
    bootstrap,
    logout,
    clear,
  }
})

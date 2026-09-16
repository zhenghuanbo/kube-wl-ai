/** Authentication endpoints (`app/api/v1/auth.py`). */
import { api, setAccessToken } from '@/api/client'
import type { CurrentUser, PermissionsPayload } from '@/types/auth'
import type { ApiMessage } from '@/types/api'

export interface LoginPayload {
  username: string
  password: string
}

export interface TokenResponse {
  accessToken: string
  tokenType: string
  expiresIn: number
  expiresAt: string
  user: CurrentUser
}

export interface ProviderInfo {
  name: string
  description: string
}

export interface LoginOptions {
  providers: ProviderInfo[]
  ldapEnabled: boolean
  oidcEnabled: boolean
}

export async function login(payload: LoginPayload): Promise<TokenResponse> {
  const result = await api.post<TokenResponse>('/auth/login', payload, { skipErrorToast: true })
  setAccessToken(result.accessToken)
  return result
}

/**
 * Exchange the httpOnly refresh cookie for a new session.
 *
 * Called by the client's interceptor on a 401; a page load also uses it to
 * restore a session after a refresh, so the user is not bounced to the login
 * screen every time.
 */
export async function refresh(): Promise<TokenResponse> {
  const result = await api.post<TokenResponse>('/auth/refresh', {}, { skipErrorToast: true })
  setAccessToken(result.accessToken)
  return result
}

export async function logout(): Promise<ApiMessage> {
  const result = await api.post<ApiMessage>('/auth/logout', {}, { skipErrorToast: true })
  setAccessToken(null)
  return result
}

export async function fetchCurrentUser(): Promise<CurrentUser> {
  return api.get<CurrentUser>('/auth/me')
}

export async function fetchPermissions(): Promise<PermissionsPayload> {
  return api.get<PermissionsPayload>('/auth/permissions')
}

export async function fetchLoginOptions(): Promise<LoginOptions> {
  return api.get<LoginOptions>('/auth/login-options', { skipErrorToast: true })
}

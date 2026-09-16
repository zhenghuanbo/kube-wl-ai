/**
 * The single HTTP client for the whole application.
 *
 * Three responsibilities, all of which must exist in exactly one place:
 *
 * 1. **Token handling.** The access token lives in module memory — never in
 *    `localStorage`, where any injected script could read it. The refresh token
 *    is an httpOnly cookie the browser attaches automatically.
 * 2. **Silent refresh.** A 401 triggers one refresh attempt and one replay of
 *    the original request. Concurrent 401s share a single refresh call, so a
 *    dashboard firing eight requests does not rotate the token eight times
 *    (which would look like refresh-token theft to the backend and kill the
 *    session).
 * 3. **Uniform errors.** Every failure becomes an `ApiError` carrying the
 *    backend's stable `code`, so callers branch on codes instead of parsing
 *    messages.
 */
import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios'

import type { ApiErrorEnvelope } from '@/types/api'

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** Access token held in memory only. */
let accessToken: string | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly detail: Record<string, unknown>

  constructor(message: string, options: { code: string; status: number; detail?: Record<string, unknown> }) {
    super(message)
    this.name = 'ApiError'
    this.code = options.code
    this.status = options.status
    this.detail = options.detail ?? {}
  }

  /** Whether the caller simply lacks the permission. */
  get isPermissionDenied(): boolean {
    return this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  get isValidation(): boolean {
    return this.status === 400 || this.status === 409 || this.status === 422
  }
}

/** Extra flags understood by the interceptors. */
interface RequestConfig extends InternalAxiosRequestConfig {
  /** Set internally once a request has been replayed after a refresh. */
  _retried?: boolean
  /** Opt out of the global error notification (the caller renders it inline). */
  skipErrorToast?: boolean
}

export interface ExtendedRequestConfig extends AxiosRequestConfig {
  skipErrorToast?: boolean
}

// --- callbacks the app installs ---------------------------------------------
type SessionExpiredHandler = () => void
type ErrorHandler = (error: ApiError) => void

let onSessionExpired: SessionExpiredHandler = () => {}
let onError: ErrorHandler = () => {}

/** Called when the refresh token is gone or rejected: the session is over. */
export function setSessionExpiredHandler(handler: SessionExpiredHandler): void {
  onSessionExpired = handler
}

/** Called for every error that is not an expired session. */
export function setErrorHandler(handler: ErrorHandler): void {
  onError = handler
}

// --- refresh de-duplication --------------------------------------------------
let refreshInFlight: Promise<string> | null = null

/**
 * Refresh the access token, sharing one call across concurrent 401s.
 *
 * Uses a bare axios call rather than the shared instance: going through the
 * instance would route a failed refresh back into this same interceptor.
 */
async function refreshAccessToken(): Promise<string> {
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = axios
    .post<{ accessToken: string }>(
      `${BASE_URL}/auth/refresh`,
      {},
      { withCredentials: true, timeout: 15000 },
    )
    .then((response) => {
      const token = response.data.accessToken
      setAccessToken(token)
      return token
    })
    .finally(() => {
      refreshInFlight = null
    })

  return refreshInFlight
}

// --- error normalisation -----------------------------------------------------
function normalizeError(error: AxiosError<ApiErrorEnvelope>): ApiError {
  const status = error.response?.status ?? 0
  const body = error.response?.data

  if (body?.error?.code) {
    return new ApiError(body.error.message || '请求失败', {
      code: body.error.code,
      status,
      detail: body.error.detail,
    })
  }

  if (error.code === 'ECONNABORTED') {
    return new ApiError('请求超时，请稍后重试', { code: 'timeout', status })
  }
  if (status === 0) {
    return new ApiError('无法连接到服务器，请检查网络', { code: 'network_error', status })
  }
  return new ApiError(error.message || '请求失败', { code: 'unknown_error', status })
}

// --- instance ----------------------------------------------------------------
export const http: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
  // Required for the httpOnly refresh cookie to travel on /auth/refresh.
  withCredentials: true,
  headers: { Accept: 'application/json' },
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`
  }
  return config
})

/** Endpoints that must never trigger the silent-refresh dance. */
const AUTH_PATHS = /\/auth\/(login|refresh|logout|login-options)/

http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<ApiErrorEnvelope>) => {
    const config = error.config as RequestConfig | undefined
    const status = error.response?.status
    const url = config?.url ?? ''

    if (status === 401 && config && !config._retried && !AUTH_PATHS.test(url)) {
      config._retried = true
      try {
        const token = await refreshAccessToken()
        config.headers.Authorization = `Bearer ${token}`
        return await http.request(config)
      } catch {
        setAccessToken(null)
        onSessionExpired()
        throw normalizeError(error)
      }
    }

    const apiError = normalizeError(error)
    if (status === 401 && !AUTH_PATHS.test(url)) {
      setAccessToken(null)
      onSessionExpired()
    } else if (!config?.skipErrorToast) {
      onError(apiError)
    }
    throw apiError
  },
)

/** Thin typed wrappers so call sites stay free of axios generics. */
export const api = {
  get: <T>(url: string, config?: ExtendedRequestConfig) =>
    http.get<T>(url, config).then((response) => response.data),
  post: <T>(url: string, body?: unknown, config?: ExtendedRequestConfig) =>
    http.post<T>(url, body, config).then((response) => response.data),
  put: <T>(url: string, body?: unknown, config?: ExtendedRequestConfig) =>
    http.put<T>(url, body, config).then((response) => response.data),
  patch: <T>(url: string, body?: unknown, config?: ExtendedRequestConfig) =>
    http.patch<T>(url, body, config).then((response) => response.data),
  delete: <T>(url: string, config?: ExtendedRequestConfig) =>
    http.delete<T>(url, config).then((response) => response.data),
}

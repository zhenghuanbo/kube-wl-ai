/**
 * Personal access token types.
 *
 * Mirrors `backend/app/schemas/api_token.py`.
 *
 * `ApiTokenIssued` extends `ApiToken` with the secret, and the two are separate
 * types on purpose: the listing returns the first and never the second, so a
 * component that wants `token` has to say so, and the compiler will not let it
 * reach for a field that only ever exists in one response.
 */

export interface ApiToken {
  id: number
  name: string
  /** Leading characters, shown so two tokens can be told apart. */
  prefix: string
  /** Empty means the token tracks the owner's roles. */
  scopes: string[]
  expiresAt: string | null
  lastUsedAt: string | null
  createdAt: string
}

/** A freshly minted token: the one and only sight of the secret. */
export interface ApiTokenIssued extends ApiToken {
  token: string
}

export interface ApiTokenCreatePayload {
  name: string
  scopes?: string[]
  expiresInDays?: number | null
}

/** A selectable scope offered when narrowing a token. */
export interface ScopeOption {
  value: string
  title: string
}

/**
 * The scopes worth offering.
 *
 * A short, curated list rather than the full 72-code catalogue: this is a
 * picker for automation, and nearly every real token wants one of a handful of
 * shapes — read a workload, submit a task, export numbers. The backend accepts
 * any code the owner holds, so an unusual need is a scripted call away and does
 * not have to be visible to everybody here.
 */
export const SCOPE_OPTIONS: ScopeOption[] = [
  { value: 'instance:read', title: '查看实例' },
  { value: 'instance:create', title: '创建实例' },
  { value: 'task:read', title: '查看任务' },
  { value: 'task:create', title: '提交任务' },
  { value: 'task:logs', title: '读取任务日志' },
  { value: 'usage:read_self', title: '查看本人用量' },
  { value: 'usage:read_group', title: '查看本组用量' },
  { value: 'usage:export', title: '导出用量' },
  { value: 'storage:read', title: '查看存储' },
  { value: 'image:read', title: '查看镜像' },
  { value: 'api_token:create', title: '创建新的 API 令牌' },
]

const SCOPE_LABELS = new Map(SCOPE_OPTIONS.map((option) => [option.value, option.title]))

/** A human label for a scope code, falling back to the code itself. */
export function scopeLabel(code: string): string {
  return SCOPE_LABELS.get(code) ?? code
}

/** Whether a token has passed its expiry. */
export function isExpired(token: ApiToken): boolean {
  return token.expiresAt !== null && new Date(token.expiresAt).getTime() <= Date.now()
}

/**
 * A short description of a token's authority.
 *
 * The empty case is the one worth wording carefully: a token with no scopes is
 * not "restricted to nothing", it *tracks the owner's roles*, and a reader who
 * assumes otherwise would think it weaker than it is.
 */
export function scopeSummary(token: ApiToken): string {
  if (token.scopes.length === 0) return '跟随我的角色（权限变化时自动同步）'
  return token.scopes.map(scopeLabel).join('、')
}

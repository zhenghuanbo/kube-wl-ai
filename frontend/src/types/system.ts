/**
 * System information read model.
 *
 * Mirrors `backend/app/schemas/system.py`.
 *
 * The shape encodes a rule rather than merely describing it: **every field here
 * is either a boolean, a count, or a non-secret string that an operator could
 * read from a ConfigMap.** No credential, no connection string, no key material.
 * An "admin can see the config" page is the single easiest place to leak a secret
 * by accident, because the natural implementation is to serialise the settings
 * object and the natural review is to skim it.
 *
 * So the settings object is never serialised. Each value is copied across
 * deliberately, which means adding a field here is a decision somebody made.
 */

export interface SystemFeature {
  /** Stable identifier, used as the row key. */
  key: string
  title: string
  /** Whether the feature is configured and usable. */
  enabled: boolean
  /** A human summary of *how* it is configured, or why it is not. */
  detail: string
  /** Where to look when it is not working. */
  configKey: string
}

export type WarningSeverity = 'critical' | 'warning' | 'info'

/**
 * A concrete, checkable problem.
 *
 * Each one answers "what is wrong, what does it cause, what do I change" — three
 * questions, because a warning that only says "grafanaPublicUrl is empty" makes
 * the reader go and find out whether that matters.
 */
export interface SystemWarning {
  code: string
  severity: WarningSeverity
  title: string
  detail: string
  /** The setting or file that fixes it. */
  remedy: string
}

export interface SystemCounts {
  users: number
  activeUsers: number
  groups: number
  workspaces: number
  nodes: number
  gpus: number
  instances: number
  runningInstances: number
}

export interface SystemInfo {
  appName: string
  appVersion: string
  environment: string
  debug: boolean

  /** The auth chain, in the order it is tried. */
  authProviders: string[]
  features: SystemFeature[]
  warnings: SystemWarning[]

  accessTokenMinutes: number
  refreshTokenDays: number

  loginRateLimitEnabled: boolean
  loginRateLimitWindowSeconds: number
  loginRateLimitAttemptsPerIp: number
  loginRateLimitAttemptsPerUsername: number

  counts: SystemCounts

  /** What the SSH feature will actually do, and where users connect. */
  sshMode: string
  sshHost: string | null

  /** Role -> storage class name; `null` means "not configured". */
  storageClasses: Record<string, string | null>
}

export const SEVERITY_COLORS: Record<WarningSeverity, string> = {
  critical: 'error',
  warning: 'warning',
  info: 'info',
}

export const SEVERITY_LABELS: Record<WarningSeverity, string> = {
  critical: '严重',
  warning: '注意',
  info: '提示',
}

export function severityColor(severity: string): string {
  return SEVERITY_COLORS[severity as WarningSeverity] ?? 'info'
}

export function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity as WarningSeverity] ?? severity
}

/**
 * Order warnings worst-first.
 *
 * The page shows a list, and the first line is the one that gets read. Sorting
 * server-side would make the frontend's rendering depend on an ordering it
 * cannot see; sorting here keeps the intent visible.
 */
export function bySeverity(warnings: SystemWarning[]): SystemWarning[] {
  const rank: Record<WarningSeverity, number> = { critical: 0, warning: 1, info: 2 }
  return [...warnings].sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3))
}

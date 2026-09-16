/**
 * Usage-centre wire types.
 *
 * Mirrors `backend/app/schemas/usage.py`. Two things about this shape are worth
 * knowing before reading a component that consumes it.
 *
 * Every measure arrives **twice**: as an exact integer and as a float in display
 * units. The integers are the truth — `gpuSeconds`, `cpuMilliSeconds`,
 * `memoryMbSeconds`, `runtimeSeconds`. The floats (`gpuHours`, `cpuCoreHours`,
 * `memoryGbHours`) exist so a chart axis and a stat card can print a number
 * without every caller re-deriving the same division. Sum the integers; show the
 * floats.
 *
 * Nothing here is money. The centre reports consumption so a lab can see where
 * its share went; there are no prices and no balances, and `gpuHours` is a unit
 * of *use*, not of cost.
 */

/** Which kind of workload consumed the resources. Narrower than `WorkloadKind`. */
export type UsageKind = 'instance' | 'task'

/** Dimensions a breakdown can be grouped by. */
export type UsageDimension = 'user' | 'group' | 'workspace' | 'gpu_type' | 'workload_kind'

export interface UsageTotals {
  runtimeSeconds: number
  gpuSeconds: number
  cpuMilliSeconds: number
  memoryMbSeconds: number
  workloadCount: number
  gpuHours: number
  cpuCoreHours: number
  memoryGbHours: number
  userCount: number
  workspaceCount: number
}

export interface UsageDay {
  /** `YYYY-MM-DD`. */
  day: string
  totals: UsageTotals
}

export interface UsageSeries {
  dayFrom: string
  dayTo: string
  points: UsageDay[]
}

export interface UsageBreakdownRow {
  key: string
  label: string
  /**
   * `null` for dimensions whose key is not a row id — a GPU model and a workload
   * kind have nothing to drill into, and the API refuses to invent one.
   */
  entityId: number | null
  totals: UsageTotals
  /** GPU-seconds split by workload kind. Empty when the dimension *is* the kind. */
  kinds: Record<string, number>
}

export interface UsageBreakdown {
  by: string
  dayFrom: string
  dayTo: string
  rows: UsageBreakdownRow[]
  /** Whether the row list was cut off at the requested limit. */
  truncated: boolean
}

export interface UsageSummary {
  dayFrom: string
  dayTo: string
  totals: UsageTotals
  /**
   * The same window restricted to the caller. `null` when the caller's scope is
   * already just themselves — the two panels would be identical.
   */
  selfTotals: UsageTotals | null
  /**
   * Whether a rollup has ever run for this window. All-zero figures mean
   * something very different when nothing was ever computed, and the page must
   * say which.
   */
  covered: boolean
}

export interface UsageBreakdownDimension {
  key: string
  label: string
}

export interface UsageMeta {
  dimensions: UsageBreakdownDimension[]
  kinds: { value: UsageKind; label: string }[]
  maxRangeDays: number
  defaultRollupLookbackDays: number
}

export interface UsageRollupResult {
  dayFrom: string
  dayTo: string
  rowsWritten: number
  instancesScanned: number
  tasksScanned: number
  runtimeSeconds: number
  gpuSeconds: number
  outsideWindow: number
}

export interface UsageQuery {
  dayFrom?: string
  dayTo?: string
  userId?: number
  groupId?: number
  workspaceId?: number
  workloadKind?: UsageKind
  gpuType?: string
}

export const USAGE_KIND_META: Record<UsageKind, { label: string; icon: string }> = {
  instance: { label: '开发实例', icon: 'mdi-developer-board' },
  task: { label: '自定义任务', icon: 'mdi-script-text-play-outline' },
}

/**
 * The filter's options before `/usage/meta` answers, and after it fails.
 *
 * The API is authoritative — the page prefers whatever it returns — but the
 * filter must not be empty for the second it takes to arrive, nor permanently if
 * the request fails. A stale-but-correct label beats a blank control.
 */
export const USAGE_KINDS_FALLBACK: { value: UsageKind; label: string }[] = [
  { value: 'instance', label: '开发实例' },
  { value: 'task', label: '自定义任务' },
]

/**
 * Named ranges the filter offers instead of a date picker.
 *
 * A date picker invites a two-year range, which is the query that takes the
 * database down during a demo. These are also the ranges anybody actually asks
 * for, so the constraint costs nothing.
 */
export interface UsageRange {
  key: string
  label: string
  days: number
}

export const USAGE_RANGES: UsageRange[] = [
  { key: '7d', label: '近 7 天', days: 7 },
  { key: '30d', label: '近 30 天', days: 30 },
  { key: '90d', label: '近 90 天', days: 90 },
]

/** `YYYY-MM-DD` for a date `days - 1` before today, in UTC. */
export function rangeStart(days: number, today = new Date()): string {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()))
  end.setUTCDate(end.getUTCDate() - (days - 1))
  return end.toISOString().slice(0, 10)
}

/** `YYYY-MM-DD` for today, in UTC. Deliberately UTC: the rollup is. */
export function todayUtc(today = new Date()): string {
  return today.toISOString().slice(0, 10)
}

/**
 * Format a GPU-hour figure for display.
 *
 * Two decimals below 100, none above. A lab that used 4 200 GPU-hours does not
 * care about the third decimal, and printing it makes the number harder to read
 * at exactly the scale where it matters most.
 */
export function formatGpuHours(hours: number): string {
  if (hours >= 100) return hours.toFixed(0)
  if (hours >= 10) return hours.toFixed(1)
  return hours.toFixed(2)
}

/** A human duration from seconds, for the "reserved time" card. */
export function formatRuntime(seconds: number): string {
  if (seconds <= 0) return '0 分钟'
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${(seconds / 86400).toFixed(1)} 天`
}

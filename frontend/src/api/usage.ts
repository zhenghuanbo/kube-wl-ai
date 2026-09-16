/** Usage-centre endpoints (`app/api/v1/usage.py`). */
import { api } from '@/api/client'
import type {
  UsageBreakdown,
  UsageDimension,
  UsageMeta,
  UsageQuery,
  UsageRollupResult,
  UsageSeries,
  UsageSummary,
} from '@/types/usage'

/**
 * Query keys are camelCase throughout — the API declares them with
 * `Query(alias=...)`, and a snake_case key is **silently ignored** rather than
 * rejected, so a typo here reads as "no data" instead of an error.
 */
export function getUsageSummary(query: UsageQuery = {}): Promise<UsageSummary> {
  return api.get<UsageSummary>('/usage/summary', { params: query })
}

export function getUsageSeries(query: UsageQuery = {}): Promise<UsageSeries> {
  return api.get<UsageSeries>('/usage/series', { params: query })
}

export function getUsageBreakdown(
  by: UsageDimension,
  query: UsageQuery = {},
  limit = 50,
): Promise<UsageBreakdown> {
  return api.get<UsageBreakdown>('/usage/breakdown', { params: { ...query, by, limit } })
}

export function getUsageMeta(): Promise<UsageMeta> {
  return api.get<UsageMeta>('/usage/meta')
}

/** Recompute the rollup. Global-admin only; the window defaults server-side. */
export function rollupUsage(payload: { dayFrom?: string; dayTo?: string } = {}): Promise<
  UsageRollupResult
> {
  return api.post<UsageRollupResult>('/usage/rollup', payload)
}

/**
 * Download the window as CSV and hand it to the browser.
 *
 * The response is fetched as a blob rather than navigated to, because the
 * endpoint authenticates with an `Authorization` header — a plain `<a href>`
 * would arrive without it and get a 401. The object URL is revoked on the next
 * tick: leaving it alive leaks the whole file into the page's memory for as long
 * as the tab exists, which for a decade of usage data is not trivial.
 */
export async function downloadUsageCsv(query: UsageQuery = {}): Promise<void> {
  // The client's default timeout is 30s, which is right for a form submit and
  // too short for a year of usage across a whole faculty. Raised rather than
  // removed: the failure this prevents is a user waiting on nothing.
  const blob = await api.get<Blob>('/usage/export', {
    params: query,
    responseType: 'blob',
    timeout: 120000,
  })

  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `usage-${query.dayFrom ?? 'from'}-${query.dayTo ?? 'to'}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Revoked on the next macrotask, after the browser has started the download.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** Audit-trail endpoints (`app/api/v1/audit.py`). Read-only by construction. */
import { api } from '@/api/client'
import type { AuditEntry, AuditMeta, AuditQuery } from '@/types/audit'

/** The paginated envelope the list endpoints share. */
interface AuditPage {
  items: AuditEntry[]
  meta: { page: number; pageSize: number; total: number; pages: number }
}

export function listAuditLogs(query: AuditQuery = {}): Promise<AuditPage> {
  return api.get<AuditPage>('/audit-logs', { params: query })
}

/**
 * The values the filter controls should offer.
 *
 * Built from rows the caller may see, so an action nobody in their scope has
 * ever performed is not offered — which would otherwise disclose that the action
 * exists elsewhere.
 */
export function getAuditMeta(): Promise<AuditMeta> {
  return api.get<AuditMeta>('/audit-logs/meta')
}

/**
 * Download the current filter as CSV.
 *
 * Goes through the same scoped statement as the listing, so the file can never
 * contain a row the caller cannot see on screen. `blob` because a CSV is not
 * JSON; the long timeout because an export can cover a year.
 */
export async function downloadAuditCsv(query: AuditQuery = {}): Promise<void> {
  // `api.get` with `responseType: 'blob'`, like the usage export: one code path
  // for authenticated downloads means the refresh-on-401 interceptor applies
  // here too, which a bare axios call would bypass.
  const blob = await api.get<Blob>('/audit-logs/export', {
    params: query,
    responseType: 'blob',
    // The default 30s is right for a form submit and too short for a year of
    // trail. Raised rather than removed.
    timeout: 120000,
  })

  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `audit-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Revoked on the next macrotask, after the browser has started the download.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

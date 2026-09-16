/** Monitoring endpoints (`app/api/v1/monitor.py`). */
import { api } from '@/api/client'
import type { MonitorEmbed, MonitorOverview } from '@/types/monitor'

/**
 * Everything the monitoring page needs, in one request.
 *
 * The embed URLs inside it are already scoped and signed, so the page can render
 * the first panel without a second round trip.
 */
export function getMonitorOverview(): Promise<MonitorOverview> {
  return api.get<MonitorOverview>('/monitor/overview')
}

/**
 * Re-sign one dashboard's URL.
 *
 * A `POST` because it creates a credential: it must not be retried blindly from
 * a cache, and the method says so. Called when a token is within
 * `EMBED_REFRESH_MARGIN_SECONDS` of expiring.
 */
export function mintMonitorEmbed(key: string): Promise<MonitorEmbed> {
  return api.post<MonitorEmbed>(`/monitor/dashboards/${encodeURIComponent(key)}/embed`)
}

/**
 * Monitoring wire types.
 *
 * Mirrors `backend/app/schemas/monitor.py`. The shape reflects one decision: the
 * page is assembled from a *single* response, because four parallel requests can
 * be answered at four different moments and the page would then describe a state
 * that never existed.
 *
 * The most important field here is `scope`. A flat monitoring line means two
 * completely different things depending on whether the caller is filtered and
 * what they are filtered to, and a page that does not say which is a page that
 * misleads. `unrestricted` and `empty` are separate flags for exactly that
 * reason — an administrator and a user with nothing running both have zero
 * namespaces.
 */

/** One dashboard the caller may open. */
export interface MonitorDashboard {
  key: string
  uid: string
  title: string
  description: string
  icon: string
}

/** A freshly minted, scope-locked Grafana URL. */
export interface MonitorEmbed {
  key: string
  url: string
  /** Seconds until `url` stops working. */
  expiresIn: number
}

export interface MonitorScope {
  namespaces: string[]
  pods: string[]
  /** No filter at all — an administrator. */
  unrestricted: boolean
  /** Filtered, and the filter matches nothing. */
  empty: boolean
}

export interface MonitorNode {
  id: number
  name: string
  displayName: string
  status: string
  schedulable: boolean
  gpuCount: number
  cpuTotalMilli: number
  memoryTotalMb: number
}

export interface GpuModelSummary {
  model: string
  total: number
  allocated: number
}

export interface InstanceStatusCount {
  status: string
  count: number
}

export interface MonitorInventory {
  nodes: MonitorNode[]
  nodesReady: number
  gpuByModel: GpuModelSummary[]
  instances: InstanceStatusCount[]
}

/** A live container the caller may watch, with the names Grafana filters on. */
export interface WatchedContainer {
  id: number
  name: string
  displayName: string
  namespace: string
  podName: string | null
  ownerUsername: string
  status: string
  gpuType: string
  gpuCount: number
  nodeName: string | null
}

export interface MonitorOverview {
  /** Whether Grafana is configured at all. */
  enabled: boolean
  /** Why the panels are unavailable, when they are. */
  disabledReason: string | null
  scope: MonitorScope
  inventory: MonitorInventory
  containers: WatchedContainer[]
  dashboards: MonitorDashboard[]
  /** Signed panel URLs, keyed by dashboard key. Absent when Grafana is off. */
  embeds: Record<string, MonitorEmbed>
}

/**
 * Refresh margin, in seconds.
 *
 * The iframe is re-signed this long before the token expires. Zero margin means
 * the frame occasionally reloads into Grafana's own login page, which looks like
 * the app is broken; a generous margin costs nothing because minting is a local
 * signature, not a round trip to Grafana.
 */
export const EMBED_REFRESH_MARGIN_SECONDS = 60

/** The percentage of a node's accelerators currently allocated. */
export function gpuUtilisation(entry: GpuModelSummary): number {
  if (entry.total === 0) return 0
  return Math.round((entry.allocated / entry.total) * 100)
}

/** A short human label for a node's state. */
export function nodeStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    ready: '就绪',
    not_ready: '未就绪',
    cordoned: '已封锁',
    unknown: '未知',
  }
  return labels[status] ?? status
}

/** A short human label for an instance state. */
export function instanceStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: '排队中',
    creating: '创建中',
    running: '运行中',
    stopping: '停止中',
    stopped: '已停止',
    failed: '失败',
    deleting: '删除中',
    error: '异常',
  }
  return labels[status] ?? status
}

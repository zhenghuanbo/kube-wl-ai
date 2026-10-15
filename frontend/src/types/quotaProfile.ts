/**
 * Compute quota templates (`app/models/quota_profile.py`).
 *
 * One table holds both kinds, and the two kinds do **not** share their fields: a
 * user template also carries how many instances, applications and custom tasks one
 * person may hold, while a workspace template carries the namespace's object counts.
 * Sending a field to the wrong kind is a 400 naming the field, so the forms here are
 * built from the lists below rather than from "everything in the row".
 */

export type QuotaProfileKind = 'user' | 'workspace'

/** `-1` means "not checked"; anything `>= 0` is a real ceiling, and `0` means none. */
export const UNLIMITED = -1

export interface QuotaProfile {
  id: number
  kind: QuotaProfileKind
  name: string
  displayName: string
  description: string | null
  cpuCores: number
  memoryGb: number
  gpu: number
  /** User templates only; `null` on a workspace template. */
  maxInstances: number | null
  maxApplications: number | null
  maxTasks: number | null
  /** Workspace templates only; `null` on a user template. */
  maxPods: number | null
  maxDeployments: number | null
  maxServices: number | null
  maxJobs: number | null
  isDefault: boolean
  isActive: boolean
}

/** Every dimension a template can carry, in the order a form should read. */
export interface QuotaProfilePayload {
  kind?: QuotaProfileKind
  name?: string
  displayName?: string
  description?: string | null
  cpuCores?: number
  memoryGb?: number
  gpu?: number
  maxInstances?: number
  maxApplications?: number
  maxTasks?: number
  maxPods?: number
  maxDeployments?: number
  maxServices?: number
  maxJobs?: number
  isDefault?: boolean
  isActive?: boolean
}

/**
 * The numeric dimensions, i.e. everything except identity and the flags.
 *
 * Narrowed to the numbers on purpose: a form writes into `payload[dimension.key]`,
 * and widening the key to `keyof QuotaProfilePayload` would make every write's value
 * type the *intersection* of all the property types, which is `undefined`.
 */
export type QuotaDimensionKey =
  | 'cpuCores'
  | 'memoryGb'
  | 'gpu'
  | 'maxInstances'
  | 'maxApplications'
  | 'maxTasks'
  | 'maxPods'
  | 'maxDeployments'
  | 'maxServices'
  | 'maxJobs'

export interface QuotaDimension {
  key: QuotaDimensionKey
  label: string
  /** The unit a person types in, e.g. 核 or GB. */
  unit: string
  hint: string
  max: number
}

/**
 * The compute dimensions, identical on both kinds — this is the set the three
 * layers are all expressed in, which is why a template can be swapped for a pool
 * ceiling or a namespace quota without a conversion table.
 */
export const COMPUTE_DIMENSIONS: readonly QuotaDimension[] = [
  { key: 'cpuCores', label: 'CPU', unit: '核', hint: '按核数填写；-1 表示不校验', max: 4096 },
  { key: 'memoryGb', label: '内存', unit: 'GB', hint: '按 GB 填写；-1 表示不校验', max: 65536 },
  { key: 'gpu', label: '加速卡', unit: '张', hint: '所有型号合计；-1 表示不校验', max: 1024 },
]

/**
 * How many workloads one person may hold, across every workspace.
 *
 * Counting objects is a property of the person, so it lives here rather than on a
 * pool: a pool's ceiling is about how much of *that machine* one user may take, and
 * a workspace's is about the namespace. Neither answers "how many may I have".
 */
export const USER_COUNT_DIMENSIONS: readonly QuotaDimension[] = [
  { key: 'maxInstances', label: '开发实例', unit: '个', hint: '含已停止的实例', max: 10000 },
  { key: 'maxApplications', label: '应用程序', unit: '个', hint: '已登记的应用程序', max: 10000 },
  { key: 'maxTasks', label: '自定义业务', unit: '个', hint: '未结束的自定义任务', max: 10000 },
]

/**
 * The namespace's object counts, which are what a Kubernetes `ResourceQuota` can
 * actually bound. These are the coarse backstop; the ceiling a person meets first is
 * usually the count on their own template.
 */
export const WORKSPACE_COUNT_DIMENSIONS: readonly QuotaDimension[] = [
  { key: 'maxPods', label: 'Pod', unit: '个', hint: '命名空间内 Pod 总数', max: 10000 },
  { key: 'maxDeployments', label: 'Deployment', unit: '个', hint: '开发实例对应的部署', max: 10000 },
  { key: 'maxServices', label: 'Service', unit: '个', hint: '每个实例含 SSH 与 IDE 两个', max: 10000 },
  { key: 'maxJobs', label: 'Job', unit: '个', hint: '自定义业务对应的任务', max: 10000 },
]

export function dimensionsFor(kind: QuotaProfileKind): readonly QuotaDimension[] {
  return kind === 'user'
    ? [...COMPUTE_DIMENSIONS, ...USER_COUNT_DIMENSIONS]
    : [...COMPUTE_DIMENSIONS, ...WORKSPACE_COUNT_DIMENSIONS]
}

export function kindLabel(kind: QuotaProfileKind): string {
  return kind === 'user' ? '用户' : '工作空间'
}

/**
 * Render a ceiling for a table cell.
 *
 * `-1` reads 不限 rather than through the arithmetic: it is an absent ceiling, and
 * showing "-1 核" would look like a negative allowance instead of no allowance.
 */
export function formatCeiling(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined) return '—'
  if (value < 0) return '不限'
  return `${value} ${unit}`
}

/**
 * True when the three compute dimensions leave a pod with nothing it may claim.
 *
 * A template is allowed to say that — it is the only way to freeze a tenant without
 * deleting it — but it is never what somebody meant to type, so the form says so.
 */
export function isAllZeroCompute(profile: Pick<QuotaProfile, 'cpuCores' | 'memoryGb' | 'gpu'>): boolean {
  return profile.cpuCores === 0 && profile.memoryGb === 0 && profile.gpu === 0
}

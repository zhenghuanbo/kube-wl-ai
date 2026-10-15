/**
 * Workspace and compute-resource wire types.
 *
 * Mirrors `backend/app/schemas/workspace.py` and `resource.py`. Keys are exactly
 * the camelCase the API emits — including `k8sNamespace`, which the backend
 * generates deliberately (see `app/schemas/base.py`).
 */

export type WorkspaceStatus =
  | 'pending'
  | 'provisioning'
  | 'active'
  | 'suspended'
  | 'deleting'
  | 'error'

export type WorkspaceRole = 'owner' | 'member'
export type ResourceGroupKind = 'public' | 'private'
export type NetworkType = 'isolated' | 'internal' | 'internet'
export type NodeStatus = 'ready' | 'not_ready' | 'cordoned' | 'unknown'
export type GpuDeviceStatus = 'available' | 'allocated' | 'unhealthy'
export type QuotaScope = 'user' | 'group' | 'workspace'

/** Known accelerator models. Kept in step with `GPU_MODELS` on the backend. */
export const GPU_MODELS = [
  'v100',
  'a100',
  'a800',
  'h100',
  'h800',
  'a10',
  'l40s',
  'none',
] as const
export type GpuModel = (typeof GPU_MODELS)[number]

export interface ResourceGroupSummary {
  id: number
  name: string
  displayName: string
  description: string | null
  kind: ResourceGroupKind
  groupId: number | null
  groupName: string | null
  networkType: NetworkType
  maxRuntimeSeconds: number | null
  storageClass: string | null
  isActive: boolean
  /** The catch-all pool: where nodes land when nothing else claims them. */
  isDefault: boolean
  /** How many nodes are filed here, counted server-side. */
  nodeCount: number
  gpuModels: string[]
  specCount: number
  createdAt: string
  updatedAt: string
}

/**
 * Per-user ceilings inside one pool, in cores and GB.
 *
 * Not milli-cores and mebibytes: these are the numbers an administrator types and
 * compares against what somebody asked for, and the conversion into Kubernetes'
 * units happens once, where a manifest is built. `-1` means "do not check this
 * dimension"; `0` still means "none at all".
 *
 * There are no workload counts here. How many instances, applications and custom
 * tasks one person may hold is a property of the person and lives on their quota
 * profile, so that the question has one answer rather than two that can disagree.
 */
export interface ResourceGroupLimits {
  maxGpuPerUser: number
  maxCpuCoresPerUser: number
  maxMemoryGbPerUser: number
  maxStorageGbPerUser: number
}

export interface ResourceSpec {
  id: number
  name: string
  displayName: string
  cpuMilli: number
  memoryMb: number
  gpuType: string
  gpuCount: number
  isActive: boolean
}

/** What `PATCH /resource-specs/{id}` accepts. `name` is fixed at creation. */
export interface ResourceSpecUpdatePayload {
  displayName?: string | null
  description?: string | null
  cpuMilli?: number | null
  memoryMb?: number | null
  gpuType?: string | null
  gpuCount?: number | null
  isActive?: boolean | null
}

export interface ResourceGroupDetail extends ResourceGroupSummary {
  k8sLabelSelector: Record<string, string>
  k8sTolerations: unknown[]
  limits: ResourceGroupLimits | null
  specs: ResourceSpec[]
}

/**
 * What `PATCH /resource-groups/{id}` accepts.
 *
 * Written out rather than derived with `Partial<ResourceGroupDetail>` for one
 * specific reason: `name` must not be in it. It is the pool's stable identifier,
 * the backend's `ResourceGroupUpdate` omits it, and `ApiModel` forbids unknown
 * fields — so sending it produced a 422 on every edit. `Partial<...Detail>`
 * invited exactly that, because the detail type legitimately carries `name`.
 */
export interface ResourceGroupUpdatePayload {
  displayName?: string | null
  description?: string | null
  kind?: ResourceGroupKind | null
  groupId?: number | null
  networkType?: NetworkType | null
  maxRuntimeSeconds?: number | null
  storageClass?: string | null
  isActive?: boolean | null
  isDefault?: boolean | null
  // Non-nullable, unlike the backend's `dict | None`, where null simply means "not
  // supplied". Keeping it non-null is also what lets one object be spread into a
  // create body, which the backend requires to carry a real dict.
  k8sLabelSelector?: Record<string, string>
  k8sTolerations?: unknown[]
}

/**
 * What `POST /resource-groups` accepts.
 *
 * Spelled out rather than `Partial<ResourceGroupDetail> & { name: string }`, which
 * is what it was: that also permitted `id`, `nodeCount`, `groupName`, `createdAt`
 * and every other read-only field — each of them a 422. A request type should
 * describe what may be *sent*, not what comes back.
 */
export interface ResourceGroupCreatePayload {
  name: string
  displayName?: string
  description?: string | null
  kind?: ResourceGroupKind
  groupId?: number | null
  networkType?: NetworkType
  maxRuntimeSeconds?: number | null
  storageClass?: string | null
  isDefault?: boolean
  k8sLabelSelector?: Record<string, string>
  k8sTolerations?: unknown[]
}

export interface RuntimeLimit {
  id: number
  name: string
  displayName: string
  maxRuntimeSeconds: number | null
  isDefault: boolean
  isActive: boolean
}

/** What `PATCH /runtime-limits/{id}` accepts. `name` is fixed at creation. */
export interface RuntimeLimitUpdatePayload {
  displayName?: string | null
  maxRuntimeSeconds?: number | null
  isDefault?: boolean | null
  isActive?: boolean | null
}

export interface ComputeNode {
  id: number
  name: string
  displayName: string
  status: NodeStatus
  schedulable: boolean
  cpuTotalMilli: number
  memoryTotalMb: number
  cpuAllocatableMilli: number
  memoryAllocatableMb: number
  labels: Record<string, string>
  isManaged: boolean
  /** The resource pool this node belongs to. */
  resourceGroupId: number | null
  resourceGroupName: string | null
  gpuCount: number
  gpuModels: string[]
  lastSeenAt: string | null
}

export interface GpuDevice {
  id: number
  index: number
  model: string
  memoryMb: number | null
  status: GpuDeviceStatus
  uuid: string | null
}

export interface NodeSyncResult {
  nodesSeen: number
  nodesCreated: number
  nodesUpdated: number
  devicesSeen: number
  /** Nodes whose pool label the sync had to rewrite. */
  labelsPatched: number
  /** Nodes with no pool because no default is configured. Should be zero. */
  nodesUnpooled: number
}

export interface Quota {
  id: number
  scopeType: QuotaScope
  scopeId: number
  resource: string
  limitValue: number
  usedValue: number
  available: number
  unit: string
  isActive: boolean
}

export interface WorkspaceGroupRef {
  id: number
  name: string
  displayName: string
}

export interface WorkspaceSummary {
  id: number
  name: string
  displayName: string
  description: string | null
  groupId: number
  groupName: string | null
  groupDisplayName: string | null
  k8sNamespace: string
  status: WorkspaceStatus
  statusMessage: string | null
  isActive: boolean
  memberCount: number
  resourceGroupCount: number
  /**
   * The quota template this workspace follows, or `null` for the default. `null` is
   * "follow the default", not "no quota" — the namespace's object counts come from
   * whichever template this resolves to.
   */
  quotaProfileId: number | null
  createdAt: string
  updatedAt: string
}

export interface WorkspaceMember {
  userId: number
  username: string
  fullName: string
  displayName: string
  email: string | null
  isActive: boolean
  roleInWorkspace: WorkspaceRole
  joinedAt: string
}

export interface WorkspaceResourceGrant {
  resourceGroupId: number
  name: string
  displayName: string
  kind: ResourceGroupKind
  networkType: NetworkType
  grantedAt: string
  isActive: boolean
}

export interface WorkspaceDetail extends WorkspaceSummary {
  members: WorkspaceMember[]
  resourceGrants: WorkspaceResourceGrant[]
}

export interface WorkspaceQuotaLine {
  resource: string
  limitValue: number
  usedValue: number
  unit: string
}

export interface WorkspaceQuotaSync {
  quotas: WorkspaceQuotaLine[]
  applied: boolean
}

export interface WorkspaceCreatePayload {
  name: string
  displayName?: string
  description?: string | null
  groupId?: number | null
  resourceGroupIds?: number[]
  ownerUserId?: number | null
}

export interface WorkspaceUpdatePayload {
  displayName?: string
  description?: string | null
  isActive?: boolean
}

export interface WorkspaceMemberAssignPayload {
  userId: number
  roleInWorkspace?: WorkspaceRole
  /**
   * Required to move a user who already belongs to another workspace. The
   * backend refuses otherwise, because relocating a workspace moves their files
   * and running workloads with it.
   */
  moveFromExisting?: boolean
}

/** Human-readable labels for the quota resource keys the API returns. */
export const QUOTA_RESOURCE_LABELS: Record<string, string> = {
  cpu: 'CPU',
  memory: '内存',
  gpu: '加速卡',
  storage: '存储',
  instance: '实例数',
  task: '任务数',
}

/** `gpu:a100` → `A100`, so a per-model line reads like the rest. */
export function quotaResourceLabel(resource: string): string {
  if (resource.startsWith('gpu:')) return `GPU · ${resource.slice(4).toUpperCase()}`
  return QUOTA_RESOURCE_LABELS[resource] ?? resource
}

/**
 * Render a quota value with its unit (`8000` + `m` → `8000 核·毫`).
 *
 * `-1` is the API's "not checked" — an absent ceiling, not a negative one. It has to
 * be rendered as 不限 rather than through the arithmetic, or a card would show
 * "剩余 -1 card" for a dimension nobody is limiting at all.
 */
export function formatQuota(value: number, unit: string): string {
  if (value < 0) return '不限'
  if (!unit) return String(value)
  return unit === 'm' ? `${value} 核·毫` : `${value} ${unit}`
}

export function formatCpu(milli: number): string {
  return milli % 1000 === 0 ? `${milli / 1000} 核` : `${(milli / 1000).toFixed(1)} 核`
}

export function formatMemory(mb: number): string {
  if (mb >= 1024) return `${Math.round(mb / 1024)} GB`
  return `${mb} MB`
}

/** Seconds → a short Chinese duration (`86400` → `1 天`). */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '不限时'
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  return `${Math.round(seconds / 60)} 分钟`
}

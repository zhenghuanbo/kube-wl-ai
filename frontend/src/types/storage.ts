/**
 * Storage wire types.
 *
 * Mirrors `backend/app/schemas/storage.py`. The shape that matters: a volume is
 * a thing in its own right, not a property of an instance. It exists before
 * anything mounts it and outlives every instance that did, which is why this
 * page has no instance in its primary key.
 */

export type StorageKind =
  | 'personal_home'
  | 'personal_data'
  | 'group_share'
  | 'dataset'
  | 'object'

export type StorageStatus =
  | 'pending'
  | 'bound'
  | 'resizing'
  | 'released'
  | 'failed'
  | 'deleting'

export interface StorageSummary {
  id: number
  name: string
  displayName: string
  description?: string | null
  kind: StorageKind
  status: StorageStatus
  statusMessage?: string | null
  workspaceId: number
  workspaceName?: string | null
  groupId?: number | null
  groupName?: string | null
  ownerUserId?: number | null
  ownerUsername?: string | null
  ownerDisplayName?: string | null
  capacityGb: number
  usedGb: number
  accessMode?: string | null
  storageClass?: string | null
  defaultMountPath: string
  isReadOnly: boolean
  isActive: boolean
  source?: string | null
  createdAt: string
  updatedAt: string
}

export interface StorageDetail extends StorageSummary {
  pvcName?: string | null
  k8sNamespace?: string | null
  sizeBytes?: number | null
  checkedAt?: string | null
  attachedInstanceIds: number[]
  attachedInstanceNames: string[]
}

export interface StorageCreatePayload {
  name: string
  displayName?: string | null
  description?: string | null
  kind?: StorageKind
  workspaceId?: number | null
  /** Only honoured for administrators; defaults to the caller. */
  ownerUserId?: number | null
  capacityGb?: number
  accessMode?: string | null
  storageClass?: string | null
  isReadOnly?: boolean | null
  /** Required for a dataset: without provenance nobody can judge it. */
  source?: string | null
  groupId?: number | null
}

/** Only the fields that can honestly change after creation. */
export interface StorageUpdatePayload {
  displayName?: string | null
  description?: string | null
  isActive?: boolean | null
  isReadOnly?: boolean | null
  source?: string | null
}

export interface StorageUsage {
  count: number
  capacityGb: number
}

/**
 * What each kind of volume is for.
 *
 * `selfService` is the important field: it decides whether the create dialog
 * offers the kind at all. A dataset is a claim about what the whole lab may
 * rely on, so it is not something every user gets to publish.
 */
export interface StorageKindMeta {
  value: StorageKind
  title: string
  description: string
  icon: string
  /** Mount path the platform assigns. `null` means the platform chooses. */
  mountPath: string | null
  /** Whether an ordinary user may create one. */
  selfService: boolean
  /** Whether the volume is always mounted read-only. */
  readOnly: boolean
  /** Whether a source URL is mandatory. */
  requiresSource: boolean
  /** True when a user may have only one, per workspace. */
  unique: boolean
}

export const STORAGE_KINDS: readonly StorageKindMeta[] = [
  {
    value: 'personal_home',
    title: '个人主目录',
    description: '容器里的 /home/jovyan。每人每工作空间一个，实例会自动挂载，不需要手动选择。',
    icon: 'mdi-home-account',
    mountPath: '/home/jovyan',
    selfService: true,
    readOnly: false,
    requiresSource: false,
    unique: true,
  },
  {
    value: 'personal_data',
    title: '个人数据盘',
    description: '挂在 /mnt/data 的持久化空间，用来放代码、数据集和训练输出。可以挂到多个实例。',
    icon: 'mdi-harddisk',
    mountPath: '/mnt/data',
    selfService: true,
    readOnly: false,
    requiresSource: false,
    unique: false,
  },
  {
    value: 'group_share',
    title: '课题组建共享盘',
    description: '整个用户组都能挂载的共享空间，挂在 /mnt/share。由管理员发布。',
    icon: 'mdi-account-group-outline',
    mountPath: '/mnt/share',
    selfService: false,
    readOnly: false,
    requiresSource: false,
    unique: false,
  },
  {
    value: 'dataset',
    title: '只读数据集',
    description: '公共数据集，强制只读地挂在 /mnt/dataset。必须填写来源，方便他人判断是否可信。',
    icon: 'mdi-database-eye-outline',
    mountPath: '/mnt/dataset',
    selfService: false,
    readOnly: true,
    requiresSource: true,
    unique: false,
  },
  {
    value: 'object',
    title: '对象存储挂载',
    description: '对象存储桶的挂载点。需要集群侧先配好 CSI 驱动。',
    icon: 'mdi-cloud-outline',
    mountPath: null,
    selfService: false,
    readOnly: false,
    requiresSource: false,
    unique: false,
  },
]

export function storageKindMeta(kind: StorageKind): StorageKindMeta {
  return STORAGE_KINDS.find((entry) => entry.value === kind) ?? STORAGE_KINDS[1]
}

/** Kinds an ordinary user may create. */
export const SELF_SERVICE_KINDS: readonly StorageKind[] = STORAGE_KINDS.filter(
  (kind) => kind.selfService,
).map((kind) => kind.value)

/** Statuses worth showing a spinner for. */
export const LIVE_STORAGE_STATUSES: readonly StorageStatus[] = [
  'pending',
  'bound',
  'resizing',
  'deleting',
]

export const MIN_CAPACITY_GB = 1
export const MAX_CAPACITY_GB = 65536

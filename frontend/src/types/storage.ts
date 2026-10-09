/**
 * Storage wire types.
 *
 * Mirrors `backend/app/schemas/storage.py`. Two things shape this file.
 *
 * A volume is a *directory*, not something the platform allocated: `hostPath` is
 * the identity, and the unique constraint on it is the rule "one directory, one
 * volume". So the path is part of the read model — it is what an administrator
 * checks by eye — rather than an internal detail.
 *
 * And a volume is not per-user, per-group or per-workspace: it is per *directory*,
 * with the binding decided by the kind. That is why the create form's fields are
 * driven by `binding` below rather than by a fixed set of inputs.
 */

export type StorageKind = 'user_volume' | 'group_share' | 'global_share'

export type StorageStatus =
  | 'pending'
  | 'available'
  | 'bound'
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

  /** The directory this volume is. */
  hostPath: string
  /** The named directory inside a group's own directory; null is the group root. */
  dirName?: string | null
  /** True when this volume *is* a group's directory, mountable only by its admins. */
  isGroupLevel: boolean

  /** Null for a global share: it belongs to no workspace. */
  workspaceId?: number | null
  workspaceName?: string | null
  groupId?: number | null
  groupName?: string | null
  ownerUserId?: number | null
  ownerUsername?: string | null
  ownerDisplayName?: string | null

  capacityGb: number
  usedGb: number
  accessMode?: string | null
  /** Where a container sees it by default, resolved by the backend. */
  defaultMountPath: string
  isReadOnly: boolean
  isActive: boolean
  source?: string | null
  createdAt: string
  updatedAt: string
}

export interface StorageDetail extends StorageSummary {
  /** Both null for a global share until something mounts it in a namespace. */
  pvcName?: string | null
  k8sNamespace?: string | null
  checkedAt?: string | null
  attachedInstanceIds: number[]
  attachedInstanceNames: string[]
}

export interface StorageCreatePayload {
  name: string
  displayName?: string | null
  description?: string | null
  kind?: StorageKind
  /** Required for a user volume. */
  ownerUserId?: number | null
  /** Required for a group share. */
  groupId?: number | null
  /**
   * For a group share: the directory inside the group's own directory. Omit it to
   * make the group's own directory the volume.
   */
  dirName?: string | null
  /** Taken from the group's single workspace; only sent when overriding. */
  workspaceId?: number | null
  capacityGb?: number
  accessMode?: string | null
  isReadOnly?: boolean | null
  source?: string | null
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
 * What each kind of volume is, and what it takes to create one.
 *
 * `binding` is the field the create dialog branches on: a user volume needs a
 * user, a group share needs a group, and a global share needs neither. Nothing
 * here says whether the *caller* may create it — that is `storage:manage`, a
 * permission, and it is the same for every kind.
 */
export interface StorageKindMeta {
  value: StorageKind
  title: string
  description: string
  icon: string
  /** Where a container sees it by default. */
  mountPath: string
  /** What the directory is derived from. */
  binding: 'user' | 'group' | 'none'
  /** Whether `dirName` applies (only a group share has one). */
  hasDirName: boolean
}

export const STORAGE_KINDS: readonly StorageKindMeta[] = [
  {
    value: 'user_volume',
    title: '用户存储卷',
    description:
      '一个用户一目录，形如 /storage/kube-wl/{组}/{用户名}。用户数据的主体，后续开发实例、个人应用、自定义任务都会默认挂载到容器的 /home/{用户名}。',
    icon: 'mdi-account',
    mountPath: '/home/{用户名}',
    binding: 'user',
    hasDirName: false,
  },
  {
    value: 'group_share',
    title: '组共享存储卷',
    description:
      '组目录下的一个共享目录，形如 /storage/kube-wl/{组}/{目录名}。目录名留空则创建组本级存储卷，即整个组目录——它包含每个成员的个人目录，因此只有组管理员可以挂载。默认不挂载，由用户在创建业务时选择，挂到 /mnt/group_shared。',
    icon: 'mdi-account-group-outline',
    mountPath: '/mnt/group_shared',
    binding: 'group',
    hasDirName: true,
  },
  {
    value: 'global_share',
    title: '全局共享存储卷',
    description:
      '与任何组无关的公共目录，形如 /storage/kube-wl/_global/{名称}。所有组都可以在创建业务时选择挂载到 /mnt/global_shared。',
    icon: 'mdi-earth',
    mountPath: '/mnt/global_shared',
    binding: 'none',
    hasDirName: false,
  },
]

export function storageKindMeta(kind: StorageKind): StorageKindMeta {
  return STORAGE_KINDS.find((entry) => entry.value === kind) ?? STORAGE_KINDS[0]
}

/** Statuses worth showing a spinner for. */
export const LIVE_STORAGE_STATUSES: readonly StorageStatus[] = [
  'pending',
  'bound',
  'deleting',
]

export const MIN_CAPACITY_GB = 1
export const MAX_CAPACITY_GB = 65536

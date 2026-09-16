/**
 * Image catalogue wire types.
 *
 * Mirrors `backend/app/schemas/image.py`. Two fields do the work:
 *
 * - `digest` is the identity. A tag can be repushed, so the picker shows the tag
 *   (that is what people recognise) while anything that must be reproducible
 *   pins the digest.
 * - `verified` says whether a sync has actually seen this in the registry. An
 *   unverified entry was confirmed once at registration and never rechecked.
 */

import type { ApplicationCategory } from '@/types/application'

export type ImageKind = 'personal' | 'official' | 'shared'
export type ImageVisibility = 'private' | 'workspace' | 'group' | 'global'

export interface ImageSummary {
  id: number
  reference: string
  displayName: string
  description?: string | null
  registry: string
  project: string
  repository: string
  tag: string
  digest?: string | null
  kind: ImageKind
  visibility: ImageVisibility
  category: ApplicationCategory
  framework?: string | null
  frameworkVersion?: string | null
  pythonVersion?: string | null
  cudaVersion?: string | null
  osVersion?: string | null
  sizeBytes?: number | null
  layerCount?: number | null
  verified: boolean
  syncedAt?: string | null
  syncError?: string | null
  ownerUserId?: number | null
  ownerUsername?: string | null
  workspaceId?: number | null
  groupId?: number | null
  isPinned: boolean
  isActive: boolean
  usageCount: number
  createdAt: string
  updatedAt: string
}

export interface ImageDetail extends ImageSummary {
  /** Whether removal is possible, so the UI does not offer a doomed action. */
  canDelete: boolean
  /** Why not, when `canDelete` is false. */
  blockedReason?: string | null
}

export interface ImageRegisterPayload {
  reference: string
  displayName?: string
  description?: string | null
  category?: ApplicationCategory
  kind?: ImageKind
  visibility?: ImageVisibility
  workspaceId?: number | null
  groupId?: number | null
  framework?: string | null
  frameworkVersion?: string | null
  pythonVersion?: string | null
  cudaVersion?: string | null
  osVersion?: string | null
  isPinned?: boolean
}

export interface ImageUpdatePayload {
  displayName?: string | null
  description?: string | null
  category?: ApplicationCategory | null
  framework?: string | null
  frameworkVersion?: string | null
  pythonVersion?: string | null
  cudaVersion?: string | null
  osVersion?: string | null
  isActive?: boolean | null
  isPinned?: boolean | null
  visibility?: ImageVisibility | null
  kind?: ImageKind | null
}

export interface ImageSyncResult {
  created: number
  updated: number
  unchanged: number
  removed: number
  failed: number
  errors: string[]
  total: number
}

export interface RegistryStatus {
  reachable: boolean
  version?: string | null
  message?: string | null
  host: string
  projects: string[]
}

export interface ImageUsage {
  count: number
  totalBytes: number
  inUse: number
}

export const IMAGE_KIND_META: Readonly<
  Record<ImageKind, { title: string; description: string; icon: string; color: string }>
> = {
  personal: {
    title: '个人镜像',
    description: '只有你自己能看到和使用。移除时会一并删除仓库中的 tag。',
    icon: 'mdi-account-outline',
    color: 'grey',
  },
  shared: {
    title: '共享镜像',
    description: '在指定的范围内共享，供他人选用。移除时不会动仓库里的 tag。',
    icon: 'mdi-share-variant-outline',
    color: 'primary',
  },
  official: {
    title: '官方镜像',
    description: '平台推荐的基础镜像，对所有已登录用户可见。',
    icon: 'mdi-shield-star-outline',
    color: 'success',
  },
}

export const IMAGE_VISIBILITY_META: Readonly<
  Record<ImageVisibility, { title: string; description: string; icon: string }>
> = {
  private: {
    title: '仅自己',
    description: '只有你能看到。',
    icon: 'mdi-lock-outline',
  },
  workspace: {
    title: '本工作空间',
    description: '同一工作空间的成员可见。',
    icon: 'mdi-folder-account-outline',
  },
  group: {
    title: '本用户组',
    description: '同一用户组的成员可见。需要共享权限。',
    icon: 'mdi-account-group-outline',
  },
  global: {
    title: '全平台',
    description: '所有已登录用户可见。需要共享权限。',
    icon: 'mdi-earth',
  },
}

/** Visibility levels an ordinary user may choose. */
export const SELF_SERVICE_VISIBILITIES: readonly ImageVisibility[] = ['private', 'workspace']

/** Ready-made framework labels, so the field is not free-text in practice. */
export const FRAMEWORKS: readonly string[] = [
  'pytorch',
  'tensorflow',
  'jax',
  'mindspore',
  'paddlepaddle',
  'onnxruntime',
  'vllm',
  'other',
]

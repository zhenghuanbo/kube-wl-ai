/**
 * Application wire types.
 *
 * Mirrors `backend/app/schemas/application.py`. An application is a reusable,
 * publishable instance shape: launching one creates an ordinary instance, so
 * `ApplicationLaunchPayload` is deliberately much smaller than
 * `InstanceCreatePayload` — everything it omits comes from the application.
 */
import type { IdeType } from '@/types/instance'

export type ApplicationVisibility = 'private' | 'workspace' | 'group' | 'global'

export type ApplicationCategory =
  | 'development'
  | 'data_science'
  | 'visualization'
  | 'database'
  | 'web'
  | 'other'

/** Label, icon and explanatory hint for the category picker. */
export const CATEGORY_OPTIONS: ReadonlyArray<{
  value: ApplicationCategory
  title: string
  icon: string
}> = [
  { value: 'development', title: '开发环境', icon: 'mdi-code-braces' },
  { value: 'data_science', title: '数据科学', icon: 'mdi-chart-scatter-plot' },
  { value: 'visualization', title: '可视化', icon: 'mdi-chart-box-outline' },
  { value: 'database', title: '数据库', icon: 'mdi-database-outline' },
  { value: 'web', title: 'Web 服务', icon: 'mdi-web' },
  { value: 'other', title: '其他', icon: 'mdi-application' },
]

/**
 * Visibility options, ordered from narrowest to widest.
 *
 * `hint` is shown under the picker: widening who can *see* a definition is the
 * one setting on this form whose consequence is not obvious from the label.
 */
export const VISIBILITY_OPTIONS: ReadonlyArray<{
  value: ApplicationVisibility
  title: string
  icon: string
  hint: string
}> = [
  {
    value: 'private',
    title: '仅自己',
    icon: 'mdi-lock-outline',
    hint: '只有你能看到，适合还没定稿的定义。',
  },
  {
    value: 'workspace',
    title: '本工作空间',
    icon: 'mdi-account-group-outline',
    hint: '同一工作空间的成员都能看到并使用。',
  },
  {
    value: 'group',
    title: '本用户组',
    icon: 'mdi-domain',
    hint: '整个实验室可见，工作空间之外的同组同学也能使用。',
  },
  {
    value: 'global',
    title: '全校公开',
    icon: 'mdi-earth',
    hint: '所有已登录用户都能看到。请确认介绍信息可以公开。',
  },
]

export interface ApplicationSummary {
  id: number
  name: string
  displayName: string
  summary: string
  icon: string
  category: ApplicationCategory
  workspaceId: number
  workspaceName?: string | null
  groupId: number
  groupName?: string | null
  ownerUserId: number
  ownerUsername?: string | null
  ownerDisplayName?: string | null
  image: string
  ideType: IdeType
  resourceGroupId?: number | null
  resourceGroupName?: string | null
  defaultCpuMilli: number
  defaultMemoryMb: number
  defaultGpuType: string
  defaultGpuCount: number
  visibility: ApplicationVisibility
  isCurated: boolean
  isActive: boolean
  launchCount: number
  createdAt: string
  updatedAt: string
}

export interface ApplicationDetail extends ApplicationSummary {
  readme?: string | null
  resourceSpecId?: number | null
  env: Record<string, string>
  ports: Array<Record<string, unknown>>
}

export interface ApplicationCreatePayload {
  name: string
  displayName?: string
  summary?: string
  readme?: string | null
  icon?: string
  category?: ApplicationCategory
  workspaceId?: number | null
  image: string
  ideType?: IdeType
  resourceGroupId?: number | null
  resourceSpecId?: number | null
  defaultCpuMilli?: number
  defaultMemoryMb?: number
  defaultGpuType?: string
  defaultGpuCount?: number
  env?: Record<string, string>
  visibility?: ApplicationVisibility
}

export type ApplicationUpdatePayload = Partial<
  Omit<ApplicationCreatePayload, 'name' | 'workspaceId'>
> & { isActive?: boolean }

/**
 * Launch overrides. Every field is optional; an empty object means "use the
 * application's own defaults", which is the common case.
 */
export interface ApplicationLaunchPayload {
  name?: string | null
  workspaceId?: number | null
  ownerUserId?: number | null
  resourceGroupId?: number | null
  resourceSpecId?: number | null
  maxRuntimeSeconds?: number | null
  sshEnabled?: boolean
}

/** One entry of a workload timeline. */
export interface WorkloadEvent {
  id: number
  eventType: string
  message?: string | null
  fromStatus?: string | null
  toStatus?: string | null
  actorUsername?: string | null
  createdAt: string
}

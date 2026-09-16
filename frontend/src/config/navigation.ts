/**
 * Navigation definition (plan §5.3 / §10.3).
 *
 * The sidebar is generated from this single source of truth and filtered by the
 * current user's roles, so an entry is never shown to a role that cannot reach
 * it. Route guards use the same `roles` arrays via `route.meta.roles`.
 */
import type { Role } from '@/types/auth'

export const WRITE_ROLES: readonly Role[] = ['user', 'group_admin', 'global_admin']
export const ADMIN_ROLES: readonly Role[] = ['group_admin', 'global_admin']

export interface NavHeading {
  kind: 'heading'
  title: string
}

export interface NavItem {
  kind: 'item'
  title: string
  icon: string
  to: string
  /** Omitted means "every authenticated role". */
  roles?: readonly Role[]
}

export type NavEntry = NavHeading | NavItem

export const NAV_ENTRIES: readonly NavEntry[] = [
  { kind: 'heading', title: '工作台' },
  { kind: 'item', title: '首页', icon: 'mdi-home-outline', to: '/dashboard' },
  { kind: 'item', title: '开发实例', icon: 'mdi-cube-outline', to: '/instances' },
  { kind: 'item', title: '应用程序', icon: 'mdi-apps', to: '/applications' },
  { kind: 'item', title: '自定义任务', icon: 'mdi-script-text-outline', to: '/tasks' },

  { kind: 'heading', title: '资源中心' },
  { kind: 'item', title: '镜像', icon: 'mdi-docker', to: '/images' },
  { kind: 'item', title: '计算资源', icon: 'mdi-expansion-card', to: '/compute', roles: WRITE_ROLES },
  { kind: 'item', title: '存储资源', icon: 'mdi-database-outline', to: '/storage' },
  { kind: 'item', title: '用量中心', icon: 'mdi-chart-timeline-variant', to: '/usage' },
  { kind: 'item', title: '监控中心', icon: 'mdi-monitor-dashboard', to: '/monitor' },

  { kind: 'heading', title: '管理' },
  { kind: 'item', title: '用户管理', icon: 'mdi-account-multiple-outline', to: '/admin/users', roles: ADMIN_ROLES },
  { kind: 'item', title: '用户组管理', icon: 'mdi-account-group-outline', to: '/admin/groups', roles: ADMIN_ROLES },
  { kind: 'item', title: '工作空间管理', icon: 'mdi-folder-multiple-outline', to: '/admin/workspaces', roles: ADMIN_ROLES },
  { kind: 'item', title: '资源与配额', icon: 'mdi-tune-variant', to: '/admin/resources', roles: ADMIN_ROLES },
  { kind: 'item', title: '系统设置', icon: 'mdi-cog-outline', to: '/admin/settings', roles: ['global_admin'] },
  { kind: 'item', title: '审计日志', icon: 'mdi-clipboard-text-clock-outline', to: '/admin/audit', roles: ADMIN_ROLES },

  { kind: 'heading', title: '个人' },
  { kind: 'item', title: '个人设置', icon: 'mdi-account-cog-outline', to: '/profile' },
]

/** Returns true when `role` may see `entry`. */
export function isVisibleTo(entry: NavEntry, roles: readonly Role[]): boolean {
  if (entry.kind === 'heading') return true
  if (!entry.roles) return true
  return entry.roles.some((role) => roles.includes(role))
}

/**
 * Filter the navigation for a role set, dropping headings whose section ended
 * up empty.
 */
export function filterNavigation(roles: readonly Role[]): NavEntry[] {
  const kept = NAV_ENTRIES.filter(
    (entry) => entry.kind === 'heading' || isVisibleTo(entry, roles),
  )
  return kept.filter((entry, index) => {
    if (entry.kind !== 'heading') return true
    const next = kept[index + 1]
    return next !== undefined && next.kind === 'item'
  })
}

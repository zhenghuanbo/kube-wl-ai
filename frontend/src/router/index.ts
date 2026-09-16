import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { ADMIN_ROLES, WRITE_ROLES } from '@/config/navigation'
import { useAuthStore } from '@/stores/auth'
import type { Role } from '@/types/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** Human readable page title, used for breadcrumbs and the document title. */
    title?: string
    /** Roles allowed to open the route. Omitted means "any authenticated role". */
    roles?: readonly Role[]
    /** Reachable without a session (login, 404). */
    public?: boolean
    /** Owning delivery phase, shown by PlaceholderView for not-yet-built screens. */
    phase?: string
  }
}

/**
 * Route table.
 *
 * Every navigation entry resolves to a real route so the sidebar can never
 * point at a missing page. Screens not yet built render `PlaceholderView`
 * describing the owning phase.
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/layouts/DefaultLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '首页' },
      },
      {
        path: 'instances',
        name: 'instances',
        component: () => import('@/views/InstancesView.vue'),
        meta: { title: '开发实例' },
      },
      {
        path: 'applications',
        name: 'applications',
        component: () => import('@/views/ApplicationsView.vue'),
        meta: { title: '应用程序' },
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('@/views/TasksView.vue'),
        meta: { title: '自定义任务' },
      },
      {
        path: 'images',
        name: 'images',
        component: () => import('@/views/ImagesView.vue'),
        meta: { title: '镜像', phase: 'Phase 5' },
      },
      {
        path: 'workspace',
        name: 'my-workspace',
        component: () => import('@/views/MyWorkspaceView.vue'),
        meta: { title: '我的工作空间' },
      },
      {
        path: 'compute',
        name: 'compute',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '计算资源', roles: WRITE_ROLES, phase: 'Phase 2' },
      },
      {
        path: 'storage',
        name: 'storage',
        component: () => import('@/views/StorageView.vue'),
        meta: { title: '存储资源', phase: 'Phase 5' },
      },
      {
        path: 'usage',
        name: 'usage',
        component: () => import('@/views/UsageView.vue'),
        meta: { title: '用量中心' },
      },
      {
        path: 'monitor',
        name: 'monitor',
        component: () => import('@/views/MonitorView.vue'),
        meta: { title: '监控中心' },
      },
      {
        path: 'admin/users',
        name: 'admin-users',
        component: () => import('@/views/admin/UsersView.vue'),
        meta: { title: '用户管理', roles: ADMIN_ROLES },
      },
      {
        path: 'admin/groups',
        name: 'admin-groups',
        component: () => import('@/views/admin/GroupsView.vue'),
        meta: { title: '用户组管理', roles: ADMIN_ROLES },
      },
      {
        path: 'admin/workspaces',
        name: 'admin-workspaces',
        component: () => import('@/views/admin/WorkspacesView.vue'),
        meta: { title: '工作空间管理', roles: ADMIN_ROLES },
      },
      {
        path: 'admin/resources',
        name: 'admin-resources',
        component: () => import('@/views/admin/ResourcesView.vue'),
        meta: { title: '资源与配额', roles: ADMIN_ROLES },
      },
      {
        path: 'admin/settings',
        name: 'admin-settings',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '系统设置', roles: ['global_admin'], phase: 'Phase 8' },
      },
      {
        path: 'admin/audit',
        name: 'admin-audit',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '审计日志', roles: ADMIN_ROLES, phase: 'Phase 8' },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/ProfileView.vue'),
        meta: { title: '个人设置' },
      },
    ],
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在', public: true },
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // Restore the session once per page load before deciding anything. The access
  // token lives in memory, so a reload always starts empty and the httpOnly
  // refresh cookie is what makes the session survive.
  if (!auth.isReady) {
    await auth.bootstrap()
  }

  if (to.meta.public) {
    // An authenticated user has no business on the login screen.
    if (to.name === 'login' && auth.isAuthenticated) return { name: 'dashboard' }
    return true
  }

  if (!auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  const required = to.meta.roles
  if (required && !auth.hasAnyRole(required)) {
    return { name: 'dashboard' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title ? `${title} · 西湖 AI 平台` : '西湖 AI 平台'
})

export default router

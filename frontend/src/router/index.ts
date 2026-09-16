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
    /** Owning delivery phase, shown by PlaceholderView for not-yet-built screens. */
    phase?: string
  }
}

/**
 * Phase 0 route table.
 *
 * Every navigation entry resolves to a real route so the sidebar can never
 * point at a missing page. Business screens are replaced phase by phase;
 * until then they render `PlaceholderView` describing the owning phase.
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
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '开发实例', phase: 'Phase 3' },
      },
      {
        path: 'applications',
        name: 'applications',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '应用程序', phase: 'Phase 4' },
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '自定义任务', phase: 'Phase 4' },
      },
      {
        path: 'images',
        name: 'images',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '镜像', phase: 'Phase 5' },
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
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '存储资源', phase: 'Phase 5' },
      },
      {
        path: 'usage',
        name: 'usage',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '用量中心', phase: 'Phase 6' },
      },
      {
        path: 'monitor',
        name: 'monitor',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '监控中心', phase: 'Phase 7' },
      },
      {
        path: 'admin/users',
        name: 'admin-users',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '用户管理', roles: ADMIN_ROLES, phase: 'Phase 1' },
      },
      {
        path: 'admin/groups',
        name: 'admin-groups',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '用户组管理', roles: ADMIN_ROLES, phase: 'Phase 1' },
      },
      {
        path: 'admin/workspaces',
        name: 'admin-workspaces',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '工作空间管理', roles: ADMIN_ROLES, phase: 'Phase 2' },
      },
      {
        path: 'admin/resources',
        name: 'admin-resources',
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '资源与配额', roles: ADMIN_ROLES, phase: 'Phase 2' },
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
        component: () => import('@/views/PlaceholderView.vue'),
        meta: { title: '个人设置', phase: 'Phase 1' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  const auth = useAuthStore()

  // Phase 0 scaffold: install the development identity so the role-driven
  // navigation can be exercised. Phase 1 replaces this with a real session
  // check against `/auth/me`.
  if (!auth.isAuthenticated) auth.useDevPlaceholder()

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

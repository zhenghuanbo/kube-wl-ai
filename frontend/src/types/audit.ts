/**
 * Audit-trail wire types.
 *
 * Mirrors `backend/app/schemas/audit.py`.
 *
 * `actorUserId` being nullable is the field that carries the most meaning: an
 * unauthenticated attempt leaves it null, and such rows belong to no group — so
 * only a global administrator sees them. A UI that rendered null as "unknown
 * user" would bury the most interesting rows in the trail.
 */

export interface AuditEntry {
  id: number
  createdAt: string
  actorUserId: number | null
  actorUsername: string
  action: string
  objectType: string | null
  objectId: string | null
  status: AuditStatus
  detail: Record<string, unknown> | null
  ipAddress: string | null
  requestId: string | null
  errorMessage: string | null
}

export type AuditStatus = 'success' | 'denied' | 'error'

export interface AuditMeta {
  actions: string[]
  objectTypes: string[]
  statuses: string[]
}

/** Query keys are camelCase; snake_case is silently ignored by the API. */
export interface AuditQuery {
  page?: number
  pageSize?: number
  search?: string
  action?: string
  status?: AuditStatus | ''
  objectType?: string
  objectId?: string
  actorUsername?: string
  requestId?: string
  occurredFrom?: string
  occurredTo?: string
}

export const STATUS_LABELS: Record<AuditStatus, string> = {
  success: '成功',
  denied: '被拒绝',
  error: '出错',
}

export const STATUS_COLORS: Record<AuditStatus, string> = {
  success: 'success',
  denied: 'error',
  error: 'warning',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as AuditStatus] ?? status
}

export function statusColor(status: string): string {
  return STATUS_COLORS[status as AuditStatus] ?? 'default'
}

/**
 * A human label for an action code.
 *
 * Only the codes worth naming are listed. Everything else falls through to the
 * raw code, which is the right default: this table is read by people debugging
 * an incident, and an invented label for a code they are grepping for is worse
 * than the code itself.
 */
const ACTION_LABELS: Record<string, string> = {
  // Authentication and access
  'auth:login': '登录',
  'auth:refresh': '刷新会话',
  'auth:logout': '登出',
  'access:denied': '越权尝试',
  'api_token:create': '创建 API 令牌',
  'api_token:revoke': '吊销 API 令牌',
  // Workloads
  'instance:create': '创建实例',
  'instance:update': '修改实例',
  'instance:delete': '删除实例',
  'instance:start': '启动实例',
  'instance:stop': '停止实例',
  'instance:exec': '进入终端',
  'application:create': '创建应用',
  'application:update': '修改应用',
  'application:delete': '删除应用',
  'application:stop': '停止应用',
  'task:create': '提交任务',
  'task:cancel': '取消任务',
  // Storage and images
  'storage:create': '创建存储',
  'storage:resize': '扩容存储',
  'storage:delete': '删除存储',
  'storage:manage': '管理存储',
  'image:create': '登记镜像',
  'image:delete': '删除镜像',
  'image:share': '共享镜像',
  // Administration
  'user:create': '创建用户',
  'user:update': '修改用户',
  'user:delete': '删除用户',
  'user:grant_role': '分配角色',
  'user:revoke_role': '撤销角色',
  'user:reset_password': '重置密码',
  'group:create': '创建用户组',
  'group:update': '修改用户组',
  'group:delete': '删除用户组',
  'group:add_member': '加入组成员',
  'group:remove_member': '移出组成员',
  'group:sync_directory': '同步目录',
  'workspace:create': '创建工作空间',
  'workspace:update': '修改工作空间',
  'workspace:assign_member': '分配成员',
  'workspace:grant_resource': '授权资源组',
  'resource_group:create': '创建资源组',
  'resource_group:update': '修改资源组',
  'resource_spec:create': '创建资源规格',
  'resource_spec:update': '修改资源规格',
  'quota:update': '调整配额',
  'usage:rollup': '重算用量',
}

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

/**
 * Whether a row can be attributed to a person.
 *
 * A failed login carries the name that was *typed*, not a proven identity. The
 * distinction is the difference between "dana did this" and "somebody claimed to
 * be dana", and the trail must not blur it.
 */
export function isAttributed(entry: AuditEntry): boolean {
  return entry.actorUserId !== null
}

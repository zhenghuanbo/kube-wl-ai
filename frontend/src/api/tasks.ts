/** Custom task endpoints (`app/api/v1/tasks.py`). */
import { api } from '@/api/client'
import type { ListQuery, Page } from '@/types/api'
import type { WorkloadEvent } from '@/types/application'
import type {
  TaskCreatePayload,
  TaskDetail,
  TaskLogs,
  TaskStatus,
  TaskSummary,
  TaskUpdatePayload,
} from '@/types/task'

export interface TaskListQuery extends ListQuery {
  workspaceId?: number
  status?: TaskStatus
  ownerUserId?: number
  /** Include cancelled and deleted rows; off by default. */
  includeInactive?: boolean
}

export function listTasks(query: TaskListQuery = {}): Promise<Page<TaskSummary>> {
  return api.get<Page<TaskSummary>>('/tasks', { params: query })
}

export function getTask(taskId: number): Promise<TaskDetail> {
  return api.get<TaskDetail>(`/tasks/${taskId}`)
}

export function createTask(payload: TaskCreatePayload): Promise<TaskDetail> {
  return api.post<TaskDetail>('/tasks', payload)
}

export function updateTask(taskId: number, payload: TaskUpdatePayload): Promise<TaskDetail> {
  return api.patch<TaskDetail>(`/tasks/${taskId}`, payload)
}

/** Stop a running task. Its Job and pods are deleted before this returns. */
export function cancelTask(taskId: number): Promise<TaskDetail> {
  return api.post<TaskDetail>(`/tasks/${taskId}/cancel`)
}

/**
 * Pull this task's state back from the cluster.
 *
 * The periodic sweep runs every 30 seconds, and "I finished three seconds ago"
 * is exactly when a user refreshes the page — so the detail view calls this
 * rather than waiting for the next tick.
 */
export function reconcileTask(taskId: number): Promise<TaskDetail> {
  return api.post<TaskDetail>(`/tasks/${taskId}/reconcile`)
}

export function getTaskLogs(
  taskId: number,
  options: { tailLines?: number; previous?: boolean } = {},
): Promise<TaskLogs> {
  return api.get<TaskLogs>(`/tasks/${taskId}/logs`, { params: options })
}

export function deleteTask(taskId: number): Promise<void> {
  return api.delete<void>(`/tasks/${taskId}`)
}

export function listTaskEvents(taskId: number, limit = 50): Promise<WorkloadEvent[]> {
  return api.get<WorkloadEvent[]>(`/tasks/${taskId}/events`, { params: { limit } })
}

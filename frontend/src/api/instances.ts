/** Instance and SSH-key endpoints (`app/api/v1/instances.py`). */
import { api, getAccessToken } from '@/api/client'
import type { ApiMessage, ListQuery, Page } from '@/types/api'
import type {
  IdeAccess,
  InstanceCreatePayload,
  InstanceDetail,
  InstanceLogs,
  InstanceStatus,
  InstanceSummary,
  InstanceUpdatePayload,
  SshKey,
  SshKeyCreatePayload,
} from '@/types/instance'

export interface InstanceListQuery extends ListQuery {
  workspaceId?: number
  ownerUserId?: number
  status?: InstanceStatus
  ideType?: string
}

export function listInstances(
  query: InstanceListQuery = {},
): Promise<Page<InstanceSummary>> {
  return api.get<Page<InstanceSummary>>('/instances', { params: query })
}

export function getInstance(instanceId: number): Promise<InstanceDetail> {
  return api.get<InstanceDetail>(`/instances/${instanceId}`)
}

export function createInstance(payload: InstanceCreatePayload): Promise<InstanceDetail> {
  return api.post<InstanceDetail>('/instances', payload)
}

export function updateInstance(
  instanceId: number,
  payload: InstanceUpdatePayload,
): Promise<InstanceDetail> {
  return api.patch<InstanceDetail>(`/instances/${instanceId}`, payload)
}

export function deleteInstance(instanceId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/instances/${instanceId}`)
}

/** Scale to one replica. The home volume is kept, so work resumes in place. */
export function startInstance(instanceId: number): Promise<InstanceDetail> {
  return api.post<InstanceDetail>(`/instances/${instanceId}/start`, {})
}

/** Scale to zero. Quota stays reserved, so a restart cannot fail for capacity. */
export function stopInstance(instanceId: number): Promise<InstanceDetail> {
  return api.post<InstanceDetail>(`/instances/${instanceId}/stop`, {})
}

/** Re-apply every object and refresh the observed status. Idempotent. */
export function reconcileInstance(instanceId: number): Promise<InstanceDetail> {
  return api.post<InstanceDetail>(`/instances/${instanceId}/reconcile`)
}

/** Container logs. `previous` reads the crashed container's output. */
export function getInstanceLogs(
  instanceId: number,
  options: { tailLines?: number; previous?: boolean } = {},
): Promise<InstanceLogs> {
  return api.get<InstanceLogs>(`/instances/${instanceId}/logs`, { params: options })
}

export function getIdeAccess(instanceId: number): Promise<IdeAccess> {
  return api.get<IdeAccess>(`/instances/${instanceId}/ide-access`)
}

export function listSshKeys(): Promise<SshKey[]> {
  return api.get<SshKey[]>('/ssh-keys')
}

export function addSshKey(payload: SshKeyCreatePayload): Promise<SshKey> {
  return api.post<SshKey>('/ssh-keys', payload)
}

export function deleteSshKey(keyId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/ssh-keys/${keyId}`)
}

/**
 * Build the terminal WebSocket URL for an instance.
 *
 * The browser cannot set an `Authorization` header on a WebSocket, so the
 * access token travels as a query parameter — the same way the backend expects
 * it. Keeping this here means the token is read from the token store rather
 * than being threaded through components.
 */
export function terminalSocketUrl(instanceId: number, rows: number, cols: number): string {
  const token = getAccessToken() ?? ''
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const url = new URL(`${scheme}://${window.location.host}/ws/terminal/${instanceId}`)
  url.searchParams.set('token', token)
  url.searchParams.set('rows', String(rows))
  url.searchParams.set('cols', String(cols))
  return url.toString()
}

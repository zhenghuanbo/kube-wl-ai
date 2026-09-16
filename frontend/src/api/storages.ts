/** Storage endpoints (`app/api/v1/storages.py`). */
import { api } from '@/api/client'
import type { ListQuery, Page } from '@/types/api'
import type { WorkloadEvent } from '@/types/application'
import type {
  StorageCreatePayload,
  StorageDetail,
  StorageKind,
  StorageStatus,
  StorageSummary,
  StorageUpdatePayload,
  StorageUsage,
} from '@/types/storage'

export interface StorageListQuery extends ListQuery {
  workspaceId?: number
  kind?: StorageKind
  status?: StorageStatus
  /** Only the caller's own volumes, ignoring anything shared with them. */
  ownedOnly?: boolean
  /** Include retired volumes; off by default. */
  includeInactive?: boolean
}

/** Query keys are camelCase; snake_case is silently ignored by the API. */
export function listStorages(query: StorageListQuery = {}): Promise<Page<StorageSummary>> {
  return api.get<Page<StorageSummary>>('/storages', { params: query })
}

export function getStorage(storageId: number): Promise<StorageDetail> {
  return api.get<StorageDetail>(`/storages/${storageId}`)
}

/** Volumes the caller could mount, for the instance create dialog. */
export function listAttachableStorages(workspaceId: number): Promise<StorageSummary[]> {
  return api.get<StorageSummary[]>('/storages/attachable', { params: { workspaceId } })
}

export function createStorage(payload: StorageCreatePayload): Promise<StorageDetail> {
  return api.post<StorageDetail>('/storages', payload)
}

export function updateStorage(
  storageId: number,
  payload: StorageUpdatePayload,
): Promise<StorageDetail> {
  return api.patch<StorageDetail>(`/storages/${storageId}`, payload)
}

/**
 * Grow a volume. Capacity only ever increases: Kubernetes cannot shrink a claim,
 * so accepting a smaller number would desynchronise the record from reality.
 */
export function resizeStorage(storageId: number, capacityGb: number): Promise<StorageDetail> {
  return api.post<StorageDetail>(`/storages/${storageId}/resize`, { capacityGb })
}

/** Pull the volume's state back from the cluster. */
export function reconcileStorage(storageId: number): Promise<StorageDetail> {
  return api.post<StorageDetail>(`/storages/${storageId}/reconcile`)
}

/**
 * Retire a volume and delete its claim.
 *
 * Refused with 409 while an instance still mounts it, so the caller can say
 * *which* instances rather than just "in use".
 */
export function deleteStorage(storageId: number): Promise<StorageDetail> {
  return api.delete<StorageDetail>(`/storages/${storageId}`)
}

export function getStorageUsage(): Promise<StorageUsage> {
  return api.get<StorageUsage>('/storages/usage')
}

export function listStorageEvents(storageId: number, limit = 50): Promise<WorkloadEvent[]> {
  return api.get<WorkloadEvent[]>(`/storages/${storageId}/events`, { params: { limit } })
}

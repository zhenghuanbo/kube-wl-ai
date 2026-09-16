/** Image catalogue endpoints (`app/api/v1/images.py`). */
import { api } from '@/api/client'
import type { ListQuery, Page } from '@/types/api'
import type { ApplicationCategory, WorkloadEvent } from '@/types/application'
import type {
  ImageDetail,
  ImageKind,
  ImageRegisterPayload,
  ImageSummary,
  ImageSyncResult,
  ImageUpdatePayload,
  ImageUsage,
  ImageVisibility,
  RegistryStatus,
} from '@/types/image'

export interface ImageListQuery extends ListQuery {
  category?: ApplicationCategory
  kind?: ImageKind
  visibility?: ImageVisibility
  framework?: string
  /** Only the caller's own entries, ignoring the shared catalogue. */
  ownedOnly?: boolean
  includeInactive?: boolean
}

export function listImages(query: ImageListQuery = {}): Promise<Page<ImageSummary>> {
  return api.get<Page<ImageSummary>>('/images', { params: query })
}

export function getImage(imageId: number): Promise<ImageDetail> {
  return api.get<ImageDetail>(`/images/${imageId}`)
}

/**
 * Add one reference to the catalogue.
 *
 * The API confirms it with the registry first and refuses a reference that does
 * not exist — which is the whole point, since a typo caught here is an
 * `ImagePullBackOff` avoided ten minutes into a job.
 */
export function registerImage(payload: ImageRegisterPayload): Promise<ImageDetail> {
  return api.post<ImageDetail>('/images', payload)
}

export function updateImage(imageId: number, payload: ImageUpdatePayload): Promise<ImageDetail> {
  return api.patch<ImageDetail>(`/images/${imageId}`, payload)
}

/**
 * Remove an entry. Returns the row rather than nothing: it is retired rather
 * than erased, and the caller needs its new state to redraw.
 */
export function deleteImage(imageId: number): Promise<ImageDetail> {
  return api.delete<ImageDetail>(`/images/${imageId}`)
}

/** Whether the registry is reachable, for the page's health banner. */
export function getRegistryStatus(): Promise<RegistryStatus> {
  return api.get<RegistryStatus>('/images/registry')
}

/**
 * Refresh the catalogue from the registry.
 *
 * A POST because it writes. An administrator action: the catalogue is shared
 * state, and refreshing it is not something every user may trigger.
 */
export function syncImages(projects: string[] = []): Promise<ImageSyncResult> {
  return api.post<ImageSyncResult>('/images/sync', { projects })
}

export function getImageUsage(): Promise<ImageUsage> {
  return api.get<ImageUsage>('/images/usage')
}

export function listImageEvents(imageId: number, limit = 50): Promise<WorkloadEvent[]> {
  return api.get<WorkloadEvent[]>(`/images/${imageId}/events`, { params: { limit } })
}

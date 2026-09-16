/** Application catalogue endpoints (`app/api/v1/applications.py`). */
import { api } from '@/api/client'
import type {
  ApplicationCreatePayload,
  ApplicationDetail,
  ApplicationSummary,
  ApplicationUpdatePayload,
  ApplicationLaunchPayload,
  ApplicationCategory,
  ApplicationVisibility,
  WorkloadEvent,
} from '@/types/application'
import type { ListQuery, Page } from '@/types/api'
import type { InstanceDetail } from '@/types/instance'

export interface ApplicationListQuery extends ListQuery {
  category?: ApplicationCategory
  visibility?: ApplicationVisibility
  ownedOnly?: boolean
  includeInactive?: boolean
}

export function listApplications(
  query: ApplicationListQuery = {},
): Promise<Page<ApplicationSummary>> {
  return api.get<Page<ApplicationSummary>>('/applications', { params: query })
}

export function getApplication(applicationId: number): Promise<ApplicationDetail> {
  return api.get<ApplicationDetail>(`/applications/${applicationId}`)
}

export function createApplication(
  payload: ApplicationCreatePayload,
): Promise<ApplicationDetail> {
  return api.post<ApplicationDetail>('/applications', payload)
}

export function updateApplication(
  applicationId: number,
  payload: ApplicationUpdatePayload,
): Promise<ApplicationDetail> {
  return api.patch<ApplicationDetail>(`/applications/${applicationId}`, payload)
}

/**
 * Retire an application from the catalogue.
 *
 * Returns the row rather than 204: the row is kept, and the caller needs the
 * updated `isActive` to redraw the card without a second request.
 */
export function deleteApplication(applicationId: number): Promise<ApplicationDetail> {
  return api.delete<ApplicationDetail>(`/applications/${applicationId}`)
}

/** Launch an application, producing an ordinary instance. */
export function launchApplication(
  applicationId: number,
  payload: ApplicationLaunchPayload = {},
): Promise<InstanceDetail> {
  return api.post<InstanceDetail>(`/applications/${applicationId}/launch`, payload)
}

export function listApplicationEvents(
  applicationId: number,
  limit = 50,
): Promise<WorkloadEvent[]> {
  return api.get<WorkloadEvent[]>(`/applications/${applicationId}/events`, {
    params: { limit },
  })
}

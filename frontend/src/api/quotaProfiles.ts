/**
 * Quota template endpoints (`app/api/v1/quota_profiles.py`).
 *
 * The two assignment calls live here rather than in `users.ts` / `workspaces.ts`
 * even though their paths sit under those resources: what they change is *which
 * template applies*, and keeping them beside the template CRUD is what makes it
 * obvious that the CRUD is the only writer of a ceiling.
 */
import { api } from '@/api/client'
import type { QuotaProfile, QuotaProfileKind, QuotaProfilePayload } from '@/types/quotaProfile'

export function listQuotaProfiles(kind?: QuotaProfileKind): Promise<QuotaProfile[]> {
  return api.get<QuotaProfile[]>('/quota-profiles', { params: { kind } })
}

export function getQuotaProfile(profileId: number): Promise<QuotaProfile> {
  return api.get<QuotaProfile>(`/quota-profiles/${profileId}`)
}

export function createQuotaProfile(payload: QuotaProfilePayload): Promise<QuotaProfile> {
  return api.post<QuotaProfile>('/quota-profiles', payload)
}

export function updateQuotaProfile(
  profileId: number,
  payload: QuotaProfilePayload,
): Promise<QuotaProfile> {
  return api.patch<QuotaProfile>(`/quota-profiles/${profileId}`, payload)
}

export function deleteQuotaProfile(profileId: number): Promise<void> {
  return api.delete<void>(`/quota-profiles/${profileId}`)
}

/**
 * Point a user at a template, or at `null` to follow the default again.
 *
 * `null` rather than a separate "unassign" verb, because "follow the default" is a
 * real state and not the absence of one.
 */
export function assignUserQuotaProfile(
  userId: number,
  profileId: number | null,
): Promise<{ quotaProfileId: number | null }> {
  return api.put<{ quotaProfileId: number | null }>(`/users/${userId}/quota-profile`, {
    profileId,
  })
}

export function assignWorkspaceQuotaProfile(
  workspaceId: number,
  profileId: number | null,
): Promise<{ quotaProfileId: number | null }> {
  return api.put<{ quotaProfileId: number | null }>(`/workspaces/${workspaceId}/quota-profile`, {
    profileId,
  })
}

/** Personal access token endpoints (`app/api/v1/api_tokens.py`). */
import { api } from '@/api/client'
import type { ApiToken, ApiTokenCreatePayload, ApiTokenIssued } from '@/types/apiToken'

/** The caller's own tokens. A bare list: a person has a handful, never a page. */
export function listApiTokens(): Promise<ApiToken[]> {
  return api.get<ApiToken[]>('/api-tokens')
}

/**
 * Mint a token.
 *
 * The response is the only time the secret is readable. It is not cached and not
 * re-fetchable, which is why the UI insists the user copies it before dismissing
 * the dialog rather than offering a "show it again" affordance.
 */
export function createApiToken(payload: ApiTokenCreatePayload): Promise<ApiTokenIssued> {
  return api.post<ApiTokenIssued>('/api-tokens', payload)
}

/** Revoke a token. Immediate: the row is deleted, so the next use fails. */
export function revokeApiToken(id: number): Promise<{ message: string }> {
  return api.delete<{ message: string }>(`/api-tokens/${id}`)
}

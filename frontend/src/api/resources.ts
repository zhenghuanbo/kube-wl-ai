/** Compute-resource endpoints (`app/api/v1/resources.py`). */
import { api } from '@/api/client'
import type { ApiMessage, ListQuery, Page } from '@/types/api'
import type {
  ComputeNode,
  GpuDevice,
  NodeSyncResult,
  Quota,
  QuotaScope,
  ResourceGroupCreatePayload,
  ResourceGroupDetail,
  ResourceGroupKind,
  ResourceGroupLimits,
  ResourceGroupSummary,
  ResourceGroupUpdatePayload,
  ResourceSpec,
  ResourceSpecUpdatePayload,
  RuntimeLimit,
  RuntimeLimitUpdatePayload,
} from '@/types/resource'

// --- resource groups ---------------------------------------------------------
export interface ResourceGroupListQuery extends ListQuery {
  kind?: ResourceGroupKind
  isActive?: boolean
}

export function listResourceGroups(
  query: ResourceGroupListQuery = {},
): Promise<Page<ResourceGroupSummary>> {
  return api.get<Page<ResourceGroupSummary>>('/resource-groups', { params: query })
}

export function getResourceGroup(groupId: number): Promise<ResourceGroupDetail> {
  return api.get<ResourceGroupDetail>(`/resource-groups/${groupId}`)
}

export function createResourceGroup(
  payload: ResourceGroupCreatePayload,
): Promise<ResourceGroupDetail> {
  return api.post<ResourceGroupDetail>('/resource-groups', payload)
}

export function updateResourceGroup(
  groupId: number,
  payload: ResourceGroupUpdatePayload,
): Promise<ResourceGroupDetail> {
  return api.patch<ResourceGroupDetail>(`/resource-groups/${groupId}`, payload)
}

export function deleteResourceGroup(groupId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/resource-groups/${groupId}`)
}

export function updateResourceGroupLimits(
  groupId: number,
  payload: Partial<ResourceGroupLimits>,
): Promise<ResourceGroupLimits> {
  return api.put<ResourceGroupLimits>(`/resource-groups/${groupId}/limits`, payload)
}

/** Replace which specs a pool offers. */
export function setResourceGroupSpecs(
  groupId: number,
  specIds: number[],
): Promise<ResourceSpec[]> {
  return api.put<ResourceSpec[]>(`/resource-groups/${groupId}/specs`, specIds)
}

/**
 * Replace which nodes belong to a pool.
 *
 * Nodes dropped from the list move to the default pool, and every move rewrites
 * the node's `wl-pool` label — so this is the call that actually re-points the
 * scheduler, not just a bookkeeping update.
 */
export function setResourceGroupNodes(
  groupId: number,
  nodeIds: number[],
): Promise<NodeSyncResult> {
  return api.put<NodeSyncResult>(`/resource-groups/${groupId}/nodes`, nodeIds)
}

// --- specs -------------------------------------------------------------------
export function listResourceSpecs(
  query: { gpuOnly?: boolean; isActive?: boolean } = {},
): Promise<ResourceSpec[]> {
  return api.get<ResourceSpec[]>('/resource-specs', { params: query })
}

export function createResourceSpec(
  payload: Omit<ResourceSpec, 'id' | 'isActive'>,
): Promise<ResourceSpec> {
  return api.post<ResourceSpec>('/resource-specs', payload)
}

export function updateResourceSpec(
  specId: number,
  payload: ResourceSpecUpdatePayload,
): Promise<ResourceSpec> {
  return api.patch<ResourceSpec>(`/resource-specs/${specId}`, payload)
}

export function deleteResourceSpec(specId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/resource-specs/${specId}`)
}

// --- runtime limits ----------------------------------------------------------
export function listRuntimeLimits(
  query: { isActive?: boolean } = {},
): Promise<RuntimeLimit[]> {
  return api.get<RuntimeLimit[]>('/runtime-limits', { params: query })
}

export function createRuntimeLimit(payload: {
  name: string
  displayName?: string
  maxRuntimeSeconds?: number | null
  isDefault?: boolean
}): Promise<RuntimeLimit> {
  return api.post<RuntimeLimit>('/runtime-limits', payload)
}

export function updateRuntimeLimit(
  limitId: number,
  payload: RuntimeLimitUpdatePayload,
): Promise<RuntimeLimit> {
  return api.patch<RuntimeLimit>(`/runtime-limits/${limitId}`, payload)
}

export function deleteRuntimeLimit(limitId: number): Promise<ApiMessage> {
  return api.delete<ApiMessage>(`/runtime-limits/${limitId}`)
}

// --- nodes -------------------------------------------------------------------
export function listNodes(): Promise<ComputeNode[]> {
  return api.get<ComputeNode[]>('/nodes')
}

export function syncNodes(): Promise<NodeSyncResult> {
  return api.post<NodeSyncResult>('/nodes/sync')
}

export function listNodeGpus(nodeId: number): Promise<GpuDevice[]> {
  return api.get<GpuDevice[]>(`/nodes/${nodeId}/gpus`)
}

// --- quotas ------------------------------------------------------------------
export function listQuotas(scopeType: QuotaScope, scopeId: number): Promise<Quota[]> {
  return api.get<Quota[]>('/quotas', { params: { scopeType, scopeId } })
}

export function updateQuota(quotaId: number, limitValue: number): Promise<Quota> {
  return api.patch<Quota>(`/quotas/${quotaId}`, { limitValue })
}

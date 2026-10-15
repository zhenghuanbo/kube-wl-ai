<script setup lang="ts">
/**
 * The caller's own workspace.
 *
 * A user belongs to exactly one workspace (plan D7), so there is no switcher
 * and no list — the page either shows the workspace or explains that none has
 * been assigned yet. Quota is read-only here: a user may see what they have been
 * given, but only an administrator changes it.
 */
import { computed, onMounted, ref } from 'vue'

import { listResourceGroups, listQuotas } from '@/api/resources'
import { getMyWorkspace, listWorkspaceGrants } from '@/api/workspaces'
import { ApiError } from '@/api/client'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import {
  formatDuration,
  formatQuota,
  quotaResourceLabel,
  type Quota,
  type ResourceGroupSummary,
  type WorkspaceResourceGrant,
  type WorkspaceSummary,
} from '@/types/resource'

const NATURE_LABELS: Record<string, { label: string; icon: string; hint: string }> = {
  isolated: {
    label: '隔离网络',
    icon: 'mdi-shield-lock-outline',
    hint: '仅工作空间内部互通，无法访问集群内其他服务',
  },
  internal: {
    label: '集群内网',
    icon: 'mdi-lan-connect',
    hint: '可访问集群内服务与 DNS，无外网出口',
  },
  internet: {
    label: '可访问外网',
    icon: 'mdi-earth',
    hint: '可访问互联网，请遵守数据安全规范',
  },
}

const loading = ref(true)
const workspace = ref<WorkspaceSummary | null>(null)
const grants = ref<WorkspaceResourceGrant[]>([])
const quotas = ref<Quota[]>([])
const pools = ref<ResourceGroupSummary[]>([])

const poolById = computed(
  () => new Map(pools.value.map((pool) => [pool.id, pool])),
)

/**
 * The effective network posture is the most restrictive granted pool's — the
 * backend applies the same rule, so reading it off the grants here cannot
 * disagree with what the namespace actually allows.
 */
const network = computed(() => {
  const kinds = grants.value
    .map((grant) => grant.networkType)
    .filter((kind) => kind in NATURE_LABELS)
  if (kinds.length === 0) return NATURE_LABELS.isolated
  const order = ['isolated', 'internal', 'internet'] as const
  const strictest = kinds.reduce((worst, kind) =>
    order.indexOf(kind) < order.indexOf(worst) ? kind : worst,
  )
  return NATURE_LABELS[strictest]
})

/** The longest runtime any granted pool permits; `null` means unlimited. */
const maxRuntime = computed(() => {
  const values = grants.value
    .map((grant) => poolById.value.get(grant.resourceGroupId)?.maxRuntimeSeconds)
    .filter((value): value is number => typeof value === 'number')
  if (values.length === 0) return null
  return Math.max(...values)
})

function usagePercent(quota: Quota): number {
  if (quota.limitValue <= 0) return 0
  return Math.min(100, Math.round((quota.usedValue / quota.limitValue) * 100))
}

function usageTone(percent: number): string {
  if (percent >= 90) return 'error'
  if (percent >= 70) return 'warning'
  return 'primary'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    workspace.value = await getMyWorkspace()
    if (!workspace.value) return

    const [grantList, quotaList, poolList] = await Promise.all([
      listWorkspaceGrants(workspace.value.id),
      listQuotas('workspace', workspace.value.id),
      listResourceGroups({ pageSize: 200 }),
    ])
    grants.value = grantList
    quotas.value = quotaList
    pools.value = poolList.items
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    workspace.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="我的工作空间" subtitle="你的开发环境、算力配额与可用资源池" />

    <v-skeleton-loader v-if="loading" type="article, card, card" />

    <EmptyState
      v-else-if="!workspace"
      icon="mdi-folder-outline"
      title="尚未分配工作空间"
      description="工作空间由所在用户组的管理员创建并分配。请联系实验室管理员为你开通。"
    />

    <template v-else>
      <v-card class="mb-6">
        <v-card-text class="d-flex flex-wrap align-center ga-4">
          <v-avatar size="56" color="primary" variant="tonal">
            <v-icon icon="mdi-folder-multiple-outline" size="28" />
          </v-avatar>

          <div class="flex-grow-1">
            <div class="d-flex align-center ga-3 flex-wrap">
              <span class="text-h6">{{ workspace.displayName }}</span>
              <StatusChip :status="workspace.status" />
            </div>
            <div class="text-body-2 text-medium-emphasis mt-1">
              用户组 {{ workspace.groupDisplayName ?? workspace.groupName }} ·
              命名空间
              <code class="wl-namespace">{{ workspace.k8sNamespace }}</code>
            </div>
          </div>

          <div class="d-flex ga-2 flex-wrap">
            <v-chip :prepend-icon="network.icon" variant="tonal" color="primary">
              {{ network.label }}
            </v-chip>
            <v-chip prepend-icon="mdi-timer-sand" variant="tonal" color="primary">
              {{ formatDuration(maxRuntime) }}
            </v-chip>
          </div>
        </v-card-text>

        <v-alert
          v-if="workspace.status === 'error'"
          type="error"
          variant="tonal"
          class="mx-4 mb-4"
          :text="workspace.statusMessage ?? '工作空间下发失败，请联系管理员处理。'"
        />
        <v-alert
          v-else
          type="info"
          variant="tonal"
          density="compact"
          class="mx-4 mb-4"
          :text="network.hint"
        />
      </v-card>

      <h2 class="text-h6 mb-3">算力配额</h2>
      <v-row v-if="quotas.length" class="mb-2">
        <v-col v-for="quota in quotas" :key="quota.id" cols="12" sm="6" lg="4">
          <v-card>
            <v-card-text>
              <div class="d-flex justify-space-between align-baseline mb-2">
                <span class="text-body-2 text-medium-emphasis">
                  {{ quotaResourceLabel(quota.resource) }}
                </span>
                <span class="text-body-2 text-medium-emphasis">
                  {{ formatQuota(quota.usedValue, quota.unit) }} /
                  {{ formatQuota(quota.limitValue, quota.unit) }}
                </span>
              </div>
              <v-progress-linear
                v-if="quota.limitValue >= 0"
                :model-value="usagePercent(quota)"
                :color="usageTone(usagePercent(quota))"
                height="8"
                rounded
              />
              <div class="text-caption text-medium-emphasis mt-2">
                <!-- `-1` is "not checked", so there is no remainder to show: saying
                     "剩余 -1" would read as a debt rather than as an absence. -->
                <template v-if="quota.limitValue < 0">管理员未限制这一项</template>
                <template v-else>剩余 {{ formatQuota(quota.available, quota.unit) }}</template>
              </div>
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>
      <EmptyState
        v-else
        icon="mdi-tune-variant"
        title="暂无配额"
        description="管理员授权资源池后，这里会显示你可用的算力。"
      />

      <h2 class="text-h6 mb-3 mt-6">可用资源池</h2>
      <v-table v-if="grants.length" hover>
        <thead>
          <tr>
            <th>资源池</th>
            <th>类型</th>
            <th>网络</th>
            <th>最长运行时长</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="grant in grants" :key="grant.resourceGroupId">
            <td>
              <div class="font-weight-medium">{{ grant.displayName }}</div>
              <div class="text-caption text-medium-emphasis">{{ grant.name }}</div>
            </td>
            <td>
              <v-chip size="small" variant="tonal">
                {{ grant.kind === 'public' ? '公共' : '专用' }}
              </v-chip>
            </td>
            <td>{{ NATURE_LABELS[grant.networkType]?.label ?? grant.networkType }}</td>
            <td>
              {{ formatDuration(poolById.get(grant.resourceGroupId)?.maxRuntimeSeconds ?? null) }}
            </td>
          </tr>
        </tbody>
      </v-table>
      <EmptyState
        v-else
        icon="mdi-expansion-card"
        title="尚未授权资源池"
        description="工作空间已就绪，但还没有可用的算力。授权后即可创建开发实例。"
      />
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-namespace {
  font-family: 'Roboto Mono', ui-monospace, monospace;
  font-size: 0.8125rem;
  background: $hover-bg;
  border-radius: 4px;
  padding: 1px 6px;
}
</style>

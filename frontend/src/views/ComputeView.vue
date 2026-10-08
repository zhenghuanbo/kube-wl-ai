<script setup lang="ts">
/**
 * Compute resources — the shared cluster, from the caller's point of view.
 *
 * Two sections, and the second one is conditional on purpose.
 *
 * **Resource pools** are what a user actually needs: which pools exist, what GPU
 * models they carry, what a single instance may ask for, and the per-user
 * ceilings. A user already holds `resource_group:read`, so this section renders
 * for everyone.
 *
 * **The node inventory** appears only for a caller holding `node:read` (group and
 * global administrators). This is not squeamishness about hiding node names — it
 * is that the endpoint refuses the call, and a section that renders an error for
 * most of its audience is worse than one that is absent. The page says what it is
 * showing and why, rather than leaving the reader to wonder whether a tab failed.
 *
 * Everything is read-only. Node sync and pool editing are administrative acts and
 * live in 资源与配额.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { getResourceGroup, listNodes, listResourceGroups } from '@/api/resources'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { ComputeNode, ResourceGroupDetail, ResourceGroupSummary } from '@/types/resource'

const notifications = useNotificationStore()
const { can } = usePermissions()

/** Whether the caller may see the cluster's nodes. Mirrors the API's `node:read`. */
const canSeeNodes = computed(() => can('node:read'))

const pools = ref<ResourceGroupSummary[]>([])
const nodes = ref<ComputeNode[]>([])
const loading = ref(false)
const nodesLoading = ref(false)

/**
 * Pool details, fetched only when a row is expanded.
 *
 * The list endpoint carries a spec *count* rather than the specs, so showing them
 * means a second request per pool. Doing that eagerly for every row would be one
 * request per pool on every page load to render something most visitors never
 * open. Keyed by id so the result survives collapsing.
 */
const details = ref<Record<number, ResourceGroupDetail>>({})
const expanded = ref<string[]>([])
const detailLoading = ref<number | null>(null)

async function loadDetail(id: number): Promise<void> {
  if (details.value[id] || detailLoading.value === id) return
  detailLoading.value = id
  try {
    details.value = { ...details.value, [id]: await getResourceGroup(id) }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    detailLoading.value = null
  }
}

/** Vuetify reports the new expanded-key list; fetch details for whatever opened. */
function onExpandedUpdate(value: unknown): void {
  const next = (Array.isArray(value) ? value : []).map(String)
  expanded.value = next
  // Only the newly opened keys are absent from `details`, and `loadDetail`
  // returns immediately for the rest — so re-fetching every key on every toggle
  // costs nothing and avoids tracking the previous list.
  for (const key of next) void loadDetail(Number(key))
}

function specLabel(spec: ResourceGroupDetail['specs'][number]): string {
  const parts = [`${cores(spec.cpuMilli)} 核`, gib(spec.memoryMb)]
  if (spec.gpuCount > 0) parts.push(`${spec.gpuType} × ${spec.gpuCount}`)
  return parts.join(' · ')
}

async function loadPools(): Promise<void> {
  loading.value = true
  try {
    // A page of 100: a lab has a handful of pools, and paginating a list this
    // short would hide the one somebody is looking for behind a control they do
    // not expect to use.
    const page = await listResourceGroups({ pageSize: 100 })
    pools.value = page.items
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

async function loadNodes(): Promise<void> {
  if (!canSeeNodes.value) return
  nodesLoading.value = true
  try {
    nodes.value = await listNodes()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    nodesLoading.value = false
  }
}

onMounted(() => {
  void loadPools()
  void loadNodes()
})

/** Only pools that are actually offered. An inactive pool is not a resource. */
const activePools = computed(() => pools.value.filter((pool) => pool.isActive))

const totals = computed(() => {
  const gpus = nodes.value.reduce((sum, node) => sum + node.gpuCount, 0)
  const schedulable = nodes.value.filter((node) => node.schedulable).length
  return { gpus, schedulable, nodes: nodes.value.length }
})

const poolHeaders = [
  { title: '资源池', key: 'displayName' },
  { title: '类型', key: 'kind', width: '110px' },
  { title: '加速卡', key: 'gpuModels' },
  { title: '规格', key: 'specCount', width: '110px' },
  { title: '单实例上限', key: 'limits' },
]

const nodeHeaders = [
  { title: '节点', key: 'displayName' },
  { title: '状态', key: 'status', width: '110px' },
  { title: '可调度', key: 'schedulable', width: '100px' },
  { title: '资源池', key: 'resourceGroupName', width: '140px' },
  { title: 'CPU', key: 'cpu', width: '150px' },
  { title: '内存', key: 'memory', width: '150px' },
  { title: '加速卡', key: 'gpu', width: '180px' },
]

function kindLabel(kind: string): string {
  return kind === 'public' ? '公共' : kind === 'dedicated' ? '专用' : kind
}

function kindColor(kind: string): string {
  return kind === 'public' ? 'info' : 'secondary'
}

function cores(milli: number): string {
  return (milli / 1000).toFixed(milli % 1000 === 0 ? 0 : 1)
}

function gib(mb: number): string {
  return `${Math.round(mb / 1024)} GB`
}

function nodeStatusLabel(status: string): string {
  return status === 'ready' ? '可调度' : status === 'not_ready' ? '不可调度' : status
}

function nodeStatusColor(status: string): string {
  return status === 'ready' ? 'success' : 'error'
}
</script>

<template>
  <div>
    <PageHeader
      title="计算资源"
      subtitle="平台提供的算力池与集群节点；只读，具体的创建与配额在「资源与配额」中管理"
    >
      <template #actions>
        <v-btn variant="text" prepend-icon="mdi-refresh" :loading="loading" @click="loadPools">
          刷新
        </v-btn>
      </template>
    </PageHeader>

    <!-- Pools: what a user needs in order to choose a spec. -->
    <v-card variant="flat" class="wl-card mb-6">
      <v-card-title>资源池</v-card-title>
      <v-card-subtitle>
        创建实例时选择其中一个池；池决定了可用的加速卡型号与单实例的资源上限。
      </v-card-subtitle>
      <v-card-text>
        <v-data-table
          :headers="poolHeaders"
          :items="activePools"
          :loading="loading"
          density="comfortable"
          item-value="id"
          show-expand
          :expanded="expanded"
          class="wl-table"
          @update:expanded="onExpandedUpdate"
        >
          <template #item.displayName="{ item }">
            <div class="d-flex flex-column">
              <span class="font-weight-medium">{{ item.displayName }}</span>
              <span class="text-caption text-medium-emphasis wl-mono">{{ item.name }}</span>
            </div>
          </template>

          <template #item.kind="{ item }">
            <v-chip size="small" variant="tonal" label :color="kindColor(item.kind)">
              {{ kindLabel(item.kind) }}
            </v-chip>
          </template>

          <template #item.gpuModels="{ item }">
            <template v-if="item.gpuModels.length">
              <v-chip
                v-for="model in item.gpuModels"
                :key="model"
                size="small"
                variant="outlined"
                label
                class="me-1"
              >
                {{ model }}
              </v-chip>
            </template>
            <span v-else class="text-caption">CPU 规格</span>
          </template>

          <template #item.specCount="{ item }">
            <span class="wl-mono">{{ item.specCount }}</span>
            <span class="text-caption text-medium-emphasis ms-1">个</span>
          </template>

          <template #item.limits="{ item }">
            <span class="text-caption">
              单实例最多 {{ item.maxRuntimeSeconds ? `${Math.round(item.maxRuntimeSeconds / 3600)} 小时` : '不限时长' }}
            </span>
          </template>

          <template #expanded-row="{ columns, item }">
            <tr>
              <td :colspan="columns.length" class="wl-expanded">
                <v-progress-linear
                  v-if="detailLoading === item.id && !details[item.id]"
                  indeterminate
                  height="2"
                />
                <template v-else-if="details[item.id]">
                  <div class="wl-expanded__title">可用规格</div>
                  <v-table v-if="details[item.id].specs.length" density="compact">
                    <thead>
                      <tr>
                        <th>名称</th>
                        <th>配置</th>
                        <th style="width: 90px">状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="spec in details[item.id].specs" :key="spec.id">
                        <td>{{ spec.displayName }}</td>
                        <td class="wl-mono text-body-2">{{ specLabel(spec) }}</td>
                        <td>
                          <v-chip
                            size="x-small"
                            variant="tonal"
                            label
                            :color="spec.isActive ? 'success' : 'default'"
                          >
                            {{ spec.isActive ? '可选用' : '已停用' }}
                          </v-chip>
                        </td>
                      </tr>
                    </tbody>
                  </v-table>
                  <div v-else class="text-caption text-medium-emphasis">
                    该资源池尚未定义规格。
                  </div>
                </template>
              </td>
            </tr>
          </template>
        </v-data-table>

        <EmptyState
          v-if="!loading && !activePools.length"
          icon="mdi-expansion-card-variant"
          title="还没有可用的资源池"
          description="资源池由管理员在「资源与配额」中创建并授权给你的工作空间。"
        />
      </v-card-text>
    </v-card>

    <!-- Nodes: only for callers the API will actually answer. -->
    <v-card v-if="canSeeNodes" variant="flat" class="wl-card">
      <v-card-title class="d-flex align-center">
        集群节点
        <v-chip v-if="nodes.length" size="small" variant="tonal" class="ms-3" label>
          {{ totals.schedulable }} / {{ totals.nodes }} 可调度 · {{ totals.gpus }} 张加速卡
        </v-chip>
      </v-card-title>
      <v-card-subtitle>
        节点由平台从集群同步而来，反映的是集群的真实容量，不随配额变化。
      </v-card-subtitle>
      <v-card-text>
        <v-data-table
          :headers="nodeHeaders"
          :items="nodes"
          :loading="nodesLoading"
          density="comfortable"
          item-value="id"
          class="wl-table"
        >
          <template #item.displayName="{ item }">
            <div class="d-flex flex-column">
              <span class="wl-mono">{{ item.name }}</span>
              <span v-if="item.displayName && item.displayName !== item.name" class="text-caption text-medium-emphasis">
                {{ item.displayName }}
              </span>
            </div>
          </template>

          <template #item.status="{ item }">
            <v-chip size="small" variant="tonal" label :color="nodeStatusColor(item.status)">
              {{ nodeStatusLabel(item.status) }}
            </v-chip>
          </template>

          <template #item.schedulable="{ item }">
            <v-icon
              :icon="item.schedulable ? 'mdi-check-circle-outline' : 'mdi-minus-circle-outline'"
              :color="item.schedulable ? 'success' : 'error'"
              size="small"
            />
          </template>

          <template #item.resourceGroupName="{ item }">
            <v-chip v-if="item.resourceGroupName" size="small" variant="tonal" label>
              {{ item.resourceGroupName }}
            </v-chip>
            <span v-else class="text-medium-emphasis text-caption">未归属</span>
          </template>

          <template #item.cpu="{ item }">
            <div class="d-flex flex-column">
              <span class="wl-mono">{{ cores(item.cpuAllocatableMilli) }} 核</span>
              <span class="text-caption text-medium-emphasis">
                共 {{ cores(item.cpuTotalMilli) }} 核
              </span>
            </div>
          </template>

          <template #item.memory="{ item }">
            <div class="d-flex flex-column">
              <span class="wl-mono">{{ gib(item.memoryAllocatableMb) }}</span>
              <span class="text-caption text-medium-emphasis">
                共 {{ gib(item.memoryTotalMb) }}
              </span>
            </div>
          </template>

          <template #item.gpu="{ item }">
            <template v-if="item.gpuCount">
              <span class="wl-mono">{{ item.gpuCount }} 张</span>
              <span v-if="item.gpuModels.length" class="text-caption text-medium-emphasis ms-2">
                {{ item.gpuModels.join('、') }}
              </span>
            </template>
            <span v-else class="text-caption">无</span>
          </template>
        </v-data-table>

        <EmptyState
          v-if="!nodesLoading && !nodes.length"
          icon="mdi-server-off"
          title="还没有同步过集群节点"
          description="管理员可在「资源与配额 → 集群节点」执行一次同步。"
        />
      </v-card-text>
    </v-card>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-card {
  box-shadow: $card-shadow;
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.wl-expanded {
  padding: 12px 16px 16px !important;
  background: rgba(83, 109, 254, 0.03);
}

.wl-expanded__title {
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: $text-color;
  margin-bottom: 6px;
}
</style>

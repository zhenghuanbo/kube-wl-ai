<script setup lang="ts">
/**
 * The monitoring centre.
 *
 * Two layers, deliberately unequal in status.
 *
 * **The platform's own inventory** is the first layer and always works. It comes
 * from rows the platform wrote itself — which containers it scheduled, on which
 * nodes, holding which accelerators — and it is available when the cluster is
 * unreachable, when Prometheus is down, and when this deployment has no Grafana
 * at all. It is what the page falls back to and, for most questions, what it
 * answers.
 *
 * **Grafana** is the second layer and is optional. It appears as an embedded
 * panel showing time-series detail the platform deliberately does not store. When
 * it is not configured, the page says so and keeps working; it does not pretend
 * the feature is broken.
 *
 * The rule this page exists to respect: *a flat line is ambiguous*. Nothing
 * running and nothing visible look identical, so the active filter is printed at
 * the top whenever there is one. A user who has been filtered to nothing will
 * read "你没有正在运行的容器" instead of wondering why their dashboard is blank.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { getMonitorOverview, mintMonitorEmbed } from '@/api/monitor'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useNotificationStore } from '@/stores/notifications'
import type { MonitorDashboard, MonitorOverview } from '@/types/monitor'
import {
  EMBED_REFRESH_MARGIN_SECONDS,
  gpuUtilisation,
  instanceStatusLabel,
  nodeStatusLabel,
} from '@/types/monitor'

const notifications = useNotificationStore()

const overview = ref<MonitorOverview | null>(null)
const loading = ref(false)
const activeKey = ref<string>('')

const scope = computed(() => overview.value?.scope ?? null)
const inventory = computed(() => overview.value?.inventory ?? null)
const embeds = computed(() => overview.value?.embeds ?? {})

const activeDashboard = computed<MonitorDashboard | null>(
  () => overview.value?.dashboards.find((item) => item.key === activeKey.value) ?? null,
)
const activeUrl = computed(() => embeds.value[activeKey.value]?.url ?? '')

/** Total accelerators across every model, and how many are spoken for. */
const gpuTotals = computed(() => {
  const entries = inventory.value?.gpuByModel ?? []
  return {
    total: entries.reduce((sum, entry) => sum + entry.total, 0),
    allocated: entries.reduce((sum, entry) => sum + entry.allocated, 0),
  }
})

const runningCount = computed(
  () => inventory.value?.instances.find((row) => row.status === 'running')?.count ?? 0,
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await getMonitorOverview()
    overview.value = data
    // Land on the first dashboard the caller may actually open, rather than on
    // an empty frame.
    if (!data.dashboards.some((item) => item.key === activeKey.value)) {
      activeKey.value = data.dashboards[0]?.key ?? ''
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)

// --- keeping the embedded credential fresh -----------------------------------
/**
 * Re-sign the active panel before its token lapses.
 *
 * Grafana will not tell the iframe that the token expired; the frame simply
 * becomes its login page, which reads as "monitoring is broken". Replacing the
 * `src` is enough — the browser reloads the frame with the new token.
 *
 * Only the *active* dashboard is refreshed. Signing all of them on a timer would
 * create credentials nobody is looking at.
 */
let refreshTimer: ReturnType<typeof setTimeout> | null = null

function scheduleRefresh(): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  const embed = embeds.value[activeKey.value]
  if (!embed) return

  const delay = Math.max(5, embed.expiresIn - EMBED_REFRESH_MARGIN_SECONDS) * 1000
  refreshTimer = setTimeout(() => void refreshActive(), delay)
}

async function refreshActive(): Promise<void> {
  const key = activeKey.value
  if (!key || !overview.value) return
  try {
    const embed = await mintMonitorEmbed(key)
    overview.value = { ...overview.value, embeds: { ...overview.value.embeds, [embed.key]: embed } }
    scheduleRefresh()
  } catch (error) {
    // A failure here is not worth an interruption: the panel is still showing
    // whatever it last drew, and the next tab switch will try again.
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

function selectDashboard(key: string): void {
  if (key === activeKey.value) return
  activeKey.value = key
  void refreshActive()
}

onBeforeUnmount(() => {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
})

/**
 * (Re)start the clock whenever the iframe's src changes.
 *
 * Driven by the URL rather than by the click: the first URL arrives with the
 * overview response, and a watcher on the click alone would leave the very first
 * panel un-refreshed.
 */
watch(activeUrl, (url) => {
  if (url) scheduleRefresh()
})

interface Column {
  title: string
  key: string
  align?: 'start' | 'end'
  width?: string
}

const containerHeaders: Column[] = [
  { title: '容器', key: 'name' },
  { title: '负责人', key: 'owner' },
  { title: '命名空间', key: 'namespace' },
  { title: 'Pod', key: 'pod' },
  { title: '加速卡', key: 'gpu', align: 'end', width: '110px' },
  { title: '节点', key: 'node' },
  { title: '状态', key: 'status', width: '110px' },
]
</script>

<template>
  <div>
    <PageHeader title="监控中心" subtitle="平台自身的负载清单与集群状态；时序曲线由既有的 Grafana 提供">
      <template #actions>
        <v-btn variant="text" prepend-icon="mdi-refresh" :loading="loading" @click="load">
          刷新
        </v-btn>
      </template>
    </PageHeader>

    <!--
      The active filter, stated rather than implied. Everything below is
      restricted to these namespaces, and a reader who does not know that reads a
      narrow view as the whole picture.
    -->
    <v-alert
      v-if="scope && !scope.unrestricted"
      :type="scope.empty ? 'warning' : 'info'"
      variant="tonal"
      density="comfortable"
      class="mb-4"
    >
      <template v-if="scope.empty">
        当前账号下没有正在运行的容器，因此没有可监控的对象。启动一个实例后即可在此查看。
      </template>
      <template v-else>
        监控范围已限定为你有权查看的命名空间：{{ scope.namespaces.join('、') }}
      </template>
    </v-alert>

    <v-alert
      v-if="overview && !overview.enabled"
      type="info"
      variant="tonal"
      density="comfortable"
      class="mb-4"
    >
      {{ overview.disabledReason }}
    </v-alert>

    <!-- The platform's own numbers. Available whatever Grafana is doing. -->
    <v-row dense class="mb-2">
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">节点</div>
          <div class="text-h5">
            {{ inventory ? `${inventory.nodesReady}/${inventory.nodes.length}` : '—' }}
          </div>
          <div class="text-caption text-medium-emphasis">就绪 / 总数</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">加速卡</div>
          <div class="text-h5">
            {{ inventory ? `${gpuTotals.allocated}/${gpuTotals.total}` : '—' }}
          </div>
          <div class="text-caption text-medium-emphasis">已分配 / 总数</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">运行中实例</div>
          <div class="text-h5">{{ runningCount }}</div>
          <div class="text-caption text-medium-emphasis">全平台</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">你能看到的容器</div>
          <div class="text-h5">{{ overview?.containers.length ?? 0 }}</div>
          <div class="text-caption text-medium-emphasis">
            {{ scope?.unrestricted ? '全部' : '按范围过滤' }}
          </div>
        </v-card>
      </v-col>
    </v-row>

    <!-- Grafana panels, one at a time: a wall of iframes is a wall of timers. -->
    <v-card v-if="overview?.enabled && overview.dashboards.length" variant="flat" class="wl-card mb-4">
      <v-card-title class="text-subtitle-1 pb-0">
        <v-chip-group
          :model-value="activeKey"
          mandatory
          selected-class="text-primary"
          @update:model-value="selectDashboard(String($event))"
        >
          <v-chip
            v-for="dashboard in overview.dashboards"
            :key="dashboard.key"
            :value="dashboard.key"
            :prepend-icon="dashboard.icon"
            variant="text"
            label
          >
            {{ dashboard.title }}
          </v-chip>
        </v-chip-group>
      </v-card-title>
      <v-card-text>
        <p v-if="activeDashboard" class="text-caption text-medium-emphasis mb-2">
          {{ activeDashboard.description }}
        </p>
        <!--
          `sandbox` is deliberately not set: Grafana needs scripts, same-origin
          storage and its own frames to render a panel, and a sandbox that breaks
          it would be security theatre rather than security. The actual control is
          the five-minute scoped token in the URL.
        -->
        <iframe
          v-if="activeUrl"
          :src="activeUrl"
          class="wl-frame"
          referrerpolicy="no-referrer"
          title="Grafana 面板"
        />
      </v-card-text>
    </v-card>

    <v-card
      v-else-if="overview && !overview.enabled"
      variant="flat"
      class="wl-card mb-4"
    >
      <v-card-text>
        <EmptyState
          icon="mdi-chart-box-outline"
          title="未接入 Grafana"
          description="时序曲线与利用率图表需要集群侧既有的 Grafana；上面的清单与统计不依赖它。"
        />
      </v-card-text>
    </v-card>

    <v-row dense>
      <v-col cols="12" lg="7">
        <v-card variant="flat" class="wl-card">
          <v-card-title class="text-subtitle-1">可见的容器</v-card-title>
          <v-card-text>
            <v-data-table
              :headers="containerHeaders"
              :items="overview?.containers ?? []"
              :loading="loading"
              item-value="id"
              hide-default-footer
              density="comfortable"
              class="wl-table"
            >
              <template #item.name="{ item }">
                <div class="d-flex flex-column">
                  <span>{{ item.displayName || item.name }}</span>
                  <span class="text-caption text-medium-emphasis wl-mono">{{ item.name }}</span>
                </div>
              </template>
              <template #item.gpu="{ item }">
                <span class="wl-mono">
                  {{ item.gpuCount > 0 ? `${item.gpuCount} × ${item.gpuType}` : '—' }}
                </span>
              </template>
              <template #item.pod="{ item }">
                <span class="wl-mono text-caption">{{ item.podName ?? '—' }}</span>
              </template>
              <template #item.node="{ item }">
                <span class="wl-mono text-caption">{{ item.nodeName ?? '—' }}</span>
              </template>
              <template #item.status="{ item }">
                <v-chip size="small" variant="tonal" label>
                  {{ instanceStatusLabel(item.status) }}
                </v-chip>
              </template>
            </v-data-table>
            <EmptyState
              v-if="!loading && !(overview?.containers ?? []).length"
              icon="mdi-cube-off-outline"
              title="没有正在运行的容器"
              description="启动一个实例后，它会出现在这里，并被纳入 Grafana 面板的过滤范围。"
            />
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" lg="5">
        <v-card variant="flat" class="wl-card mb-4">
          <v-card-title class="text-subtitle-1">加速卡</v-card-title>
          <v-card-text>
            <div
              v-for="entry in inventory?.gpuByModel ?? []"
              :key="entry.model"
              class="mb-3"
            >
              <div class="d-flex align-center">
                <span class="wl-mono">{{ entry.model }}</span>
                <v-spacer />
                <span class="text-caption text-medium-emphasis">
                  {{ entry.allocated }} / {{ entry.total }} 已分配
                </span>
              </div>
              <v-progress-linear
                :model-value="gpuUtilisation(entry)"
                height="6"
                rounded
                class="mt-1"
                color="primary"
              />
            </div>
            <EmptyState
              v-if="!(inventory?.gpuByModel ?? []).length"
              icon="mdi-expansion-card"
              title="没有登记的加速卡"
              description="节点同步后，物理卡会按型号汇总在这里。"
            />
          </v-card-text>
        </v-card>

        <v-card variant="flat" class="wl-card">
          <v-card-title class="text-subtitle-1">节点</v-card-title>
          <v-card-text>
            <v-list density="compact" class="pa-0">
              <v-list-item
                v-for="node in inventory?.nodes ?? []"
                :key="node.id"
                class="px-0"
              >
                <v-list-item-title class="wl-mono">{{ node.name }}</v-list-item-title>
                <v-list-item-subtitle>
                  {{ nodeStatusLabel(node.status) }} · {{ node.gpuCount }} 张卡 ·
                  {{ Math.round(node.cpuTotalMilli / 1000) }} 核
                </v-list-item-subtitle>
                <template #append>
                  <v-icon
                    v-if="!node.schedulable"
                    icon="mdi-cancel"
                    color="warning"
                    size="small"
                  />
                </template>
              </v-list-item>
            </v-list>
            <EmptyState
              v-if="!(inventory?.nodes ?? []).length"
              icon="mdi-server-off"
              title="没有同步到节点"
              description="平台尚未从集群拉取节点清单。"
            />
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-card {
  box-shadow: $card-shadow;
}

.wl-stat {
  padding: 14px 16px;
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.wl-frame {
  width: 100%;
  height: 460px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 6px;
}
</style>

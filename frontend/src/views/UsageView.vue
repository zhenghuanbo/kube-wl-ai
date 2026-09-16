<script setup lang="ts">
/**
 * The usage centre.
 *
 * Three things shape this page.
 *
 * **It reports consumption; it does not bill.** There are no prices anywhere,
 * and the headline unit is GPU-hours — a measure of how much of the cluster a
 * lab held, not of what it owes. Adding money later is a different feature with
 * different correctness requirements.
 *
 * **Every total is computed on the server, inside the caller's data scope.** The
 * page therefore never filters rows to enforce privacy; it decides what is worth
 * showing. A group administrator's figures cover their group because the query
 * did, not because a component hid something.
 *
 * **Zero is ambiguous until you say which zero it is.** "No usage" and "the
 * rollup has not run" both render as an empty chart, and they mean opposite
 * things. The `covered` flag is surfaced prominently for that reason: a flat line
 * across a month the platform never measured is a lie that looks like data.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  downloadUsageCsv,
  getUsageBreakdown,
  getUsageMeta,
  getUsageSeries,
  getUsageSummary,
  rollupUsage,
} from '@/api/usage'
import EChart from '@/components/common/EChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type {
  UsageBreakdown,
  UsageDimension,
  UsageKind,
  UsageMeta,
  UsageQuery,
  UsageSummary,
} from '@/types/usage'
import {
  USAGE_KINDS_FALLBACK,
  USAGE_RANGES,
  formatGpuHours,
  formatRuntime,
  rangeStart,
  todayUtc,
} from '@/types/usage'

const { can } = usePermissions()
const notifications = useNotificationStore()

const canExport = computed(() => can('usage:export'))
const canRollup = computed(() => can('usage:manage'))

const meta = ref<UsageMeta | null>(null)
const summary = ref<UsageSummary | null>(null)
const breakdown = ref<UsageBreakdown | null>(null)
const points = ref<{ day: string; hours: number }[]>([])
const loading = ref(false)
const exporting = ref(false)
const rollingUp = ref(false)

const filters = reactive({
  range: '30d',
  kind: '' as UsageKind | '',
  dimension: 'user' as UsageDimension,
})

const kinds = computed(() => meta.value?.kinds ?? USAGE_KINDS_FALLBACK)

const query = computed<UsageQuery>(() => {
  const range = USAGE_RANGES.find((item) => item.key === filters.range) ?? USAGE_RANGES[1]!
  const params: UsageQuery = {
    dayFrom: rangeStart(range.days),
    dayTo: todayUtc(),
  }
  if (filters.kind) params.workloadKind = filters.kind
  return params
})

const totals = computed(() => summary.value?.totals ?? null)
const windowLabel = computed(() =>
  summary.value ? `${summary.value.dayFrom} 至 ${summary.value.dayTo}` : '',
)

/**
 * The caller's own share of the totals, or `null` when it would be redundant.
 *
 * An administrator's own consumption beside the group's answers "how much of
 * this is me", and that question cannot be answered once the rows have been
 * summed — so the server computes it separately. A normal user *is* the self
 * case, and the server sends `null` rather than repeating the same figures in a
 * second panel.
 */
const selfTotals = computed(() => summary.value?.selfTotals ?? null)

/** Share of the window's GPU-hours, as a whole percent. `null` if meaningless. */
const shareOfTotal = computed(() => {
  if (!selfTotals.value || !totals.value || totals.value.gpuSeconds === 0) return null
  return Math.round((selfTotals.value.gpuSeconds / totals.value.gpuSeconds) * 100)
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const params = query.value
    const [nextSummary, series, byDimension] = await Promise.all([
      getUsageSummary(params),
      getUsageSeries(params),
      getUsageBreakdown(filters.dimension, params, 20),
    ])
    summary.value = nextSummary
    points.value = series.points.map((point) => ({
      day: point.day,
      hours: point.totals.gpuHours,
    }))
    breakdown.value = byDimension
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

watch(() => [filters.range, filters.kind], () => void load())
watch(() => filters.dimension, () => void load())

onMounted(async () => {
  try {
    meta.value = await getUsageMeta()
  } catch {
    // The meta endpoint only supplies filter vocabulary; the page is still
    // usable with the built-in defaults, so a failure here must not blank it.
    meta.value = null
  }
  await load()
})

// --- charts ------------------------------------------------------------------
const TREND_COLOR = '#536DFE'
const BAR_COLOR = '#3CD4A0'

const trendOption = computed(() => ({
  grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
  tooltip: {
    trigger: 'axis',
    // The unit is stated so nobody reads the figure as money. ECharts types
    // this argument as a deep union; coercing is honest, narrowing is not.
    valueFormatter: (value: unknown) => `${formatGpuHours(Number(value))} GPU·h`,
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: points.value.map((point) => point.day.slice(5)),
    axisLine: { lineStyle: { color: '#E8EAFC' } },
    axisLabel: { color: '#b9b9b9' },
  },
  yAxis: {
    type: 'value',
    name: 'GPU·h',
    nameTextStyle: { color: '#b9b9b9' },
    splitLine: { lineStyle: { color: '#F0F1FA' } },
    axisLabel: { color: '#b9b9b9' },
  },
  series: [
    {
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: points.value.map((point) => point.hours),
      lineStyle: { width: 2, color: TREND_COLOR },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0,
          y: 0,
          x2: 0,
          y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(83,109,254,0.28)' },
            { offset: 1, color: 'rgba(83,109,254,0.02)' },
          ],
        },
      },
    },
  ],
}))

const breakdownOption = computed(() => {
  const rows = (breakdown.value?.rows ?? []).slice(0, 10).reverse()
  return {
    grid: { left: 8, right: 32, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (value: unknown) => `${formatGpuHours(Number(value))} GPU·h`,
    },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#F0F1FA' } },
      axisLabel: { color: '#b9b9b9' },
    },
    yAxis: {
      type: 'category',
      data: rows.map((row) => row.label),
      axisLine: { lineStyle: { color: '#E8EAFC' } },
      axisLabel: { color: '#6e6e6e' },
    },
    series: [
      {
        type: 'bar',
        data: rows.map((row) => row.totals.gpuHours),
        barMaxWidth: 18,
        itemStyle: { color: BAR_COLOR, borderRadius: [0, 4, 4, 0] },
      },
    ],
  }
})

const hasTrend = computed(() => points.value.some((point) => point.hours > 0))
const hasBreakdown = computed(() =>
  (breakdown.value?.rows ?? []).some((row) => row.totals.gpuSeconds > 0),
)

// --- actions -----------------------------------------------------------------
async function exportCsv(): Promise<void> {
  exporting.value = true
  try {
    await downloadUsageCsv(query.value)
    notifications.success('用量数据已开始下载')
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    exporting.value = false
  }
}

async function recompute(): Promise<void> {
  rollingUp.value = true
  try {
    const result = await rollupUsage({ dayFrom: query.value.dayFrom, dayTo: query.value.dayTo })
    notifications.success(
      `已重算 ${result.dayFrom} 至 ${result.dayTo}：写入 ${result.rowsWritten} 条记录`,
    )
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    rollingUp.value = false
  }
}

interface Column {
  title: string
  key: string
  align?: 'start' | 'end'
  width?: string
}

const headers: Column[] = [
  { title: '对象', key: 'label' },
  { title: 'GPU 卡时', key: 'gpu', align: 'end', width: '130px' },
  { title: 'CPU 核时', key: 'cpu', align: 'end', width: '130px' },
  { title: '内存 GB·时', key: 'memory', align: 'end', width: '140px' },
  { title: '运行时长', key: 'runtime', align: 'end', width: '120px' },
  { title: '负载数', key: 'count', align: 'end', width: '90px' },
]

function kindLabel(value: string): string {
  return kinds.value.find((kind) => kind.value === value)?.label ?? value
}

/** The instance/task split, rendered as a short readable string. */
function kindsSummary(row: { kinds: Record<string, number> }): string {
  const entries = Object.entries(row.kinds).filter(([, seconds]) => seconds > 0)
  if (!entries.length) return ''
  return entries.map(([kind, seconds]) => `${kindLabel(kind)} ${formatGpuHours(seconds / 3600)}`).join(' · ')
}
</script>

<template>
  <div>
    <PageHeader title="用量中心" subtitle="统计集群资源消耗，折算为卡时；仅作计量，不做计费">
      <template #actions>
        <v-btn
          v-if="canRollup"
          variant="text"
          prepend-icon="mdi-refresh"
          :loading="rollingUp"
          @click="recompute"
        >
          重算用量
        </v-btn>
        <v-btn
          v-if="canExport"
          color="primary"
          variant="flat"
          prepend-icon="mdi-download"
          :loading="exporting"
          @click="exportCsv"
        >
          导出 CSV
        </v-btn>
      </template>
    </PageHeader>

    <!--
      The rollup having never run is not the same as nothing having been used,
      and the two look identical on an empty chart. Said out loud, once, at the
      top — a flat line across a month that was never measured reads as "steady
      usage", which is the opposite of the truth.
    -->
    <v-alert
      v-if="summary && !summary.covered"
      type="info"
      variant="tonal"
      density="comfortable"
      class="mb-4"
    >
      该时间段内还没有用量汇总数据。这表示平台尚未统计，而不是没有消耗；请点击「重算用量」生成。
    </v-alert>

    <FilterBar>
      <v-btn-toggle v-model="filters.range" mandatory density="comfortable" variant="outlined">
        <v-btn v-for="range in USAGE_RANGES" :key="range.key" :value="range.key" size="small">
          {{ range.label }}
        </v-btn>
      </v-btn-toggle>
      <v-select
        v-model="filters.kind"
        :items="kinds"
        item-title="label"
        item-value="value"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        placeholder="全部负载类型"
        style="max-width: 200px"
      />
      <span v-if="windowLabel" class="text-caption text-medium-emphasis">{{ windowLabel }}</span>
    </FilterBar>

    <!--
      "What you are charged for", in the only sense this system has: the hours
      actually held. The reserved-time card is beside the GPU-hours card on
      purpose — one GPU for ten hours and ten GPUs for one hour are the same
      number of GPU-hours and very different shapes of work.
    -->
    <v-row dense class="mb-2">
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">GPU 卡时</div>
          <div class="text-h5">{{ totals ? formatGpuHours(totals.gpuHours) : '—' }}</div>
          <div class="text-caption text-medium-emphasis">GPU·h</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">CPU 核时</div>
          <div class="text-h5">{{ totals ? totals.cpuCoreHours.toFixed(1) : '—' }}</div>
          <div class="text-caption text-medium-emphasis">core·h</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">内存 GB·时</div>
          <div class="text-h5">{{ totals ? totals.memoryGbHours.toFixed(1) : '—' }}</div>
          <div class="text-caption text-medium-emphasis">GB·h</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">占用时长</div>
          <div class="text-h5">{{ totals ? formatRuntime(totals.runtimeSeconds) : '—' }}</div>
          <div class="text-caption text-medium-emphasis">
            {{ totals ? `${totals.workloadCount} 个负载` : '' }}
          </div>
        </v-card>
      </v-col>
    </v-row>

    <!--
      An administrator gets their own share of the total underneath, because
      "how much of this is me" is the first question they ask of a group report
      and the answer cannot be derived once the rows have been summed.
    -->
    <v-card v-if="selfTotals" variant="flat" class="wl-card mb-4">
      <v-card-text class="d-flex align-center flex-wrap ga-6">
        <div>
          <div class="text-caption text-medium-emphasis">其中你自己</div>
          <div class="text-h6">{{ formatGpuHours(selfTotals.gpuHours) }} GPU·h</div>
        </div>
        <div v-if="shareOfTotal !== null">
          <div class="text-caption text-medium-emphasis">占总量</div>
          <div class="text-h6">{{ shareOfTotal }}%</div>
        </div>
        <div>
          <div class="text-caption text-medium-emphasis">你的负载数</div>
          <div class="text-h6">{{ selfTotals.workloadCount }}</div>
        </div>
        <v-spacer />
        <div class="text-caption text-medium-emphasis">
          共 {{ totals?.userCount ?? 0 }} 人 / {{ totals?.workspaceCount ?? 0 }} 个工作空间
        </div>
      </v-card-text>
    </v-card>

    <v-row dense>
      <v-col cols="12" lg="7">
        <v-card variant="flat" class="wl-card">
          <v-card-title class="text-subtitle-1">每日用量</v-card-title>
          <v-card-text>
            <EChart v-if="hasTrend" :option="trendOption" height="300px" :loading="loading" />
            <EmptyState
              v-else
              icon="mdi-chart-timeline-variant"
              title="该时间段没有用量"
              description="选中的区间内没有负载运行，或用量尚未汇总。"
            />
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" lg="5">
        <v-card variant="flat" class="wl-card">
          <v-card-title class="text-subtitle-1 d-flex align-center">
            <span>构成</span>
            <v-spacer />
            <v-select
              v-model="filters.dimension"
              :items="meta?.dimensions ?? []"
              item-title="label"
              item-value="key"
              density="compact"
              variant="outlined"
              hide-details
              style="max-width: 170px"
            />
          </v-card-title>
          <v-card-text>
            <EChart
              v-if="hasBreakdown"
              :option="breakdownOption"
              height="300px"
              :loading="loading"
            />
            <EmptyState
              v-else
              icon="mdi-chart-bar"
              title="暂无可拆分的数据"
              description="换个时间范围或负载类型试试。"
            />
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-card variant="flat" class="wl-card mt-4">
      <v-card-title class="text-subtitle-1 d-flex align-center">
        <span>明细</span>
        <v-spacer />
        <span v-if="breakdown?.truncated" class="text-caption text-medium-emphasis">
          仅显示前 {{ breakdown.rows.length }} 项
        </span>
      </v-card-title>
      <v-card-text>
        <v-data-table
          :headers="headers"
          :items="breakdown?.rows ?? []"
          :loading="loading"
          item-value="key"
          hide-default-footer
          density="comfortable"
          class="wl-table"
        >
          <template #item.label="{ item }">
            <div class="d-flex flex-column">
              <span>{{ item.label }}</span>
              <span v-if="kindsSummary(item)" class="text-caption text-medium-emphasis">
                {{ kindsSummary(item) }}
              </span>
            </div>
          </template>
          <template #item.gpu="{ item }">
            <span class="wl-mono">{{ formatGpuHours(item.totals.gpuHours) }}</span>
          </template>
          <template #item.cpu="{ item }">
            <span class="wl-mono">{{ item.totals.cpuCoreHours.toFixed(2) }}</span>
          </template>
          <template #item.memory="{ item }">
            <span class="wl-mono">{{ item.totals.memoryGbHours.toFixed(2) }}</span>
          </template>
          <template #item.runtime="{ item }">
            <span class="wl-mono">{{ formatRuntime(item.totals.runtimeSeconds) }}</span>
          </template>
          <template #item.count="{ item }">
            <span class="wl-mono">{{ item.totals.workloadCount }}</span>
          </template>
        </v-data-table>
        <EmptyState
          v-if="!loading && !(breakdown?.rows ?? []).length"
          icon="mdi-table-off"
          title="没有可显示的记录"
          description="调整筛选条件，或确认用量汇总是否已经运行。"
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

.wl-stat {
  padding: 14px 16px;
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.wl-table :deep(td) {
  white-space: nowrap;
}
</style>

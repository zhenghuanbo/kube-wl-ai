<script setup lang="ts">
/**
 * Audit trail.
 *
 * A reading screen for a security record, and three decisions follow from that.
 *
 * **`requestId` is a first-class column, not a detail.** Every log line the
 * application emitted while handling a request carries the same id, so this is
 * the value that turns "something failed at 14:02" into a specific trace. It is
 * also clickable, because the way you use it is to copy it.
 *
 * **Unattributed rows are marked, not hidden.** A failed login has no verified
 * actor — the name on it is whatever was typed. Rendering that as a normal
 * username would put an attacker's chosen string in the same column as a real
 * identity, so such rows are tagged and the difference is stated in the tooltip.
 *
 * **Nothing here writes.** No delete, no clear, no edit. The API refuses those
 * methods, and this page does not offer them.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { downloadAuditCsv, getAuditMeta, listAuditLogs } from '@/api/audit'
import { ApiError } from '@/api/client'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useNotificationStore } from '@/stores/notifications'
import type { AuditEntry, AuditMeta, AuditQuery } from '@/types/audit'
import { actionLabel, isAttributed, statusColor, statusLabel } from '@/types/audit'

const notifications = useNotificationStore()

const entries = ref<AuditEntry[]>([])
const meta = ref<AuditMeta>({ actions: [], objectTypes: [], statuses: [] })
const loading = ref(false)
const exporting = ref(false)
const total = ref(0)

const filters = reactive<{
  search: string
  action: string
  status: string
  actorUsername: string
  requestId: string
}>({
  search: '',
  action: '',
  status: '',
  actorUsername: '',
  requestId: '',
})

const page = ref(1)
const pageSize = ref(20)

/** The non-empty filters, in the API's camelCase query-key spelling. */
const query = computed<AuditQuery>(() => {
  const built: AuditQuery = { page: page.value, pageSize: pageSize.value }
  if (filters.search) built.search = filters.search
  if (filters.action) built.action = filters.action
  if (filters.status) built.status = filters.status as AuditQuery['status']
  if (filters.actorUsername) built.actorUsername = filters.actorUsername
  if (filters.requestId) built.requestId = filters.requestId
  return built
})

const hasFilters = computed(() =>
  Boolean(
    filters.search ||
      filters.action ||
      filters.status ||
      filters.actorUsername ||
      filters.requestId,
  ),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listAuditLogs(query.value)
    entries.value = result.items
    total.value = result.meta.total
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

async function loadMeta(): Promise<void> {
  try {
    meta.value = await getAuditMeta()
  } catch (error) {
    // The filters degrade to free text; the table itself still works.
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

onMounted(() => {
  void loadMeta()
  void load()
})

/**
 * Re-query when a filter changes, but not on every keystroke.
 *
 * The two text filters are debounced; the selects apply at once because a
 * selection is a completed decision, where a half-typed username is not.
 */
let debounce: ReturnType<typeof setTimeout> | null = null

watch(
  () => [filters.search, filters.actorUsername, filters.requestId],
  () => {
    if (debounce) clearTimeout(debounce)
    debounce = setTimeout(() => {
      page.value = 1
      void load()
    }, 300)
  },
)

watch(
  () => [filters.action, filters.status, page.value, pageSize.value],
  () => {
    void load()
  },
)

function reset(): void {
  filters.search = ''
  filters.action = ''
  filters.status = ''
  filters.actorUsername = ''
  filters.requestId = ''
  page.value = 1
}

function focusRequestId(value: string | null): void {
  if (!value) return
  filters.requestId = value
  // The watcher debounces, but this is an explicit click and should feel
  // immediate; setting it again after the debounce would be wasted work.
  if (debounce) clearTimeout(debounce)
  page.value = 1
  void load()
}

async function exportCsv(): Promise<void> {
  exporting.value = true
  try {
    await downloadAuditCsv(query.value)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    exporting.value = false
  }
}

const headers = [
  { title: '时间', key: 'createdAt', width: '170px' },
  { title: '操作者', key: 'actorUsername', width: '160px' },
  { title: '操作', key: 'action', width: '180px' },
  { title: '对象', key: 'object', width: '180px' },
  { title: '结果', key: 'status', width: '100px' },
  { title: '来源 IP', key: 'ipAddress', width: '140px' },
  { title: '请求 ID', key: 'requestId', width: '150px' },
]

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function objectLabel(entry: AuditEntry): string {
  if (!entry.objectType) return '—'
  return entry.objectId ? `${entry.objectType}#${entry.objectId}` : entry.objectType
}

const statusItems = computed(() => [
  { title: '全部结果', value: '' },
  ...meta.value.statuses.map((value) => ({ title: statusLabel(value), value })),
])

const actionItems = computed(() => [
  { title: '全部操作', value: '' },
  ...meta.value.actions.map((value) => ({ title: `${actionLabel(value)}（${value}）`, value })),
])
</script>

<template>
  <div>
    <PageHeader title="审计日志" subtitle="全部写操作、越权尝试与登录事件；只读，且按数据范围过滤">
      <template #actions>
        <v-btn
          variant="text"
          prepend-icon="mdi-download"
          :loading="exporting"
          @click="exportCsv"
        >
          导出 CSV
        </v-btn>
        <v-btn variant="text" prepend-icon="mdi-refresh" :loading="loading" @click="load">
          刷新
        </v-btn>
      </template>
    </PageHeader>

    <v-card variant="flat" class="wl-card mb-4">
      <v-card-text>
        <v-row dense>
          <v-col cols="12" md="4">
            <v-text-field
              v-model="filters.search"
              label="搜索"
              density="compact"
              variant="outlined"
              hide-details
              prepend-inner-icon="mdi-magnify"
              placeholder="操作者、操作、对象或错误信息"
            />
          </v-col>
          <v-col cols="12" sm="6" md="2">
            <v-select
              v-model="filters.action"
              :items="actionItems"
              label="操作"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="6" md="2">
            <v-select
              v-model="filters.status"
              :items="statusItems"
              label="结果"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="6" md="2">
            <v-text-field
              v-model="filters.actorUsername"
              label="操作者"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="6" md="2">
            <v-text-field
              v-model="filters.requestId"
              label="请求 ID"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
        </v-row>
        <div v-if="hasFilters" class="d-flex justify-end mt-2">
          <v-btn size="small" variant="text" @click="reset">清除筛选</v-btn>
        </div>
      </v-card-text>
    </v-card>

    <v-card variant="flat" class="wl-card">
      <v-card-text>
        <v-data-table-server
          :headers="headers"
          :items="entries"
          :items-length="total"
          :loading="loading"
          v-model:page="page"
          v-model:items-per-page="pageSize"
          :items-per-page-options="[20, 50, 100]"
          density="comfortable"
          item-value="id"
          class="wl-table"
        >
          <template #item.createdAt="{ item }">
            <span class="wl-mono text-caption">{{ formatTime(item.createdAt) }}</span>
          </template>

          <template #item.actorUsername="{ item }">
            <div class="d-flex align-center">
              <span class="wl-mono">{{ item.actorUsername }}</span>
              <!--
                An unattributed row carries a *claimed* name, not a proven one.
                Marking it is the difference between "dana did this" and
                "somebody typed dana".
              -->
              <v-tooltip v-if="!isAttributed(item)" location="top">
                <template #activator="{ props: tip }">
                  <v-icon
                    v-bind="tip"
                    icon="mdi-help-circle-outline"
                    size="x-small"
                    color="warning"
                    class="ms-1"
                  />
                </template>
                未通过认证：该名称是尝试者填写的，不是已验证的身份
              </v-tooltip>
            </div>
          </template>

          <template #item.action="{ item }">
            <div class="d-flex flex-column">
              <span>{{ actionLabel(item.action) }}</span>
              <!--
                The raw code, but only when it differs from the label. An
                unmapped action shows the code alone rather than the same string
                twice, and a mapped one still shows the code because that is what
                an operator greps the application logs for.
              -->
              <span
                v-if="actionLabel(item.action) !== item.action"
                class="text-caption text-medium-emphasis wl-mono"
              >
                {{ item.action }}
              </span>
            </div>
          </template>

          <template #item.object="{ item }">
            <span class="wl-mono text-caption">{{ objectLabel(item) }}</span>
          </template>

          <template #item.status="{ item }">
            <v-chip size="small" variant="tonal" label :color="statusColor(item.status)">
              {{ statusLabel(item.status) }}
            </v-chip>
          </template>

          <template #item.ipAddress="{ item }">
            <span class="wl-mono text-caption">{{ item.ipAddress ?? '—' }}</span>
          </template>

          <template #item.requestId="{ item }">
            <v-btn
              v-if="item.requestId"
              size="x-small"
              variant="text"
              class="wl-mono"
              @click="focusRequestId(item.requestId)"
            >
              {{ item.requestId.slice(0, 8) }}
            </v-btn>
            <span v-else class="text-caption">—</span>
          </template>
        </v-data-table-server>

        <EmptyState
          v-if="!loading && !entries.length"
          icon="mdi-clipboard-text-clock-outline"
          :title="hasFilters ? '没有符合条件的记录' : '还没有审计记录'"
          :description="
            hasFilters
              ? '换一组条件，或清除筛选后重试。'
              : '写操作、越权尝试与登录事件都会记录在这里。'
          "
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
</style>

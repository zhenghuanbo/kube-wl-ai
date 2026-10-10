<script setup lang="ts">
/**
 * Custom tasks — a shell command that runs to completion.
 *
 * The page is built around the fact that a task *ends*. That drives three
 * choices a table of instances would not need:
 *
 * 1. **It polls while anything is unfinished.** A user watching a job cannot be
 *    asked to hit refresh, and the backend's sweep only runs every 30 seconds.
 *    Polling stops as soon as nothing is live, so an idle page is silent.
 * 2. **Runtime and exit code are first-class columns.** They are the two things
 *    a task is judged on.
 * 3. **Logs and the timeline share the detail drawer.** "It failed" and "why"
 *    should not be two navigations apart.
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { getResourceGroup } from '@/api/resources'
import { listAttachableStorages } from '@/api/storages'
import {
  cancelTask,
  createTask,
  deleteTask,
  getTask,
  getTaskLogs,
  listTaskEvents,
  listTasks,
  reconcileTask,
  updateTask,
} from '@/api/tasks'
import { getMyWorkspace, listWorkspaceGrants, listWorkspaces } from '@/api/workspaces'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import ImagePicker from '@/components/image/ImagePicker.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { WorkloadEvent } from '@/types/application'
import type { ResourceGroupDetail, ResourceSpec, WorkspaceSummary } from '@/types/resource'
import { formatCpu, formatDuration, formatMemory } from '@/types/resource'
import { storageKindMeta, type StorageSummary } from '@/types/storage'
import type { TaskDetail, TaskStatus, TaskSummary } from '@/types/task'
import { SCRIPT_PRESETS, isTerminal } from '@/types/task'

const { can, isAdmin } = usePermissions()
const notifications = useNotificationStore()

const canCreate = computed(() => can('task:create'))
const canCancel = computed(() => can('task:cancel'))
const canLogs = computed(() => can('task:logs'))

/** Refresh cadence while something is still running. */
const POLL_MS = 5000

const STATUS_OPTIONS: { value: TaskStatus; title: string }[] = [
  { value: 'running', title: '运行中' },
  { value: 'succeeded', title: '已完成' },
  { value: 'failed', title: '失败' },
  { value: 'timeout', title: '已超时' },
  { value: 'cancelled', title: '已取消' },
  { value: 'error', title: '异常' },
]

// --- listing -----------------------------------------------------------------
const rows = ref<TaskSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  status: null as TaskStatus | null,
  workspaceId: null as number | null,
  includeInactive: false,
})

interface Column {
  title: string
  key: string
  sortable: boolean
  align?: 'start' | 'end'
  width?: string
}

const headers = computed<Column[]>(() => {
  const base: Column[] = [
    { title: '任务', key: 'displayName', sortable: false },
    { title: '规格', key: 'shape', sortable: false },
    { title: '状态', key: 'status', sortable: false },
    { title: '耗时', key: 'duration', sortable: false, width: '110px' },
  ]
  if (isAdmin.value) {
    base.push(
      { title: '工作空间', key: 'workspaceName', sortable: false },
      { title: '提交人', key: 'ownerUsername', sortable: false },
    )
  }
  base.push({ title: '操作', key: 'actions', sortable: false, align: 'end', width: '150px' })
  return base
})

/** True while at least one row could still change, which is when polling pays. */
const hasLiveTask = computed(() => rows.value.some((task) => !isTerminal(task.status)))

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const result = await listTasks({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      status: filters.status ?? undefined,
      workspaceId: filters.workspaceId ?? undefined,
      includeInactive: filters.includeInactive || undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // A background poll failing should not shout at the user every 5 seconds;
    // the visible list simply keeps its last good contents.
    if (!silent) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

let timer: ReturnType<typeof setInterval> | undefined

function syncPolling(): void {
  if (timer !== undefined) {
    clearInterval(timer)
    timer = undefined
  }
  if (!hasLiveTask.value) return
  timer = setInterval(() => {
    // A hidden tab does not need fresh data, and browsers throttle timers
    // there anyway — skipping outright is cheaper and more honest.
    if (document.visibilityState === 'visible') void load(true)
  }, POLL_MS)
}

watch(hasLiveTask, syncPolling)
onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer)
  clearTimeout(searchTimer)
})

let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => filters.search,
  () => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      page.value = 1
      void load()
    }, 300)
  },
)
watch(
  () => [filters.status, filters.workspaceId, filters.includeInactive, page.value, pageSize.value],
  () => void load(),
)

// --- shared lookups ----------------------------------------------------------
const myWorkspace = ref<WorkspaceSummary | null>(null)
const workspaces = ref<WorkspaceSummary[]>([])

async function loadWorkspaces(): Promise<void> {
  try {
    if (isAdmin.value) {
      workspaces.value = (await listWorkspaces({ pageSize: 200 })).items
    } else {
      myWorkspace.value = await getMyWorkspace()
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

onMounted(async () => {
  await loadWorkspaces()
  await load()
})

// --- submit ------------------------------------------------------------------
interface SubmitForm {
  name: string
  displayName: string
  workspaceId: number | null
  resourceGroupId: number | null
  resourceSpecId: number | null
  image: string
  script: string
  /** Extra volumes to mount, beyond the home volume that is always mounted. */
  storageIds: number[]
  maxRuntimeSeconds: number | null
}

const submitOpen = ref(false)
const submitting = ref(false)
const submitError = ref('')
const form = reactive<SubmitForm>({
  name: '',
  displayName: '',
  workspaceId: null,
  resourceGroupId: null,
  resourceSpecId: null,
  image: '',
  script: SCRIPT_PRESETS[0].value,
  storageIds: [],
  maxRuntimeSeconds: null,
})

const grants = ref<{ resourceGroupId: number; displayName: string; isActive: boolean }[]>([])
const selectedPool = ref<ResourceGroupDetail | null>(null)

/**
 * Volumes this task could mount, beyond the home volume it always gets.
 *
 * Personal homes are excluded: one is mounted automatically, and asking for it
 * explicitly is refused by the API — offering it would be offering an action that
 * always fails.
 */
const attachableStorages = ref<StorageSummary[]>([])
const loadingStorages = ref(false)

async function loadAttachable(): Promise<void> {
  const workspaceId = form.workspaceId
  if (workspaceId === null) {
    attachableStorages.value = []
    return
  }
  loadingStorages.value = true
  try {
    attachableStorages.value = await listAttachableStorages(workspaceId)
  } catch {
    // Optional. Failing to load the list must not stop a task being submitted
    // without any extra mounts.
    attachableStorages.value = []
  } finally {
    loadingStorages.value = false
  }
}

// Reload on workspace change and drop a selection that no longer belongs to it: a
// volume from another namespace cannot be mounted here at all.
watch(
  () => form.workspaceId,
  () => {
    form.storageIds = []
    void loadAttachable()
  },
)

function storageSubtitle(row: StorageSummary): string {
  const parts = [row.defaultMountPath, `${row.capacityGb} GB`]
  if (row.isReadOnly) parts.push('只读')
  parts.push(storageKindMeta(row.kind).title)
  return parts.join(' · ')
}

const availableSpecs = computed<ResourceSpec[]>(() =>
  (selectedPool.value?.specs ?? []).filter((spec) => spec.isActive),
)

const runtimeOptions = computed(() => {
  const cap = selectedPool.value?.maxRuntimeSeconds ?? null
  const steps = [1, 2, 4, 8, 12, 24, 48, 72].map((hours) => hours * 3600)
  const allowed = cap === null ? steps : steps.filter((seconds) => seconds <= cap)
  return [
    ...allowed.map((seconds) => ({ value: seconds, title: formatDuration(seconds) ?? '' })),
    {
      value: null,
      title: cap === null ? '不限制' : `资源组上限（${formatDuration(cap) ?? ''}）`,
    },
  ]
})

const submitCanSubmit = computed(
  () =>
    /^[a-z0-9][a-z0-9-]*$/.test(form.name) &&
    form.name.length <= 52 &&
    form.image.trim().length > 0 &&
    form.script.trim().length > 0 &&
    form.resourceGroupId !== null &&
    form.resourceSpecId !== null &&
    (isAdmin.value ? form.workspaceId !== null : true),
)

function openSubmit(): void {
  submitError.value = ''
  submitOpen.value = true
  grants.value = []
  selectedPool.value = null
  Object.assign(form, {
    name: '',
    displayName: '',
    workspaceId: isAdmin.value ? null : (myWorkspace.value?.id ?? null),
    resourceGroupId: null,
    resourceSpecId: null,
    image: '',
    script: SCRIPT_PRESETS[0].value,
    storageIds: [],
    maxRuntimeSeconds: null,
  })
  void loadGrants(form.workspaceId)
}

async function loadGrants(workspaceId: number | null): Promise<void> {
  grants.value = []
  selectedPool.value = null
  form.resourceGroupId = null
  form.resourceSpecId = null
  if (workspaceId === null) return
  try {
    grants.value = (await listWorkspaceGrants(workspaceId)).filter((grant) => grant.isActive)
    if (grants.value.length === 1) await selectPool(grants.value[0].resourceGroupId)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function selectPool(poolId: number | null): Promise<void> {
  form.resourceGroupId = poolId
  form.resourceSpecId = null
  selectedPool.value = null
  if (poolId === null) return
  try {
    selectedPool.value = await getResourceGroup(poolId)
    if (availableSpecs.value.length === 1) form.resourceSpecId = availableSpecs.value[0].id
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

watch(
  () => form.workspaceId,
  (workspaceId, previous) => {
    if (!submitOpen.value || previous === undefined) return
    void loadGrants(workspaceId)
  },
)

async function submit(): Promise<void> {
  if (!submitCanSubmit.value) return
  submitting.value = true
  submitError.value = ''
  try {
    const task = await createTask({
      name: form.name.trim(),
      displayName: form.displayName.trim() || form.name.trim(),
      workspaceId: form.workspaceId,
      resourceGroupId: form.resourceGroupId!,
      resourceSpecId: form.resourceSpecId!,
      image: form.image.trim(),
      script: form.script,
      storageIds: form.storageIds,
      maxRuntimeSeconds: form.maxRuntimeSeconds,
    })
    notifications.success(`任务 ${task.name} 已提交`)
    submitOpen.value = false
    page.value = 1
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // Kept in the dialog: a quota refusal names a resource, and the form is
    // where the user can do something about it.
    submitError.value = error.message
  } finally {
    submitting.value = false
  }
}

// --- detail ------------------------------------------------------------------
const detailOpen = ref(false)
const detailLoading = ref(false)
const detailTab = ref<'overview' | 'logs' | 'timeline'>('overview')
const detail = ref<TaskDetail | null>(null)
const logs = ref('')
const logsLoading = ref(false)
const logsPrevious = ref(false)
const events = ref<WorkloadEvent[]>([])
const eventsLoading = ref(false)

async function openDetail(id: number, tab: 'overview' | 'logs' | 'timeline' = 'overview'): Promise<void> {
  detailOpen.value = true
  detailTab.value = tab
  detailLoading.value = true
  detail.value = null
  logs.value = ''
  events.value = []
  try {
    detail.value = await getTask(id)
    await Promise.all([refreshEvents(), tab === 'logs' ? loadLogs() : Promise.resolve()])
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

/** Ask the backend to look at the cluster now, rather than waiting for the sweep. */
async function refreshDetail(): Promise<void> {
  if (!detail.value) return
  try {
    detail.value = await reconcileTask(detail.value.id)
    await Promise.all([refreshEvents(), detailTab.value === 'logs' ? loadLogs() : Promise.resolve()])
    await load(true)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function loadLogs(): Promise<void> {
  if (!detail.value) return
  logsLoading.value = true
  try {
    const result = await getTaskLogs(detail.value.id, {
      tailLines: 1000,
      previous: logsPrevious.value,
    })
    logs.value = result.logs || '（暂无输出）'
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // A task with no container yet is a normal state, not a failure; show the
    // reason instead of an empty console the user would read as "no output".
    logs.value = error.message
  } finally {
    logsLoading.value = false
  }
}

async function refreshEvents(): Promise<void> {
  if (!detail.value) return
  eventsLoading.value = true
  try {
    events.value = await listTaskEvents(detail.value.id)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    eventsLoading.value = false
  }
}

watch(logsPrevious, () => {
  if (detailTab.value === 'logs') void loadLogs()
})
watch(detailTab, (tab) => {
  if (tab === 'logs' && !logs.value) void loadLogs()
  if (tab === 'timeline' && events.value.length === 0) void refreshEvents()
})

onBeforeUnmount(() => clearTimeout(renameTimer))

// --- rename ------------------------------------------------------------------
const renameOpen = ref(false)
const renaming = ref(false)
const renameTarget = ref<TaskSummary | null>(null)
const renameValue = ref('')
let renameTimer: ReturnType<typeof setTimeout> | undefined

function openRename(task: TaskSummary): void {
  renameTarget.value = task
  renameValue.value = task.displayName
  renameOpen.value = true
}

async function confirmRename(): Promise<void> {
  if (!renameTarget.value) return
  renaming.value = true
  try {
    const updated = await updateTask(renameTarget.value.id, { displayName: renameValue.value.trim() })
    if (detail.value?.id === updated.id) detail.value = updated
    notifications.success('已重命名')
    renameOpen.value = false
    await load(true)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    renaming.value = false
  }
}

// --- cancel / delete ---------------------------------------------------------
const cancelOpen = ref(false)
const cancelling = ref(false)
const cancelTarget = ref<TaskSummary | null>(null)

function askCancel(task: TaskSummary): void {
  cancelTarget.value = task
  cancelOpen.value = true
}

async function confirmCancel(): Promise<void> {
  if (!cancelTarget.value) return
  cancelling.value = true
  try {
    await cancelTask(cancelTarget.value.id)
    notifications.success('任务已取消')
    cancelOpen.value = false
    if (detail.value?.id === cancelTarget.value.id) {
      detail.value = await getTask(cancelTarget.value.id)
      await refreshEvents()
    }
    await load(true)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    cancelling.value = false
  }
}

const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTarget = ref<TaskSummary | null>(null)

function askDelete(task: TaskSummary): void {
  deleteTarget.value = task
  deleteOpen.value = true
}

/**
 * Delete a task and its scratch volume.
 *
 * The row survives as history, so the confirmation says "从列表移除" rather
 * than "永久删除" — telling the user their data is gone when it is not would be
 * its own kind of bug.
 */
async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteTask(deleteTarget.value.id)
    notifications.success('任务已移除')
    deleteOpen.value = false
    if (detail.value?.id === deleteTarget.value.id) detailOpen.value = false
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    deleting.value = false
  }
}

// --- presentation helpers ----------------------------------------------------
function shapeOf(task: TaskSummary): string {
  const parts = [formatCpu(task.cpuMilli), formatMemory(task.memoryMb)]
  if (task.gpuCount > 0) parts.push(`${task.gpuCount}× ${task.gpuType.toUpperCase()}`)
  return parts.join(' · ')
}

/** Exit code is only meaningful once the container has terminated. */
function exitLabel(task: TaskSummary): string | null {
  if (task.exitCode === null || task.exitCode === undefined) return null
  return task.exitCode === 0 ? '退出码 0' : `退出码 ${task.exitCode}`
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function eventTone(event: WorkloadEvent): string {
  if (event.eventType === 'failed') return 'error'
  if (event.toStatus === 'succeeded') return 'success'
  if (event.toStatus === 'cancelled') return 'neutral'
  return 'info'
}
</script>

<template>
  <div>
    <PageHeader title="自定义任务" subtitle="直接运行一段命令，跑完即退出 —— 适合训练、批处理和数据处理">
      <template #actions>
        <v-btn
          variant="text"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="load()"
        >
          刷新
        </v-btn>
        <v-btn
          v-if="canCreate"
          color="primary"
          variant="flat"
          prepend-icon="mdi-plus"
          @click="openSubmit"
        >
          提交任务
        </v-btn>
      </template>
    </PageHeader>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索任务名"
        style="max-width: 260px"
      />
      <v-select
        v-model="filters.status"
        :items="STATUS_OPTIONS"
        item-title="title"
        item-value="value"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="状态"
        style="max-width: 160px"
      />
      <v-select
        v-if="isAdmin"
        v-model="filters.workspaceId"
        :items="workspaces"
        item-title="k8sNamespace"
        item-value="id"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="工作空间"
        style="max-width: 240px"
      />
      <v-switch
        v-model="filters.includeInactive"
        label="含已移除"
        color="primary"
        density="compact"
        hide-details
        inset
      />
    </FilterBar>

    <v-card flat class="wl-table-card">
      <EmptyState
        v-if="!loading && rows.length === 0"
        icon="mdi-script-text-outline"
        title="还没有任务"
        :description="
          filters.search || filters.status || filters.workspaceId
            ? '换个筛选条件试试。'
            : '提交一个任务，让它在集群上跑完就退出。'
        "
      />
      <v-data-table
        v-else
        :headers="headers"
        :items="rows"
        :loading="loading"
        item-value="id"
        show-expand
        hover
      >
        <template #item.displayName="{ item }">
          <div class="wl-task-name">
            <span class="wl-task-name__text">{{ item.displayName }}</span>
            <span class="wl-task-name__slug">{{ item.name }}</span>
            <v-chip
              v-if="hasLiveTask && !isTerminal(item.status)"
              size="x-small"
              variant="tonal"
              color="info"
              label
              class="ml-2"
            >
              自动刷新中
            </v-chip>
          </div>
        </template>

        <template #item.shape="{ item }">
          <span class="text-body-2">{{ shapeOf(item) }}</span>
        </template>

        <template #item.status="{ item }">
          <StatusChip :status="item.status" />
          <div v-if="item.statusMessage" class="wl-task-message">{{ item.statusMessage }}</div>
        </template>

        <template #item.duration="{ item }">
          <span class="text-body-2">{{ formatDuration(item.durationSeconds ?? null) ?? '—' }}</span>
          <div v-if="exitLabel(item)" class="text-caption text-medium-emphasis">
            {{ exitLabel(item) }}
          </div>
        </template>

        <template #item.workspaceName="{ item }">
          <span class="text-body-2">{{ item.workspaceName ?? '—' }}</span>
        </template>

        <template #item.ownerUsername="{ item }">
          <span class="text-body-2">{{ item.ownerDisplayName || item.ownerUsername || '—' }}</span>
        </template>

        <template #item.actions="{ item }">
          <div class="wl-row-actions">
            <v-btn
              v-if="canLogs"
              icon="mdi-text-box-search-outline"
              variant="text"
              size="small"
              title="查看日志"
              @click="openDetail(item.id, 'logs')"
            />
            <v-btn
              icon="mdi-information-outline"
              variant="text"
              size="small"
              title="详情"
              @click="openDetail(item.id)"
            />
            <v-btn
              v-if="canCancel && !isTerminal(item.status)"
              icon="mdi-stop-circle-outline"
              variant="text"
              size="small"
              color="warning"
              title="取消"
              @click="askCancel(item)"
            />
            <v-btn
              v-else-if="canCancel"
              icon="mdi-delete-outline"
              variant="text"
              size="small"
              color="error"
              title="移除"
              @click="askDelete(item)"
            />
          </div>
        </template>

        <template #expanded-row="{ columns, item }">
          <tr>
            <td :colspan="columns.length" class="wl-expanded">
              <div class="wl-expanded__grid">
                <div>
                  <div class="wl-expanded__label">镜像</div>
                  <div class="wl-expanded__value">{{ item.image }}</div>
                </div>
                <div>
                  <div class="wl-expanded__label">资源组</div>
                  <div class="wl-expanded__value">{{ item.resourceGroupName ?? '—' }}</div>
                </div>
                <div>
                  <div class="wl-expanded__label">节点</div>
                  <div class="wl-expanded__value">{{ item.nodeName ?? '—' }}</div>
                </div>
                <div>
                  <div class="wl-expanded__label">提交于</div>
                  <div class="wl-expanded__value">{{ formatTime(item.createdAt) }}</div>
                </div>
                <div>
                  <div class="wl-expanded__label">开始</div>
                  <div class="wl-expanded__value">{{ formatTime(item.startedAt) }}</div>
                </div>
                <div>
                  <div class="wl-expanded__label">结束</div>
                  <div class="wl-expanded__value">{{ formatTime(item.finishedAt) }}</div>
                </div>
              </div>
            </td>
          </tr>
        </template>
      </v-data-table>

      <div v-if="total > pageSize" class="wl-pagination">
        <v-pagination
          v-model="page"
          :length="Math.ceil(total / pageSize)"
          :total-visible="7"
          density="comfortable"
        />
      </div>
    </v-card>

    <!-- Submit -->
    <v-dialog v-model="submitOpen" max-width="880" scrollable>
      <v-card>
        <v-card-title>提交自定义任务</v-card-title>
        <v-card-text>
          <v-alert v-if="submitError" type="error" variant="tonal" class="mb-4 wl-form-error">
            {{ submitError }}
          </v-alert>

          <v-row dense>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model="form.name"
                label="标识名"
                :rules="[(v: string) => /^[a-z0-9][a-z0-9-]*$/.test(v) || '只能用小写字母、数字和连字符']"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model="form.displayName"
                label="显示名"
                placeholder="例如 第 3 轮消融实验"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col v-if="isAdmin" cols="12" sm="4">
              <v-select
                v-model="form.workspaceId"
                :items="workspaces"
                item-title="k8sNamespace"
                item-value="id"
                label="工作空间"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceGroupId"
                :items="grants"
                item-title="displayName"
                item-value="resourceGroupId"
                label="资源组"
                variant="outlined"
                density="comfortable"
                @update:model-value="selectPool($event as number | null)"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceSpecId"
                :items="availableSpecs"
                item-title="displayName"
                item-value="id"
                label="资源规格"
                :disabled="form.resourceGroupId === null"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <ImagePicker v-model="form.image" label="容器镜像 *" />
            </v-col>

            <v-col cols="12">
              <v-select
                v-model="form.storageIds"
                :items="attachableStorages"
                item-title="displayName"
                item-value="id"
                label="挂载存储（可选）"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                multiple
                chips
                closable-chips
                :loading="loadingStorages"
                :disabled="form.workspaceId === null"
                :no-data-text="
                  form.workspaceId === null ? '请先选择工作空间' : '这个工作空间里还没有可挂载的存储'
                "
                hint="个人主目录会自动挂载到 /home/{用户名}，不需要在这里选择；不选就不挂任何共享卷。"
                persistent-hint
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item v-bind="itemProps" :subtitle="storageSubtitle(item)">
                    <template #prepend>
                      <v-icon :icon="storageKindMeta(item.kind).icon" />
                    </template>
                    <template #append>
                      <v-icon
                        v-if="item.isReadOnly"
                        icon="mdi-lock-outline"
                        size="small"
                        title="只读挂载"
                      />
                    </template>
                  </v-list-item>
                </template>
              </v-select>
            </v-col>

            <v-col cols="12">
              <div class="d-flex align-center justify-space-between mb-2">
                <span class="text-subtitle-2">要运行的命令</span>
                <div class="d-flex ga-1">
                  <v-chip
                    v-for="preset in SCRIPT_PRESETS"
                    :key="preset.title"
                    size="small"
                    variant="tonal"
                    label
                    @click="form.script = preset.value"
                  >
                    <v-icon start size="x-small" :icon="preset.icon" />
                    {{ preset.title }}
                  </v-chip>
                </div>
              </div>
              <v-textarea
                v-model="form.script"
                rows="10"
                auto-grow
                variant="outlined"
                density="comfortable"
                spellcheck="false"
                class="wl-script"
                hint="以 /bin/sh -lc 执行，可以用管道、循环和变量；工作目录与容器一致"
                persistent-hint
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.maxRuntimeSeconds"
                :items="runtimeOptions"
                item-title="title"
                item-value="value"
                label="最长运行时间"
                hint="由 Kubernetes 强制中止，超时会标记为「已超时」"
                persistent-hint
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-alert type="info" variant="tonal" density="compact" class="mt-2">
                任务失败不会自动重试 —— 结果会立刻返回，不会白烧一次加速卡。
              </v-alert>
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="submitOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="submitting"
            :disabled="!submitCanSubmit"
            @click="submit"
          >
            提交
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Detail -->
    <v-navigation-drawer v-model="detailOpen" location="right" temporary width="720">
      <div class="wl-drawer">
        <div class="wl-drawer__head">
          <div>
            <div class="wl-drawer__title">{{ detail?.displayName ?? '加载中…' }}</div>
            <div class="wl-drawer__slug">{{ detail?.name }}</div>
          </div>
          <div class="d-flex align-center ga-2">
            <StatusChip v-if="detail" :status="detail.status" />
            <v-btn icon="mdi-close" variant="text" size="small" @click="detailOpen = false" />
          </div>
        </div>

        <v-skeleton-loader v-if="detailLoading" type="article" />
        <template v-else-if="detail">
          <v-tabs v-model="detailTab" density="comfortable" color="primary">
            <v-tab value="overview">概览</v-tab>
            <v-tab v-if="canLogs" value="logs">日志</v-tab>
            <v-tab value="timeline">时间线</v-tab>
          </v-tabs>

          <v-window v-model="detailTab" class="wl-drawer__body">
            <v-window-item value="overview">
              <div class="wl-overview">
                <div v-if="detail.statusMessage" class="mb-4">
                  <v-alert
                    :type="detail.status === 'succeeded' ? 'success' : 'warning'"
                    variant="tonal"
                    density="comfortable"
                  >
                    {{ detail.statusMessage }}
                  </v-alert>
                </div>

                <dl class="wl-kv">
                  <dt>镜像</dt>
                  <dd>{{ detail.image }}</dd>
                  <dt>规格</dt>
                  <dd>{{ shapeOf(detail) }}</dd>
                  <dt>主目录</dt>
                  <dd class="wl-mono">
                    {{ detail.ownerUsername ? `/home/${detail.ownerUsername}` : '—' }}
                  </dd>
                  <dt>耗时</dt>
                  <dd>{{ formatDuration(detail.durationSeconds ?? null) ?? '—' }}</dd>
                  <dt>退出码</dt>
                  <dd>{{ detail.exitCode ?? '—' }}</dd>
                  <dt>最长运行</dt>
                  <dd>{{ formatDuration(detail.maxRuntimeSeconds ?? null) ?? '不限制' }}</dd>
                  <dt>Job</dt>
                  <dd>{{ detail.k8sJobName ?? '—' }}</dd>
                  <dt>容器</dt>
                  <dd>{{ detail.podName ?? '—' }}</dd>
                  <dt>节点</dt>
                  <dd>{{ detail.nodeName ?? '—' }}</dd>
                  <dt>工作空间</dt>
                  <dd>{{ detail.workspaceName ?? '—' }}</dd>
                  <dt>提交人</dt>
                  <dd>{{ detail.ownerDisplayName || detail.ownerUsername || '—' }}</dd>
                  <dt>提交时间</dt>
                  <dd>{{ formatTime(detail.createdAt) }}</dd>
                  <dt>开始</dt>
                  <dd>{{ formatTime(detail.startedAt) }}</dd>
                  <dt>结束</dt>
                  <dd>{{ formatTime(detail.finishedAt) }}</dd>
                </dl>

                <div class="mt-4">
                  <div class="text-subtitle-2 mb-2">命令</div>
                  <pre class="wl-script-view">{{ detail.script }}</pre>
                </div>
              </div>
            </v-window-item>

            <v-window-item value="logs">
              <div class="d-flex align-center justify-space-between mb-3">
                <v-switch
                  v-model="logsPrevious"
                  label="上一次运行"
                  color="primary"
                  density="compact"
                  hide-details
                  inset
                />
                <div class="d-flex ga-2">
                  <v-btn
                    variant="text"
                    size="small"
                    prepend-icon="mdi-refresh"
                    :loading="logsLoading"
                    @click="loadLogs"
                  >
                    重新读取
                  </v-btn>
                  <v-btn
                    variant="tonal"
                    size="small"
                    prepend-icon="mdi-sync"
                    @click="refreshDetail"
                  >
                    同步状态
                  </v-btn>
                </div>
              </div>
              <pre class="wl-logs">{{ logsLoading ? '读取中…' : logs }}</pre>
            </v-window-item>

            <v-window-item value="timeline">
              <div class="d-flex justify-end mb-2">
                <v-btn
                  variant="text"
                  size="small"
                  prepend-icon="mdi-refresh"
                  :loading="eventsLoading"
                  @click="refreshEvents"
                >
                  刷新
                </v-btn>
              </div>
              <EmptyState
                v-if="events.length === 0"
                icon="mdi-timeline-outline"
                title="暂无记录"
              />
              <v-timeline v-else density="compact" side="end" truncate-line="both">
                <v-timeline-item
                  v-for="event in events"
                  :key="event.id"
                  :dot-color="eventTone(event)"
                  size="small"
                >
                  <div class="wl-event">
                    <div class="wl-event__line">
                      <StatusChip v-if="event.toStatus" :status="event.toStatus" />
                      <span v-else class="text-body-2">{{ event.eventType }}</span>
                      <span class="wl-event__time">{{ formatTime(event.createdAt) }}</span>
                    </div>
                    <div v-if="event.message" class="wl-event__message">{{ event.message }}</div>
                    <div class="wl-event__actor">
                      {{ event.actorUsername ? `由 ${event.actorUsername} 操作` : '系统自动' }}
                    </div>
                  </div>
                </v-timeline-item>
              </v-timeline>
            </v-window-item>
          </v-window>
        </template>

        <div v-if="detail" class="wl-drawer__foot">
          <v-btn
            v-if="!isTerminal(detail.status)"
            variant="text"
            size="small"
            prepend-icon="mdi-pencil-outline"
            @click="openRename(detail)"
          >
            重命名
          </v-btn>
          <v-spacer />
          <v-btn
            v-if="canCancel && !isTerminal(detail.status)"
            color="warning"
            variant="tonal"
            size="small"
            prepend-icon="mdi-stop-circle-outline"
            @click="askCancel(detail)"
          >
            取消任务
          </v-btn>
          <v-btn
            v-else-if="canCancel"
            color="error"
            variant="text"
            size="small"
            prepend-icon="mdi-delete-outline"
            @click="askDelete(detail)"
          >
            移除
          </v-btn>
        </div>
      </div>
    </v-navigation-drawer>

    <!-- Rename -->
    <v-dialog v-model="renameOpen" max-width="440">
      <v-card>
        <v-card-title>重命名任务</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="renameValue"
            label="显示名"
            variant="outlined"
            density="comfortable"
            autofocus
            @keyup.enter="confirmRename"
          />
          <p class="text-caption text-medium-grey mt-2">
            只能改显示名。镜像、命令和资源在提交后不可修改 —— 请提交一个新任务。
          </p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="renameOpen = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="renaming" @click="confirmRename">
            保存
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="cancelOpen"
      title="取消任务"
      :text="`「${cancelTarget?.displayName ?? ''}」会被立即中止，容器和它占用的加速卡随即释放。已完成的部分输出不会回滚。`"
      confirm-text="取消任务"
      confirm-color="warning"
      :loading="cancelling"
      @confirm="confirmCancel"
    />

    <ConfirmDialog
      v-model="deleteOpen"
      title="移除任务"
      :text="`「${deleteTarget?.displayName ?? ''}」会从列表中移除，临时存储（若有）随之删除。历史记录和用量统计会保留。`"
      confirm-text="移除"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-table-card {
  border-radius: 12px;
  box-shadow: $card-shadow;
  overflow: hidden;
}

.wl-task-name__text {
  font-weight: 500;
  color: $title-grey;
}

.wl-task-name__slug {
  margin-left: 8px;
  font-size: 12px;
  color: $text-grey;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.wl-task-message {
  font-size: 11px;
  color: $text-grey;
  margin-top: 4px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wl-row-actions {
  display: flex;
  justify-content: flex-end;
  gap: 2px;
}

.wl-expanded {
  background: rgba(83, 109, 254, 0.04);
  padding: 16px 24px !important;
}

.wl-expanded__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px 24px;
}

.wl-expanded__label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: $text-grey;
}

.wl-expanded__value {
  font-size: 13px;
  color: $text-color;
  word-break: break-all;
}

.wl-pagination {
  display: flex;
  justify-content: center;
  padding: 16px 0;
}

.wl-script :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
}

.wl-script-view,
.wl-logs {
  margin: 0;
  padding: 14px 16px;
  border-radius: 8px;
  background: #1f2233;
  color: #e6e8f5;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}

.wl-logs {
  max-height: calc(100vh - 320px);
  overflow-y: auto;
}

.wl-drawer {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.wl-drawer__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 20px 20px 12px;
}

.wl-drawer__title {
  font-size: 18px;
  font-weight: 600;
  color: $title-grey;
}

.wl-drawer__slug {
  font-size: 12px;
  color: $text-grey;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.wl-drawer__body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 16px 20px;
}

.wl-drawer__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
}

.wl-overview {
  padding-top: 8px;
}

.wl-kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 8px 16px;
  margin: 0;

  dt {
    font-size: 12px;
    color: $text-grey;
  }

  dd {
    margin: 0;
    font-size: 13px;
    color: $text-color;
    word-break: break-word;
  }
}

.wl-event__line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wl-event__time {
  font-size: 11px;
  color: $text-grey;
}

.wl-event__message {
  font-size: 13px;
  color: $text-color;
  margin-top: 4px;
}

.wl-event__actor {
  font-size: 11px;
  color: $text-grey;
  margin-top: 2px;
}

.text-medium-grey {
  color: $text-grey;
}
</style>

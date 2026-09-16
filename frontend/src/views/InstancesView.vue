<script setup lang="ts">
/**
 * Container instances — the platform's core resource.
 *
 * One page serves both a normal user and an administrator. The API already
 * scopes the listing by data scope, so the only difference is what is worth
 * showing: an administrator gets workspace and owner columns and a workspace
 * picker in the create dialog; a user's own workspace is implicit and their
 * granted pools are resolved for them.
 */
import {
  computed,
  defineAsyncComponent,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from 'vue'

import { ApiError } from '@/api/client'
import {
  createInstance,
  deleteInstance,
  getIdeAccess,
  getInstance,
  getInstanceLogs,
  listInstances,
  listSshKeys,
  reconcileInstance,
  startInstance,
  stopInstance,
  updateInstance,
} from '@/api/instances'
import { getResourceGroup } from '@/api/resources'
import { getMyWorkspace, listWorkspaceGrants, listWorkspaces } from '@/api/workspaces'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { listAttachableStorages } from '@/api/storages'
import ImagePicker from '@/components/image/ImagePicker.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type {
  IdeType,
  InstanceDetail,
  InstanceStatus,
  InstanceSummary,
} from '@/types/instance'
import { IDE_OPTIONS } from '@/types/instance'
import { storageKindMeta, type StorageSummary } from '@/types/storage'
import {
  formatCpu,
  formatDuration,
  formatMemory,
  type ResourceGroupDetail,
  type ResourceSpec,
  type WorkspaceSummary,
} from '@/types/resource'

/**
 * Loaded on demand: xterm is by far the largest dependency on this page, and
 * most visits never open a shell. Deferring it keeps the list route small.
 */
const InstanceTerminal = defineAsyncComponent(
  () => import('@/components/instance/InstanceTerminal.vue'),
)

const { can, isAdmin } = usePermissions()
const notifications = useNotificationStore()

const STATUS_OPTIONS: { value: InstanceStatus; title: string }[] = [
  { value: 'running', title: '运行中' },
  { value: 'creating', title: '创建中' },
  { value: 'stopped', title: '已停止' },
  { value: 'error', title: '异常' },
  { value: 'failed', title: '失败' },
]

/** Statuses in which a shell can actually be opened. */
const INTERACTIVE_STATUSES: readonly InstanceStatus[] = ['running']

// --- listing -----------------------------------------------------------------
const rows = ref<InstanceSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  status: null as InstanceStatus | null,
  workspaceId: null as number | null,
})

const workspaces = ref<WorkspaceSummary[]>([])

interface Column {
  title: string
  key: string
  sortable: boolean
  align?: 'start' | 'end'
}

const headers = computed<Column[]>(() => {
  const base: Column[] = [
    { title: '实例', key: 'displayName', sortable: false },
    { title: '规格', key: 'shape', sortable: false },
    { title: '镜像', key: 'image', sortable: false },
    { title: '状态', key: 'status', sortable: false },
  ]
  if (isAdmin.value) {
    base.push(
      { title: '工作空间', key: 'workspaceName', sortable: false },
      { title: '归属人', key: 'ownerUsername', sortable: false },
    )
  }
  base.push(
    { title: '访问', key: 'access', sortable: false },
    { title: '操作', key: 'actions', sortable: false, align: 'end' },
  )
  return base
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listInstances({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      status: filters.status ?? undefined,
      workspaceId: filters.workspaceId ?? undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

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
watch([() => filters.status, () => filters.workspaceId], () => {
  page.value = 1
  void load()
})

// --- create ------------------------------------------------------------------
interface CreateForm {
  name: string
  displayName: string
  description: string
  workspaceId: number | null
  resourceGroupId: number | null
  resourceSpecId: number | null
  image: string
  ideType: IdeType
  storageGb: number | null
  /** Persistent volumes to mount beyond the instance's own scratch claim. */
  storageIds: number[]
  sshEnabled: boolean
  maxRuntimeSeconds: number | null
}

const createOpen = ref(false)
const creating = ref(false)
const form = reactive<CreateForm>({
  name: '',
  displayName: '',
  description: '',
  workspaceId: null,
  resourceGroupId: null,
  resourceSpecId: null,
  image: '',
  ideType: 'jupyter',
  storageGb: null,
  storageIds: [],
  sshEnabled: true,
  maxRuntimeSeconds: null,
})

const grants = ref<{ resourceGroupId: number; displayName: string; isActive: boolean }[]>([])
const selectedPool = ref<ResourceGroupDetail | null>(null)
const loadingPools = ref(false)

const availableSpecs = computed<ResourceSpec[]>(() =>
  (selectedPool.value?.specs ?? []).filter((spec) => spec.isActive),
)

/** Runtime caps offered for the chosen pool: its limit, or a few sane steps. */
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

const storageCap = computed(() => selectedPool.value?.limits?.maxStorageGbPerUser ?? null)

/**
 * Volumes this instance could mount.
 *
 * Excludes personal homes: those are mounted automatically, and choosing one
 * explicitly is refused by the API — offering it here would be offering an
 * action that always fails.
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
    // The mount list is optional; failing to load it must not block creating an
    // instance without any.
    attachableStorages.value = []
  } finally {
    loadingStorages.value = false
  }
}

// Reload whenever the target workspace changes, and drop any selection that no
// longer belongs to it — a volume from another namespace simply cannot be seen.
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

const canSubmit = computed(
  () =>
    /^[a-z0-9][a-z0-9-]*$/.test(form.name) &&
    form.name.length <= 55 &&
    form.resourceGroupId !== null &&
    form.resourceSpecId !== null &&
    form.image.trim().length > 0 &&
    (isAdmin.value ? form.workspaceId !== null : true),
)

function resetForm(): void {
  Object.assign(form, {
    name: '',
    displayName: '',
    description: '',
    workspaceId: isAdmin.value ? null : (myWorkspace.value?.id ?? null),
    resourceGroupId: null,
    resourceSpecId: null,
    image: '',
    ideType: 'jupyter',
    storageGb: null,
    storageIds: [],
    sshEnabled: true,
    maxRuntimeSeconds: null,
  })
  selectedPool.value = null
  grants.value = []
}

/** Load the pools granted to the chosen workspace, then preselect the first. */
async function loadGrants(workspaceId: number | null): Promise<void> {
  grants.value = []
  selectedPool.value = null
  form.resourceGroupId = null
  form.resourceSpecId = null
  if (workspaceId === null) return

  loadingPools.value = true
  try {
    grants.value = (await listWorkspaceGrants(workspaceId)).filter((grant) => grant.isActive)
    if (grants.value.length === 1) {
      await selectPool(grants.value[0].resourceGroupId)
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loadingPools.value = false
  }
}

async function selectPool(resourceGroupId: number | null): Promise<void> {
  form.resourceGroupId = resourceGroupId
  form.resourceSpecId = null
  selectedPool.value = null
  if (resourceGroupId === null) return

  try {
    selectedPool.value = await getResourceGroup(resourceGroupId)
    // Storage and runtime default to whatever the pool allows, so the common
    // case is one click rather than three decisions.
    form.storageGb = selectedPool.value.limits?.maxStorageGbPerUser
      ? Math.min(10, selectedPool.value.limits.maxStorageGbPerUser)
      : 10
    form.maxRuntimeSeconds = null
    if (availableSpecs.value.length === 1) {
      form.resourceSpecId = availableSpecs.value[0].id
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

watch(
  () => form.workspaceId,
  (workspaceId) => void loadGrants(workspaceId),
)

async function submitCreate(): Promise<void> {
  if (!canSubmit.value) return
  creating.value = true
  try {
    const created = await createInstance({
      name: form.name.trim(),
      displayName: form.displayName.trim() || form.name.trim(),
      description: form.description.trim() || null,
      workspaceId: form.workspaceId,
      resourceGroupId: form.resourceGroupId!,
      resourceSpecId: form.resourceSpecId!,
      image: form.image.trim(),
      ideType: form.ideType,
      storageGb: form.storageGb,
      storageIds: form.storageIds,
      sshEnabled: form.sshEnabled,
      maxRuntimeSeconds: form.maxRuntimeSeconds,
    })
    notifications.success(`实例 ${created.name} 已下发，正在启动`)
    createOpen.value = false
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // A quota or grant rejection carries the reason in the message; showing it
    // verbatim is more useful than a generic failure.
    notifications.error(error.message)
  } finally {
    creating.value = false
  }
}

// --- actions -----------------------------------------------------------------
const busyId = ref<number | null>(null)

async function run(
  instance: InstanceSummary,
  action: () => Promise<unknown>,
  successMessage: string,
): Promise<void> {
  busyId.value = instance.id
  try {
    await action()
    notifications.success(successMessage)
    await load()
    if (detail.value?.id === instance.id) {
      detail.value = await getInstance(instance.id)
    }
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    busyId.value = null
  }
}

function toggleRunState(instance: InstanceSummary): void {
  const running = instance.status === 'running' || instance.status === 'creating'
  void run(
    instance,
    () => (running ? stopInstance(instance.id) : startInstance(instance.id)),
    running ? `实例 ${instance.name} 已停止` : `实例 ${instance.name} 正在启动`,
  )
}

function reconcile(instance: InstanceSummary): void {
  void run(instance, () => reconcileInstance(instance.id), `实例 ${instance.name} 已重新下发`)
}

/**
 * Open the web IDE.
 *
 * The ticket is fetched fresh on every click and expires in minutes, so opening
 * the tab is the only way to use it — a URL copied out of the address bar will
 * not work later.
 */
async function openIde(instance: InstanceSummary): Promise<void> {
  busyId.value = instance.id
  try {
    const access = await getIdeAccess(instance.id)
    window.open(access.url, '_blank', 'noopener')
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    busyId.value = null
  }
}

function copySsh(instance: InstanceSummary): void {
  void (async () => {
    try {
      const full = await getInstance(instance.id)
      if (!full.sshCommand) {
        notifications.warning('该实例尚未分配 SSH 端口')
        return
      }
      await navigator.clipboard.writeText(full.sshCommand)
      notifications.success('SSH 命令已复制')
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      notifications.error(error.message)
    }
  })()
}

// --- detail ------------------------------------------------------------------
const detailOpen = ref(false)
const detail = ref<InstanceDetail | null>(null)
const detailTab = ref('overview')

async function openDetail(instance: InstanceSummary): Promise<void> {
  detailOpen.value = true
  detailTab.value = 'overview'
  detail.value = null
  try {
    detail.value = await getInstance(instance.id)
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
    detailOpen.value = false
  }
}

// --- logs ----------------------------------------------------------------
const logsOpen = ref(false)
const logs = ref('')
const logsLoading = ref(false)
const logsPrevious = ref(false)
const logInstance = ref<InstanceSummary | null>(null)

async function openLogs(instance: InstanceSummary, previous = false): Promise<void> {
  logsOpen.value = true
  logInstance.value = instance
  logsPrevious.value = previous
  logsLoading.value = true
  logs.value = ''
  try {
    const result = await getInstanceLogs(instance.id, { previous, tailLines: 500 })
    logs.value = result.logs || '（暂无日志输出）'
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    logs.value = `读取日志失败：${error.message}`
  } finally {
    logsLoading.value = false
  }
}

// --- rename ------------------------------------------------------------------
const renameOpen = ref(false)
const renameTarget = ref<InstanceSummary | null>(null)
const renameForm = reactive({ displayName: '', description: '' })

function openRename(instance: InstanceSummary): void {
  renameTarget.value = instance
  renameForm.displayName = instance.displayName
  renameForm.description = instance.description ?? ''
  renameOpen.value = true
}

async function submitRename(): Promise<void> {
  const target = renameTarget.value
  if (!target) return
  await run(
    target,
    () =>
      updateInstance(target.id, {
        displayName: renameForm.displayName.trim() || target.name,
        description: renameForm.description.trim() || null,
      }),
    '已保存',
  )
  renameOpen.value = false
}

// --- delete ------------------------------------------------------------------
const deleteOpen = ref(false)
const deleteTarget = ref<InstanceSummary | null>(null)

function confirmDelete(instance: InstanceSummary): void {
  deleteTarget.value = instance
  deleteOpen.value = true
}

async function submitDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  deleteOpen.value = false
  if (detail.value?.id === target.id) detailOpen.value = false
  await run(
    target,
    () => deleteInstance(target.id),
    // Deleting removes the home volume too, which is the part users care about.
    `实例 ${target.name} 已删除，其主目录数据同时清除`,
  )
  deleteTarget.value = null
}

// --- helpers -----------------------------------------------------------------
const myWorkspace = ref<WorkspaceSummary | null>(null)
/**
 * Whether the caller has registered a key. Creating an instance without one is
 * allowed — the keys can be added later and a reconcile picks them up — but it
 * is worth warning about, because the resulting container accepts no SSH login.
 */
const hasSshKey = ref(true)

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function shapeOf(instance: InstanceSummary): string {
  const parts = [formatCpu(instance.cpuMilli), formatMemory(instance.memoryMb)]
  if (instance.gpuCount > 0) parts.push(`${instance.gpuType.toUpperCase()} ×${instance.gpuCount}`)
  return parts.join(' · ')
}

/** The same "2 核 · 4 GiB · A100 ×1" phrasing, for a pool spec. */
function specShape(spec: ResourceSpec): string {
  const parts = [formatCpu(spec.cpuMilli), formatMemory(spec.memoryMb)]
  if (spec.gpuCount > 0) parts.push(`${spec.gpuType.toUpperCase()} ×${spec.gpuCount}`)
  return parts.join(' · ')
}

function ideLabel(ideType: IdeType): string {
  return IDE_OPTIONS.find((option) => option.value === ideType)?.title ?? ideType
}

function isInteractive(instance: InstanceSummary): boolean {
  return INTERACTIVE_STATUSES.includes(instance.status)
}

onMounted(async () => {
  void load()
  try {
    hasSshKey.value = (await listSshKeys()).length > 0
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
  }
  try {
    if (isAdmin.value) {
      const result = await listWorkspaces({ pageSize: 200 })
      workspaces.value = result.items
    } else {
      myWorkspace.value = await getMyWorkspace()
    }
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
  }
})

onBeforeUnmount(() => clearTimeout(searchTimer))

const workspaceChoices = computed(() =>
  workspaces.value.map((workspace) => ({
    value: workspace.id,
    title: `${workspace.displayName}（${workspace.k8sNamespace}）`,
  })),
)
</script>

<template>
  <div>
    <PageHeader
      title="开发实例"
      :subtitle="
        isAdmin
          ? '全部实例；可查看终端、日志并代为启停'
          : '你的容器开发环境：SSH、Web IDE 与终端'
      "
    >
      <template #actions>
        <v-btn
          v-if="can('instance:create')"
          color="primary"
          prepend-icon="mdi-plus"
          @click="
            () => {
              resetForm()
              createOpen = true
            }
          "
        >
          新建实例
        </v-btn>
      </template>
    </PageHeader>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        density="compact"
        variant="outlined"
        hide-details
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索名称"
        clearable
        style="max-width: 260px"
      />
      <v-select
        v-if="isAdmin"
        v-model="filters.workspaceId"
        :items="workspaceChoices"
        density="compact"
        variant="outlined"
        hide-details
        clearable
        label="工作空间"
        style="max-width: 260px"
      />
      <v-select
        v-model="filters.status"
        :items="STATUS_OPTIONS"
        density="compact"
        variant="outlined"
        hide-details
        clearable
        label="状态"
        style="max-width: 180px"
      />
    </FilterBar>

    <v-card>
      <v-data-table
        :headers="headers"
        :items="rows"
        :loading="loading"
        :items-per-page="pageSize"
        :page="page"
        :items-length="total"
        item-value="id"
        hover
        @update:page="page = $event"
        @update:items-per-page="pageSize = $event"
      >
        <template #item.displayName="{ item }">
          <div class="font-weight-medium">{{ item.displayName }}</div>
          <div class="text-caption text-medium-emphasis">
            {{ item.name }}
            <span v-if="ideLabel(item.ideType) !== '无（仅 SSH）'">
              · {{ ideLabel(item.ideType) }}
            </span>
          </div>
        </template>

        <template #item.shape="{ item }">
          <div>{{ shapeOf(item) }}</div>
          <div class="text-caption text-medium-emphasis">{{ item.storageGb }} GB 存储</div>
        </template>

        <template #item.image="{ item }">
          <span class="text-caption wl-mono">{{ item.image }}</span>
        </template>

        <template #item.status="{ item }">
          <StatusChip :status="item.status" />
          <div v-if="item.statusMessage" class="text-caption text-medium-emphasis mt-1">
            {{ item.statusMessage }}
          </div>
        </template>

        <template #item.workspaceName="{ item }">
          <span class="wl-mono text-caption">{{ item.workspaceName ?? '—' }}</span>
        </template>

        <template #item.ownerUsername="{ item }">
          {{ item.ownerDisplayName ?? item.ownerUsername ?? '—' }}
        </template>

        <template #item.access="{ item }">
          <div class="d-flex flex-column ga-1">
            <span v-if="item.sshNodePort" class="text-caption wl-mono">
              ssh -p {{ item.sshNodePort }}
            </span>
            <span v-else class="text-caption text-medium-emphasis">无 SSH</span>
            <span v-if="item.isExpired" class="text-caption text-error">已超时</span>
          </div>
        </template>

        <template #item.actions="{ item }">
          <div class="d-flex justify-end ga-1">
            <v-btn
              size="small"
              variant="text"
              icon="mdi-console"
              :disabled="!isInteractive(item)"
              title="终端"
              @click="openDetail(item)"
            />
            <v-btn
              v-if="item.ideType !== 'none'"
              size="small"
              variant="text"
              icon="mdi-open-in-new"
              :disabled="!isInteractive(item)"
              title="打开 Web IDE"
              @click="openIde(item)"
            />
            <v-btn
              size="small"
              variant="text"
              icon="mdi-text-box-outline"
              title="日志"
              @click="openLogs(item)"
            />
            <v-menu location="bottom end">
              <template #activator="{ props }">
                <v-btn size="small" variant="text" icon="mdi-dots-vertical" v-bind="props" />
              </template>
              <v-list density="compact">
                <v-list-item
                  v-if="can('instance:start') || can('instance:stop')"
                  :title="
                    item.status === 'running' || item.status === 'creating' ? '停止' : '启动'
                  "
                  :prepend-icon="
                    item.status === 'running' || item.status === 'creating'
                      ? 'mdi-stop'
                      : 'mdi-play'
                  "
                  @click="toggleRunState(item)"
                />
                <v-list-item
                  title="复制 SSH 命令"
                  prepend-icon="mdi-content-copy"
                  :disabled="!item.sshNodePort"
                  @click="copySsh(item)"
                />
                <v-list-item
                  v-if="can('instance:update')"
                  title="重新下发"
                  prepend-icon="mdi-refresh"
                  @click="reconcile(item)"
                />
                <v-list-item
                  v-if="can('instance:update')"
                  title="重命名"
                  prepend-icon="mdi-rename-outline"
                  @click="openRename(item)"
                />
                <v-divider />
                <v-list-item
                  v-if="can('instance:delete')"
                  title="删除"
                  prepend-icon="mdi-delete-outline"
                  base-color="error"
                  @click="confirmDelete(item)"
                />
              </v-list>
            </v-menu>
          </div>
        </template>

        <template #no-data>
          <EmptyState
            icon="mdi-cube-outline"
            title="还没有实例"
            description="实例是一个容器化的开发环境，带独立主目录、SSH 与可选的 Web IDE。"
          />
        </template>
      </v-data-table>
    </v-card>

    <!-- Create -->
    <v-dialog v-model="createOpen" max-width="720" persistent>
      <v-card>
        <v-card-title>新建实例</v-card-title>
        <v-card-text>
          <v-alert
            v-if="!isAdmin && !myWorkspace"
            type="warning"
            variant="tonal"
            class="mb-4"
            text="你尚未被分配工作空间，请联系管理员后再创建实例。"
          />
          <v-alert
            v-else-if="!isAdmin && myWorkspace"
            type="info"
            variant="tonal"
            density="compact"
            class="mb-4"
            :text="`将创建在你的工作空间：${myWorkspace.displayName}（${myWorkspace.k8sNamespace}）`"
          />

          <v-row dense>
            <v-col v-if="isAdmin" cols="12">
              <v-select
                v-model="form.workspaceId"
                :items="workspaceChoices"
                label="工作空间 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.name"
                label="实例名 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :rules="[
                  (v: string) =>
                    /^[a-z0-9][a-z0-9-]*$/.test(v) || '仅小写字母、数字与连字符，且以字母数字开头',
                  (v: string) => v.length <= 55 || '最多 55 个字符',
                ]"
                hint="名称会进入容器主机名与 SSH 命令，创建后不可修改"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.displayName"
                label="显示名称"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceGroupId"
                :items="grants"
                item-title="displayName"
                item-value="resourceGroupId"
                label="资源组 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :loading="loadingPools"
                :disabled="isAdmin && form.workspaceId === null"
                :no-data-text="
                  isAdmin && form.workspaceId === null ? '请先选择工作空间' : '该工作空间未获授权'
                "
                @update:model-value="selectPool"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceSpecId"
                :items="availableSpecs"
                item-title="displayName"
                item-value="id"
                label="规格 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :disabled="!selectedPool"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item v-bind="itemProps" :subtitle="specShape(item)" />
                </template>
              </v-select>
            </v-col>

            <v-col cols="12">
              <ImagePicker
                v-model="form.image"
                label="镜像 *"
                hint="目录中没有想要的镜像时，可以直接输入完整地址"
              />
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
                hint="个人主目录会自动挂载，不需要在这里选择。附加的存储不重复占用实例配额。"
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

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.ideType"
                :items="IDE_OPTIONS"
                item-title="title"
                item-value="value"
                label="Web IDE"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item
                    v-bind="itemProps"
                    :prepend-icon="item.icon"
                    :title="item.title"
                  />
                </template>
              </v-select>
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.maxRuntimeSeconds"
                :items="runtimeOptions"
                label="最长运行时长"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :disabled="!selectedPool"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="form.storageGb"
                type="number"
                label="主目录存储 (GB)"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :max="storageCap ?? undefined"
                :hint="storageCap ? `资源组上限 ${storageCap} GB` : undefined"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="6" class="d-flex align-center">
              <v-switch
                v-model="form.sshEnabled"
                color="primary"
                label="启用 SSH"
                hide-details
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <v-alert
                v-if="form.sshEnabled && !hasSshKey"
                type="info"
                variant="tonal"
                density="compact"
                class="mt-2"
              >
                你还没有登记 SSH 公钥，实例仍会创建但无法登录。请先到
                <RouterLink to="/profile">个人设置</RouterLink> 添加公钥。
              </v-alert>
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            :loading="creating"
            :disabled="!canSubmit"
            @click="submitCreate"
          >
            创建
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Detail -->
    <v-navigation-drawer
      v-model="detailOpen"
      location="right"
      temporary
      width="720"
      class="wl-detail-drawer"
    >
      <div v-if="detail" class="pa-5">
        <div class="d-flex align-center ga-2 mb-1">
          <h2 class="text-h6">{{ detail.displayName }}</h2>
          <StatusChip :status="detail.status" />
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" size="small" @click="detailOpen = false" />
        </div>
        <div class="text-caption text-medium-emphasis wl-mono mb-4">
          {{ detail.workspaceName }} / {{ detail.name }}
        </div>

        <v-alert
          v-if="detail.statusMessage"
          :type="detail.status === 'error' ? 'error' : 'info'"
          variant="tonal"
          density="compact"
          class="mb-4"
        >
          {{ detail.statusMessage }}
        </v-alert>

        <v-tabs v-model="detailTab" density="compact" class="mb-4">
          <v-tab value="overview">概览</v-tab>
          <v-tab value="connect">连接</v-tab>
          <v-tab value="console" :disabled="!isInteractive(detail)">终端</v-tab>
        </v-tabs>

        <v-window v-model="detailTab">
          <v-window-item value="overview">
            <v-table density="compact">
              <tbody>
                <tr>
                  <th class="wl-key">状态</th>
                  <td><StatusChip :status="detail.status" /></td>
                </tr>
                <tr>
                  <th class="wl-key">规格</th>
                  <td>{{ shapeOf(detail) }}</td>
                </tr>
                <tr>
                  <th class="wl-key">存储</th>
                  <td>{{ detail.storageGb }} GB</td>
                </tr>
                <tr>
                  <th class="wl-key">镜像</th>
                  <td class="wl-mono">{{ detail.image }}</td>
                </tr>
                <tr>
                  <th class="wl-key">Web IDE</th>
                  <td>{{ ideLabel(detail.ideType) }}</td>
                </tr>
                <tr>
                  <th class="wl-key">所在节点</th>
                  <td class="wl-mono">{{ detail.nodeName ?? '未调度' }}</td>
                </tr>
                <tr>
                  <th class="wl-key">启动时间</th>
                  <td>{{ formatTime(detail.startedAt) }}</td>
                </tr>
                <tr>
                  <th class="wl-key">到期时间</th>
                  <td :class="{ 'text-error': detail.isExpired }">
                    {{ detail.expiresAt ? formatTime(detail.expiresAt) : '不限制' }}
                  </td>
                </tr>
                <tr>
                  <th class="wl-key">K8s 对象</th>
                  <td class="wl-mono text-caption">
                    {{ detail.k8sDeployment ?? '—' }}<br />
                    {{ detail.k8sSshService ?? '—' }}<br />
                    {{ detail.k8sIdeService ?? '—' }}<br />
                    {{ detail.k8sPvc ?? '—' }}
                  </td>
                </tr>
              </tbody>
            </v-table>
          </v-window-item>

          <v-window-item value="connect">
            <v-text-field
              :model-value="detail.sshCommand ?? '未分配 SSH 端口'"
              label="SSH 命令"
              readonly
              density="comfortable"
              variant="outlined"
              class="wl-mono"
              append-inner-icon="mdi-content-copy"
              @click:append-inner="copySsh(detail)"
            />
            <v-btn
              v-if="detail.ideType !== 'none'"
              color="primary"
              variant="tonal"
              prepend-icon="mdi-open-in-new"
              :disabled="!isInteractive(detail)"
              @click="openIde(detail)"
            >
              打开 {{ ideLabel(detail.ideType) }}
            </v-btn>
            <p class="text-caption text-medium-emphasis mt-3">
              IDE 链接由后端签发一次性短时票据，每次打开都会重新生成；把地址栏里的链接转给别人会在几分钟后失效。
            </p>
          </v-window-item>

          <v-window-item value="console">
            <InstanceTerminal
              v-if="detailTab === 'console' && detail"
              :instance-id="detail.id"
              :active="detailTab === 'console'"
              :session-key="detail.status"
            />
          </v-window-item>
        </v-window>
      </div>
      <div v-else class="pa-5">
        <v-skeleton-loader type="article" />
      </div>
    </v-navigation-drawer>

    <!-- Logs -->
    <v-dialog v-model="logsOpen" max-width="900" scrollable>
      <v-card>
        <v-card-title class="d-flex align-center">
          容器日志
          <span v-if="logInstance" class="text-caption text-medium-emphasis ms-2">
            {{ logInstance.name }}
          </span>
          <v-spacer />
          <v-btn-toggle
            :model-value="logsPrevious ? 1 : 0"
            density="compact"
            variant="outlined"
            divided
            mandatory
            @update:model-value="
              (value: unknown) => {
                const previous = value === 1
                if (logInstance && previous !== logsPrevious) void openLogs(logInstance, previous)
              }
            "
          >
            <v-btn :value="0" size="small">当前</v-btn>
            <v-btn :value="1" size="small">上次崩溃</v-btn>
          </v-btn-toggle>
        </v-card-title>
        <v-card-text style="max-height: 60vh">
          <v-progress-linear v-if="logsLoading" indeterminate class="mb-3" />
          <pre class="wl-logs">{{ logs }}</pre>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="logsOpen = false">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Rename -->
    <v-dialog v-model="renameOpen" max-width="520">
      <v-card>
        <v-card-title>编辑实例</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="renameForm.displayName"
            label="显示名称"
            density="comfortable"
            variant="outlined"
          />
          <v-textarea
            v-model="renameForm.description"
            label="备注"
            rows="2"
            density="comfortable"
            variant="outlined"
          />
          <p class="text-caption text-medium-emphasis">
            资源规格、镜像与存储创建后不可更改——需要不同配置时请新建实例。
          </p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="renameOpen = false">取消</v-btn>
          <v-btn color="primary" @click="submitRename">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="删除实例"
      :text="
        deleteTarget
          ? `将删除实例 ${deleteTarget.name} 及其主目录数据（${deleteTarget.storageGb} GB）。此操作不可撤销。`
          : ''
      "
      confirm-text="删除"
      confirm-color="error"
      @confirm="submitDelete"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
}

.wl-key {
  width: 110px;
  font-weight: 500;
  color: $title-grey;
}

.wl-logs {
  margin: 0;
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-size: 0.8125rem;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

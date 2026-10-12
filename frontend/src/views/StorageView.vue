<script setup lang="ts">
/**
 * Managed storage: one directory, one volume.
 *
 * The page is built around one idea: a volume is a *directory the platform names*,
 * not space it allocates. It exists before any instance mounts it and outlives
 * every instance that did, and its identity is the path — so the path is a column
 * rather than a detail.
 *
 * Every mutation needs `storage:manage`, because the layout is provisioned outside
 * the platform. A user reads their own volume and their group's shares; an
 * administrator places them.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { listGroups, type GroupSummary } from '@/api/groups'
import {
  createStorage,
  deleteStorage,
  getStorage,
  getStorageUsage,
  listStorageEvents,
  listStorages,
  reconcileStorage,
  resizeStorage,
  updateStorage,
} from '@/api/storages'
import { listUsers, type UserSummary } from '@/api/users'
import { getMyWorkspace, listWorkspaces } from '@/api/workspaces'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { WorkloadEvent } from '@/types/application'
import type {
  StorageDetail,
  StorageKind,
  StorageStatus,
  StorageSummary,
  StorageUsage,
} from '@/types/storage'
import {
  MAX_CAPACITY_GB,
  MIN_CAPACITY_GB,
  STORAGE_KINDS,
  storageKindMeta,
} from '@/types/storage'
import type { WorkspaceSummary } from '@/types/resource'

const { can, isAdmin } = usePermissions()
const notifications = useNotificationStore()

const canManage = computed(() => can('storage:manage'))

// --- listing -----------------------------------------------------------------
const rows = ref<StorageSummary[]>([])
const total = ref(0)
const usage = ref<StorageUsage | null>(null)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  kind: null as StorageKind | null,
  status: null as StorageStatus | null,
  ownedOnly: false,
  includeInactive: false,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listStorages({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      kind: filters.kind ?? undefined,
      status: filters.status ?? undefined,
      ownedOnly: filters.ownedOnly || undefined,
      includeInactive: filters.includeInactive || undefined,
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

async function loadUsage(): Promise<void> {
  try {
    usage.value = await getStorageUsage()
  } catch {
    // The header total is a convenience; its absence must not blank the page.
    usage.value = null
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
watch(
  () => [
    filters.kind,
    filters.status,
    filters.ownedOnly,
    filters.includeInactive,
    page.value,
    pageSize.value,
  ],
  () => void load(),
)

onMounted(async () => {
  await Promise.all([load(), loadUsage(), loadWorkspaces(), loadBindings()])
})

// --- workspace lookups -------------------------------------------------------
const myWorkspace = ref<WorkspaceSummary | null>(null)
const workspaces = ref<WorkspaceSummary[]>([])

async function loadWorkspaces(): Promise<void> {
  try {
    if (isAdmin.value) {
      const result = await listWorkspaces({ pageSize: 200 })
      workspaces.value = result.items
    } else {
      myWorkspace.value = await getMyWorkspace()
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

/** A global share has no workspace at all, which is a real answer and not a gap. */
const workspaceName = (id?: number | null): string => {
  if (id === null || id === undefined) return '—'
  return (
    workspaces.value.find((row) => row.id === id)?.displayName ||
    (myWorkspace.value?.id === id ? myWorkspace.value.displayName : '') ||
    `#${id}`
  )
}

// --- create / edit dialog ----------------------------------------------------
const dialogOpen = ref(false)
const saving = ref(false)
const editing = ref<StorageDetail | null>(null)

const form = reactive({
  name: '',
  displayName: '',
  description: '',
  kind: 'user_volume' as StorageKind,
  ownerUserId: null as number | null,
  groupId: null as number | null,
  dirName: '',
  capacityGb: 20,
  source: '',
  isReadOnly: false,
})

const selectedKind = computed(() => storageKindMeta(form.kind))
const isEditing = computed(() => editing.value !== null)

// --- who a volume is bound to ------------------------------------------------
//
// Loaded only for a caller who may create, because the pickers only exist in that
// dialog. A page that fetched every user for a reader would be disclosing the
// roster to somebody who has no use for it.
const groups = ref<GroupSummary[]>([])
const users = ref<UserSummary[]>([])

async function loadBindings(): Promise<void> {
  if (!canManage.value) return
  try {
    const [groupPage, userPage] = await Promise.all([
      listGroups({ pageSize: 200 }),
      listUsers({ pageSize: 200 }),
    ])
    groups.value = groupPage.items
    users.value = userPage.items
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

/**
 * How many workspaces each group has.
 *
 * A volume's claim lives in a namespace, so its directory must resolve to *the*
 * workspace that owns it. Zero workspaces means there is nowhere to put the claim;
 * two or more means the owner is ambiguous. The API refuses both
 * (`group_has_no_workspace` / `group_has_multiple_workspaces`), and both are
 * decidable here from the workspace list this page already loads — so the form
 * never offers a group whose submission is guaranteed to fail.
 */
const groupWorkspaceCount = computed(() => {
  const counts = new Map<number, number>()
  for (const row of workspaces.value) {
    counts.set(row.groupId, (counts.get(row.groupId) ?? 0) + 1)
  }
  return counts
})

/** Every group, marked with whether a volume can be bound to it and why not. */
const groupOptions = computed(() =>
  groups.value.map((group) => {
    const count = groupWorkspaceCount.value.get(group.id) ?? 0
    if (count === 1) return { ...group, storageDisabled: false, storageReason: '' }
    return {
      ...group,
      storageDisabled: true,
      storageReason:
        count === 0
          ? '还没有工作空间 —— 存储卷的声明无处存放'
          : `有 ${count} 个工作空间 —— 无法确定存储卷归属`,
    }
  }),
)

/** Groups a volume may actually be bound to: exactly one workspace, no more. */
const eligibleGroups = computed(() => groupOptions.value.filter((row) => !row.storageDisabled))

/**
 * The same test applied to a *user*.
 *
 * A user volume takes its group from the owner (`owner.primary_group`), never from
 * the request — the directory is `{root}/{group}/{username}`, so letting the caller
 * name a different group would put one group's user inside another group's
 * directory. That means picking an owner is also picking a group, and an owner
 * whose group cannot hold a claim cannot have a volume either.
 */
const userOptions = computed(() =>
  users.value.map((user) => {
    if (user.primaryGroupId === null) {
      return {
        ...user,
        storageDisabled: true,
        storageReason: '不属于任何用户组 —— 无法确定存储目录',
      }
    }
    const label = user.primaryGroupName ?? `#${user.primaryGroupId}`
    const count = groupWorkspaceCount.value.get(user.primaryGroupId) ?? 0
    if (count === 1) return { ...user, storageDisabled: false, storageReason: '' }
    return {
      ...user,
      storageDisabled: true,
      storageReason:
        count === 0
          ? `组「${label}」还没有工作空间 —— 存储卷的声明无处存放`
          : `组「${label}」有 ${count} 个工作空间 —— 无法确定存储卷归属`,
    }
  }),
)

/** Whether *any* group could own a volume: false means creation cannot succeed. */
const anyGroupEligible = computed(() => eligibleGroups.value.length > 0)
const anyUserEligible = computed(() => userOptions.value.some((row) => !row.storageDisabled))

/**
 * The name is fixed after creation.
 *
 * It is part of the PV and PVC names in the cluster, so changing it here would
 * either rename nothing or orphan the claim — and a volume whose record and whose
 * claim disagree is worse than one with an awkward name.
 */
const nameEditable = computed(() => !isEditing.value)

function openCreate(): void {
  editing.value = null
  Object.assign(form, {
    name: '',
    displayName: '',
    description: '',
    kind: 'user_volume' as StorageKind,
    // Both defaults skip ineligible rows: a user volume is bound to the owner's
    // group, so an owner whose group cannot hold a claim would fail on submit.
    ownerUserId: userOptions.value.find((row) => !row.storageDisabled)?.id ?? null,
    groupId: eligibleGroups.value[0]?.id ?? null,
    dirName: '',
    capacityGb: 20,
    source: '',
    isReadOnly: false,
  })
  dialogOpen.value = true
}

async function openEdit(row: StorageSummary): Promise<void> {
  try {
    const detail = await getStorage(row.id)
    editing.value = detail
    Object.assign(form, {
      name: detail.name,
      displayName: detail.displayName,
      description: detail.description ?? '',
      kind: detail.kind,
      ownerUserId: detail.ownerUserId ?? null,
      groupId: detail.groupId ?? null,
      dirName: detail.dirName ?? '',
      capacityGb: detail.capacityGb,
      source: detail.source ?? '',
      isReadOnly: detail.isReadOnly,
    })
    dialogOpen.value = true
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

const formValid = computed(() => {
  if (form.capacityGb < MIN_CAPACITY_GB || form.capacityGb > MAX_CAPACITY_GB) return false
  if (!nameEditable.value) return true
  if (!/^[a-z0-9][a-z0-9-]*$/.test(form.name)) return false
  // The directory is derived from these, so a missing one is not a preference the
  // server could default — it is a volume with no path to point at. Eligibility is
  // checked here as well as in the pickers: the server refuses an unresolvable
  // group, and a button that is enabled for a request that cannot succeed is a
  // worse answer than a disabled one.
  if (selectedKind.value.binding === 'user') {
    if (form.ownerUserId === null) return false
    if (!userOptions.value.some((row) => row.id === form.ownerUserId && !row.storageDisabled)) {
      return false
    }
  }
  if (selectedKind.value.binding === 'group') {
    if (form.groupId === null) return false
    if (!eligibleGroups.value.some((row) => row.id === form.groupId)) return false
  }
  return true
})

async function save(): Promise<void> {
  if (!formValid.value) return
  saving.value = true
  try {
    if (editing.value) {
      await updateStorage(editing.value.id, {
        displayName: form.displayName || form.name,
        description: form.description || null,
        source: form.source || null,
        isReadOnly: form.isReadOnly,
      })
      notifications.success('已保存')
    } else {
      const created = await createStorage({
        name: form.name,
        displayName: form.displayName || form.name,
        description: form.description || null,
        kind: form.kind,
        // Only the field the kind actually uses. Sending the others would create a
        // volume whose record disagrees with its directory.
        ownerUserId: selectedKind.value.binding === 'user' ? form.ownerUserId : null,
        groupId: selectedKind.value.binding === 'group' ? form.groupId : null,
        // Empty means "the group's own directory", which is a real choice and not
        // an omission, so it is sent as null rather than as an empty string.
        dirName: selectedKind.value.hasDirName ? form.dirName.trim() || null : null,
        capacityGb: form.capacityGb,
        isReadOnly: form.isReadOnly || undefined,
      })
      notifications.success(`存储 ${created.displayName} 已创建`)
    }
    dialogOpen.value = false
    await Promise.all([load(), loadUsage()])
  } catch (error) {
    // `throw` on a non-ApiError would be silent *here*: the dialog stays open, the
    // button re-enables, and the user sees nothing happen. A failure the user
    // cannot see is worse than a vague message, so anything unexpected is still
    // announced.
    notifications.error(
      error instanceof ApiError ? error.message : '创建存储失败，请稍后重试或查看服务端日志',
    )
    if (!(error instanceof ApiError)) console.error('createStorage failed', error)
  } finally {
    saving.value = false
  }
}

// --- resize ------------------------------------------------------------------
const resizeOpen = ref(false)
const resizeTarget = ref<StorageDetail | null>(null)
const resizeValue = ref(0)
const resizing = ref(false)

async function openResize(row: StorageSummary): Promise<void> {
  try {
    const detail = await getStorage(row.id)
    resizeTarget.value = detail
    // Start above the current size: capacity only grows, so pre-filling the
    // current value would offer a number the API will reject.
    resizeValue.value = detail.capacityGb + 10
    resizeOpen.value = true
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

const resizeValid = computed(
  () => resizeTarget.value !== null && resizeValue.value > resizeTarget.value.capacityGb,
)

async function confirmResize(): Promise<void> {
  if (!resizeValid.value || !resizeTarget.value) return
  resizing.value = true
  try {
    await resizeStorage(resizeTarget.value.id, resizeValue.value)
    notifications.success(`已扩容到 ${resizeValue.value} GB`)
    resizeOpen.value = false
    await Promise.all([load(), loadUsage()])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    resizing.value = false
  }
}

// --- reconcile ---------------------------------------------------------------
const reconcilingId = ref<number | null>(null)

/**
 * Ask the cluster what is actually there.
 *
 * Worth a button because a volume stuck at "pending" is the normal state for a
 * `WaitForFirstConsumer` storage class until a pod schedules, and "is it stuck
 * or just waiting?" is the question people actually have.
 */
async function reconcile(row: StorageSummary): Promise<void> {
  reconcilingId.value = row.id
  try {
    const detail = await reconcileStorage(row.id)
    if (detail.status === 'failed') {
      notifications.error(detail.statusMessage || '存储下发失败')
    } else if (detail.statusMessage) {
      notifications.info(detail.statusMessage)
    } else {
      notifications.success('状态已刷新')
    }
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    reconcilingId.value = null
  }
}

// --- delete ------------------------------------------------------------------
const deleteOpen = ref(false)
const deleteTarget = ref<StorageSummary | null>(null)
const deleting = ref(false)

function openDelete(row: StorageSummary): void {
  deleteTarget.value = row
  deleteOpen.value = true
}

async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteStorage(deleteTarget.value.id)
    notifications.success('存储已释放，记录已保留')
    deleteOpen.value = false
    await Promise.all([load(), loadUsage()])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // 409 names the instances still holding it, which is the whole reason the
    // API refuses rather than cascading.
    const detail = error.detail as { instanceNames?: string[] } | undefined
    const names = detail?.instanceNames?.join('、')
    notifications.error(names ? `${error.message}（${names}）` : error.message)
  } finally {
    deleting.value = false
  }
}

// --- detail drawer -----------------------------------------------------------
const detailOpen = ref(false)
const detail = ref<StorageDetail | null>(null)
const events = ref<WorkloadEvent[]>([])
const detailLoading = ref(false)

async function openDetail(row: StorageSummary): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  events.value = []
  try {
    detail.value = await getStorage(row.id)
    events.value = await listStorageEvents(row.id)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

// --- table -------------------------------------------------------------------
interface Column {
  title: string
  key: string
  sortable?: boolean
  align?: 'start' | 'end'
  width?: string
}

/**
 * Declared explicitly. Left to itself, `v-data-table` builds one column per field
 * of the first row, which puts `pvcName`, `k8sNamespace` and `hostPath` on screen
 * as raw values and leaves the `capacity` and `owner` slots with no column to
 * render into.
 *
 * The directory gets its own column because it *is* the volume: two rows that
 * differ only in `hostPath` are two different volumes, and that is the fastest
 * thing for an administrator to check.
 *
 * The actions column is dropped for a reader. Every mutation needs
 * `storage:manage`, so a reader's row of buttons could only ever produce a 403 —
 * and a visible control that refuses is worse than no control.
 */
const headers = computed<Column[]>(() => {
  const columns: Column[] = [
    { title: '存储', key: 'name' },
    { title: '目录', key: 'hostPath' },
    { title: '类型', key: 'kind', width: '140px' },
    { title: '容量', key: 'capacity', width: '150px' },
    { title: '状态', key: 'status', width: '150px' },
    { title: '拥有者', key: 'owner', width: '160px' },
  ]
  if (canManage.value) {
    columns.push({ title: '操作', key: 'actions', align: 'end', width: '180px' })
  }
  return columns
})

// --- helpers -----------------------------------------------------------------
function percentUsed(row: StorageSummary): number {
  if (!row.capacityGb) return 0
  return Math.min(100, Math.round((row.usedGb / row.capacityGb) * 100))
}

function usedTone(row: StorageSummary): string {
  const used = percentUsed(row)
  if (used >= 90) return 'error'
  if (used >= 75) return 'warning'
  return 'primary'
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

const EVENT_LABELS: Record<string, string> = {
  created: '已创建',
  started: '已开始',
  stopped: '已停止',
  deleted: '已释放',
  failed: '失败',
  status_changed: '状态变化',
  updated: '已更新',
}

/**
 * `v-data-table` hands the row event a payload whose shape differs by version;
 * accepting both keeps the row clickable without depending on which one it is.
 */
function onRowClick(_event: unknown, row: unknown): void {
  const item = (row as { item?: StorageSummary })?.item
  if (item) void openDetail(item)
}

</script>

<template>
  <div>
    <PageHeader title="存储" subtitle="独立于容器的持久化卷：容器可以随时删掉，数据不会跟着走">
      <template #actions>
        <v-btn
          v-if="canManage"
          color="primary"
          variant="flat"
          prepend-icon="mdi-plus"
          @click="openCreate"
        >
          新建存储
        </v-btn>
      </template>
    </PageHeader>

    <!--
      The total is deliberately "what you are charged for", not "what you can
      see": a lab dataset is visible to every member and owned by none of them,
      and counting it here would tell each member they were consuming it.
    -->
    <v-row v-if="usage" dense class="mb-2">
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">我的存储卷</div>
          <div class="text-h5">{{ usage.count }}</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">我的已分配容量</div>
          <div class="text-h5">{{ usage.capacityGb }} GB</div>
        </v-card>
      </v-col>
    </v-row>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索名称"
        style="max-width: 260px"
      />
      <v-select
        v-model="filters.kind"
        :items="STORAGE_KINDS"
        item-title="title"
        item-value="value"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="类型"
        style="max-width: 190px"
      />
      <v-select
        v-model="filters.status"
        :items="[
          { title: '已绑定', value: 'bound' },
          { title: '待绑定', value: 'pending' },
          { title: '目录已登记', value: 'available' },
          { title: '失败', value: 'failed' },
          { title: '已释放', value: 'released' },
        ]"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="状态"
        style="max-width: 170px"
      />
      <v-switch
        v-model="filters.ownedOnly"
        label="只看我拥有的"
        color="primary"
        density="compact"
        hide-details
        inset
      />
      <v-switch
        v-model="filters.includeInactive"
        label="包含已释放"
        color="primary"
        density="compact"
        hide-details
        inset
      />
    </FilterBar>

    <v-card variant="flat" class="wl-card">
      <v-data-table
        :headers="headers"
        :items="rows"
        :loading="loading"
        item-value="id"
        :items-per-page="pageSize"
        :page="page"
        hide-default-footer
        density="comfortable"
        @click:row="onRowClick"
      >
        <template #no-data>
          <EmptyState
            icon="mdi-harddisk-plus-outline"
            title="还没有存储卷"
            description="存储卷把一个存储目录登记到系统里。用户卷由管理员按「组/用户名」创建，创建后会自动挂载到容器的 /home/用户名。"
          >
            <v-btn v-if="canManage" color="primary" variant="flat" @click="openCreate">
              新建存储
            </v-btn>
          </EmptyState>
        </template>

        <template #item.name="{ item }">
          <div class="d-flex align-center ga-2">
            <v-icon :icon="storageKindMeta(item.kind).icon" size="small" />
            <div>
              <div class="font-weight-medium">{{ item.displayName }}</div>
              <div class="text-caption text-medium-emphasis wl-mono">{{ item.name }}</div>
            </div>
          </div>
        </template>

        <template #item.hostPath="{ item }">
          <div class="wl-mono text-caption" style="word-break: break-all">
            {{ item.hostPath }}
          </div>
          <div v-if="item.isGroupLevel" class="text-caption text-warning">
            组本级目录 · 仅组管理员可挂载
          </div>
        </template>

        <template #item.kind="{ item }">
          <v-chip size="small" variant="tonal" label>
            {{ storageKindMeta(item.kind).title }}
          </v-chip>
          <v-icon
            v-if="item.isReadOnly"
            icon="mdi-lock-outline"
            size="x-small"
            class="ml-1"
            title="只读挂载"
          />
        </template>

        <template #item.capacity="{ item }">
          <div style="min-width: 140px">
            <div class="d-flex justify-space-between text-caption">
              <span>{{ item.capacityGb }} GB</span>
              <span class="text-medium-emphasis">{{ percentUsed(item) }}%</span>
            </div>
            <v-progress-linear
              :model-value="percentUsed(item)"
              :color="usedTone(item)"
              height="5"
              rounded
              class="mt-1"
            />
          </div>
        </template>

        <template #item.status="{ item }">
          <div class="d-flex flex-column align-start ga-1">
            <StatusChip :status="item.status" size="small" />
            <span v-if="item.statusMessage" class="text-caption text-medium-emphasis">
              {{ item.statusMessage }}
            </span>
          </div>
        </template>

        <template #item.owner="{ item }">
          <div>{{ item.ownerDisplayName || item.ownerUsername || '—' }}</div>
          <div class="text-caption text-medium-emphasis">
            {{ item.groupName || workspaceName(item.workspaceId) }}
          </div>
        </template>

        <template #item.actions="{ item }">
          <div class="d-flex ga-1 justify-end" @click.stop>
            <v-tooltip text="刷新状态" location="top">
              <template #activator="{ props: tipProps }">
                <v-btn
                  v-bind="tipProps"
                  icon="mdi-refresh"
                  size="small"
                  variant="text"
                  :loading="reconcilingId === item.id"
                  @click="reconcile(item)"
                />
              </template>
            </v-tooltip>
            <v-tooltip text="扩容" location="top">
              <template #activator="{ props: tipProps }">
                <v-btn
                  v-bind="tipProps"
                  icon="mdi-arrow-expand-horizontal"
                  size="small"
                  variant="text"
                  :disabled="!item.isActive || item.isReadOnly"
                  @click="openResize(item)"
                />
              </template>
            </v-tooltip>
            <v-tooltip text="编辑" location="top">
              <template #activator="{ props: tipProps }">
                <v-btn
                  v-bind="tipProps"
                  icon="mdi-pencil-outline"
                  size="small"
                  variant="text"
                  @click="openEdit(item)"
                />
              </template>
            </v-tooltip>
            <v-tooltip text="释放" location="top">
              <template #activator="{ props: tipProps }">
                <v-btn
                  v-bind="tipProps"
                  icon="mdi-delete-outline"
                  size="small"
                  variant="text"
                  color="error"
                  :disabled="!item.isActive"
                  @click="openDelete(item)"
                />
              </template>
            </v-tooltip>
          </div>
        </template>
      </v-data-table>

      <div v-if="total > pageSize" class="d-flex justify-end pa-3">
        <v-pagination
          v-model="page"
          :length="Math.ceil(total / pageSize)"
          :total-visible="7"
          density="comfortable"
        />
      </div>
    </v-card>

    <!-- Create / edit -->
    <v-dialog v-model="dialogOpen" max-width="720" persistent>
      <v-card>
        <v-card-title>{{ isEditing ? '编辑存储' : '新建存储' }}</v-card-title>
        <v-divider />
        <v-card-text>
          <v-row dense>
            <v-col cols="12">
              <v-select
                v-model="form.kind"
                :items="STORAGE_KINDS"
                item-title="title"
                item-value="value"
                label="类型 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :disabled="isEditing"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item v-bind="itemProps" :subtitle="item.description">
                    <template #prepend>
                      <v-icon :icon="item.icon" />
                    </template>
                  </v-list-item>
                </template>
              </v-select>
              <div class="text-caption text-medium-emphasis mt-1">
                {{ selectedKind.description }}
                <template v-if="selectedKind.mountPath">
                  挂载点 <span class="wl-mono">{{ selectedKind.mountPath }}</span>。
                </template>
              </div>
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.name"
                label="名称 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :disabled="!nameEditable"
                :rules="[
                  (v: string) => /^[a-z0-9][a-z0-9-]*$/.test(v) || '只能用小写字母、数字和连字符',
                ]"
                :hint="
                  nameEditable
                    ? '小写字母、数字、连字符。会进入集群里的 PVC 名称，创建后不可修改。'
                    : '名称已固定：它已经是集群中 PVC 名的一部分。'
                "
                persistent-hint
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.displayName"
                label="显示名"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="留空则与名称相同"
              />
            </v-col>

            <v-col v-if="!isEditing" cols="12" sm="6">
              <v-text-field
                v-model.number="form.capacityGb"
                type="number"
                :min="MIN_CAPACITY_GB"
                :max="MAX_CAPACITY_GB"
                label="容量 (GB) *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                hint="创建后只能扩容，不能缩小。"
                persistent-hint
              />
            </v-col>

            <!--
              The directory is derived, never typed. Which field is required
              depends on the kind, and the backend takes the group from the user
              for a user volume, so only a group share asks for one.

              The workspace is not asked for at all: one group has exactly one, so
              a picker would offer a choice that does not exist.
            -->
            <v-col v-if="selectedKind.binding === 'user' && !isEditing" cols="12" sm="6">
              <v-select
                v-model="form.ownerUserId"
                :items="userOptions"
                item-title="displayName"
                item-value="id"
                label="用户 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :rules="[(v: number | null) => v !== null || '请选择用户']"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item
                    v-bind="itemProps"
                    :disabled="item.storageDisabled"
                    :subtitle="item.storageDisabled ? item.storageReason : item.username"
                    :title="item.displayName || item.username"
                  />
                </template>
                <template #selection="{ item }">
                  {{ item.displayName || item.username }}
                </template>
              </v-select>
              <div class="text-caption text-medium-emphasis mt-1">
                目录取自该用户所在组：
                <span class="wl-mono">/storage/kube-wl/{组}/{用户名}</span>
              </div>
              <v-alert
                v-if="!anyUserEligible"
                type="warning"
                variant="tonal"
                density="compact"
                class="mt-2"
              >
                还没有用户具备存储条件：用户存储卷的声明要放进该用户所在组的<b>工作空间</b>里，
                而现在没有任何用户组恰好拥有一个工作空间。请先到「工作空间」页面为该组创建一个。
              </v-alert>
            </v-col>

            <v-col v-if="selectedKind.binding === 'group' && !isEditing" cols="12" sm="6">
              <v-select
                v-model="form.groupId"
                :items="groupOptions"
                item-title="displayName"
                item-value="id"
                label="用户组 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                :rules="[(v: number | null) => v !== null || '请选择用户组']"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item
                    v-bind="itemProps"
                    :disabled="item.storageDisabled"
                    :subtitle="item.storageDisabled ? item.storageReason : undefined"
                    :title="item.displayName"
                  />
                </template>
                <template #selection="{ item }">
                  {{ item.displayName }}
                </template>
              </v-select>
              <v-alert
                v-if="!anyGroupEligible"
                type="warning"
                variant="tonal"
                density="compact"
                class="mt-2"
              >
                还没有可用的用户组：存储卷的声明要放进该组的<b>工作空间</b>里，
                而现在没有任何用户组恰好拥有一个工作空间。请先到「工作空间」页面为该组创建一个。
              </v-alert>
            </v-col>

            <v-col v-if="selectedKind.hasDirName && !isEditing" cols="12">
              <v-text-field
                v-model="form.dirName"
                label="目录名"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="留空则登记整个组目录"
                hint="留空会创建「组本级存储卷」，即整个组目录 —— 它包含每个成员的个人目录，因此只有组管理员可以挂载。"
                persistent-hint
              />
            </v-col>

            <v-col v-if="isEditing" cols="12">
              <v-text-field
                v-model="form.source"
                label="备注"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                hint="只对人有用，不参与任何目录推导。"
                persistent-hint
              />
            </v-col>

            <v-col cols="12">
              <v-textarea
                v-model="form.description"
                label="说明"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                rows="2"
                auto-grow
              />
            </v-col>

            <v-col cols="12">
              <v-switch
                v-model="form.isReadOnly"
                label="只读挂载"
                color="primary"
                density="compact"
                hide-details
                inset
              />
              <div class="text-caption text-medium-emphasis">
                开启后容器只能读取。这是挂载级别的限制，不是界面上的提示。
              </div>
            </v-col>
          </v-row>
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialogOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="saving"
            :disabled="!formValid"
            @click="save"
          >
            {{ isEditing ? '保存' : '创建' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Resize -->
    <v-dialog v-model="resizeOpen" max-width="460">
      <v-card v-if="resizeTarget">
        <v-card-title>扩容</v-card-title>
        <v-divider />
        <v-card-text>
          <p class="text-body-2">
            {{ resizeTarget.displayName }} 当前为
            <strong>{{ resizeTarget.capacityGb }} GB</strong>。
          </p>
          <v-text-field
            v-model.number="resizeValue"
            type="number"
            :min="resizeTarget.capacityGb + 1"
            :max="MAX_CAPACITY_GB"
            label="新容量 (GB)"
            density="comfortable"
            variant="outlined"
            hide-details="auto"
            class="mt-2"
            :rules="[
              (v: number) => v > resizeTarget!.capacityGb || '只能扩容，不能缩小',
            ]"
            hint="Kubernetes 无法缩小 PVC，所以这里不接受更小的数字。"
            persistent-hint
          />
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="resizeOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="resizing"
            :disabled="!resizeValid"
            @click="confirmResize"
          >
            扩容
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="释放存储"
      :text="`确定要释放 ${deleteTarget?.displayName ?? ''} 吗？集群中的卷会被删除，数据无法恢复。记录会保留为历史。`"
      confirm-text="释放"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    />

    <!-- Detail -->
    <v-navigation-drawer v-model="detailOpen" location="right" temporary width="520">
      <div v-if="detailLoading" class="d-flex justify-center pa-8">
        <v-progress-circular indeterminate color="primary" />
      </div>
      <div v-else-if="detail" class="pa-4">
        <div class="d-flex align-center justify-space-between mb-3">
          <div>
            <div class="text-h6">{{ detail.displayName }}</div>
            <div class="text-caption text-medium-emphasis wl-mono">{{ detail.name }}</div>
          </div>
          <v-btn icon="mdi-close" variant="text" @click="detailOpen = false" />
        </div>

        <div class="d-flex ga-2 mb-4">
          <StatusChip :status="detail.status" />
          <v-chip size="small" variant="tonal" label>
            {{ storageKindMeta(detail.kind).title }}
          </v-chip>
          <v-chip v-if="detail.isReadOnly" size="small" variant="tonal" color="warning" label>
            只读
          </v-chip>
        </div>

        <v-alert v-if="detail.statusMessage" type="info" variant="tonal" density="compact" class="mb-3">
          {{ detail.statusMessage }}
        </v-alert>

        <v-list density="compact" class="wl-detail-list">
          <v-list-item>
            <template #prepend><v-icon icon="mdi-folder-outline" /></template>
            <v-list-item-title>存储目录</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">{{ detail.hostPath }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-arrow-right-thin" /></template>
            <v-list-item-title>容器内挂载点</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">{{ detail.defaultMountPath }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-harddisk" /></template>
            <v-list-item-title>容量（账面）</v-list-item-title>
            <v-list-item-subtitle>
              {{ detail.capacityGb }} GB
              <span class="text-medium-emphasis">
                · hostPath 不强制容量，这个数字只用于统计
              </span>
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item v-if="detail.isGroupLevel">
            <template #prepend><v-icon icon="mdi-shield-account-outline" /></template>
            <v-list-item-title>组本级目录</v-list-item-title>
            <v-list-item-subtitle>
              这就是整个组目录（{{ detail.groupName || '—' }}），包含每个成员的个人目录。
              普通成员看不到它，只有组管理员可以挂载。
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item v-else-if="detail.dirName">
            <template #prepend><v-icon icon="mdi-folder-multiple-outline" /></template>
            <v-list-item-title>组内目录名</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">{{ detail.dirName }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-account-outline" /></template>
            <v-list-item-title>拥有者</v-list-item-title>
            <v-list-item-subtitle>
              {{ detail.ownerDisplayName || detail.ownerUsername || '—' }} ·
              {{ detail.groupName || workspaceName(detail.workspaceId) }}
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-kubernetes" /></template>
            <v-list-item-title>集群对象</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">
              <template v-if="detail.pvcName">{{ detail.k8sNamespace }} / {{ detail.pvcName }}</template>
              <template v-else>
                尚无存储声明 —— 全局共享卷在每个挂载它的命名空间各需要一个，
                因此要等到第一次被挂载时才创建。
              </template>
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item v-if="detail.source">
            <template #prepend><v-icon icon="mdi-link-variant" /></template>
            <v-list-item-title>备注</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">{{ detail.source }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-clock-outline" /></template>
            <v-list-item-title>最近核对</v-list-item-title>
            <v-list-item-subtitle>{{ formatTime(detail.checkedAt) }}</v-list-item-subtitle>
          </v-list-item>
        </v-list>

        <div class="text-subtitle-2 mt-4 mb-2">
          挂载中的实例
          <span v-if="detail.attachedInstanceNames.length" class="text-medium-emphasis">
            （{{ detail.attachedInstanceNames.length }}）
          </span>
        </div>
        <div v-if="detail.attachedInstanceNames.length" class="d-flex flex-wrap ga-2">
          <v-chip
            v-for="(name, index) in detail.attachedInstanceNames"
            :key="detail.attachedInstanceIds[index]"
            size="small"
            variant="tonal"
            prepend-icon="mdi-cube-outline"
          >
            {{ name }}
          </v-chip>
        </div>
        <div v-else class="text-body-2 text-medium-emphasis">
          当前没有被任何实例挂载，可以安全释放。
        </div>

        <div class="text-subtitle-2 mt-4 mb-2">操作记录</div>
        <v-timeline v-if="events.length" density="compact" side="end" truncate-line="both">
          <v-timeline-item
            v-for="event in events"
            :key="event.id"
            :dot-color="event.eventType === 'failed' ? 'error' : 'primary'"
            size="x-small"
          >
            <div class="text-body-2">{{ EVENT_LABELS[event.eventType] ?? event.eventType }}</div>
            <div class="text-caption text-medium-emphasis">
              {{ formatTime(event.createdAt) }}
              <template v-if="event.actorUsername"> · {{ event.actorUsername }}</template>
            </div>
            <div v-if="event.message" class="text-caption">{{ event.message }}</div>
          </v-timeline-item>
        </v-timeline>
        <div v-else class="text-body-2 text-medium-emphasis">暂无记录。</div>
      </div>
    </v-navigation-drawer>
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
}

.wl-detail-list :deep(.v-list-item) {
  padding-inline: 0;
}

:deep(.v-data-table tbody tr) {
  cursor: pointer;
}
</style>

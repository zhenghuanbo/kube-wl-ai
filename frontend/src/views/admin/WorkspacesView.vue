<script setup lang="ts">
/**
 * Workspace management.
 *
 * A workspace is one Kubernetes namespace plus the people and pools assigned to
 * it. Deleting one tears the namespace down — including everything running
 * inside it — so that action is separated from the rest and confirmed.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { listGroups } from '@/api/groups'
import { listResourceGroups, listQuotas } from '@/api/resources'
import {
  grantResourceGroup,
  listWorkspaceGrants,
  listWorkspaces,
  revokeResourceGroup,
} from '@/api/workspaces'
import {
  assignWorkspaceMember,
  createWorkspace,
  deleteWorkspace,
  getWorkspace,
  listWorkspaceMembers,
  reconcileWorkspace,
  removeWorkspaceMember,
  updateWorkspace,
} from '@/api/workspaces'
import { listUsers } from '@/api/users'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { GroupSummary } from '@/api/groups'
import type {
  Quota,
  ResourceGroupSummary,
  WorkspaceDetail,
  WorkspaceMember,
  WorkspaceResourceGrant,
  WorkspaceStatus,
  WorkspaceSummary,
} from '@/types/resource'
import { formatCpu, formatMemory, formatQuota, quotaResourceLabel } from '@/types/resource'

const { can, isGlobalAdmin, adminGroupIds } = usePermissions()
const notifications = useNotificationStore()

const STATUS_OPTIONS: { value: WorkspaceStatus; title: string }[] = [
  { value: 'active', title: '正常' },
  { value: 'provisioning', title: '下发中' },
  { value: 'error', title: '异常' },
  { value: 'suspended', title: '已停用' },
]

// --- listing -----------------------------------------------------------------
const rows = ref<WorkspaceSummary[]>([])
const groups = ref<GroupSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  groupId: null as number | null,
  status: null as WorkspaceStatus | null,
})

const headers = [
  { title: '工作空间', key: 'displayName', sortable: false },
  { title: '用户组', key: 'groupName', sortable: false },
  { title: '命名空间', key: 'k8sNamespace', sortable: false },
  { title: '成员', key: 'memberCount', sortable: false },
  { title: '资源池', key: 'resourceGroupCount', sortable: false },
  { title: '状态', key: 'status', sortable: false },
  { title: '操作', key: 'actions', sortable: false, align: 'end' as const },
]

/** Only groups the caller may actually create a workspace in. */
const assignableGroups = computed(() =>
  isGlobalAdmin.value
    ? groups.value
    : groups.value.filter((group) => adminGroupIds.value.includes(group.id)),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listWorkspaces({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      groupId: filters.groupId ?? undefined,
      status: filters.status ?? undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
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
watch([() => filters.groupId, () => filters.status], () => {
  page.value = 1
  void load()
})
watch([page, pageSize], load)

// --- create ------------------------------------------------------------------
const createOpen = ref(false)
const creating = ref(false)
const createForm = reactive({
  name: '',
  displayName: '',
  description: '',
  groupId: null as number | null,
  resourceGroupIds: [] as number[],
})
const createError = ref<string | null>(null)
const pools = ref<ResourceGroupSummary[]>([])

const nameValid = computed(() => /^[a-z0-9][a-z0-9-]*$/.test(createForm.name))
const nameHint = computed(() => {
  if (!createForm.name) return '小写字母、数字与连字符，需以字母或数字开头'
  if (!nameValid.value) return '格式不合法：仅限小写字母、数字与连字符'
  const group = groups.value.find((item) => item.id === createForm.groupId)
  const prefix = group ? `wl-${group.name}-` : 'wl-<用户组>-'
  return `命名空间将为 ${prefix}${createForm.name}`
})

function openCreate(): void {
  createForm.name = ''
  createForm.displayName = ''
  createForm.description = ''
  // A group admin administers one lab, so preselect it rather than make them
  // pick from a list of one.
  createForm.groupId =
    assignableGroups.value.length === 1 ? assignableGroups.value[0]!.id : filters.groupId
  createForm.resourceGroupIds = []
  createError.value = null
  createOpen.value = true
}

async function submitCreate(): Promise<void> {
  creating.value = true
  createError.value = null
  try {
    const created = await createWorkspace({
      name: createForm.name,
      displayName: createForm.displayName || createForm.name,
      description: createForm.description || null,
      groupId: createForm.groupId,
      resourceGroupIds: createForm.resourceGroupIds,
    })
    notifications.success(`工作空间 ${created.displayName} 已创建`)
    createOpen.value = false
    await load()
    await openDetail(created.id)
  } catch (error) {
    createError.value = error instanceof ApiError ? error.message : '创建失败'
  } finally {
    creating.value = false
  }
}

// --- detail drawer -----------------------------------------------------------
const detailOpen = ref(false)
const detail = ref<WorkspaceDetail | null>(null)
const members = ref<WorkspaceMember[]>([])
const grants = ref<WorkspaceResourceGrant[]>([])
/** The namespace's ceilings and measured usage, shown in the detail panel. */
const quotas = ref<Quota[]>([])
const detailLoading = ref(false)

async function openDetail(workspaceId: number): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  try {
    detail.value = await getWorkspace(workspaceId)
    ;[members.value, grants.value, quotas.value] = await Promise.all([
      listWorkspaceMembers(workspaceId),
      listWorkspaceGrants(workspaceId),
      // The namespace's own numbers, with usage measured from the workloads. Read
      // here rather than taken from the reconcile response so the panel is right on
      // first open, not only after somebody re-applies by hand.
      listQuotas('workspace', workspaceId),
    ])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
  } finally {
    detailLoading.value = false
  }
}

async function refreshDetail(): Promise<void> {
  if (!detail.value) return
  await openDetail(detail.value.id)
  await load()
}

/**
 * One quota line as `已用 / 上限`, in the unit a person types into the template.
 *
 * The API speaks Kubernetes — CPU in milli-cores, memory in MB — so a raw render of
 * the 24-core default would read "24000 核·毫" and look like the page disagreed with
 * the form that produced it. `-1` is "not checked" and has to bypass the arithmetic
 * entirely, or the ceiling would print as a negative quantity.
 */
function quotaLine(row: Quota): string {
  const limit = row.limitValue < 0 ? '不限' : quotaValue(row, row.limitValue)
  return `${quotaValue(row, row.usedValue)} / ${limit}`
}

function quotaValue(row: Quota, value: number): string {
  if (row.resource === 'cpu') return formatCpu(value)
  if (row.resource === 'memory') return formatMemory(value)
  return formatQuota(value, row.unit)
}

const grantingPoolId = ref<number | null>(null)

/** Pools not already granted, and that this workspace is allowed to use. */
const grantablePools = computed(() => {
  const taken = new Set(grants.value.map((grant) => grant.resourceGroupId))
  return pools.value.filter((pool) => {
    if (taken.has(pool.id) || !pool.isActive) return false
    // A private pool belongs to one lab; offering it elsewhere would only
    // produce a 400 from the backend.
    if (pool.kind === 'private' && pool.groupId !== detail.value?.groupId) return false
    return true
  })
})

async function addGrant(): Promise<void> {
  if (!detail.value || grantingPoolId.value === null) return
  try {
    await grantResourceGroup(detail.value.id, grantingPoolId.value)
    notifications.success('资源池已授权，配额已重新下发')
    grantingPoolId.value = null
    await refreshDetail()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function dropGrant(resourceGroupId: number): Promise<void> {
  if (!detail.value) return
  try {
    await revokeResourceGroup(detail.value.id, resourceGroupId)
    notifications.success('已撤销授权')
    await refreshDetail()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function reconcile(): Promise<void> {
  if (!detail.value) return
  try {
    await reconcileWorkspace(detail.value.id)
    notifications.success('已重新下发命名空间与配额')
    await refreshDetail()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function toggleActive(): Promise<void> {
  if (!detail.value) return
  try {
    await updateWorkspace(detail.value.id, { isActive: !detail.value.isActive })
    notifications.success(detail.value.isActive ? '已停用' : '已启用')
    await refreshDetail()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

// --- members -----------------------------------------------------------------
const memberDialogOpen = ref(false)
const memberSearch = ref('')
/** Title carries both the name and the login, so no custom slot is needed. */
const memberCandidates = ref<{ id: number; title: string }[]>([])
const memberSearching = ref(false)
const selectedUserId = ref<number | null>(null)
const moveFromExisting = ref(false)
const memberError = ref<string | null>(null)

let memberTimer: ReturnType<typeof setTimeout> | undefined
watch(memberSearch, (value) => {
  clearTimeout(memberTimer)
  if (!value.trim()) {
    memberCandidates.value = []
    return
  }
  memberTimer = setTimeout(async () => {
    memberSearching.value = true
    try {
      const result = await listUsers({ search: value.trim(), pageSize: 10, isActive: true })
      memberCandidates.value = result.items.map((user) => ({
        id: user.id,
        title: `${user.displayName}（${user.username}）`,
      }))
    } finally {
      memberSearching.value = false
    }
  }, 300)
})

function openMemberDialog(): void {
  memberSearch.value = ''
  memberCandidates.value = []
  selectedUserId.value = null
  moveFromExisting.value = false
  memberError.value = null
  memberDialogOpen.value = true
}

async function submitMember(): Promise<void> {
  if (!detail.value || selectedUserId.value === null) return
  memberError.value = null
  try {
    await assignWorkspaceMember(detail.value.id, {
      userId: selectedUserId.value,
      moveFromExisting: moveFromExisting.value,
    })
    notifications.success('成员已添加')
    memberDialogOpen.value = false
    await refreshDetail()
  } catch (error) {
    // The backend refuses a silent relocation; surface the reason and let the
    // administrator opt in explicitly rather than retrying blindly.
    memberError.value = error instanceof ApiError ? error.message : '添加失败'
  }
}

async function dropMember(userId: number): Promise<void> {
  if (!detail.value) return
  try {
    await removeWorkspaceMember(detail.value.id, userId)
    notifications.success('成员已移除')
    await refreshDetail()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

// --- delete ------------------------------------------------------------------
const confirmOpen = ref(false)
const deleting = ref(false)
const pendingDelete = ref<WorkspaceSummary | null>(null)

function askDelete(workspace: WorkspaceSummary): void {
  pendingDelete.value = workspace
  confirmOpen.value = true
}

async function confirmDelete(): Promise<void> {
  if (!pendingDelete.value) return
  deleting.value = true
  try {
    await deleteWorkspace(pendingDelete.value.id)
    notifications.success('工作空间已删除，命名空间已销毁')
    confirmOpen.value = false
    detailOpen.value = false
    pendingDelete.value = null
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    deleting.value = false
  }
}

// --- init --------------------------------------------------------------------
onMounted(async () => {
  const [groupPage, poolPage] = await Promise.all([
    listGroups({ pageSize: 200, isActive: true }),
    listResourceGroups({ pageSize: 200, isActive: true }),
  ])
  groups.value = groupPage.items
  pools.value = poolPage.items
  await load()
})
</script>

<template>
  <div>
    <PageHeader title="工作空间管理" subtitle="每个工作空间对应一个 Kubernetes 命名空间">
      <template #actions>
        <v-btn
          v-if="can('workspace:create')"
          color="primary"
          prepend-icon="mdi-plus"
          @click="openCreate"
        >
          新建工作空间
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
        placeholder="搜索名称或命名空间"
        clearable
        style="max-width: 280px"
      />
      <v-select
        v-if="assignableGroups.length > 1"
        v-model="filters.groupId"
        :items="assignableGroups"
        item-title="displayName"
        item-value="id"
        density="compact"
        variant="outlined"
        hide-details
        clearable
        label="用户组"
        style="max-width: 220px"
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
          <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
        </template>

        <template #item.groupName="{ item }">
          {{ item.groupDisplayName ?? item.groupName ?? '—' }}
        </template>

        <template #item.k8sNamespace="{ item }">
          <code class="wl-namespace">{{ item.k8sNamespace }}</code>
        </template>

        <template #item.status="{ item }">
          <StatusChip :status="item.status" />
        </template>

        <template #item.actions="{ item }">
          <v-btn
            size="small"
            variant="text"
            color="primary"
            @click="openDetail(item.id)"
          >
            详情
          </v-btn>
          <v-btn
            v-if="can('workspace:delete')"
            size="small"
            variant="text"
            color="error"
            @click="askDelete(item)"
          >
            删除
          </v-btn>
        </template>

        <template #no-data>
          <EmptyState
            icon="mdi-folder-multiple-outline"
            title="暂无工作空间"
            description="新建工作空间后，用户即可在获得授权的资源池上创建开发实例。"
          />
        </template>
      </v-data-table>
    </v-card>

    <!-- create -->
    <v-dialog v-model="createOpen" max-width="600" persistent>
      <v-card>
        <v-card-title>新建工作空间</v-card-title>
        <v-card-text>
          <v-alert
            v-if="createError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-4 wl-form-error"
          >
            {{ createError }}
          </v-alert>

          <v-text-field
            v-model="createForm.name"
            label="标识 *"
            :error-messages="createForm.name && !nameValid ? nameHint : ''"
            :hint="createForm.name && nameValid ? nameHint : '小写字母、数字与连字符，需以字母或数字开头'"
            persistent-hint
            class="mb-4"
          />
          <v-text-field v-model="createForm.displayName" label="显示名称" class="mb-2" />
          <v-textarea
            v-model="createForm.description"
            label="描述"
            rows="2"
            auto-grow
            class="mb-2"
          />

          <v-select
            v-model="createForm.groupId"
            :items="assignableGroups"
            item-title="displayName"
            item-value="id"
            label="所属用户组 *"
            class="mb-2"
          />

          <v-select
            v-model="createForm.resourceGroupIds"
            :items="pools"
            item-title="displayName"
            item-value="id"
            label="初始资源池（可稍后授权）"
            multiple
            chips
            closable-chips
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="creating"
            :disabled="!nameValid || !createForm.groupId"
            @click="submitCreate"
          >
            创建
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- detail -->
    <v-navigation-drawer
      v-model="detailOpen"
      location="right"
      temporary
      width="560"
      class="wl-detail"
    >
      <div v-if="detail" class="pa-6">
        <div class="d-flex align-start justify-space-between ga-4 mb-1">
          <div>
            <div class="text-h6">{{ detail.displayName }}</div>
            <code class="wl-namespace">{{ detail.k8sNamespace }}</code>
          </div>
          <v-btn icon="mdi-close" variant="text" size="small" @click="detailOpen = false" />
        </div>

        <div class="d-flex align-center ga-2 mt-3 mb-4 flex-wrap">
          <StatusChip :status="detail.status" />
          <v-chip size="small" variant="tonal">
            {{ detail.groupDisplayName ?? detail.groupName }}
          </v-chip>
          <v-chip v-if="!detail.isActive" size="small" variant="tonal" color="warning">
            已停用
          </v-chip>
        </div>

        <v-alert
          v-if="detail.status === 'error'"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-4"
          :text="detail.statusMessage ?? '下发失败'"
        />

        <div class="d-flex ga-2 mb-6 flex-wrap">
          <v-btn
            v-if="can('workspace:update')"
            size="small"
            variant="tonal"
            prepend-icon="mdi-refresh"
            @click="reconcile"
          >
            重新下发
          </v-btn>
          <v-btn
            v-if="can('workspace:update')"
            size="small"
            variant="tonal"
            :prepend-icon="detail.isActive ? 'mdi-pause' : 'mdi-play'"
            @click="toggleActive"
          >
            {{ detail.isActive ? '停用' : '启用' }}
          </v-btn>
          <v-btn
            v-if="can('workspace:delete')"
            size="small"
            variant="tonal"
            color="error"
            prepend-icon="mdi-delete-outline"
            @click="askDelete(detail)"
          >
            删除
          </v-btn>
        </div>

        <v-progress-linear v-if="detailLoading" indeterminate class="mb-4" />

        <!-- members -->
        <div class="d-flex align-center justify-space-between mb-2">
          <h3 class="text-subtitle-1">成员</h3>
          <v-btn
            v-if="can('workspace:assign_member')"
            size="small"
            variant="text"
            prepend-icon="mdi-account-plus-outline"
            @click="openMemberDialog"
          >
            添加
          </v-btn>
        </div>

        <v-list v-if="members.length" density="compact" class="wl-detail__list mb-6">
          <v-list-item v-for="member in members" :key="member.userId">
            <v-list-item-title>
              {{ member.displayName }}
              <v-chip
                v-if="member.roleInWorkspace === 'owner'"
                size="x-small"
                variant="tonal"
                color="primary"
                class="ms-2"
              >
                负责人
              </v-chip>
            </v-list-item-title>
            <v-list-item-subtitle>{{ member.username }}</v-list-item-subtitle>
            <template #append>
              <v-btn
                v-if="can('workspace:assign_member')"
                icon="mdi-close"
                size="x-small"
                variant="text"
                @click="dropMember(member.userId)"
              />
            </template>
          </v-list-item>
        </v-list>
        <p v-else class="text-body-2 text-medium-emphasis mb-6">尚未分配成员</p>

        <!-- resource grants -->
        <h3 class="text-subtitle-1 mb-2">资源池授权</h3>
        <v-list v-if="grants.length" density="compact" class="wl-detail__list mb-3">
          <v-list-item v-for="grant in grants" :key="grant.resourceGroupId">
            <v-list-item-title>{{ grant.displayName }}</v-list-item-title>
            <v-list-item-subtitle>
              {{ grant.kind === 'public' ? '公共资源池' : '专用资源池' }}
            </v-list-item-subtitle>
            <template #append>
              <v-btn
                v-if="can('workspace:grant_resource')"
                icon="mdi-close"
                size="x-small"
                variant="text"
                @click="dropGrant(grant.resourceGroupId)"
              />
            </template>
          </v-list-item>
        </v-list>
        <p v-else class="text-body-2 text-medium-emphasis mb-3">
          尚未授权资源池，该工作空间无法调度任何负载
        </p>

        <div v-if="can('workspace:grant_resource')" class="d-flex ga-2">
          <v-select
            v-model="grantingPoolId"
            :items="grantablePools"
            item-title="displayName"
            item-value="id"
            density="compact"
            variant="outlined"
            hide-details
            placeholder="选择要授权的资源池"
            :no-data-text="'没有可授权的资源池'"
          />
          <v-btn
            color="primary"
            variant="flat"
            :disabled="grantingPoolId === null"
            @click="addGrant"
          >
            授权
          </v-btn>
        </div>

        <!-- The namespace's quota, with the usage measured from the workloads. -->
        <h3 class="text-subtitle-1 mb-2 mt-4">命名空间配额</h3>
        <v-table v-if="quotas.length" density="compact" class="wl-detail__list">
          <tbody>
            <tr v-for="row in quotas" :key="row.id">
              <td class="text-medium-emphasis">{{ quotaResourceLabel(row.resource) }}</td>
              <td class="text-end wl-num">{{ quotaLine(row) }}</td>
            </tr>
          </tbody>
        </v-table>
        <p v-else class="text-body-2 text-medium-emphasis">
          尚未授权资源池，该命名空间只允许 0 个 Pod
        </p>
        <p class="text-caption text-medium-emphasis mt-2 mb-0">
          算力三项来自该工作空间的配额模板（管理 → 配额管理），存储上限来自已授权资源池的合计；
          「不限」表示没有设这一项，而不是设成了 0。
        </p>
      </div>
    </v-navigation-drawer>

    <!-- member dialog -->
    <v-dialog v-model="memberDialogOpen" max-width="460" persistent>
      <v-card>
        <v-card-title>添加成员</v-card-title>
        <v-card-text>
          <v-alert
            v-if="memberError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-4 wl-form-error"
          >
            {{ memberError }}
          </v-alert>
          <p class="text-body-2 text-medium-emphasis mb-4">
            一名用户只能属于一个工作空间。
          </p>

          <v-autocomplete
            v-model="selectedUserId"
            v-model:search="memberSearch"
            :items="memberCandidates"
            item-title="title"
            item-value="id"
            :loading="memberSearching"
            label="搜索用户"
            prepend-inner-icon="mdi-magnify"
            hide-no-data
            no-filter
            class="mb-2"
          />

          <v-checkbox
            v-model="moveFromExisting"
            density="compact"
            hide-details
            class="mt-3"
            label="若该用户已有工作空间，将其迁移至此"
          />
          <p class="text-caption text-medium-emphasis">
            迁移会移动其文件目录与运行中的实例，请确认后勾选。
          </p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="memberDialogOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :disabled="selectedUserId === null"
            @click="submitMember"
          >
            添加
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="confirmOpen"
      title="删除工作空间"
      :text="
        pendingDelete
          ? `将删除 Kubernetes 命名空间 ${pendingDelete.k8sNamespace} 及其中的全部数据与运行实例。此操作不可撤销。`
          : ''
      "
      confirm-text="删除"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-namespace {
  font-family: 'Roboto Mono', ui-monospace, monospace;
  font-size: 0.75rem;
  background: $hover-bg;
  border-radius: 4px;
  padding: 1px 6px;
}

.wl-detail__list {
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 8px;
  padding: 0;
}

// Quota figures sit in a column and must line up; proportional digits make
// "6 核 / 24 核" and "16 GB / 不限" look ragged against each other.
.wl-num {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
</style>

<script setup lang="ts">
/**
 * User-group management.
 *
 * A user group is the tenancy boundary of the platform: it owns workspaces,
 * holds resource-group bindings and is the unit of data scope (plan D7). This
 * page manages the group itself and its membership; workspaces and quotas get
 * their own pages in Phase 2.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  addMember,
  createGroup,
  deleteGroup,
  getGroup,
  listGroups,
  listMembers,
  removeMember,
  syncGroups,
  updateGroup,
  updateMember,
  type GroupMember,
  type GroupSummary,
  type GroupSyncResult,
} from '@/api/groups'
import { listUsers } from '@/api/users'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import { GROUP_ROLE_LABELS, type GroupKind, type GroupRole } from '@/types/auth'

const { can } = usePermissions()
const notifications = useNotificationStore()

const KIND_LABELS: Record<GroupKind, string> = { lab: '实验室', public: '公共组' }

// --- listing -----------------------------------------------------------------
const rows = ref<GroupSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  kind: null as GroupKind | null,
  isActive: null as boolean | null,
})

const headers = [
  { title: '用户组', key: 'displayName', sortable: false },
  { title: '标识', key: 'name', sortable: false },
  { title: '目录组名', key: 'directoryName', sortable: false },
  { title: '类型', key: 'kind', sortable: false },
  { title: '成员', key: 'memberCount', sortable: false },
  { title: '状态', key: 'isActive', sortable: false },
  { title: '创建时间', key: 'createdAt', sortable: false },
  { title: '操作', key: 'actions', sortable: false, align: 'end' as const },
]

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listGroups({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      kind: filters.kind ?? undefined,
      isActive: filters.isActive ?? undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
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

watch([() => filters.kind, () => filters.isActive], () => {
  page.value = 1
  void load()
})

onMounted(load)

// --- create / edit -----------------------------------------------------------
const editDialog = ref(false)
const editing = ref<GroupSummary | null>(null)
const saving = ref(false)
const formError = ref('')

const form = reactive({
  name: '',
  directoryName: '',
  displayName: '',
  description: '',
  kind: 'lab' as GroupKind,
  isActive: true,
})

const dialogTitle = computed(() =>
  editing.value ? `编辑用户组 · ${editing.value.displayName}` : '新建用户组',
)

function openCreate(): void {
  editing.value = null
  formError.value = ''
  Object.assign(form, {
    name: '',
    directoryName: '',
    displayName: '',
    description: '',
    kind: 'lab' as GroupKind,
    isActive: true,
  })
  editDialog.value = true
}

function openEdit(row: GroupSummary): void {
  editing.value = row
  formError.value = ''
  Object.assign(form, {
    name: row.name,
    directoryName: row.directoryName ?? '',
    displayName: row.displayName,
    description: row.description ?? '',
    kind: row.kind,
    isActive: row.isActive,
  })
  editDialog.value = true
}

async function save(): Promise<void> {
  saving.value = true
  formError.value = ''
  // ``null`` clears it and reverts the group to matching on ``name``.
  const directoryName = form.directoryName.trim() || null
  try {
    if (editing.value) {
      await updateGroup(editing.value.id, {
        directoryName,
        displayName: form.displayName,
        description: form.description || null,
        kind: form.kind,
        isActive: form.isActive,
      })
      notifications.success('用户组已更新')
    } else {
      await createGroup({
        name: form.name,
        directoryName,
        displayName: form.displayName || form.name,
        description: form.description || null,
        kind: form.kind,
      })
      notifications.success('用户组已创建')
    }
    editDialog.value = false
    await load()
  } catch (error) {
    formError.value = error instanceof ApiError ? error.message : '保存失败'
  } finally {
    saving.value = false
  }
}

// --- members -----------------------------------------------------------------
const membersOpen = ref(false)
const memberDialog = ref(false)
const activeGroup = ref<GroupSummary | null>(null)
const members = ref<GroupMember[]>([])
const membersLoading = ref(false)
const memberSearch = ref('')

/** Candidate users for the "add member" picker. */
const candidates = ref<{ id: number; username: string; fullName: string }[]>([])
const candidateQuery = ref('')
const selectedUserId = ref<number | null>(null)
const selectedRole = ref<GroupRole>('member')

async function openMembers(row: GroupSummary): Promise<void> {
  activeGroup.value = await getGroup(row.id)
  membersOpen.value = true
  await loadMembers()
}

async function loadMembers(): Promise<void> {
  if (!activeGroup.value) return
  membersLoading.value = true
  try {
    const result = await listMembers(activeGroup.value.id, {
      page: 1,
      pageSize: 200,
      search: memberSearch.value.trim() || undefined,
    })
    members.value = result.items
  } finally {
    membersLoading.value = false
  }
}

let memberTimer: ReturnType<typeof setTimeout> | undefined
watch(memberSearch, () => {
  clearTimeout(memberTimer)
  memberTimer = setTimeout(() => void loadMembers(), 300)
})

let candidateTimer: ReturnType<typeof setTimeout> | undefined
watch(candidateQuery, () => {
  clearTimeout(candidateTimer)
  candidateTimer = setTimeout(async () => {
    const term = candidateQuery.value.trim()
    if (!term) {
      candidates.value = []
      return
    }
    const result = await listUsers({ page: 1, pageSize: 20, search: term })
    const existing = new Set(members.value.map((member) => member.userId))
    candidates.value = result.items
      .filter((user) => !existing.has(user.id))
      .map((user) => ({ id: user.id, username: user.username, fullName: user.fullName }))
  }, 300)
})

async function confirmAddMember(): Promise<void> {
  if (!activeGroup.value || selectedUserId.value === null) return
  saving.value = true
  try {
    await addMember(activeGroup.value.id, {
      userId: selectedUserId.value,
      roleInGroup: selectedRole.value,
    })
    notifications.success('成员已添加')
    memberDialog.value = false
    selectedUserId.value = null
    candidateQuery.value = ''
    candidates.value = []
    await Promise.all([loadMembers(), load()])
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '添加失败')
  } finally {
    saving.value = false
  }
}

async function changeMemberRole(member: GroupMember, role: GroupRole): Promise<void> {
  if (!activeGroup.value) return
  try {
    await updateMember(activeGroup.value.id, member.userId, { roleInGroup: role })
    notifications.success('成员角色已更新')
    await loadMembers()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '更新失败')
  }
}

async function removeSelected(member: GroupMember): Promise<void> {
  if (!activeGroup.value) return
  try {
    await removeMember(activeGroup.value.id, member.userId)
    notifications.success(`已移除 ${member.username}`)
    await Promise.all([loadMembers(), load()])
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '移除失败')
  }
}

// --- directory sync ----------------------------------------------------------
const syncing = ref(false)
const syncResultOpen = ref(false)
const syncResult = ref<GroupSyncResult | null>(null)
const syncResultDryRun = ref(false)

/** Directory groups that nothing in the platform answers to yet. */
const unmatchedGroups = computed(() => syncResult.value?.unmatchedGroups ?? [])

async function runSync(dryRun: boolean): Promise<void> {
  syncing.value = true
  try {
    const result = await syncGroups(dryRun)
    syncResult.value = result
    syncResultDryRun.value = dryRun
    syncResultOpen.value = true
    // A toast alone cannot carry this. "新增 0" with no explanation is exactly
    // the state an administrator hits after creating a lab in the directory and
    // finding that nothing happened, so the counts say what changed and the
    // dialog says what did not and why.
    const summary = `新增 ${result.membershipsAdded}、更新 ${result.membershipsUpdated}、移除 ${result.membershipsRemoved}`
    if (result.unmatchedGroups.length || result.errors.length) {
      notifications.info(
        dryRun ? `预演完成：${summary}，有需要处理的事项` : `同步完成：${summary}，有需要处理的事项`,
      )
    } else {
      notifications.success(dryRun ? `预演完成：${summary}` : `同步完成：${summary}`)
    }
    if (!dryRun) await load()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '同步失败')
  } finally {
    syncing.value = false
  }
}

// --- delete ------------------------------------------------------------------
const confirmOpen = ref(false)
const deleteTarget = ref<GroupSummary | null>(null)

function askDelete(row: GroupSummary): void {
  deleteTarget.value = row
  confirmOpen.value = true
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  try {
    await deleteGroup(target.id)
    notifications.success(`已删除 ${target.displayName}`)
    confirmOpen.value = false
    await load()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '删除失败')
  }
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN')
}
</script>

<template>
  <div class="wl-page">
    <PageHeader title="用户组管理" subtitle="实验室与公共组、成员归属与角色">
      <template #actions>
        <!-- `group:update`, not `group:read`: every role holds `group:read`, so
             gating on it showed this button to everyone and 403'd on click. -->
        <v-btn
          v-if="can('group:update')"
          variant="tonal"
          prepend-icon="mdi-sync"
          rounded="lg"
          :loading="syncing"
          @click="runSync(false)"
        >
          同步目录
        </v-btn>
        <v-btn
          v-if="can('group:create')"
          color="primary"
          prepend-icon="mdi-account-group-outline"
          rounded="lg"
          @click="openCreate"
        >
          新建用户组
        </v-btn>
      </template>
    </PageHeader>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        placeholder="搜索名称或标识"
        prepend-inner-icon="mdi-magnify"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 240px"
      />

      <v-select
        v-model="filters.kind"
        :items="[
          { title: '实验室', value: 'lab' },
          { title: '公共组', value: 'public' },
        ]"
        placeholder="类型"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 150px"
      />

      <v-select
        v-model="filters.isActive"
        :items="[
          { title: '已启用', value: true },
          { title: '已停用', value: false },
        ]"
        placeholder="状态"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 140px"
      />

      <template #actions>
        <v-btn icon="mdi-refresh" variant="text" size="small" :loading="loading" @click="load" />
      </template>
    </FilterBar>

    <v-card class="mt-4" rounded="lg">
      <v-data-table-server
        v-model:page="page"
        v-model:items-per-page="pageSize"
        :headers="headers"
        :items="rows"
        :items-length="total"
        :loading="loading"
        item-value="id"
        hover
        @update:options="load"
      >
        <template #[`item.displayName`]="{ item }">
          <div class="d-flex align-center ga-3 py-1">
            <v-avatar
              size="34"
              :color="item.kind === 'lab' ? 'primary' : 'info'"
              variant="tonal"
            >
              <v-icon
                :icon="item.kind === 'lab' ? 'mdi-flask-outline' : 'mdi-earth'"
                size="18"
              />
            </v-avatar>
            <div>
              <div class="font-weight-medium">{{ item.displayName }}</div>
              <div class="text-caption text-medium-emphasis">
                {{ item.description || '暂无描述' }}
              </div>
            </div>
          </div>
        </template>

        <template #[`item.name`]="{ item }">
          <code class="text-caption">{{ item.name }}</code>
        </template>

        <template #[`item.directoryName`]="{ item }">
          <!-- Falls back to `name`, because that is what matching actually uses
               when no explicit directory name is set. A dash here would read as
               "not synced at all", which is the opposite of the truth. -->
          <code class="text-caption">{{ item.directoryName ?? item.name }}</code>
          <v-chip
            v-if="!item.directoryName"
            size="x-small"
            variant="text"
            class="ml-1 text-medium-emphasis"
          >
            同标识
          </v-chip>
        </template>

        <template #[`item.kind`]="{ item }">
          <v-chip size="small" variant="tonal" label>{{ KIND_LABELS[item.kind] }}</v-chip>
        </template>

        <template #[`item.memberCount`]="{ item }">
          <v-chip size="small" variant="outlined" label>{{ item.memberCount }} 人</v-chip>
        </template>

        <template #[`item.isActive`]="{ item }">
          <v-chip size="small" :color="item.isActive ? 'success' : 'grey'" variant="flat" label>
            {{ item.isActive ? '已启用' : '已停用' }}
          </v-chip>
        </template>

        <template #[`item.createdAt`]="{ item }">
          <span class="text-body-2">{{ formatDate(item.createdAt) }}</span>
        </template>

        <template #[`item.actions`]="{ item }">
          <div class="d-flex justify-end ga-1">
            <v-btn
              icon="mdi-account-multiple-outline"
              size="small"
              variant="text"
              title="成员管理"
              @click="openMembers(item)"
            />
            <v-btn
              v-if="can('group:update')"
              icon="mdi-pencil-outline"
              size="small"
              variant="text"
              title="编辑"
              @click="openEdit(item)"
            />
            <v-btn
              v-if="can('group:delete')"
              icon="mdi-trash-can-outline"
              size="small"
              variant="text"
              color="error"
              title="删除"
              @click="askDelete(item)"
            />
          </div>
        </template>

        <template #no-data>
          <div class="py-10 text-center text-medium-emphasis">
            <v-icon icon="mdi-account-group-outline" size="42" class="mb-2" />
            <div>暂无用户组</div>
          </div>
        </template>
      </v-data-table-server>
    </v-card>

    <!-- create / edit -->
    <v-dialog v-model="editDialog" max-width="540" persistent>
      <v-card rounded="lg">
        <v-card-title class="pt-5 px-6">{{ dialogTitle }}</v-card-title>
        <v-card-text class="px-6">
          <v-alert
            v-if="formError"
            type="error"
            variant="tonal"
            density="comfortable"
            class="mb-4 wl-form-error"
            rounded="lg"
          >
            {{ formError }}
          </v-alert>

          <v-text-field
            v-model="form.name"
            label="标识 *"
            variant="outlined"
            density="comfortable"
            :disabled="editing !== null"
            hint="用于 K8s 命名空间命名，创建后不可修改"
            persistent-hint
            class="mb-5"
          />
          <v-text-field
            v-model="form.directoryName"
            label="目录组名"
            variant="outlined"
            density="comfortable"
            hint="LDAP 中的组名；留空则用上面的标识去匹配"
            persistent-hint
            class="mb-5"
          />
          <v-text-field
            v-model="form.displayName"
            label="显示名称"
            variant="outlined"
            density="comfortable"
            class="mb-4"
          />
          <v-textarea
            v-model="form.description"
            label="描述"
            variant="outlined"
            density="comfortable"
            rows="2"
            auto-grow
            class="mb-4"
          />
          <v-select
            v-model="form.kind"
            :items="[
              { title: '实验室', value: 'lab' },
              { title: '公共组', value: 'public' },
            ]"
            label="类型"
            variant="outlined"
            density="comfortable"
            class="mb-4"
          />
          <v-switch
            v-if="editing"
            v-model="form.isActive"
            label="启用"
            color="primary"
            density="comfortable"
            hide-details
          />
        </v-card-text>
        <v-card-actions class="px-6 pb-5">
          <v-spacer />
          <v-btn variant="text" @click="editDialog = false">取消</v-btn>
          <v-btn color="primary" :loading="saving" rounded="lg" @click="save">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- members -->
    <v-dialog v-model="membersOpen" max-width="760" scrollable>
      <v-card rounded="lg">
        <v-card-title class="pt-5 px-6 d-flex align-center">
          <span>成员管理 · {{ activeGroup?.displayName }}</span>
          <v-spacer />
          <v-btn
            v-if="can('group:manage_member')"
            color="primary"
            size="small"
            prepend-icon="mdi-account-plus-outline"
            rounded="lg"
            @click="memberDialog = true"
          >
            添加成员
          </v-btn>
        </v-card-title>

        <v-card-text class="px-6">
          <v-text-field
            v-model="memberSearch"
            placeholder="搜索成员"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            density="compact"
            hide-details
            clearable
            class="mb-4"
          />

          <v-progress-linear v-if="membersLoading" indeterminate color="primary" class="mb-3" />

          <v-list v-if="members.length" lines="two" class="pa-0">
            <v-list-item v-for="member in members" :key="member.userId" rounded="lg">
              <template #prepend>
                <v-avatar size="38" color="primary" variant="tonal">
                  <span class="text-body-2 font-weight-bold">
                    {{ member.displayName.slice(0, 1).toUpperCase() }}
                  </span>
                </v-avatar>
              </template>

              <v-list-item-title>
                {{ member.fullName || member.username }}
                <v-chip
                  v-if="member.isPrimary"
                  size="x-small"
                  color="primary"
                  variant="tonal"
                  label
                  class="ml-2"
                >
                  主组
                </v-chip>
                <v-chip
                  v-if="member.isDirectoryManaged"
                  size="x-small"
                  color="info"
                  variant="tonal"
                  label
                  class="ml-1"
                >
                  目录
                </v-chip>
              </v-list-item-title>
              <v-list-item-subtitle>
                {{ member.username }}<span v-if="member.email"> · {{ member.email }}</span>
              </v-list-item-subtitle>

              <template #append>
                <div class="d-flex align-center ga-2">
                  <v-select
                    v-if="can('group:manage_member') && !member.isDirectoryManaged"
                    :model-value="member.roleInGroup"
                    :items="[
                      { title: GROUP_ROLE_LABELS.member, value: 'member' },
                      { title: GROUP_ROLE_LABELS.group_admin, value: 'group_admin' },
                    ]"
                    variant="outlined"
                    density="compact"
                    hide-details
                    style="width: 130px"
                    @update:model-value="(value: GroupRole) => changeMemberRole(member, value)"
                  />
                  <v-chip v-else size="small" variant="tonal" label>
                    {{ GROUP_ROLE_LABELS[member.roleInGroup] }}
                  </v-chip>

                  <v-btn
                    v-if="can('group:manage_member') && !member.isDirectoryManaged"
                    icon="mdi-close"
                    size="small"
                    variant="text"
                    color="error"
                    title="移除"
                    @click="removeSelected(member)"
                  />
                </div>
              </template>
            </v-list-item>
          </v-list>

          <div v-else-if="!membersLoading" class="py-10 text-center text-medium-emphasis">
            <v-icon icon="mdi-account-off-outline" size="40" class="mb-2" />
            <div>该用户组暂无成员</div>
          </div>
        </v-card-text>

        <v-card-actions class="px-6 pb-5">
          <v-spacer />
          <v-btn variant="text" @click="membersOpen = false">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- add member -->
    <v-dialog v-model="memberDialog" max-width="480">
      <v-card rounded="lg">
        <v-card-title class="pt-5 px-6">添加成员</v-card-title>
        <v-card-text class="px-6">
          <v-autocomplete
            v-model="candidateQuery"
            :items="candidates"
            item-title="username"
            item-value="username"
            label="搜索用户"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            density="comfortable"
            hide-details
            class="mb-4"
            no-filter
            hint="输入至少一个字符以搜索"
            persistent-hint
            @update:model-value="
              (value: string) =>
                (selectedUserId = candidates.find((c) => c.username === value)?.id ?? null)
            "
          />
          <v-select
            v-model="selectedRole"
            :items="[
              { title: GROUP_ROLE_LABELS.member, value: 'member' },
              { title: GROUP_ROLE_LABELS.group_admin, value: 'group_admin' },
            ]"
            label="组内角色"
            variant="outlined"
            density="comfortable"
            hide-details
          />
        </v-card-text>
        <v-card-actions class="px-6 pb-5">
          <v-spacer />
          <v-btn variant="text" @click="memberDialog = false">取消</v-btn>
          <v-btn
            color="primary"
            :loading="saving"
            :disabled="selectedUserId === null"
            rounded="lg"
            @click="confirmAddMember"
          >
            添加
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="confirmOpen"
      title="删除用户组"
      :text="`确定要删除 ${deleteTarget?.displayName ?? ''} 吗？组内的工作空间与资源绑定需要提前处理。`"
      confirm-text="删除"
      confirm-color="error"
      @confirm="confirmDelete"
    />

    <v-dialog v-model="syncResultOpen" max-width="620">
      <v-card rounded="lg">
        <v-card-title class="pt-5 px-6">
          {{ syncResultDryRun ? '目录同步预演' : '目录同步结果' }}
        </v-card-title>
        <v-card-text class="px-6">
          <v-alert
            v-if="syncResultDryRun"
            type="info"
            variant="tonal"
            density="comfortable"
            class="mb-4"
          >
            这是预演，没有写入任何改动。
          </v-alert>

          <div class="d-flex ga-2 mb-5 flex-wrap">
            <v-chip size="small" color="success" variant="tonal" label>
              新增 {{ syncResult?.membershipsAdded ?? 0 }}
            </v-chip>
            <v-chip size="small" color="info" variant="tonal" label>
              更新 {{ syncResult?.membershipsUpdated ?? 0 }}
            </v-chip>
            <v-chip size="small" color="warning" variant="tonal" label>
              移除 {{ syncResult?.membershipsRemoved ?? 0 }}
            </v-chip>
            <v-chip
              size="small"
              :color="unmatchedGroups.length ? 'warning' : 'grey'"
              variant="tonal"
              label
            >
              未匹配 {{ unmatchedGroups.length }}
            </v-chip>
          </div>

          <!-- The counts above are what changed. This is what did not, and it
               is the part an administrator actually needs: after creating a lab
               in the directory, `0 新增` on its own explains nothing. -->
          <template v-if="unmatchedGroups.length">
            <div class="text-subtitle-2 mb-1">目录中存在、平台上还没有的用户组</div>
            <div class="text-caption text-medium-emphasis mb-2">
              这些组里的成员不会被同步。请为每个组新建一个用户组，把「目录组名」填成下面的名字；
              若标识与目录名完全一致，留空即可。
            </div>
            <v-chip-group column class="mb-4">
              <v-chip
                v-for="name in unmatchedGroups"
                :key="name"
                size="small"
                variant="outlined"
                label
              >
                <code>{{ name }}</code>
              </v-chip>
            </v-chip-group>
          </template>

          <template v-if="syncResult?.errors.length">
            <div class="text-subtitle-2 mb-1">需要注意</div>
            <ul class="text-body-2 text-medium-emphasis mb-2 pl-4">
              <li v-for="(message, index) in syncResult.errors" :key="index">{{ message }}</li>
            </ul>
          </template>

          <v-alert
            v-if="!unmatchedGroups.length && !syncResult?.errors.length"
            type="success"
            variant="tonal"
            density="comfortable"
          >
            目录中的所有用户组都已在平台上匹配，没有遗留事项。
          </v-alert>
        </v-card-text>
        <v-card-actions class="px-6 pb-5">
          <v-spacer />
          <v-btn variant="text" @click="syncResultOpen = false">关闭</v-btn>
          <v-btn
            v-if="syncResultDryRun"
            color="primary"
            rounded="lg"
            :loading="syncing"
            @click="runSync(false)"
          >
            确认执行
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

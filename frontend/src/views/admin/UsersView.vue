<script setup lang="ts">
/**
 * User management.
 *
 * What the page offers is driven by the caller's permissions, never by their
 * role name: a group admin and a global admin open the same route and simply
 * see different controls, exactly mirroring what the backend will allow.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { listGroups, type GroupSummary } from '@/api/groups'
import { listRoles, type RoleDetail } from '@/api/rbac'
import {
  createUser,
  deleteUser,
  getUser,
  listUsers,
  grantRole,
  resetPassword,
  revokeRole,
  setUserActive,
  setUserGroups,
  updateUser,
  type UserDetail,
  type UserSummary,
} from '@/api/users'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import { SOURCE_LABELS, type UserSource } from '@/types/auth'

const { can, isGlobalAdmin, adminGroupIds } = usePermissions()
const notifications = useNotificationStore()

// --- listing -----------------------------------------------------------------
const rows = ref<UserSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  search: '',
  role: null as string | null,
  groupId: null as number | null,
  isActive: null as boolean | null,
})

const groups = ref<GroupSummary[]>([])
const roles = ref<RoleDetail[]>([])

const canFilterByGroup = computed(() => isGlobalAdmin.value || adminGroupIds.value.length > 0)

const headers = [
  { title: '用户', key: 'username', sortable: false },
  { title: '姓名', key: 'fullName', sortable: false },
  { title: '用户组', key: 'primaryGroupName', sortable: false },
  { title: '角色', key: 'roles', sortable: false },
  { title: '来源', key: 'source', sortable: false },
  { title: '状态', key: 'isActive', sortable: false },
  { title: '最近登录', key: 'lastLoginAt', sortable: false },
  { title: '操作', key: 'actions', sortable: false, align: 'end' as const },
]

const activeFilterCount = computed(
  () => [filters.role, filters.groupId, filters.isActive].filter((value) => value !== null).length,
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listUsers({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      role: filters.role ?? undefined,
      groupId: filters.groupId ?? undefined,
      isActive: filters.isActive ?? undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
  } finally {
    loading.value = false
  }
}

function resetFilters(): void {
  filters.search = ''
  filters.role = null
  filters.groupId = null
  filters.isActive = null
  page.value = 1
  void load()
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

watch([() => filters.role, () => filters.groupId, () => filters.isActive], () => {
  page.value = 1
  void load()
})

onMounted(async () => {
  await load()
  // Reference data for the filters and dialogs; a failure here must not break
  // the list itself.
  try {
    const [groupPage, roleList] = await Promise.all([
      listGroups({ page: 1, pageSize: 200 }),
      can('role:read') ? listRoles() : Promise.resolve([]),
    ])
    groups.value = groupPage.items
    roles.value = roleList
  } catch {
    /* the table above is still usable */
  }
})

// --- create / edit -----------------------------------------------------------
const editDialog = ref(false)
const editing = ref<UserDetail | null>(null)
const saving = ref(false)
const formError = ref('')

const form = reactive({
  username: '',
  fullName: '',
  email: '',
  password: '',
  isActive: true,
  roleCodes: ['user'] as string[],
  groupIds: [] as number[],
})

const isEditMode = computed(() => editing.value !== null)
const dialogTitle = computed(() => (isEditMode.value ? `编辑用户 · ${editing.value?.username}` : '新建用户'))

function openCreate(): void {
  editing.value = null
  formError.value = ''
  Object.assign(form, {
    username: '',
    fullName: '',
    email: '',
    password: '',
    isActive: true,
    roleCodes: ['user'],
    groupIds: [],
  })
  editDialog.value = true
}

async function openEdit(row: UserSummary): Promise<void> {
  formError.value = ''
  const detail = await getUser(row.id)
  editing.value = detail
  Object.assign(form, {
    username: detail.username,
    fullName: detail.fullName,
    email: detail.email ?? '',
    password: '',
    isActive: detail.isActive,
    roleCodes: detail.roleAssignments.filter((a) => a.scope === 'global').map((a) => a.code),
    groupIds: detail.groups.map((group) => group.groupId),
  })
  editDialog.value = true
}

async function save(): Promise<void> {
  saving.value = true
  formError.value = ''
  try {
    if (editing.value) {
      await updateUser(editing.value.id, {
        fullName: form.fullName,
        email: form.email || null,
        isActive: form.isActive,
      })
      if (form.password) await resetPassword(editing.value.id, form.password)
      await setUserGroups(editing.value.id, {
        groupIds: form.groupIds,
        primaryGroupId: form.groupIds[0] ?? null,
      })
      notifications.success('用户已更新')
    } else {
      await createUser({
        username: form.username,
        fullName: form.fullName,
        email: form.email || null,
        password: form.password || undefined,
        isActive: form.isActive,
        roleCodes: form.roleCodes,
        groupIds: form.groupIds,
      })
      notifications.success('用户已创建')
    }
    editDialog.value = false
    await load()
  } catch (error) {
    formError.value = error instanceof ApiError ? error.message : '保存失败'
  } finally {
    saving.value = false
  }
}

// --- status ------------------------------------------------------------------
async function toggleActive(row: UserSummary): Promise<void> {
  try {
    await setUserActive(row.id, !row.isActive)
    notifications.success(row.isActive ? `已停用 ${row.username}` : `已启用 ${row.username}`)
    await load()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '操作失败')
  }
}

// --- roles -------------------------------------------------------------------
const rolesDialog = ref(false)
const rolesTarget = ref<UserDetail | null>(null)
const roleDraft = ref<{ code: string; groupId: number | null }[]>([])

async function openRoles(row: UserSummary): Promise<void> {
  rolesTarget.value = await getUser(row.id)
  roleDraft.value = rolesTarget.value.roleAssignments.map((assignment) => ({
    code: assignment.code,
    groupId: assignment.groupId,
  }))
  rolesDialog.value = true
}

async function applyRoles(): Promise<void> {
  const target = rolesTarget.value
  if (!target) return
  saving.value = true
  try {
    const before = new Map(
      target.roleAssignments.map((a) => [`${a.code}:${a.groupId ?? 'global'}`, a]),
    )
    const after = new Map(roleDraft.value.map((a) => [`${a.code}:${a.groupId ?? 'global'}`, a]))

    for (const [key, assignment] of after) {
      if (!before.has(key)) {
        await grantRole(target.id, {
          roleCode: assignment.code,
          scope: assignment.groupId === null ? 'global' : 'group',
          groupId: assignment.groupId,
        })
      }
    }
    for (const [key, assignment] of before) {
      if (!after.has(key)) {
        await revokeRole(target.id, assignment.code, assignment.groupId)
      }
    }

    notifications.success('角色已更新')
    rolesDialog.value = false
    await load()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '角色更新失败')
  } finally {
    saving.value = false
  }
}

function toggleDraftRole(code: string, groupId: number | null): void {
  const index = roleDraft.value.findIndex((a) => a.code === code && a.groupId === groupId)
  if (index >= 0) roleDraft.value.splice(index, 1)
  else roleDraft.value.push({ code, groupId })
}

function isDraftRole(code: string, groupId: number | null): boolean {
  return roleDraft.value.some((a) => a.code === code && a.groupId === groupId)
}

// --- delete ------------------------------------------------------------------
const confirmOpen = ref(false)
const deleteTarget = ref<UserSummary | null>(null)

function askDelete(row: UserSummary): void {
  deleteTarget.value = row
  confirmOpen.value = true
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  try {
    await deleteUser(target.id)
    notifications.success(`已删除 ${target.username}`)
    confirmOpen.value = false
    await load()
  } catch (error) {
    notifications.error(error instanceof ApiError ? error.message : '删除失败')
  }
}

// --- helpers -----------------------------------------------------------------
function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

const ROLE_TONE: Record<string, string> = {
  global_admin: 'deep-purple',
  group_admin: 'indigo',
  user: 'blue-grey',
  observer: 'grey',
}

const sourceLabel = (source: UserSource): string => SOURCE_LABELS[source] ?? source
</script>

<template>
  <div class="wl-page">
    <PageHeader title="用户管理" subtitle="平台账户、角色分配与用户组归属">
      <template #actions>
        <v-btn
          v-if="can('user:create')"
          color="primary"
          prepend-icon="mdi-account-plus-outline"
          rounded="lg"
          @click="openCreate"
        >
          新建用户
        </v-btn>
      </template>
    </PageHeader>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        placeholder="搜索用户名 / 姓名 / 邮箱"
        prepend-inner-icon="mdi-magnify"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 260px"
      />

      <v-select
        v-model="filters.role"
        :items="roles.map((role) => ({ title: role.name, value: role.code }))"
        placeholder="角色"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 160px"
      />

      <v-select
        v-if="canFilterByGroup"
        v-model="filters.groupId"
        :items="groups.map((group) => ({ title: group.displayName, value: group.id }))"
        placeholder="用户组"
        variant="outlined"
        density="compact"
        hide-details
        clearable
        style="min-width: 180px"
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
        <v-btn
          v-if="activeFilterCount > 0 || filters.search"
          variant="text"
          size="small"
          @click="resetFilters"
        >
          重置
        </v-btn>
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
        :items-per-page-options="[
          { value: 10, title: '10' },
          { value: 20, title: '20' },
          { value: 50, title: '50' },
        ]"
        item-value="id"
        hover
        @update:options="load"
      >
        <template #[`item.username`]="{ item }">
          <div class="d-flex align-center ga-3 py-1">
            <v-avatar size="34" :color="ROLE_TONE[item.roles[0]] ?? 'grey'" variant="tonal">
              <span class="text-caption font-weight-bold">
                {{ item.displayName.slice(0, 1).toUpperCase() }}
              </span>
            </v-avatar>
            <div>
              <div class="font-weight-medium">{{ item.username }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.email || '—' }}</div>
            </div>
          </div>
        </template>

        <template #[`item.fullName`]="{ item }">{{ item.fullName || '—' }}</template>

        <template #[`item.primaryGroupName`]="{ item }">
          <v-chip v-if="item.primaryGroupName" size="small" variant="tonal" label>
            {{ item.primaryGroupName }}
          </v-chip>
          <span v-else class="text-medium-emphasis">未分配</span>
        </template>

        <template #[`item.roles`]="{ item }">
          <div class="d-flex flex-wrap ga-1">
            <v-chip
              v-for="role in item.roles"
              :key="role"
              size="x-small"
              :color="ROLE_TONE[role] ?? 'grey'"
              variant="flat"
              label
            >
              {{ role }}
            </v-chip>
          </div>
        </template>

        <template #[`item.source`]="{ item }">
          <span class="text-body-2">{{ sourceLabel(item.source) }}</span>
        </template>

        <template #[`item.isActive`]="{ item }">
          <v-chip
            size="small"
            :color="item.isActive ? 'success' : 'grey'"
            variant="flat"
            label
          >
            {{ item.isActive ? '已启用' : '已停用' }}
          </v-chip>
        </template>

        <template #[`item.lastLoginAt`]="{ item }">
          <span class="text-body-2">{{ formatDateTime(item.lastLoginAt) }}</span>
        </template>

        <template #[`item.actions`]="{ item }">
          <div class="d-flex justify-end ga-1">
            <v-btn
              v-if="can('user:assign_role')"
              icon="mdi-shield-account-outline"
              size="small"
              variant="text"
              title="分配角色"
              @click="openRoles(item)"
            />
            <v-btn
              v-if="can('user:update')"
              icon="mdi-pencil-outline"
              size="small"
              variant="text"
              title="编辑"
              @click="openEdit(item)"
            />
            <v-btn
              v-if="can('user:update')"
              :icon="item.isActive ? 'mdi-account-off-outline' : 'mdi-account-check-outline'"
              size="small"
              variant="text"
              :title="item.isActive ? '停用' : '启用'"
              @click="toggleActive(item)"
            />
            <v-btn
              v-if="can('user:delete')"
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
            <v-icon icon="mdi-account-search-outline" size="42" class="mb-2" />
            <div>{{ filters.search || activeFilterCount ? '没有符合条件的用户' : '暂无用户' }}</div>
          </div>
        </template>
      </v-data-table-server>
    </v-card>

    <!-- create / edit -->
    <v-dialog v-model="editDialog" max-width="620" persistent>
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
            v-model="form.username"
            label="用户名 *"
            variant="outlined"
            density="comfortable"
            :disabled="isEditMode"
            :hint="isEditMode ? '用户名创建后不可修改' : '支持字母、数字、点、下划线和短横线'"
            persistent-hint
            class="mb-5"
          />

          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="form.fullName" label="姓名" variant="outlined" density="comfortable" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field v-model="form.email" label="邮箱" type="email" variant="outlined" density="comfortable" />
            </v-col>
          </v-row>

          <v-text-field
            v-model="form.password"
            :label="isEditMode ? '重置密码（留空则不修改）' : '初始密码 *'"
            type="password"
            variant="outlined"
            density="comfortable"
            autocomplete="new-password"
            hint="至少 12 位；外部目录账户无法在此设置密码"
            persistent-hint
            class="mb-5"
          />

          <template v-if="!isEditMode">
            <div class="text-subtitle-2 mb-2">初始角色</div>
            <div class="d-flex flex-wrap ga-2 mb-5">
              <v-chip
                v-for="role in roles.filter((item) => item.code !== 'global_admin')"
                :key="role.code"
                :color="form.roleCodes.includes(role.code) ? 'primary' : undefined"
                :variant="form.roleCodes.includes(role.code) ? 'flat' : 'outlined'"
                label
                @click="
                  form.roleCodes.includes(role.code)
                    ? (form.roleCodes = form.roleCodes.filter((code) => code !== role.code))
                    : form.roleCodes.push(role.code)
                "
              >
                {{ role.name }}
              </v-chip>
            </div>
          </template>

          <v-select
            v-model="form.groupIds"
            :items="groups.map((group) => ({ title: group.displayName, value: group.id }))"
            label="所属用户组"
            variant="outlined"
            density="comfortable"
            multiple
            chips
            closable-chips
            hint="第一个选中的用户组将成为主用户组"
            persistent-hint
            class="mb-4"
          />

          <v-switch
            v-model="form.isActive"
            label="账户启用"
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

    <!-- roles -->
    <v-dialog v-model="rolesDialog" max-width="560">
      <v-card rounded="lg">
        <v-card-title class="pt-5 px-6">分配角色 · {{ rolesTarget?.username }}</v-card-title>
        <v-card-text class="px-6">
          <v-alert
            type="info"
            variant="tonal"
            density="comfortable"
            rounded="lg"
            class="mb-4 text-body-2"
          >
            全局角色作用于整个平台；组角色只在该用户组内生效。
          </v-alert>

          <div class="text-subtitle-2 mb-2">全局角色</div>
          <div class="d-flex flex-wrap ga-2 mb-6">
            <v-chip
              v-for="role in roles"
              :key="role.code"
              :color="isDraftRole(role.code, null) ? 'primary' : undefined"
              :variant="isDraftRole(role.code, null) ? 'flat' : 'outlined'"
              label
              @click="toggleDraftRole(role.code, null)"
            >
              {{ role.name }}
            </v-chip>
          </div>

          <template v-if="groups.length">
            <div class="text-subtitle-2 mb-2">组角色</div>
            <v-select
              v-for="group in groups"
              :key="group.id"
              :model-value="isDraftRole('group_admin', group.id)"
              :items="[
                { title: '不授予', value: false },
                { title: '组管理员', value: true },
              ]"
              :label="group.displayName"
              variant="outlined"
              density="compact"
              hide-details
              class="mb-3"
              @update:model-value="
                (value: boolean) =>
                  value
                    ? roleDraft.push({ code: 'group_admin', groupId: group.id })
                    : (roleDraft = roleDraft.filter(
                        (a) => !(a.code === 'group_admin' && a.groupId === group.id),
                      ))
              "
            />
          </template>
        </v-card-text>
        <v-card-actions class="px-6 pb-5">
          <v-spacer />
          <v-btn variant="text" @click="rolesDialog = false">取消</v-btn>
          <v-btn color="primary" :loading="saving" rounded="lg" @click="applyRoles">应用</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="confirmOpen"
      title="删除用户"
      :text="`确定要删除 ${deleteTarget?.username ?? ''} 吗？该操作不可撤销，其个人资源归属需提前转移。`"
      confirm-text="删除"
      confirm-color="error"
      @confirm="confirmDelete"
    />
  </div>
</template>

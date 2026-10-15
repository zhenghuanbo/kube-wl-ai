<script setup lang="ts">
/**
 * Quota templates: the ceilings, and who follows which one.
 *
 * The page is organised around the one axis that matters — the **kind** — because
 * the two kinds answer different questions and cannot be swapped:
 *
 * - a **user** template is a ceiling on a person, counted across every workspace
 *   they belong to, and it is the only place "how many instances may I hold" is
 *   answered;
 * - a **workspace** template is the namespace's own quota, so it is handed to
 *   Kubernetes almost verbatim and is bound 1:1 to one namespace.
 *
 * A person is checked against all three layers and the tightest one wins, so a
 * number on this page is never *the* answer on its own — the alert says so, because
 * an administrator who raises a user ceiling and sees no change needs to know that
 * the pool or the namespace may be the one refusing.
 *
 * Everything here writes through the templates and only the templates. A workspace's
 * numbers cannot be edited directly: they are derived, and an edit to a derived value
 * is one the next reconcile silently undoes.
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  assignUserQuotaProfile,
  assignWorkspaceQuotaProfile,
  createQuotaProfile,
  deleteQuotaProfile,
  listQuotaProfiles,
  updateQuotaProfile,
} from '@/api/quotaProfiles'
import { ApiError } from '@/api/client'
import { listUsers, type UserSummary } from '@/api/users'
import { listWorkspaces } from '@/api/workspaces'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { WorkspaceSummary } from '@/types/resource'
import {
  dimensionsFor,
  formatCeiling,
  isAllZeroCompute,
  kindLabel,
  type QuotaProfile,
  type QuotaProfileKind,
  type QuotaProfilePayload,
} from '@/types/quotaProfile'

const notifications = useNotificationStore()
const { can, isGlobalAdmin } = usePermissions()

// --- templates ---------------------------------------------------------------
const kind = ref<QuotaProfileKind>('user')
const profiles = ref<QuotaProfile[]>([])
const loading = ref(false)

const canCreate = computed(() => can('quota_profile:create'))
const canUpdate = computed(() => can('quota_profile:update'))
const canDelete = computed(() => can('quota_profile:delete'))
const canAssign = computed(() => can('quota_profile:assign'))

const ofKind = computed(() => profiles.value.filter((profile) => profile.kind === kind.value))
const defaultProfile = computed(() => ofKind.value.find((profile) => profile.isDefault) ?? null)
const dimensions = computed(() => dimensionsFor(kind.value))

/**
 * True while the three compute fields are all `0`: a real ceiling meaning "none at
 * all", which is almost never what was meant, so the form says so out loud.
 */
const computedComputeIsZero = computed(() =>
  isAllZeroCompute({
    cpuCores: draft.value.cpuCores ?? -1,
    memoryGb: draft.value.memoryGb ?? -1,
    gpu: draft.value.gpu ?? -1,
  }),
)

/** The templates offered by the assignment picker: this kind, and usable. */
const assignable = computed(() => ofKind.value.filter((profile) => profile.isActive))

async function load(): Promise<void> {
  loading.value = true
  try {
    profiles.value = await listQuotaProfiles()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

// --- create / edit -----------------------------------------------------------
const editDialog = ref(false)
const editing = ref<QuotaProfile | null>(null)
const saving = ref(false)
const formError = ref('')
const draft = ref<QuotaProfilePayload>({})

function emptyDraft(target: QuotaProfileKind): QuotaProfilePayload {
  // Every dimension starts at "not checked" rather than at 0. Zero is a real
  // ceiling meaning "none at all", so defaulting a blank form to 0 would hand out a
  // tenant that cannot run anything while looking like it was never filled in.
  const payload: QuotaProfilePayload = {
    kind: target,
    name: '',
    displayName: '',
    description: null,
    cpuCores: -1,
    memoryGb: -1,
    gpu: -1,
  }
  for (const dimension of dimensionsFor(target)) {
    payload[dimension.key] = -1
  }
  return payload
}

function openCreate(): void {
  editing.value = null
  draft.value = emptyDraft(kind.value)
  formError.value = ''
  editDialog.value = true
}

function openEdit(profile: QuotaProfile): void {
  editing.value = profile
  const payload: QuotaProfilePayload = {
    displayName: profile.displayName,
    description: profile.description,
    cpuCores: profile.cpuCores,
    memoryGb: profile.memoryGb,
    gpu: profile.gpu,
    isDefault: profile.isDefault,
    isActive: profile.isActive,
  }
  for (const dimension of dimensionsFor(profile.kind)) {
    payload[dimension.key] = profile[dimension.key] ?? -1
  }
  draft.value = payload
  formError.value = ''
  editDialog.value = true
}

async function save(): Promise<void> {
  saving.value = true
  formError.value = ''
  try {
    if (editing.value) {
      await updateQuotaProfile(editing.value.id, draft.value)
      notifications.success('配额模板已更新')
    } else {
      await createQuotaProfile(draft.value)
      notifications.success('配额模板已创建')
    }
    editDialog.value = false
    await load()
  } catch (error) {
    // Kept in the dialog rather than only in a snackbar: the message names the field
    // that is wrong (`max_pods < max_deployments + max_jobs` is the interesting one)
    // and the form is where it can be fixed.
    formError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    saving.value = false
  }
}

// --- default / active / delete ----------------------------------------------
async function makeDefault(profile: QuotaProfile): Promise<void> {
  try {
    await updateQuotaProfile(profile.id, { isDefault: true })
    notifications.success(`「${profile.displayName || profile.name}」现在是${kindLabel(profile.kind)}默认模板`)
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function toggleActive(profile: QuotaProfile): Promise<void> {
  try {
    await updateQuotaProfile(profile.id, { isActive: !profile.isActive })
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

const deleteOpen = ref(false)
const deleteTarget = ref<QuotaProfile | null>(null)
const deleting = ref(false)

function askDelete(profile: QuotaProfile): void {
  deleteTarget.value = profile
  deleteOpen.value = true
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteQuotaProfile(target.id)
    notifications.success('配额模板已删除')
    deleteOpen.value = false
    await load()
    if (kind.value === target.kind) await loadTargets()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    deleting.value = false
  }
}

// --- who follows what --------------------------------------------------------
const targets = ref<{ id: number; name: string; profileId: number | null }[]>([])
const targetsLoading = ref(false)
const search = ref('')

/**
 * Load the assignable targets for the current kind.
 *
 * Users and workspaces are read through their own list endpoints rather than a
 * dedicated "who follows this template" call, because those endpoints already scope
 * themselves: a group administrator sees their own group's members and nobody
 * else's, and this page must not widen that.
 */
async function loadTargets(): Promise<void> {
  targetsLoading.value = true
  try {
    if (kind.value === 'user') {
      const page = await listUsers({ pageSize: 200, search: search.value || undefined })
      users.value = page.items
      targets.value = page.items.map((user) => ({
        id: user.id,
        name: user.displayName ? `${user.username}（${user.displayName}）` : user.username,
        // `?? null` and not a bare read: an older response without the field would
        // otherwise render as `#undefined` in the table's 生效模板 column.
        profileId: user.quotaProfileId ?? null,
      }))
    } else {
      const page = await listWorkspaces({ pageSize: 200, search: search.value || undefined })
      workspaces.value = page.items
      targets.value = page.items.map((workspace) => ({
        id: workspace.id,
        name: workspace.displayName ? `${workspace.name}（${workspace.displayName}）` : workspace.name,
        profileId: workspace.quotaProfileId ?? null,
      }))
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    targetsLoading.value = false
  }
}

const users = ref<UserSummary[]>([])
const workspaces = ref<WorkspaceSummary[]>([])

function profileName(profileId: number | null): string {
  if (profileId === null) {
    const fallback = defaultProfile.value
    return fallback ? `默认（${fallback.displayName || fallback.name}）` : '默认模板未配置'
  }
  const found = profiles.value.find((profile) => profile.id === profileId)
  return found ? found.displayName || found.name : `#${profileId}`
}

async function assign(targetId: number, profileId: number | null): Promise<void> {
  try {
    if (kind.value === 'user') {
      await assignUserQuotaProfile(targetId, profileId)
    } else {
      await assignWorkspaceQuotaProfile(targetId, profileId)
    }
    await loadTargets()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

// --- bulk assignment ---------------------------------------------------------
const bulkOpen = ref(false)
const bulkProfileId = ref<number | null>(null)
const bulkTargets = ref<number[]>([])
const bulkSaving = ref(false)

function openBulk(): void {
  bulkProfileId.value = defaultProfile.value?.id ?? null
  bulkTargets.value = []
  bulkOpen.value = true
}

async function applyBulk(): Promise<void> {
  if (!bulkTargets.value.length) return
  bulkSaving.value = true
  try {
    // Sequential rather than parallel: each assignment is its own transaction that
    // re-derives a namespace quota, and firing two hundred reconciles at the API
    // concurrently is how a bulk edit turns into a rate-limit or a lock timeout.
    for (const targetId of bulkTargets.value) {
      await assign(targetId, bulkProfileId.value)
    }
    notifications.success(`已为 ${bulkTargets.value.length} 个${kindLabel(kind.value)}目标更新配额模板`)
    bulkOpen.value = false
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    bulkSaving.value = false
  }
}

watch(kind, () => {
  search.value = ''
  loadTargets()
})

onMounted(() => {
  load()
  loadTargets()
})
</script>

<template>
  <div class="wl-page">
    <PageHeader
      title="配额管理"
      subtitle="算力配额模板：每人能要多少，以及每个工作空间的命名空间上限"
    >
      <template #actions>
        <v-btn
          v-if="canCreate && isGlobalAdmin"
          color="primary"
          prepend-icon="mdi-plus"
          rounded="lg"
          @click="openCreate"
        >
          新建{{ kindLabel(kind) }}模板
        </v-btn>
      </template>
    </PageHeader>

    <v-alert type="info" variant="tonal" density="comfortable" class="mb-4">
      <div class="text-body-2">
        一个请求要同时满足<strong>三层</strong>上限，取最严的一层：这个人自己的模板（跨全部工作空间）、
        所用资源池的「每人上限」、以及所在工作空间的模板（也就是命名空间配额，下发给 Kubernetes）。
        因此改高这里的数字不一定立刻生效——可能卡在另外两层。
      </div>
      <div class="text-body-2 mt-2">
        <strong>-1 表示不校验</strong>：命名空间配额里对应的 key 会被直接省略（Kubernetes 把缺失读作
        「无限制」，把 0 读作「一个都不许」，两者相反）。<strong>0 是真实上限</strong>，表示完全不允许。
        用量由工作负载实时算出，不是存在库里的计数器。
      </div>
      <div v-if="!isGlobalAdmin" class="text-body-2 mt-2">
        当前账号可以查看模板，但只有全局管理员能修改——这些数字决定<em>每个</em>租户的额度。
      </div>
    </v-alert>

    <v-tabs v-model="kind" class="mb-4">
      <v-tab value="user" prepend-icon="mdi-account-outline">用户模板</v-tab>
      <v-tab value="workspace" prepend-icon="mdi-folder-outline">工作空间模板</v-tab>
    </v-tabs>

    <!-- ------------------------------------------------------------------ -->
    <!-- The templates themselves                                            -->
    <!-- ------------------------------------------------------------------ -->
    <v-card variant="flat" class="wl-card mb-6">
      <v-card-title class="d-flex align-center">
        {{ kindLabel(kind) }}配额模板
        <v-chip size="small" variant="tonal" class="ms-3" label>{{ ofKind.length }} 个</v-chip>
      </v-card-title>
      <v-card-subtitle>
        <template v-if="kind === 'user'">
          上限按人计算，跨该用户所在的<strong>全部</strong>工作空间累加；实例、应用与自定义业务的
          数量口径是「现存未删除的对象数」——运行中和已停止都占名额，删除后释放。
        </template>
        <template v-else>
          与该工作空间的命名空间一一对应，直接下发给 Kubernetes；对象计数是兜底，个人先撞到的通常是自己
          模板上的数量上限。
        </template>
      </v-card-subtitle>

      <v-card-text>
        <v-skeleton-loader v-if="loading && !profiles.length" type="table" />

        <v-table v-else-if="ofKind.length" density="comfortable">
          <thead>
            <tr>
              <th>模板</th>
              <th v-for="dimension in dimensions" :key="dimension.key">
                {{ dimension.label }}
              </th>
              <th style="width: 90px">默认</th>
              <th style="width: 90px">状态</th>
              <th style="width: 210px" class="text-end">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="profile in ofKind" :key="profile.id">
              <td>
                <div class="font-weight-medium">{{ profile.displayName || profile.name }}</div>
                <code class="wl-mono text-caption">{{ profile.name }}</code>
                <div v-if="profile.description" class="text-caption text-medium-emphasis wl-wrap">
                  {{ profile.description }}
                </div>
              </td>
              <td v-for="dimension in dimensions" :key="dimension.key" class="wl-num">
                {{ formatCeiling(profile[dimension.key], dimension.unit) }}
              </td>
              <td>
                <v-chip v-if="profile.isDefault" size="small" color="primary" variant="tonal" label>
                  默认
                </v-chip>
              </td>
              <td>
                <v-chip
                  size="small"
                  variant="tonal"
                  label
                  :color="profile.isActive ? 'success' : 'default'"
                >
                  {{ profile.isActive ? '启用' : '停用' }}
                </v-chip>
              </td>
              <td class="text-end">
                <v-btn
                  v-if="canUpdate && isGlobalAdmin"
                  size="small"
                  variant="text"
                  @click="openEdit(profile)"
                >
                  编辑
                </v-btn>
                <v-btn
                  v-if="canUpdate && isGlobalAdmin && !profile.isDefault"
                  size="small"
                  variant="text"
                  @click="makeDefault(profile)"
                >
                  设为默认
                </v-btn>
                <v-btn
                  v-if="canUpdate && isGlobalAdmin"
                  size="small"
                  variant="text"
                  @click="toggleActive(profile)"
                >
                  {{ profile.isActive ? '停用' : '启用' }}
                </v-btn>
                <v-btn
                  v-if="canDelete && isGlobalAdmin && !profile.isDefault"
                  size="small"
                  variant="text"
                  color="error"
                  @click="askDelete(profile)"
                >
                  删除
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>

        <EmptyState
          v-else
          icon="mdi-tune-variant"
          title="没有这一类的配额模板"
          description="默认模板通常在首次启动时自动创建；若这里为空，请新建一个并设为默认。"
        />
      </v-card-text>
    </v-card>

    <!-- ------------------------------------------------------------------ -->
    <!-- Who follows which template                                          -->
    <!-- ------------------------------------------------------------------ -->
    <v-card variant="flat" class="wl-card">
      <v-card-title>{{ kindLabel(kind) }}与模板的关联</v-card-title>
      <v-card-subtitle>
        「跟随默认」是真实状态而非缺失值：默认模板一改，跟随它的人立刻跟着变。给某个人单独指定模板，
        只在确实需要区别对待时使用。
      </v-card-subtitle>

      <v-card-text>
        <FilterBar class="mb-3">
          <v-text-field
            v-model="search"
            :placeholder="kind === 'user' ? '搜索用户名 / 姓名' : '搜索工作空间'"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            density="compact"
            hide-details
            clearable
            style="min-width: 240px"
            @keyup.enter="loadTargets"
          />
          <template #actions>
            <v-btn variant="text" size="small" @click="loadTargets">查询</v-btn>
            <v-btn
              v-if="canAssign && isGlobalAdmin"
              color="primary"
              variant="tonal"
              size="small"
              prepend-icon="mdi-account-multiple-check-outline"
              :disabled="!targets.length"
              @click="openBulk"
            >
              批量关联
            </v-btn>
          </template>
        </FilterBar>

        <v-skeleton-loader v-if="targetsLoading" type="table-row@4" />

        <v-table v-else-if="targets.length" density="comfortable">
          <thead>
            <tr>
              <th>{{ kind === 'user' ? '用户' : '工作空间' }}</th>
              <th>生效模板</th>
              <th style="width: 320px" class="text-end">改为</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="target in targets" :key="target.id">
              <td class="font-weight-medium">{{ target.name }}</td>
              <td>
                <span :class="{ 'text-medium-emphasis': target.profileId === null }">
                  {{ profileName(target.profileId) }}
                </span>
              </td>
              <td class="text-end">
                <v-btn
                  v-for="profile in assignable"
                  :key="profile.id"
                  size="small"
                  variant="text"
                  :disabled="!canAssign || !isGlobalAdmin || target.profileId === profile.id"
                  @click="assign(target.id, profile.id)"
                >
                  {{ profile.displayName || profile.name }}
                </v-btn>
                <v-btn
                  size="small"
                  variant="text"
                  :disabled="!canAssign || !isGlobalAdmin || target.profileId === null"
                  @click="assign(target.id, null)"
                >
                  跟随默认
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>

        <EmptyState
          v-else
          icon="mdi-account-search-outline"
          title="没有可关联的对象"
          description="没有查到用户或工作空间；也可能是当前账号的数据范围不包含它们。"
        />
      </v-card-text>
    </v-card>

    <!-- ------------------------------------------------------------------ -->
    <!-- Dialogs                                                             -->
    <!-- ------------------------------------------------------------------ -->
    <v-dialog v-model="editDialog" max-width="680" persistent>
      <v-card>
        <v-card-title>
          {{ editing ? '编辑配额模板' : `新建${kindLabel(kind)}模板` }}
        </v-card-title>
        <v-card-text>
          <v-alert v-if="formError" type="error" variant="tonal" density="comfortable" class="mb-3">
            {{ formError }}
          </v-alert>

          <v-row dense>
            <v-col v-if="!editing" cols="12" sm="6">
              <v-text-field
                v-model="draft.name"
                label="标识"
                hint="小写字母、数字、点、下划线或短横线"
                persistent-hint
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" :sm="editing ? 12 : 6">
              <v-text-field
                v-model="draft.displayName"
                label="显示名"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="draft.description"
                label="说明"
                rows="2"
                variant="outlined"
                density="comfortable"
                hide-details
              />
            </v-col>

            <v-col v-for="dimension in dimensions" :key="dimension.key" cols="12" sm="6">
              <v-text-field
                :model-value="draft[dimension.key] as number"
                :label="`${dimension.label}（${dimension.unit}）`"
                :hint="dimension.hint"
                persistent-hint
                type="number"
                variant="outlined"
                density="comfortable"
                @update:model-value="draft[dimension.key] = Number($event)"
              />
            </v-col>
          </v-row>

          <v-alert
            v-if="computedComputeIsZero"
            type="warning"
            variant="tonal"
            density="comfortable"
            class="mt-3"
          >
            三项算力都是 0，这个模板下的对象将无法申请任何 CPU、内存或加速卡。
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="saving" @click="editDialog = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="saving" @click="save">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="bulkOpen" max-width="620">
      <v-card>
        <v-card-title>批量关联{{ kindLabel(kind) }}配额模板</v-card-title>
        <v-card-subtitle>
          选中的对象会一起改为同一个模板；工作空间的命名空间配额会立即重新下发。
        </v-card-subtitle>
        <v-card-text>
          <v-select
            v-model="bulkProfileId"
            :items="[
              { title: '跟随默认模板', value: null },
              ...assignable.map((profile) => ({
                title: profile.displayName || profile.name,
                value: profile.id,
              })),
            ]"
            label="目标模板"
            variant="outlined"
            density="comfortable"
          />
          <v-select
            v-model="bulkTargets"
            :items="targets.map((target) => ({ title: target.name, value: target.id }))"
            label="对象"
            multiple
            chips
            closable-chips
            variant="outlined"
            density="comfortable"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="bulkSaving" @click="bulkOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="bulkSaving"
            :disabled="!bulkTargets.length"
            @click="applyBulk"
          >
            应用
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="删除配额模板"
      confirm-text="删除"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    >
      删除「{{ deleteTarget?.displayName || deleteTarget?.name }}」不会改变已经跟随它的人——
      他们的关联会先被清空并回到默认模板；若还有人跟着它，服务端会拒绝删除。
    </ConfirmDialog>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-card {
  box-shadow: $card-shadow;
}

.wl-num {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
}

// Descriptions are prose and must wrap; a table cell truncates by default.
.wl-wrap {
  white-space: normal;
}
</style>
